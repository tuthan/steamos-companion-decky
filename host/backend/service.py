"""The Decky host service: authenticated API, adapters, pairing, and recovery."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import ipaddress
import math
import os
import re
import secrets
import socket
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .bridge import BridgeBroker, BridgeError
from .display import (
    DisplayError,
    display_identity,
    mode_profile,
    resolve_restore_mode,
    resolve_active_output,
    same_display_identity,
    same_mode_profile,
    same_selection_identity,
    selection_identity,
)
from .drm import DrmInventory
from .gamescope import GamescopeError, GamescopeOutputManager
from .identity import ensure_tls_material, opaque_id, read_boot_id, read_cpu_temperature, system_uptime_seconds
from .operations import ACTIVE_STATES, OperationJournal, iso_timestamp
from .pairing import derive_pairing_code, encode_payload
from .protocol import (
    ProtocolError,
    canonical_digest,
    client_name,
    identifier,
    normalize_pairing_code,
    pairing_session,
    verification_nonce,
    request_id,
    validate_mac,
    validate_host,
    validate_route_body,
    validate_scopes,
)
from .provider import DeckySunshineProcessObserver, BridgeSunshineProvider, ProviderAdapter, SunshineMonitor
from .storage import StateStore


DEFAULT_SETTINGS = {
    "listen_enabled": True,
    "listen_address": "0.0.0.0",
    "listen_port": 18443,
    "advertised_host": "",
    "device_name": "SteamOS device",
    "monitor_sunshine": False,
    "auto_recover_sunshine": True,
}

_INTERFACE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
_CHANNEL_BINDING_RE = re.compile(r"^[A-Za-z0-9_-]{16,510}={0,2}$")
MAX_PAIRING_RECORDS = 256
PAIRING_RETENTION_SECONDS = 3600.0
MAX_CLIENTS = 4
LOCAL_OPERATION_OWNER = "decky-local"
LOCAL_DISPLAY_PREVIEW_SECONDS = 20.0
BRIDGE_SNAPSHOT_MAX_AGE = 8.0


class ApiError(ProtocolError):
    pass


def _endpoint(host: str, port: int) -> str:
    rendered = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"https://{rendered}:{port}"


def _usable_ipv4_host(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        address = ipaddress.IPv4Address(value)
    except (TypeError, ValueError):
        return False
    return not (
        address.is_loopback
        or address.is_unspecified
        or address.is_link_local
        or address.is_multicast
    )


def _route_selected_ipv4() -> str | None:
    """Return the source address selected by the OS for the active route.

    UDP connect only selects a local route; it does not send a packet. The
    reserved TEST-NET destination avoids depending on an Internet service,
    while the limited-broadcast fallback still works on a host with only a
    local network route.
    """
    for destination in (("192.0.2.1", 9), ("255.255.255.255", 9)):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect(destination)
                host = probe.getsockname()[0]
        except (OSError, IndexError, TypeError):
            continue
        if _usable_ipv4_host(host):
            return host
    return None


def _default_route_interface() -> str | None:
    """Return the Linux interface carrying the first default IPv4 route."""
    try:
        lines = Path("/proc/net/route").read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    for line in lines[1:]:
        fields = line.split()
        if len(fields) < 4 or fields[1] != "00000000" or not _INTERFACE_RE.fullmatch(fields[0]):
            continue
        try:
            flags = int(fields[3], 16)
        except ValueError:
            continue
        if flags & 0x1:
            return fields[0]
    return None


def _interface_mac(interface: str) -> str | None:
    if not _INTERFACE_RE.fullmatch(interface) or interface == "lo":
        return None
    try:
        value = (Path("/sys/class/net") / interface / "address").read_text(encoding="ascii").strip()
        value = validate_mac(value)
    except (OSError, UnicodeDecodeError, ProtocolError, TypeError):
        return None
    if value == "000000000000" or int(value[:2], 16) & 1:
        return None
    return value


def _active_wake_target() -> dict[str, Any]:
    """Describe the host NIC that should receive a LAN magic packet.

    This is local, read-only network inspection. The interface name is sent
    as diagnostic information; the Omarchy client selects its own active LAN
    route when transmitting the packet because interface names differ between
    machines.
    """
    route_interface = _default_route_interface()
    candidates: list[str] = []
    if route_interface:
        candidates.append(route_interface)
    try:
        candidates.extend(name for _, name in socket.if_nameindex() if name not in candidates)
    except (AttributeError, OSError):
        pass
    try:
        candidates.extend(path.name for path in Path("/sys/class/net").iterdir() if path.name not in candidates)
    except OSError:
        pass
    for interface in candidates:
        try:
            operstate = (Path("/sys/class/net") / interface / "operstate").read_text(encoding="ascii").strip().lower()
        except (OSError, UnicodeDecodeError):
            operstate = ""
        if interface != route_interface and operstate not in {"up", "unknown"}:
            continue
        mac = _interface_mac(interface)
        if mac:
            return {
                "available": True,
                "mac": mac,
                "interface": interface,
                "source_address": _route_selected_ipv4(),
                "reason": None,
            }
    return {
        "available": False,
        "mac": "",
        "interface": "",
        "source_address": _route_selected_ipv4(),
        "reason": "No active network interface with a readable hardware address",
    }


def _pairing_qr_svg_base64(payload: str) -> str | None:
    """Render the local pairing payload without placing the secret in argv."""
    encoder = "/usr/bin/qrencode"
    if not os.path.isfile(encoder):
        return None
    try:
        result = subprocess.run(
            [encoder, "-t", "SVG", "-o", "-", "-l", "L", "-m", "2"],
            input=payload.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if not 1 <= len(result.stdout) <= 128 * 1024 or b"<svg" not in result.stdout[:1024]:
        return None
    return base64.b64encode(result.stdout).decode("ascii")


def _hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _hash_channel_binding(value: str | None) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return hashlib.sha256(value.encode("ascii", "strict")).hexdigest()


def _valid_channel_binding(value: Any) -> str | None:
    if not isinstance(value, str) or not _CHANNEL_BINDING_RE.fullmatch(value):
        return None
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError, binascii.Error):
        return None
    if len(decoded) < 12:
        return None
    return value


def _token() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


def _pairing_code() -> str:
    return f"{secrets.randbelow(100_000_000):08d}"


def _display_pairing_code(value: str) -> str:
    return f"{value[:4]}-{value[4:]}"


def _constant_time_hash_match(value: str, expected_hash: str) -> bool:
    return hmac.compare_digest(_hash_secret(value), expected_hash)


def _pairing_expiry(value: Any) -> float:
    if not isinstance(value, dict):
        return 0.0
    try:
        expiry = float(value.get("expires_at", 0))
    except (TypeError, ValueError):
        return 0.0
    return expiry if math.isfinite(expiry) else 0.0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _result_ok(result: Any) -> bool:
    return isinstance(result, dict) and result.get("ok", True) is True


class HostService:
    """A testable service object used by Decky's async plugin entry point."""

    def __init__(
        self,
        state_root: str | os.PathLike[str],
        *,
        bridge: BridgeBroker | None = None,
        provider: Any = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        drm_root: str | os.PathLike[str] = "/sys/class/drm",
        gamescope: GamescopeOutputManager | None = None,
    ):
        self.clock = clock
        self.monotonic = monotonic
        self.store = StateStore(state_root, self._initial_state)
        self._migrate_display_state()
        stored_host_id = self.store.get("host_id")
        if not isinstance(stored_host_id, str) or not stored_host_id:
            stored_host_id = opaque_id("host-")
            self.store.mutate(lambda state: state.__setitem__("host_id", stored_host_id))
        self.host_id = stored_host_id
        self.boot_id = read_boot_id()
        self.bridge = bridge or BridgeBroker(monotonic)
        self._drm_inventory = DrmInventory(drm_root)
        self.journal = OperationJournal(self.store, clock)
        self._mutation_lock = threading.RLock()
        self._sunshine_recovery_armed = False
        self.monitor = SunshineMonitor(monotonic, clock)
        self.monitor.set_observer(DeckySunshineProcessObserver())
        self.gamescope = gamescope or GamescopeOutputManager(self.store.root)
        self.monitor.set_state_callback(self._on_sunshine_state_change)
        self._sunshine_owner_reason: str | None = None
        self._pairing_lock = threading.RLock()
        self._rate_lock = threading.RLock()
        self._rate_buckets: dict[str, tuple[float, int]] = {}
        self._approved_tokens: dict[str, str] = {}
        self._workers: set[threading.Thread] = set()
        self._running = False
        self._server = None
        self._server_error = ""
        stored_tls = self.store.get("tls", {})
        self._tls: dict[str, Any] = stored_tls if isinstance(stored_tls, dict) else {}
        self._tls_lock = threading.RLock()
        self._tls_initialized = False
        self._preview_stop = threading.Event()
        self._preview_thread: threading.Thread | None = None
        self._prune_pairings()
        if provider is not None:
            self.set_sunshine_provider(provider)

    def _initial_state(self) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "host_id": opaque_id("host-"),
            "settings": dict(DEFAULT_SETTINGS),
            "clients": {},
            "pairings": {},
            "operations": {},
            "profiles": {},
            "preview": None,
            "local_display_preview": None,
            "tls": {},
        }

    def _migrate_display_state(self) -> None:
        """Add the local display transaction slot without replacing old state."""
        state = self.store.snapshot()
        version = state.get("schema_version", 1)
        if not isinstance(version, int) or version > 2:
            return
        if version < 2 or "local_display_preview" not in state:
            def migrate(value: dict[str, Any]) -> None:
                value.setdefault("local_display_preview", None)
                value["schema_version"] = 2
            self.store.mutate(migrate)

    # ---- lifecycle -----------------------------------------------------

    def _ensure_tls_material(self, *, force: bool = False) -> dict[str, Any]:
        with self._tls_lock:
            if self._tls_initialized and not force:
                return self._tls
            try:
                tls = ensure_tls_material(self.store.root, self.host_id)
            except Exception as exc:
                tls = {"ready": False, "reason": f"TLS initialization failed: {str(exc)[:220]}"}
            self._tls = tls
            self._tls_initialized = True
            self.store.mutate(lambda state: state.__setitem__("tls", {
                "fingerprint": tls.get("fingerprint"),
                "ready": tls.get("ready") is True,
                "reason": tls.get("reason"),
            }))
            return self._tls

    def start(self, *, start_server: bool = True) -> dict[str, Any]:
        if self._running:
            return self.get_local_status()
        self._running = True
        self.bridge.start()
        self._preview_stop.clear()
        # Generate/revalidate identity material in the service worker before
        # the settings RPC is polled. Status reads remain non-blocking.
        self._ensure_tls_material(force=True)
        if self.store.get("settings", {}).get("monitor_sunshine") is True:
            self.monitor.set_enabled(True)
        self._start_preview_watchdog()
        self._reconcile_local_display_preview()
        if start_server and self.store.get("settings", {}).get("listen_enabled", True):
            self._start_server()
        return self.get_local_status()

    def stop(self) -> None:
        self._running = False
        self._preview_stop.set()
        self.monitor.close()
        self.bridge.stop()
        for worker in list(self._workers):
            if worker is not threading.current_thread():
                worker.join(timeout=1)
        if self._server is not None:
            self._server.stop()
            self._server = None
        self._server_error = ""

    def _start_server(self) -> None:
        self._ensure_tls_material()
        if not self._tls.get("ready"):
            self._server_error = str(self._tls.get("reason", "TLS material is unavailable"))[:256]
            return
        try:
            from .server import HostHttpServer

            settings = self.store.get("settings", {})
            self._server = HostHttpServer(
                self,
                str(settings.get("listen_address", DEFAULT_SETTINGS["listen_address"])),
                int(settings.get("listen_port", DEFAULT_SETTINGS["listen_port"])),
                str(self._tls["certificate_path"]),
                str(self._tls["key_path"]),
            )
            self._server.start()
            self._server_error = ""
        except Exception as exc:
            self._server = None
            self._server_error = f"HTTPS listener unavailable: {str(exc)[:220]}"

    def _restart_server(self) -> None:
        if self._server is not None:
            self._server.stop()
            self._server = None
        self._server_error = ""
        if self._running and self.store.get("settings", {}).get("listen_enabled", True):
            self._start_server()

    # ---- local Decky methods ------------------------------------------

    def _prune_pairings(self) -> None:
        """Keep unauthenticated pairing state bounded and short-lived."""
        now = self.clock()
        pairings = self.store.get("pairings", {})
        if not isinstance(pairings, dict):
            return
        needs_prune = len(pairings) > MAX_PAIRING_RECORDS
        if not needs_prune:
            for value in pairings.values():
                if not isinstance(value, dict):
                    needs_prune = True
                    break
                expires_at = _pairing_expiry(value)
                status = value.get("status")
                if status in {"created", "pending", "approved"} and expires_at <= now:
                    needs_prune = True
                    break
                if expires_at <= now - PAIRING_RETENTION_SECONDS:
                    needs_prune = True
                    break
        if not needs_prune:
            return

        with self._pairing_lock:
            removed: list[str] = []
            expired_tokens: list[str] = []

            def mutate(state: dict[str, Any]) -> None:
                records = state.setdefault("pairings", {})
                for pairing_id, value in list(records.items()):
                    if not isinstance(value, dict):
                        removed.append(pairing_id)
                        records.pop(pairing_id, None)
                        continue
                    expires_at = _pairing_expiry(value)
                    if value.get("status") in {"created", "pending", "approved"} and expires_at <= now:
                        value["status"] = "expired"
                        expired_tokens.append(pairing_id)
                    if expires_at <= now - PAIRING_RETENTION_SECONDS:
                        removed.append(pairing_id)
                        records.pop(pairing_id, None)

                if len(records) > MAX_PAIRING_RECORDS:
                    # Prefer removing terminal and oldest records. If a burst
                    # consists entirely of active requests, the oldest ones
                    # are still discarded to preserve a hard state bound.
                    ordered = sorted(
                        records.items(),
                        key=lambda item: (
                            item[1].get("status") in {"created", "pending", "approved"},
                            _pairing_expiry(item[1]),
                            item[1].get("created_at", ""),
                        ),
                    )
                    for pairing_id, _ in ordered[: max(0, len(records) - MAX_PAIRING_RECORDS)]:
                        removed.append(pairing_id)
                        records.pop(pairing_id, None)

            self.store.mutate(mutate)
            for pairing_id in set(removed).union(expired_tokens):
                self._approved_tokens.pop(pairing_id, None)

    def get_local_status(self) -> dict[str, Any]:
        self._prune_pairings()
        stored_settings = self.store.get("settings", {})
        settings = {**DEFAULT_SETTINGS, **stored_settings} if isinstance(stored_settings, dict) else dict(DEFAULT_SETTINGS)
        clients = self.store.get("clients", {})
        pairings = self.store.get("pairings", {})
        pairing_host = self._resolve_pairing_host(settings)
        stored_tls = self.store.get("tls", {})
        tls = self._tls if self._tls else (stored_tls if isinstance(stored_tls, dict) else {})
        return {
            "protocol_version": 1,
            "host_id": self.host_id,
            "settings": settings,
            "pairing_host": pairing_host,
            "pairing_host_auto_detected": not bool(settings.get("advertised_host")),
            "wake_target": self._wake_target(),
            "tls": {
                "ready": tls.get("ready") is True,
                "fingerprint": tls.get("fingerprint"),
                "reason": tls.get("reason"),
            },
            "listener": {
                "running": self._server is not None,
                "address": settings.get("listen_address"),
                "port": settings.get("listen_port"),
                "error": self._server_error or None,
            },
            "provider": self.provider_compatibility(),
            "clients": [self._public_client(value) for value in clients.values()],
            "pending_pairings": [
                self._public_pairing(value)
                for value in pairings.values()
                if isinstance(value, dict)
                and value.get("status") == "pending"
                and _pairing_expiry(value) > self.clock()
            ],
            "bridge": self._bridge_public(),
            "local_display": self.local_display_outputs(),
            "sunshine": self.monitor.public(),
        }

    def get_settings(self) -> dict[str, Any]:
        return self.get_local_status()

    def update_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(changes, dict):
            raise ApiError("settings must be an object")
        allowed = set(DEFAULT_SETTINGS)
        if set(changes) - allowed:
            raise ApiError("unsupported setting")
        validated: dict[str, Any] = {}
        if "listen_enabled" in changes:
            if not isinstance(changes["listen_enabled"], bool):
                raise ApiError("listen_enabled must be boolean")
            validated["listen_enabled"] = changes["listen_enabled"]
        if "listen_address" in changes:
            validated["listen_address"] = validate_host(changes["listen_address"])
        if "listen_port" in changes:
            port = changes["listen_port"]
            if not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535:
                raise ApiError("listen_port must be between 1024 and 65535")
            validated["listen_port"] = port
        if "advertised_host" in changes:
            host = changes["advertised_host"]
            validated["advertised_host"] = "" if host in (None, "") else validate_host(host)
        if "device_name" in changes:
            name = changes["device_name"]
            if not isinstance(name, str) or not name.strip() or len(name.strip()) > 96:
                raise ApiError("device_name must be a non-empty string of at most 96 characters")
            validated["device_name"] = " ".join(name.replace("\x00", "").split())[:96]
        if "monitor_sunshine" in changes:
            if not isinstance(changes["monitor_sunshine"], bool):
                raise ApiError("monitor_sunshine must be boolean")
            validated["monitor_sunshine"] = changes["monitor_sunshine"]
        if "auto_recover_sunshine" in changes:
            if not isinstance(changes["auto_recover_sunshine"], bool):
                raise ApiError("auto_recover_sunshine must be boolean")
            validated["auto_recover_sunshine"] = changes["auto_recover_sunshine"]
        self.store.mutate(lambda state: state.setdefault("settings", DEFAULT_SETTINGS).update(validated))
        settings = self.store.get("settings", DEFAULT_SETTINGS)
        if "monitor_sunshine" in validated:
            self.monitor.set_enabled(bool(settings.get("monitor_sunshine")))
            if not settings.get("monitor_sunshine"):
                with self._mutation_lock:
                    self._sunshine_recovery_armed = False
                self.journal.cancel_kind("sunshine.", "cancelled because Monitor Sunshine was disabled")
        if "auto_recover_sunshine" in validated:
            with self._mutation_lock:
                if not settings.get("auto_recover_sunshine", DEFAULT_SETTINGS["auto_recover_sunshine"]):
                    self._sunshine_recovery_armed = False
                elif settings.get("monitor_sunshine") and self.monitor.public().get("state") == "running":
                    self._sunshine_recovery_armed = True
        if any(key in validated for key in ("listen_enabled", "listen_address", "listen_port")):
            self._restart_server()
        return self.get_local_status()

    def create_pairing(self, requested_scopes: Any = None) -> dict[str, Any]:
        scopes = validate_scopes(requested_scopes)
        self._prune_pairings()
        self._ensure_tls_material()
        settings = self.store.get("settings", DEFAULT_SETTINGS)
        host = self._resolve_pairing_host(settings)
        port = int(settings.get("listen_port", DEFAULT_SETTINGS["listen_port"]))
        fingerprint = self._tls.get("fingerprint") or self.store.get("tls", {}).get("fingerprint")
        if not fingerprint:
            raise ApiError("host certificate is not ready; start the listener first", 503, "tls_unavailable")
        pairing_id = opaque_id("pair-")
        secret = _token()
        expires_at = self.clock() + 120
        endpoint = _endpoint(host, port)
        payload = encode_payload({
            "protocol_version": 1,
            "endpoint": endpoint,
            "host_id": self.host_id,
            "certificate_fingerprint": fingerprint,
            "pairing_id": pairing_id,
            "secret": secret,
            "expires_at": expires_at,
        })
        record = {
            "pairing_id": pairing_id,
            "secret_hash": _hash_secret(secret),
            "pairing_method": "payload",
            "requested_scopes": scopes,
            "created_at": _utc_now(),
            "expires_at": expires_at,
            "status": "created",
            "client_name": "Omarchy client",
            "client_id": None,
        }
        with self._pairing_lock:
            self.store.mutate(lambda state: state.setdefault("pairings", {}).__setitem__(pairing_id, record))
        return {
            "pairing_id": pairing_id,
            "payload": payload,
            "expires_at": expires_at,
            "endpoint": endpoint,
            "host_id": self.host_id,
            "certificate_fingerprint": fingerprint,
            "requested_scopes": scopes,
            "wake_target": self._wake_target(),
            "qr_svg_base64": _pairing_qr_svg_base64(payload),
        }

    def create_pairing_code(self, requested_scopes: Any = None) -> dict[str, Any]:
        """Create a short bootstrap code; the client still needs local approval."""
        scopes = validate_scopes(requested_scopes)
        self._prune_pairings()
        self._ensure_tls_material()
        settings = self.store.get("settings", DEFAULT_SETTINGS)
        host = self._resolve_pairing_host(settings)
        port = int(settings.get("listen_port", DEFAULT_SETTINGS["listen_port"]))
        fingerprint = self._tls.get("fingerprint") or self.store.get("tls", {}).get("fingerprint")
        if not fingerprint:
            raise ApiError("host certificate is not ready; start the listener first", 503, "tls_unavailable")
        pairing_id = opaque_id("pair-")
        code = _pairing_code()
        expires_at = self.clock() + 120
        endpoint = _endpoint(host, port)

        def record_code(state: dict[str, Any]) -> None:
            # Keep one active code visible to the owner. Existing full-payload
            # pairings remain valid so the advanced fallback is not disrupted.
            for previous in state.setdefault("pairings", {}).values():
                if previous.get("pairing_method") == "code" and previous.get("status") in {"created", "pending"}:
                    previous["status"] = "superseded"
            state.setdefault("pairings", {})[pairing_id] = {
                "pairing_id": pairing_id,
                "pairing_code_hash": _hash_secret(code),
                "pairing_method": "code",
                "requested_scopes": scopes,
                "created_at": _utc_now(),
                "expires_at": expires_at,
                "status": "created",
                "client_name": "Omarchy client",
                "client_id": None,
            }

        with self._pairing_lock:
            self.store.mutate(record_code)
        return {
            "pairing_id": pairing_id,
            "pairing_code": _display_pairing_code(code),
            "expires_at": expires_at,
            "endpoint": endpoint,
            "host_id": self.host_id,
            "certificate_fingerprint": fingerprint,
            "requested_scopes": scopes,
            "wake_target": self._wake_target(),
        }

    def list_pairings(self) -> list[dict[str, Any]]:
        self._prune_pairings()
        return [self._public_pairing(value) for value in self.store.get("pairings", {}).values()]

    def expire_pending_pairings(self) -> int:
        """Expire unauthenticated requests when the Server role is removed."""
        expired: list[str] = []
        with self._pairing_lock:
            def expire(state: dict[str, Any]) -> None:
                for pairing_id, value in state.setdefault("pairings", {}).items():
                    if isinstance(value, dict) and value.get("status") in {"created", "pending"}:
                        value["status"] = "expired"
                        expired.append(pairing_id)
            self.store.mutate(expire)
            for pairing_id in expired:
                self._approved_tokens.pop(pairing_id, None)
        return len(expired)

    def approve_pairing(self, pairing_id: str, scopes: Any = None) -> dict[str, Any]:
        pairing_id = identifier(pairing_id, "pairing_id")
        with self._pairing_lock:
            pairings = self.store.get("pairings", {})
            pairing = pairings.get(pairing_id)
            if not pairing:
                raise ApiError("pairing is not pending", 404, "pairing_not_found")
            if pairing.get("status") == "created":
                raise ApiError("pairing request has not reached the host", 409, "pairing_not_requested")
            if pairing.get("status") != "pending":
                raise ApiError("pairing is not pending", 404, "pairing_not_found")
            if _pairing_expiry(pairing) <= self.clock():
                raise ApiError("pairing has expired", 410, "pairing_expired")
            allowed = set(pairing.get("requested_scopes", []))
            requested_by_client = pairing.get("requested_by_client")
            if not isinstance(requested_by_client, list):
                raise ApiError("pairing request has not reached the host", 409, "pairing_not_requested")
            requested = set(validate_scopes(requested_by_client))
            granted = validate_scopes(scopes if scopes is not None else list(requested))
            if not set(granted).issubset(allowed) or not set(granted).issubset(requested):
                raise ApiError("approval cannot grant an unrequested scope")
            client_id = pairing.get("client_id") or opaque_id("client-")
            clients = self.store.get("clients", {})
            if client_id not in clients and len(clients) >= MAX_CLIENTS:
                raise ApiError("maximum paired clients reached; revoke one before approving another", 429, "client_limit_reached")
            token = _token()
            client_label = client_name(pairing.get("client_name"))
            client_record = {
                "client_id": client_id,
                "name": client_label,
                "token_hash": _hash_secret(token),
                "scopes": granted,
                "created_at": _utc_now(),
                "last_seen": None,
            }

            def mutate(state: dict[str, Any]) -> None:
                state.setdefault("clients", {})[client_id] = client_record
                state.setdefault("pairings", {}).setdefault(pairing_id, {}).update({
                    "status": "approved",
                    "client_id": client_id,
                    "granted_scopes": granted,
                })

            self.store.mutate(mutate)
            self._approved_tokens[pairing_id] = token
            return self._public_pairing(self.store.get("pairings", {})[pairing_id])

    def reject_pairing(self, pairing_id: str) -> dict[str, Any]:
        pairing_id = identifier(pairing_id, "pairing_id")
        changed = self.store.mutate(lambda state: self._set_pairing_status(state, pairing_id, "rejected"))
        if not changed:
            raise ApiError("pairing is not pending", 404, "pairing_not_found")
        self._approved_tokens.pop(pairing_id, None)
        return {"pairing_id": pairing_id, "status": "rejected"}

    def revoke_client(self, client_id: str) -> dict[str, Any]:
        client_id = identifier(client_id, "client_id")
        removed = self.store.mutate(lambda state: state.setdefault("clients", {}).pop(client_id, None))
        if removed is None:
            raise ApiError("client is not paired", 404, "client_not_found")
        self.journal.cancel_for_client(client_id, "sunshine.", "cancelled because the credential was revoked")
        return {"client_id": client_id, "revoked": True}

    def set_sunshine_provider(self, provider: Any) -> dict[str, Any]:
        if provider is not None and not isinstance(provider, ProviderAdapter):
            provider = ProviderAdapter(provider)
        if provider is not None:
            self._sunshine_owner_reason = None
        self.monitor.set_provider(provider)
        return self.provider_compatibility()

    def _on_sunshine_state_change(self, state: str, _previous_state: str | None) -> None:
        """Recover once when an observed Sunshine process goes down.

        The monitor deliberately reports state changes instead of owning
        process control. A running observation arms one recovery request; the
        first confirmed stopped observation consumes that arm. This gives a
        failed owner plugin a bounded, one-request-per-outage path and leaves
        the existing manual action available for another attempt.
        """
        with self._mutation_lock:
            settings = self.store.get("settings", {})
            monitoring = settings.get("monitor_sunshine") is True
            automatic = settings.get(
                "auto_recover_sunshine", DEFAULT_SETTINGS["auto_recover_sunshine"]
            ) is True
            if state == "running":
                self._sunshine_recovery_armed = monitoring and automatic
                return
            if state != "stopped" or not monitoring or not automatic:
                return
            if _previous_state != "running":
                # Do not turn an unavailable/unknown-to-stopped transition
                # into a recovery request. Automatic recovery is specifically
                # for a confirmed running-to-stopped outage.
                return
            if not self._sunshine_recovery_armed or not self.monitor.provider_ready():
                return
            if self.monitor.public().get("operation_id"):
                return
            self._sunshine_recovery_armed = False
        try:
            self._start_sunshine_restart("auto_monitor")
        except ApiError:
            # The provider may have disappeared or another mutation may have
            # won the lane between the status read and this callback. Do not
            # retry from the same stopped sample.
            return

    def report_sunshine_owner(self, report: Any) -> dict[str, Any]:
        """Register the guarded frontend connection to Decky Sunshine.

        The frontend first probes the owner plugin through Decky Loader. A
        successful probe installs the narrow bridge-backed provider; a failed
        probe clears it so the UI reports an unavailable owner instead of
        pretending that monitoring is active.
        """
        if not isinstance(report, dict) or not isinstance(report.get("available"), bool):
            raise ApiError("sunshine owner report is invalid")
        if report["available"]:
            self._sunshine_owner_reason = None
            return self.set_sunshine_provider(BridgeSunshineProvider(self.bridge))
        self._sunshine_owner_reason = str(report.get("reason") or "Decky Sunshine owner plugin was not reachable")[:256]
        self.monitor.set_provider(None)
        return self.provider_compatibility()

    def provider_compatibility(self) -> dict[str, Any]:
        provider = self.monitor.provider()
        if provider is None:
            return {
                "ready": False,
                "provider": None,
                "contract_version": None,
                "reason": self._sunshine_owner_reason or "No narrow Decky Sunshine owner adapter is connected",
            }
        return {
            "ready": True,
            "provider": str(getattr(provider, "provider_name", "provider"))[:128],
            "contract_version": str(getattr(provider, "contract_version", "unknown"))[:64],
            "reason": None,
        }

    # ---- internal bridge methods --------------------------------------

    def next_bridge_command(self) -> dict[str, Any] | None:
        return self.bridge.next_command()

    def report_bridge_result(self, command_id: str, result: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(command_id, str) or len(command_id) > 128:
            raise ApiError("command_id is invalid")
        if isinstance(result, dict) and isinstance(result.get("snapshot"), dict):
            self.report_bridge_snapshot(result["snapshot"])
        try:
            return self.bridge.report_result(command_id, result)
        except BridgeError as exc:
            raise ApiError(str(exc), 404, "bridge_command_not_found") from exc

    def report_bridge_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        try:
            return self.bridge.report_snapshot(snapshot)
        except BridgeError as exc:
            raise ApiError(str(exc), 400, "invalid_bridge_snapshot") from exc

    def _bridge_public(self) -> dict[str, Any]:
        snapshot, age_ms = self.bridge.snapshot()
        return {
            "ready": snapshot is not None and snapshot.get("ready") is True,
            "age_ms": age_ms,
            "reason": None if snapshot is not None else "Decky frontend bridge has not reported readiness",
            "methods": snapshot.get("methods", {}) if snapshot else {},
        }

    # ---- authenticated route handling --------------------------------

    def handle_http(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        *,
        peer_address: str | None = None,
        channel_binding: str | None = None,
    ) -> tuple[int, dict[str, str], dict[str, Any]]:
        headers = headers or {}
        method = method.upper()
        if "?" in path or "#" in path or not path.startswith("/v1/"):
            raise ApiError("route is not supported", 404, "not_found")
        body = validate_route_body(method, path, body)
        if method == "POST" and path == "/v1/pair/request":
            if peer_address is not None and not channel_binding and not body.get("pairing_session"):
                raise ApiError("TLS channel binding is unavailable; pairing must use a direct TLS connection", 503, "pairing_transport_unavailable")
            binding_header = next(
                (
                    value
                    for key, value in headers.items()
                    if isinstance(key, str) and key.lower() == "x-steamos-companion-tls-binding"
                ),
                None,
            )
            client_channel_binding = _valid_channel_binding(binding_header)
            if peer_address is not None and not body.get("pairing_session") and (
                not client_channel_binding
                or not hmac.compare_digest(str(channel_binding), client_channel_binding)
            ):
                raise ApiError("pairing channel verification failed; select the host again and retry", 401, "pairing_channel_mismatch")
            is_poll = self._has_valid_pairing_session(body)
            self._enforce_rate_limit("pair:" + (peer_address or "direct"), 120 if is_poll else 12, 60.0)
            return 200, {}, self._handle_pair_request(
                body,
                channel_binding=channel_binding,
                client_channel_binding=client_channel_binding,
            )
        if method == "POST" and path == "/v1/pair/cancel":
            self._enforce_rate_limit("pair:" + (peer_address or "direct"), 120, 60.0)
            return 200, {}, self._handle_pair_cancel(body)
        if method == "GET" and path == "/v1/discovery":
            tls = self._ensure_tls_material()
            if not tls.get("ready") or not tls.get("fingerprint"):
                raise ApiError("host certificate is not ready", 503, "tls_unavailable")
            settings = self.store.get("settings", {})
            return 200, {}, {
                "protocol_version": 1,
                "service": "steamos-companion",
                "host_id": self.host_id,
                "certificate_fingerprint": tls["fingerprint"],
                "name": settings.get("device_name") if isinstance(settings.get("device_name"), str) else None,
            }
        if method == "GET" and path == "/v1/status":
            client = self._authenticate(headers, "status.read", peer_address)
            return 200, {}, self.status_for(client["client_id"], client["scopes"])
        if method == "GET" and path == "/v1/display/outputs":
            client = self._authenticate(headers, "status.read", peer_address)
            return 200, {}, self.display_outputs(client["client_id"])
        if method == "GET" and path == "/v1/display/order":
            client = self._authenticate(headers, "status.read", peer_address)
            return 200, {}, self.display_order(client["client_id"])
        if method == "GET" and path == "/v1/operations":
            client = self._authenticate(headers, None, peer_address)
            with self.journal.lock:
                records = self.store.get("operations", {})
                operations = []
                for record in records.values():
                    if not isinstance(record, dict):
                        continue
                    owner = record.get("client_id")
                    kind = record.get("kind")
                    host_display_record = owner == "host" and isinstance(kind, str) and (
                        kind.startswith("display.selection.") or kind == "display.preference"
                    )
                    if owner == client["client_id"] or host_display_record:
                        operations.append(self.journal.public(record))
            operations.sort(
                key=lambda item: (str(item.get("updated_at", "")), str(item.get("id", ""))),
                reverse=True,
            )
            return 200, {}, {
                "protocol_version": 1,
                "operations": operations[:self.journal.MAX_OPERATIONS],
            }
        if method == "GET" and path.startswith("/v1/operations/"):
            client = self._authenticate(headers, None, peer_address)
            operation_id = identifier(path.rsplit("/", 1)[-1], "operation_id")
            operation = self.journal.get(operation_id, client["client_id"])
            if operation is None:
                raise ApiError("operation is not available to this credential", 404, "operation_not_found")
            return 200, {}, {"protocol_version": 1, "operation": operation}
        if method == "POST" and path == "/v1/pair/revoke-self":
            client = self._authenticate(headers, None, peer_address)
            operation = self._revoke_self(client, body)
            return 202, {}, {"protocol_version": 1, "operation": operation}
        client = self._authenticate(headers, self._scope_for_route(path), peer_address)
        if path == "/v1/power":
            return self._power(client, body)
        if path == "/v1/display/order":
            return self._display_order(client, body)
        if path == "/v1/display/order/automatic":
            return self._display_order_reset(client, body)
        if path == "/v1/display/preview":
            return self._display_preview(client, body)
        if path == "/v1/display/confirm":
            return self._display_confirm(client, body)
        if path == "/v1/display/restore":
            return self._display_restore(client, body)
        if path == "/v1/display/save-current":
            return self._display_save_current(client, body)
        if path == "/v1/sunshine/restart":
            return self._sunshine_restart(client, body)
        raise ApiError("route is not supported", 404, "not_found")

    def _authenticate(self, headers: dict[str, str], scope: str | None, peer_address: str | None = None) -> dict[str, Any]:
        authorization = headers.get("Authorization") or headers.get("authorization") or ""
        if not authorization.startswith("Bearer ") or len(authorization) > 512:
            raise ApiError("authentication required", 401, "unauthorized")
        supplied = authorization[7:]
        clients = self.store.get("clients", {})
        for client in clients.values():
            if _constant_time_hash_match(supplied, str(client.get("token_hash", ""))):
                if scope is not None and scope not in set(client.get("scopes", [])):
                    raise ApiError("credential does not have the required scope", 403, "forbidden")
                self._enforce_rate_limit("client:" + str(client.get("client_id")), 120, 60.0)
                return client
        raise ApiError("authentication failed", 401, "unauthorized")

    @staticmethod
    def _scope_for_route(path: str) -> str:
        if path == "/v1/power":
            return "power.control"
        if path.startswith("/v1/display/"):
            return "display.control"
        if path == "/v1/sunshine/restart":
            # Sunshine monitoring is a Decky-owned setting. A normally paired
            # client with the mutation scope may request recovery when Decky
            # exposes a stopped, provider-confirmed process.
            return "power.control"
        return "status.read"

    def _pairing_certificate_fingerprint(self) -> str:
        """The host's own certificate fingerprint, used to bind the shown code."""
        fingerprint = self._tls.get("fingerprint") or self.store.get("tls", {}).get("fingerprint")
        if not fingerprint:
            raise ApiError("host certificate is not ready", 503, "tls_unavailable")
        return str(fingerprint)

    def _find_pairing_by_code(self, pairing_code: str) -> tuple[str, dict[str, Any]]:
        """Resolve a legacy host-issued code without exposing it."""
        match: tuple[str, dict[str, Any]] | None = None
        for candidate_id, candidate in self.store.get("pairings", {}).items():
            expected_hash = candidate.get("pairing_code_hash")
            if isinstance(expected_hash, str) and _constant_time_hash_match(pairing_code, expected_hash):
                match = (candidate_id, candidate)
        if match is None:
            raise ApiError("pairing code is invalid or expired", 401, "unauthorized")
        return match

    def _find_verification_pairing(self, client_id: str, nonce: str) -> tuple[str, dict[str, Any]] | None:
        """Find the client-created request, including terminal outcomes.

        Matching happens on the stored nonce hash, so the slow certificate-bound
        derivation never runs inside this loop. Terminal records are retained
        briefly so the requesting client can receive an explicit rejection or
        cancellation acknowledgement instead of seeing a generic new request.
        """
        nonce_hash = _hash_secret(nonce)
        match: tuple[str, dict[str, Any]] | None = None
        for candidate_id, candidate in self.store.get("pairings", {}).items():
            if candidate.get("pairing_method") != "verification":
                continue
            candidate_hash = candidate.get("verification_nonce_hash")
            if not isinstance(candidate_hash, str) or not hmac.compare_digest(nonce_hash, candidate_hash):
                continue
            if candidate.get("client_id") != client_id:
                raise ApiError("pairing verification code is already in use", 409, "pairing_code_conflict")
            match = (candidate_id, candidate)
        return match

    def _has_valid_pairing_session(self, body: dict[str, Any]) -> bool:
        """Return whether a supplied session is an active verification poll."""
        session = body.get("pairing_session")
        nonce = body.get("verification_nonce")
        client_id = body.get("client_id")
        if not all(isinstance(value, str) for value in (session, nonce, client_id)):
            return False
        nonce_hash = _hash_secret(nonce)
        with self._pairing_lock:
            for candidate in self.store.get("pairings", {}).values():
                if not isinstance(candidate, dict) or candidate.get("pairing_method") != "verification":
                    continue
                if candidate.get("client_id") != client_id:
                    continue
                candidate_hash = candidate.get("verification_nonce_hash")
                expected_hash = candidate.get("pairing_session_hash")
                if (
                    isinstance(candidate_hash, str)
                    and hmac.compare_digest(nonce_hash, candidate_hash)
                    and isinstance(expected_hash, str)
                    and candidate.get("status") in {"pending", "approved", "rejected", "cancelled"}
                    and _pairing_expiry(candidate) > self.clock()
                    and _constant_time_hash_match(session, expected_hash)
                ):
                    return True
        return False

    def _handle_pair_request(
        self,
        body: dict[str, Any],
        *,
        channel_binding: str | None = None,
        client_channel_binding: str | None = None,
    ) -> dict[str, Any]:
        self._prune_pairings()
        pairing_code = normalize_pairing_code(body["pairing_code"]) if "pairing_code" in body else None
        nonce = verification_nonce(body["verification_nonce"]) if "verification_nonce" in body else None
        pairing_session_value = pairing_session(body["pairing_session"]) if "pairing_session" in body else None
        name = client_name(body.get("client_name"))
        requested = validate_scopes(body.get("scopes"))
        client_id = identifier(body.get("client_id"), "client_id")
        with self._pairing_lock:
            secret = None
            pairing_session_token: str | None = None
            newly_created = False
            if nonce is not None:
                found = self._find_verification_pairing(client_id, nonce)
                if found is None:
                    if pairing_session_value is not None:
                        raise ApiError("pairing session is invalid", 401, "unauthorized")
                    active_requests = sum(
                        1
                        for candidate in self.store.get("pairings", {}).values()
                        if candidate.get("pairing_method") == "verification"
                        and candidate.get("status") == "pending"
                        and _pairing_expiry(candidate) > self.clock()
                    )
                    if active_requests >= 32:
                        raise ApiError("too many pending pairing requests", 429, "pairing_queue_full")
                    pairing_id = opaque_id("pair-")
                    newly_created = True
                    expires_at = self.clock() + 120
                    if channel_binding and client_channel_binding:
                        pairing_session_token = _token()
                    # The comparison digits are derived from the client nonce
                    # and this host's own certificate fingerprint, so a relay
                    # presenting a different certificate cannot make both
                    # screens agree. scrypt runs once, here only.
                    derived_code = derive_pairing_code(nonce, self._pairing_certificate_fingerprint())
                    record = {
                        "pairing_id": pairing_id,
                        # This is a human-verification string, not a bearer
                        # credential. It is kept only until the request expires
                        # so the local Decky UI can show the comparison value.
                        "verification_code": _display_pairing_code(derived_code),
                        "verification_nonce_hash": _hash_secret(nonce),
                        "pairing_method": "verification",
                        "requested_scopes": requested,
                        "created_at": _utc_now(),
                        "expires_at": expires_at,
                        "status": "pending",
                        "client_name": name,
                        "client_id": client_id,
                        "requested_by_client": requested,
                    }
                    if pairing_session_token:
                        record.update({
                            "pairing_session_hash": _hash_secret(pairing_session_token),
                            "channel_binding_hash": _hash_channel_binding(channel_binding),
                        })
                    self.store.mutate(lambda state: state.setdefault("pairings", {}).__setitem__(pairing_id, record))
                else:
                    pairing_id, _ = found
            elif pairing_code is not None:
                pairing_id, _ = self._find_pairing_by_code(pairing_code)
            else:
                pairing_id = identifier(body.get("pairing_id"), "pairing_id")
                secret = str(body.get("secret"))
            pairing = self.store.get("pairings", {}).get(pairing_id)
            if not pairing:
                raise ApiError("pairing is not available", 404, "pairing_not_found")
            if pairing.get("status") in {"created", "pending", "approved"} and _pairing_expiry(pairing) <= self.clock():
                self.store.mutate(lambda state: self._set_pairing_status(state, pairing_id, "expired"))
                self._approved_tokens.pop(pairing_id, None)
                raise ApiError("pairing has expired", 410, "pairing_expired")
            if (
                not newly_created
                and pairing.get("status") == "created"
                and not pairing.get("pairing_session_hash")
                and channel_binding
                and client_channel_binding
            ):
                pairing_session_token = _token()
            expected_session_hash = pairing.get("pairing_session_hash")
            if expected_session_hash:
                if newly_created:
                    pass
                elif pairing_session_value is None:
                    raise ApiError("pairing session is required; start a new request", 409, "pairing_session_required")
                elif not _constant_time_hash_match(pairing_session_value, str(expected_session_hash)):
                    raise ApiError("pairing session is invalid", 401, "unauthorized")
            elif pairing_session_value is not None:
                raise ApiError("pairing session is invalid", 401, "unauthorized")
            if nonce is not None:
                valid_code = pairing.get("pairing_method") == "verification" and _constant_time_hash_match(
                    nonce, str(pairing.get("verification_nonce_hash", ""))
                )
                if not valid_code:
                    raise ApiError("pairing verification code is invalid", 401, "unauthorized")
            elif pairing_code is not None:
                valid_code = pairing.get("pairing_method") == "code" and _constant_time_hash_match(
                    pairing_code, str(pairing.get("pairing_code_hash", ""))
                )
                if not valid_code:
                    raise ApiError("pairing code is invalid", 401, "unauthorized")
            elif not _constant_time_hash_match(secret, str(pairing.get("secret_hash", ""))):
                raise ApiError("pairing secret is invalid", 401, "unauthorized")
            if not set(requested).issubset(set(pairing.get("requested_scopes", []))):
                raise ApiError("pairing requested an unapproved scope", 403, "forbidden")
            existing_client_id = pairing.get("client_id")
            if existing_client_id is not None and existing_client_id != client_id:
                raise ApiError("pairing is already claimed by another client", 409, "pairing_client_conflict")
            previous_requested = pairing.get("requested_by_client")
            if previous_requested is not None and set(requested) != set(previous_requested):
                raise ApiError("pairing scopes changed while approval was pending", 409, "pairing_scope_conflict")

            def record_request(state: dict[str, Any]) -> None:
                record = state.setdefault("pairings", {}).setdefault(pairing_id, {})
                record.update({"client_name": name, "requested_by_client": requested, "client_id": client_id})
                if pairing_session_token:
                    record["pairing_session_hash"] = _hash_secret(pairing_session_token)
                    record["channel_binding_hash"] = _hash_channel_binding(channel_binding)
                if record.get("status") == "created":
                    record["status"] = "pending"

            self.store.mutate(record_request)
            pairing = self.store.get("pairings", {})[pairing_id]
            if pairing.get("status") == "pending":
                response = {"protocol_version": 1, "state": "pending", "pairing_id": pairing_id, "expires_at": pairing.get("expires_at")}
                if pairing_session_token:
                    response["pairing_session"] = pairing_session_token
                return response
            if pairing.get("status") == "rejected":
                raise ApiError("pairing was rejected by the device owner", 409, "pairing_rejected")
            if pairing.get("status") == "cancelled":
                raise ApiError("pairing was cancelled by the requesting client", 409, "pairing_cancelled")
            if pairing.get("status") == "expired":
                raise ApiError("pairing has expired", 410, "pairing_expired")
            if pairing.get("status") in {"consumed", "superseded"}:
                raise ApiError("pairing is not available", 404, "pairing_not_found")
            if pairing.get("status") != "approved":
                raise ApiError("pairing is not approved", 409, "pairing_not_approved")
            token = self._approved_tokens.pop(pairing_id, None)
            if token is None:
                raise ApiError("approved credential has already been delivered; create a new pairing", 410, "pairing_consumed")
            self.store.mutate(lambda state: self._set_pairing_status(state, pairing_id, "consumed"))
            client_id = pairing.get("client_id")
            client = self.store.get("clients", {}).get(client_id, {})
            return {
                "protocol_version": 1,
                "state": "approved",
                "pairing_id": pairing_id,
                "host_id": self.host_id,
                "credential": {
                    "client_id": client_id,
                    "token": token,
                    "scopes": client.get("scopes", []),
                    "host_id": self.host_id,
                },
                "wake_target": self._wake_target(),
            }

    def _handle_pair_cancel(self, body: dict[str, Any]) -> dict[str, Any]:
        """Cancel a verification request after authenticating its nonce.

        The requesting client has no bearer token yet. Its private nonce is
        therefore the cancellation proof; the optional pairing session is
        checked as an additional binding when the initial exchange created
        one. Returning terminal states idempotently lets either side observe
        the same outcome when reject and cancel race each other.
        """
        self._prune_pairings()
        pairing_id = identifier(body.get("pairing_id"), "pairing_id")
        nonce = verification_nonce(body.get("verification_nonce"))
        client_id = identifier(body.get("client_id"), "client_id")
        pairing_session_value = pairing_session(body["pairing_session"]) if "pairing_session" in body else None
        with self._pairing_lock:
            pairing = self.store.get("pairings", {}).get(pairing_id)
            if not isinstance(pairing, dict) or pairing.get("pairing_method") != "verification":
                raise ApiError("pairing is not available", 404, "pairing_not_found")
            if pairing.get("client_id") != client_id:
                raise ApiError("pairing cancellation is not authorized", 401, "unauthorized")
            if not _constant_time_hash_match(nonce, str(pairing.get("verification_nonce_hash", ""))):
                raise ApiError("pairing verification code is invalid", 401, "unauthorized")
            expected_session_hash = pairing.get("pairing_session_hash")
            if pairing_session_value is not None:
                if not expected_session_hash or not _constant_time_hash_match(pairing_session_value, str(expected_session_hash)):
                    raise ApiError("pairing session is invalid", 401, "unauthorized")

            status = pairing.get("status")
            if status in {"created", "pending"} and _pairing_expiry(pairing) <= self.clock():
                self.store.mutate(lambda state: self._set_pairing_status(state, pairing_id, "expired"))
                status = "expired"
            elif status == "pending":
                self.store.mutate(lambda state: self._set_pairing_status(state, pairing_id, "cancelled"))
                status = "cancelled"
            elif status in {"cancelled", "rejected", "expired"}:
                pass
            elif status == "approved":
                raise ApiError("pairing was already approved", 409, "pairing_already_approved")
            else:
                raise ApiError("pairing is not available", 404, "pairing_not_found")

        return {
            "protocol_version": 1,
            "state": status,
            "pairing_id": pairing_id,
        }

    # ---- status --------------------------------------------------------

    def status_for(self, client_id: str, scopes: list[str] | None = None) -> dict[str, Any]:
        snapshot, age_ms = self.bridge.snapshot()
        ready = snapshot is not None and snapshot.get("ready") is True
        bridge_seen = snapshot is not None
        methods = snapshot.get("methods", {}) if snapshot else {}
        reason = None if ready else (snapshot.get("reason") if snapshot else "Decky frontend bridge has not reported readiness")
        profiles = self.store.get("profiles", {})
        sunshine = self.monitor.public()
        sunshine_capability = "disabled"
        if sunshine["enabled"]:
            if sunshine["state"] == "stopped" and self.monitor.provider_ready():
                sunshine_capability = "available"
            elif sunshine["state"] in {"unavailable", "unknown"}:
                sunshine_capability = "unavailable"
            else:
                sunshine_capability = "unavailable"
        return {
            "protocol_version": 1,
            "host_id": self.host_id,
            "boot_id": self.boot_id,
            "steam_bridge": "ready" if ready else "unavailable",
            "steam_reason": reason,
            "steam_age_ms": age_ms,
            "wake_target": self._wake_target(),
            "uptime_seconds": system_uptime_seconds(),
            "cpu_temperature": (snapshot.get("cpu_temperature") if snapshot else None) or read_cpu_temperature(),
            "capabilities": {
                "suspend": "available" if bridge_seen and methods.get("suspend") else "unavailable",
                "restart": "available" if bridge_seen and methods.get("restart") else "unavailable",
                "shutdown": "available" if bridge_seen and methods.get("shutdown") else "unavailable",
                "display_rescue": "available" if ready and methods.get("display") else ("unavailable" if not ready else "unverified"),
                "sunshine_restart": sunshine_capability,
            },
            "sunshine": sunshine,
        }

    def display_outputs(self, client_id: str) -> dict[str, Any]:
        snapshot, age_ms = self.bridge.snapshot()
        if snapshot is None:
            return {"protocol_version": 1, "available": False, "age_ms": age_ms, "reason": "Steam display bridge is unavailable", "outputs": []}
        profiles = self.store.get("profiles", {})
        public_profiles = [self._public_profile(value) for value in profiles.values()]
        return {
            "protocol_version": 1,
            "available": bool(snapshot.get("methods", {}).get("display")),
            "age_ms": age_ms,
            "reason": None if snapshot.get("methods", {}).get("display") else "Steam display bridge is unavailable",
            "generation": max((output.get("generation", 0) for output in snapshot.get("outputs", [])), default=0),
            # Keep local Gaming Mode identity/capability metadata out of the
            # existing authenticated wire contract. The local Decky RPC below
            # is the only consumer of those additive bridge fields.
            "outputs": [self._public_remote_output(output) for output in snapshot.get("outputs", [])],
            "profiles": public_profiles,
            "preview": self._public_preview(self.store.get("preview")),
        }

    @staticmethod
    def _public_display_order_output(output: dict[str, Any], active_output_key: str | None) -> dict[str, Any]:
        output_key = output["output_key"]
        display_name = next(
            (
                value
                for value in (output.get("display_name"), output.get("name"), output.get("connector"))
                if isinstance(value, str) and value
            ),
            None,
        )
        connector = output.get("connector")
        if not isinstance(connector, str) or not connector:
            connector = None
        return {
            "output_key": output_key,
            "display_name": display_name[:256] if isinstance(display_name, str) else None,
            "connector": connector[:128] if isinstance(connector, str) else None,
            "connected": output.get("connected") is True,
            "active": (
                None
                if output.get("connected") is not True or active_output_key is None
                else output_key == active_output_key
            ),
        }

    def display_order(self, client_id: str) -> dict[str, Any]:
        """Return the fresh physical inventory and the saved Gamescope order."""
        del client_id
        inventory = self._drm_inventory.snapshot()
        gamescope = self.gamescope.status()
        if not isinstance(gamescope, dict):
            gamescope = {}
        supported = gamescope.get("available") is True

        raw_outputs = inventory.get("outputs", []) if isinstance(inventory, dict) else []
        outputs = [
            output
            for output in raw_outputs
            if (
                isinstance(output, dict)
                and isinstance(output.get("output_key"), str)
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:|/-]{0,127}", output["output_key"])
            )
        ]
        connected_outputs = [output for output in outputs if output.get("connected") is True]
        connector_counts: dict[str, int] = {}
        invalid_connected_identity = False
        for output in connected_outputs:
            connector = output.get("connector")
            if not isinstance(connector, str) or not connector:
                invalid_connected_identity = True
                continue
            connector_counts[connector] = connector_counts.get(connector, 0) + 1
        ambiguous = invalid_connected_identity or any(count > 1 for count in connector_counts.values())

        active_connector = gamescope.get("active_connector")
        active_matches = [
            output
            for output in connected_outputs
            if isinstance(active_connector, str) and output.get("connector") == active_connector
        ]
        active_output_key = active_matches[0]["output_key"] if len(active_matches) == 1 else None
        public_outputs = [
            self._public_display_order_output(output, active_output_key)
            for output in outputs
        ]

        configured_connectors = gamescope.get("configured_connectors", [])
        if not isinstance(configured_connectors, list):
            configured_connectors = []
        saved_output_keys: list[str] = []
        unavailable_connector: str | None = None
        for connector in configured_connectors:
            if not isinstance(connector, str):
                continue
            matches = [output for output in outputs if output.get("connector") == connector]
            if len(matches) == 1:
                selected = matches[0]
            elif len(matches) > 1:
                connected_matches = [output for output in matches if output.get("connected") is True]
                if len(connected_matches) != 1:
                    ambiguous = True
                    if unavailable_connector is None:
                        unavailable_connector = connector
                    continue
                selected = connected_matches[0]
            else:
                if unavailable_connector is None:
                    unavailable_connector = connector
                continue
            output_key = selected["output_key"]
            if output_key not in saved_output_keys:
                saved_output_keys.append(output_key)
            if selected.get("connected") is not True and unavailable_connector is None:
                unavailable_connector = connector

        if inventory is None:
            reason = (
                "This host does not expose remote Gaming Mode display ordering."
                if not supported
                else "Physical display inventory is unavailable."
            )
        elif not supported:
            reason = gamescope.get("reason") or "This host does not expose remote Gaming Mode display ordering."
        elif ambiguous:
            reason = "A connected display connector identity is ambiguous; display ordering is unavailable."
        elif not connected_outputs:
            reason = "No connected display outputs are available."
        elif unavailable_connector:
            reason = f"The preferred {unavailable_connector} output is currently unavailable; the next connected output is used."
        else:
            reason = None

        return {
            "protocol_version": 1,
            "display_order": {
                "available": bool(inventory is not None and supported and connected_outputs and not ambiguous),
                "generation": inventory.get("generation") if isinstance(inventory, dict) else None,
                "observed_at": _utc_now() if inventory is not None else None,
                "output_keys": [output["output_key"] for output in public_outputs],
                "outputs": public_outputs,
                "saved_output_keys": saved_output_keys,
                "restart_required": bool(supported and gamescope.get("requires_restart") is True),
                "restart_available": bool(supported and gamescope.get("restart_available") is True),
                "adapter": (
                    gamescope.get("adapter")[:128]
                    if supported and isinstance(gamescope.get("adapter"), str)
                    else None
                ),
                "unsupported": not supported,
                "stale": False,
                "ambiguous": ambiguous,
                "previous_reading": False,
                "reason": reason[:256] if isinstance(reason, str) else None,
            },
        }


    # ---- local Gaming Mode display selection -------------------------

    @staticmethod
    def _bridge_has_verified_local_adapter(snapshot: dict[str, Any]) -> bool:
        methods = snapshot.get("methods", {})
        return isinstance(methods, dict) and methods.get("display_selection") is True

    def _local_inventory_snapshot(self) -> tuple[dict[str, Any] | None, int | None, bool]:
        """Return a fresh physical inventory or a separately-labelled old one.

        Steam's legacy DisplayManager state can contain only the logical
        Gamescope surface. Linux DRM sysfs remains a read-only source of the
        physical connector inventory in that case. A verified local adapter,
        when one exists, remains authoritative for selection-capable state.
        """
        current, age_ms = self.bridge.snapshot(BRIDGE_SNAPSHOT_MAX_AGE)
        physical = self._drm_inventory.snapshot()
        if physical is not None and (current is None or not self._bridge_has_verified_local_adapter(current)):
            return physical, 0, False
        if current is not None and current.get("ready") is True:
            return current, age_ms, False
        previous, previous_age = self.bridge.previous_ready_snapshot()
        if previous is not None:
            return previous, previous_age, True
        return None, age_ms if age_ms is not None else previous_age, False

    @staticmethod
    def _local_output_key(value: Any) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:|/-]{0,127}", value):
            raise ApiError("display output key is invalid", 400, "invalid_display_target")
        return value

    @staticmethod
    def _local_generation(value: Any) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 2_147_483_647:
            raise ApiError("display generation is invalid", 400, "invalid_display_generation")
        return value

    @staticmethod
    def _local_current_mode(output: dict[str, Any] | None) -> dict[str, Any] | None:
        return HostService._find_mode(output, output.get("current_mode_id")) if output else None

    @staticmethod
    def _local_find_output(snapshot: dict[str, Any] | None, output_key: Any) -> dict[str, Any] | None:
        if not snapshot:
            return None
        return next((output for output in snapshot.get("outputs", []) if output.get("output_key") == output_key), None)

    @staticmethod
    def _local_selection_capability(snapshot: dict[str, Any]) -> dict[str, Any]:
        selection = snapshot.get("selection", {}) if isinstance(snapshot.get("selection"), dict) else {}
        active_key, active_state = resolve_active_output(snapshot.get("outputs", []), snapshot.get("active_output_key"))
        can_switch = snapshot.get("ready") is True and snapshot.get("methods", {}).get("display_selection") is True and selection.get("can_switch_live") is True
        can_startup = selection.get("can_set_startup_preference") is True
        restart_required = selection.get("restart_required") is True
        recovery_available = can_switch and active_state == "known"
        reason = selection.get("reason") or ""
        if snapshot.get("ready") is not True:
            reason = snapshot.get("reason") or "Steam display bridge is unavailable"
            can_switch = False
            can_startup = False
            restart_required = False
            recovery_available = False
        elif active_state == "ambiguous":
            reason = "The active Gaming Mode screen is ambiguous; selection is disabled until it is identified."
            recovery_available = False
        elif active_state != "known" and can_switch:
            reason = "The active Gaming Mode screen could not be identified reliably."
            recovery_available = False
        elif not can_switch and not reason:
            reason = "Live Gaming Mode screen selection is not verified on this SteamOS build."
        return {
            "active_output_key": active_key,
            "active_state": active_state,
            "can_switch_live": can_switch,
            "can_set_startup_preference": can_startup,
            "restart_required": restart_required,
            "recovery_available": recovery_available,
            "reason": reason[:256],
            "adapter": selection.get("adapter"),
        }

    def _local_gamescope_output_capability(self, *, previous_reading: bool = False) -> dict[str, Any]:
        """Report the real Gamescope output-preference capability.

        Steam's ``Settings.SetPreferredMonitor`` only changes a Steam
        preference and does not move the active Gaming Mode scanout. SteamOS
        starts Gamescope with ``--prefer-output`` instead, so this local
        control manages a guarded user-session override and reads the active
        connector from gamescopectl when available.
        """
        status = self.gamescope.status()
        if previous_reading:
            reason = "Previous display reading; refresh before applying the Gamescope output preference."
        elif status["available"]:
            reason = "Gamescope output preference is available; leave and re-enter Gaming Mode to apply it."
        else:
            reason = status.get("reason") or "Gamescope output preference is unavailable on this SteamOS build."
        return {
            "available": bool(status["available"] and not previous_reading),
            "readback_available": status.get("readback_available") is True,
            "active_connector": status.get("active_connector"),
            "configured_connector": status.get("configured_connector"),
            "configured_connectors": status.get("configured_connectors", []),
            "requires_restart": status.get("requires_restart") is True,
            "restart_available": bool(status.get("restart_available") is True and not previous_reading),
            "age_ms": None,
            "adapter": status.get("adapter"),
            "reason": str(reason)[:256],
        }

    @staticmethod
    def _mark_gamescope_active(snapshot: dict[str, Any], connector: Any) -> dict[str, Any]:
        """Add Gamescope's connector readback to a physical DRM snapshot."""
        if not isinstance(connector, str):
            return snapshot
        matches = [
            output for output in snapshot.get("outputs", [])
            if isinstance(output, dict) and output.get("connector") == connector
        ]
        # A connector name without its GPU is not a sufficient identity when
        # two cards expose the same name.
        if len(matches) != 1:
            return snapshot
        active_key = matches[0].get("output_key")
        if not isinstance(active_key, str):
            return snapshot
        return {
            **snapshot,
            "active_output_key": active_key,
            "outputs": [
                {**output, "active": output.get("output_key") == active_key}
                for output in snapshot.get("outputs", [])
            ],
        }

    def local_display_outputs(self) -> dict[str, Any]:
        """Return bounded local display inventory and independent capabilities."""
        snapshot, age_ms, previous_reading = self._local_inventory_snapshot()
        pending = self.store.get("local_display_preview")
        if snapshot is None:
            return {
                "protocol_version": 1,
                "available": False,
                "fresh": False,
                "previous_reading": False,
                "stale": age_ms is not None,
                "age_ms": age_ms,
                "generation": None,
                "active_output_key": None,
                "active_state": "unknown",
                "reason": "Steam display bridge is unavailable",
                "selection": {
                    "active_output_key": None,
                    "active_state": "unknown",
                    "can_switch_live": False,
                    "can_set_startup_preference": False,
                    "restart_required": False,
                    "recovery_available": False,
                    "reason": "Steam display bridge is unavailable",
                    "adapter": None,
                },
                "outputs": [],
                "source": None,
                "connected_count": 0,
                "monitor_switch": self._local_gamescope_output_capability(previous_reading=previous_reading),
                "last_operation": self._latest_local_display_operation(),
                "preview": self._public_local_preview(pending),
            }
        monitor_switch = self._local_gamescope_output_capability(previous_reading=previous_reading)
        if snapshot.get("source") == "linux-drm-sysfs":
            snapshot = self._mark_gamescope_active(snapshot, monitor_switch.get("active_connector"))
        selection = self._local_selection_capability(snapshot)
        if previous_reading:
            selection = {
                **selection,
                "can_switch_live": False,
                "can_set_startup_preference": False,
                "restart_required": False,
                "recovery_available": False,
                "reason": "Previous display reading; refresh before applying a change.",
            }
        return {
            "protocol_version": 1,
            "available": snapshot.get("ready") is True and not previous_reading,
            "fresh": not previous_reading and snapshot.get("ready") is True,
            "previous_reading": previous_reading,
            "stale": previous_reading or age_ms is None or age_ms > int(BRIDGE_SNAPSHOT_MAX_AGE * 1000),
            "age_ms": age_ms,
            "generation": snapshot.get("generation"),
            "active_output_key": selection["active_output_key"],
            "active_state": selection["active_state"],
            "reason": None if snapshot.get("ready") is True and not previous_reading else (snapshot.get("reason") or "Previous display reading; refresh before applying a change."),
            "selection": selection,
            "outputs": snapshot.get("outputs", []),
            "source": snapshot.get("source", "steam-display-manager"),
            "connected_count": sum(1 for output in snapshot.get("outputs", []) if output.get("connected") is True),
            "monitor_switch": monitor_switch,
            "last_operation": self._latest_local_display_operation(),
            "preview": self._public_local_preview(pending),
        }

    def _queue_local_gamescope_output(
        self,
        connector: str,
        *,
        output_key: str | None = None,
        generation: int | None = None,
    ) -> dict[str, Any]:
        with self._mutation_lock:
            self._reject_if_conflicting("display_preference")
            capability = self._local_gamescope_output_capability()
            if not capability["available"] and connector:
                raise ApiError(capability["reason"], 409, "gamescope_output_unsupported")
            if output_key is not None:
                inventory = self._drm_inventory.snapshot()
                target = self._local_find_output(inventory, output_key) if inventory else None
                if inventory is None or target is None:
                    raise ApiError("the selected screen is no longer available", 409, "stale_display_target")
                if target.get("connected") is not True:
                    raise ApiError("the selected screen is no longer connected", 409, "stale_display_target")
                if target.get("connector") != connector:
                    raise ApiError("the selected monitor identity changed", 409, "stale_display_target")
                same_connector = [
                    output for output in inventory.get("outputs", [])
                    if output.get("connected") is True and output.get("connector") == connector
                ]
                if len(same_connector) != 1:
                    raise ApiError("the selected connector identity is ambiguous", 409, "ambiguous_display_identity")
                generation = inventory.get("generation")
            operation = self.journal.internal("display.preference", {
                "output_key": output_key,
                "connector": connector or None,
                "generation": generation,
            }, owner=LOCAL_OPERATION_OWNER)
            self.journal.update(operation["id"], target={
                "output_key": output_key,
                "connector": connector or None,
                "generation": generation,
            })
            try:
                result = self.gamescope.apply(connector) if connector else self.gamescope.clear()
            except GamescopeError as exc:
                operation = self.journal.update(operation["id"], state="failed", reason=str(exc)[:256])
                raise ApiError(str(exc), 409, "gamescope_output_update_failed") from exc
            reason = (
                "Gamescope output preference saved; leave and re-enter Gaming Mode to apply it."
                if connector
                else "Gamescope output preference cleared; leave and re-enter Gaming Mode to restore defaults."
            )
            operation = self.journal.update(
                operation["id"],
                state="succeeded",
                outcome="gamescope_output_configured" if connector else "gamescope_output_cleared",
                reason=reason,
            )
            return {"protocol_version": 1, "operation": operation, "gamescope": result}

    def local_gamescope_output(self, output_key: str) -> dict[str, Any]:
        """Save a validated connector as the next Gaming Mode output."""
        output_key = self._local_output_key(output_key)
        inventory = self._drm_inventory.snapshot()
        target = self._local_find_output(inventory, output_key) if inventory else None
        connector = target.get("connector") if target else None
        if not isinstance(connector, str):
            raise ApiError("the selected screen has no usable monitor identity", 409, "invalid_display_target")
        return self._queue_local_gamescope_output(connector, output_key=output_key)

    def local_gamescope_outputs(self, output_keys: Any, generation: Any, restart: Any = False) -> dict[str, Any]:
        """Save an ordered list of attached outputs and optionally restart Gaming Mode."""
        if not isinstance(output_keys, list) or not 1 <= len(output_keys) <= 16:
            raise ApiError("output order must contain 1 to 16 screens", 400, "invalid_display_target")
        if type(restart) is not bool:
            raise ApiError("restart must be a boolean", 400, "invalid_request")
        generation = self._local_generation(generation)
        validated_keys = [self._local_output_key(value) for value in output_keys]
        if len(set(validated_keys)) != len(validated_keys):
            raise ApiError("output order contains duplicate screens", 400, "invalid_display_target")

        with self._mutation_lock:
            self._reject_if_conflicting("display_preference")
            capability = self._local_gamescope_output_capability()
            if not capability["available"]:
                raise ApiError(capability["reason"], 409, "gamescope_output_unsupported")
            inventory = self._drm_inventory.snapshot()
            if inventory is None:
                raise ApiError("the screen inventory is unavailable", 409, "stale_display_target")
            if inventory.get("generation") != generation:
                raise ApiError("display inventory changed; refresh the order", 409, "stale_display_inventory")
            connected_outputs = [
                output for output in inventory.get("outputs", [])
                if isinstance(output, dict) and output.get("connected") is True
            ]
            connectors: list[str] = []
            for output_key in validated_keys:
                target = self._local_find_output(inventory, output_key)
                if target is None or target.get("connected") is not True:
                    raise ApiError("a selected screen is no longer connected", 409, "stale_display_target")
                connector = target.get("connector")
                if not isinstance(connector, str):
                    raise ApiError("a selected screen has no usable connector", 409, "invalid_display_target")
                if sum(1 for output in connected_outputs if output.get("connector") == connector) != 1:
                    raise ApiError("a selected connector identity is ambiguous", 409, "ambiguous_display_identity")
                connectors.append(connector)
            if len(set(connectors)) != len(connectors):
                raise ApiError("output order contains duplicate connectors", 409, "ambiguous_display_identity")

            target = {
                "output_keys": validated_keys,
                "connectors": connectors,
                "generation": generation,
                "restart": restart,
            }
            operation = self.journal.internal("display.preference", target, owner=LOCAL_OPERATION_OWNER)
            self.journal.update(operation["id"], target=target)
            try:
                result = self.gamescope.apply_order(connectors)
            except GamescopeError as exc:
                operation = self.journal.update(operation["id"], state="failed", reason=str(exc)[:256])
                raise ApiError(str(exc), 409, "gamescope_output_update_failed") from exc
            if restart:
                try:
                    restart_result = self.gamescope.restart_session()
                except GamescopeError as exc:
                    reason = f"Output order was saved, but Gaming Mode could not restart: {exc}"
                    operation = self.journal.update(
                        operation["id"],
                        state="failed",
                        outcome="gamescope_output_configured_restart_failed",
                        reason=reason[:256],
                    )
                    raise ApiError(reason, 409, "gamescope_session_restart_failed") from exc
                result = {**result, "restart": restart_result}
            operation = self.journal.update(
                operation["id"],
                state="succeeded",
                outcome="gamescope_output_configured_and_restart_requested" if restart else "gamescope_output_configured",
                reason="Output order saved; Gaming Mode restart requested." if restart else "Output order saved for the next Gaming Mode session.",
            )
            return {"protocol_version": 1, "operation": operation, "gamescope": result}

    def local_clear_gamescope_output(self) -> dict[str, Any]:
        """Remove the plugin-owned Gamescope output override."""
        return self._queue_local_gamescope_output("")

    def local_gamescope_restart(self) -> dict[str, Any]:
        """Restart only the current Gaming Mode session using a fixed target."""
        with self._mutation_lock:
            self._reject_if_conflicting("display_preference")
            operation = self.journal.internal(
                "display.session-restart",
                {"target": "gamescope-session.target"},
                owner=LOCAL_OPERATION_OWNER,
            )
            self.journal.update(operation["id"], target={"target": "gamescope-session.target"})
            try:
                result = self.gamescope.restart_session()
            except GamescopeError as exc:
                operation = self.journal.update(operation["id"], state="failed", reason=str(exc)[:256])
                raise ApiError(str(exc), 409, "gamescope_session_restart_failed") from exc
            operation = self.journal.update(
                operation["id"],
                state="succeeded",
                outcome="gamescope_session_restart_requested",
                reason="Gaming Mode restart requested; running games and the Steam UI will close.",
            )
            return {"protocol_version": 1, "operation": operation, "gamescope": result}

    # Keep the v0.5.7 local RPC names as aliases so an older frontend can use
    # the corrected Gamescope implementation after only the backend reloads.
    local_preferred_monitor = local_gamescope_output
    local_clear_preferred_monitor = local_clear_gamescope_output

    def _local_mutation_inventory(self) -> tuple[dict[str, Any], dict[str, Any]]:
        snapshot, age_ms = self.bridge.snapshot(BRIDGE_SNAPSHOT_MAX_AGE)
        if snapshot is None and age_ms is not None:
            raise ApiError("Steam display inventory is stale; refresh before applying a change", 409, "stale_display_inventory")
        if snapshot is None or snapshot.get("ready") is not True:
            raise ApiError("Steam display bridge is unavailable or stale", 503, "bridge_unavailable")
        selection = self._local_selection_capability(snapshot)
        if selection["active_state"] != "known":
            code = "active_output_ambiguous" if selection["active_state"] == "ambiguous" else "active_output_unknown"
            raise ApiError(selection["reason"] or "The active Gaming Mode screen cannot be identified reliably", 409, code)
        if not selection["can_switch_live"]:
            reason = selection["reason"] or "Live Gaming Mode screen selection is not verified on this SteamOS build."
            raise ApiError(reason, 409, "display_selection_unsupported")
        if not selection["recovery_available"]:
            raise ApiError(selection["reason"] or "Baseline screen recovery is unavailable", 409, "recovery_unavailable")
        return snapshot, selection

    def local_display_preview(self, output_key: str, generation: int) -> dict[str, Any]:
        """Start a host-owned live output preview, if the adapter is verified."""
        output_key = self._local_output_key(output_key)
        generation = self._local_generation(generation)
        with self._mutation_lock:
            existing = self.store.get("local_display_preview")
            if isinstance(existing, dict) and existing.get("preview_id"):
                if existing.get("target_output_key") == output_key and existing.get("generation") == generation and not existing.get("restore_started"):
                    operation = self.journal.get(existing["operation_id"]) or {"id": existing["operation_id"], "state": "unknown"}
                    return {"protocol_version": 1, "operation": operation, "preview": self._public_local_preview(existing)}
                raise ApiError("a Gaming Mode screen preview is already active", 409, "mutation_conflict")
            self._reject_if_conflicting("local_display_preview")
            snapshot, selection = self._local_mutation_inventory()
            if snapshot.get("generation") != generation:
                raise ApiError("display inventory generation is stale", 409, "stale_display_inventory")
            target = self._local_find_output(snapshot, output_key)
            if not target or target.get("connected") is not True:
                raise ApiError("the selected screen is no longer connected", 409, "stale_display_target")
            if target.get("identity_confidence") == "ambiguous":
                raise ApiError("the selected screen identity is ambiguous; choose it again", 409, "ambiguous_display_identity")
            if not target.get("can_switch_live"):
                raise ApiError("the selected screen is not offered by the verified adapter", 409, "display_selection_unsupported")
            active_key = selection["active_output_key"]
            baseline = self._local_find_output(snapshot, active_key)
            if not baseline or baseline.get("connected") is not True:
                raise ApiError("the active screen is no longer available for recovery", 409, "recovery_unavailable")
            if active_key == output_key:
                raise ApiError("the selected screen is already active", 409, "no_change")
            baseline_mode = self._local_current_mode(baseline)
            target_mode = self._local_current_mode(target)
            if baseline_mode is None or target_mode is None:
                raise ApiError("a readable mode is required for display recovery", 409, "recovery_unavailable")
            operation = self.journal.internal("display.selection.preview", {
                "target_output_key": output_key, "generation": generation,
            }, owner=LOCAL_OPERATION_OWNER)
            preview = {
                "kind": "gaming_mode_output",
                "preview_id": operation["id"],
                "operation_id": operation["id"],
                "owner_client_id": LOCAL_OPERATION_OWNER,
                "target_output_key": output_key,
                "target_identity": selection_identity(target),
                "target_mode": target_mode,
                "baseline_output_key": active_key,
                "baseline_identity": selection_identity(baseline),
                "baseline_mode": baseline_mode,
                "generation": generation,
                "deadline": None,
                "restore_started": False,
                "restore_state": None,
                "created_at": _utc_now(),
                "session_id": self.boot_id,
            }
            self.store.mutate(lambda state: state.__setitem__("local_display_preview", preview))
            self.journal.update(operation["id"], preview_id=operation["id"], target={
                "target_output_key": output_key, "baseline_output_key": active_key, "generation": generation,
            })
            operation = self.journal.get(operation["id"]) or operation
            self._spawn(operation["id"], lambda: self._run_local_display_preview(operation["id"]))
            return {"protocol_version": 1, "operation": operation, "preview": self._public_local_preview(self.store.get("local_display_preview"))}

    def _run_local_display_preview(self, operation_id: str) -> None:
        with self._mutation_lock:
            preview = self.store.get("local_display_preview")
            if not preview or preview.get("preview_id") != operation_id:
                self.journal.update(operation_id, state="failed", reason="display selection intent disappeared before dispatch")
                return
            dispatched = False
            try:
                snapshot, selection = self._local_mutation_inventory()
                if snapshot.get("generation") != preview.get("generation"):
                    raise DisplayError("display topology changed; preview not sent")
                target = self._local_find_output(snapshot, preview["target_output_key"])
                baseline = self._local_find_output(snapshot, preview["baseline_output_key"])
                if not target or not baseline or target.get("connected") is not True or baseline.get("connected") is not True:
                    raise DisplayError("target or baseline screen disappeared; preview not sent")
                if not same_selection_identity(preview["target_identity"], selection_identity(target)):
                    raise DisplayError("target screen identity changed; preview not sent")
                if not same_selection_identity(preview["baseline_identity"], selection_identity(baseline)):
                    raise DisplayError("baseline screen identity changed; preview not sent")
                current_target_mode = self._local_current_mode(target)
                if not same_mode_profile(preview["target_mode"], current_target_mode):
                    raise DisplayError("target screen mode changed; preview not sent")
                deadline = self.clock() + LOCAL_DISPLAY_PREVIEW_SECONDS
                self.store.mutate(lambda state: state.setdefault("local_display_preview", {}).update({"deadline": deadline}))
                self.journal.update(operation_id, state="dispatched", target={
                    "target_output_key": target["output_key"], "baseline_output_key": baseline["output_key"],
                    "generation": snapshot["generation"],
                })
                dispatched = True
                result = self.bridge.request("select_output", {
                    "output_key": target["output_key"], "generation": snapshot["generation"],
                }, operation_id, timeout=8)
                if not _result_ok(result):
                    raise BridgeError(str(result.get("reason", "Steam rejected Gaming Mode screen selection")))
                readback = result.get("snapshot") if isinstance(result.get("snapshot"), dict) else None
                if readback:
                    self.store.mutate(lambda state: state.setdefault("local_display_preview", {}).update({
                        "readback": readback, "readback_at": _utc_now(),
                    }))
                latest, _ = self.bridge.snapshot(BRIDGE_SNAPSHOT_MAX_AGE)
                if not self._local_readback_matches(preview, latest, target=True):
                    raise BridgeError("active screen readback did not confirm the selected target")
                self.journal.update(operation_id, state="observed_return", outcome="target_active_read_back", reason="Preview is waiting for visible-screen confirmation or host timeout")
            except DisplayError as exc:
                self.journal.update(operation_id, state="failed", reason=str(exc)[:256])
                self._clear_local_preview_if(operation_id)
            except Exception as exc:
                if dispatched:
                    try:
                        restore = self._send_local_restore(preview, operation_id)
                        self.journal.update(operation_id, state="failed", restore_state="succeeded", restored=True, resolved_by=restore["matched_by"], reason=f"screen preview was not confirmed; baseline restored: {str(exc)[:140]}")
                        self._clear_local_preview_if(operation_id)
                    except Exception as restore_error:
                        self.store.mutate(lambda state: state.setdefault("local_display_preview", {}).update({"restore_state": "unknown", "restore_started": False}))
                        self.journal.update(operation_id, state="unknown", restore_state="unknown", reason=f"screen selection/readback failed; baseline restoration is unknown: {str(restore_error)[:180]}")
                else:
                    self.journal.update(operation_id, state="failed", reason=str(exc)[:256])
                    self._clear_local_preview_if(operation_id)

    def local_display_confirm(self, preview_id: str) -> dict[str, Any]:
        preview_id = self._local_output_key(preview_id)
        with self._mutation_lock:
            preview = self.store.get("local_display_preview")
            operation = self.journal.get(preview_id)
            if not preview or preview.get("preview_id") != preview_id:
                if operation and operation.get("kind") == "display.selection.preview" and operation.get("state") == "succeeded":
                    return {"protocol_version": 1, "operation": operation, "preview": None}
                raise ApiError("screen preview is unavailable or expired", 409, "preview_not_found")
            if preview.get("restore_started"):
                raise ApiError("screen preview restoration is already in progress", 409, "preview_expired")
            if preview.get("restore_state") in {"failed", "unknown"}:
                raise ApiError("screen preview restoration needs to be retried or inspected before it can be kept", 409, "restore_not_confirmed")
            if preview.get("deadline") is None:
                raise ApiError("screen preview is still being applied", 409, "preview_not_ready")
            if float(preview.get("deadline", 0)) <= self.clock():
                raise ApiError("screen preview deadline has expired", 409, "preview_expired")
            latest, _ = self.bridge.snapshot(BRIDGE_SNAPSHOT_MAX_AGE)
            if not self._local_readback_matches(preview, latest, target=True):
                raise ApiError("active screen readback does not match the preview target", 409, "readback_not_confirmed")
            self.journal.update(preview_id, state="succeeded", outcome="active_screen_visible_and_confirmed", reason="Owner confirmed the selected Gaming Mode screen")
            operation = self.journal.get(preview_id) or operation
            self._clear_local_preview_if(preview_id)
            return {"protocol_version": 1, "operation": operation, "preview": None}

    def local_display_revert(self, preview_id: str) -> dict[str, Any]:
        preview_id = self._local_output_key(preview_id)
        with self._mutation_lock:
            preview = self.store.get("local_display_preview")
            original = self.journal.get(preview_id)
            if not preview or preview.get("preview_id") != preview_id:
                if original and original.get("kind") == "display.selection.preview" and original.get("restore_state") == "succeeded":
                    return {"protocol_version": 1, "operation": original, "preview": None}
                raise ApiError("screen preview is unavailable or expired", 409, "preview_not_found")
            restore_state = preview.get("restore_state")
            if preview.get("restore_started") and restore_state not in {"failed", "unknown"}:
                raise ApiError("screen preview restoration is already in progress", 409, "preview_expired")
            if restore_state in {"failed", "unknown"}:
                latest, _ = self.bridge.snapshot(BRIDGE_SNAPSHOT_MAX_AGE)
                if self._local_readback_matches(preview, latest, target=False):
                    self.journal.update(preview_id, state="failed", restore_state="succeeded", restored=True, resolved_by="active_output_and_mode", reason="baseline screen was already restored")
                    self._clear_local_preview_if(preview_id)
                    return {"protocol_version": 1, "operation": self.journal.get(preview_id) or original, "preview": None}
            if restore_state not in {"failed", "unknown"} and preview.get("deadline") is not None and float(preview.get("deadline", 0)) <= self.clock():
                raise ApiError("screen preview deadline has expired; host restoration is responsible", 409, "preview_expired")
            restore_operation = self.journal.internal("display.selection.revert", {"preview_id": preview_id}, owner=LOCAL_OPERATION_OWNER)
            self.store.mutate(lambda state: state.setdefault("local_display_preview", {}).update({
                "restore_started": True, "restore_state": "dispatched", "restore_operation_id": restore_operation["id"],
            }))
            self._spawn(restore_operation["id"], lambda: self._run_local_display_revert(preview_id, restore_operation["id"]))
            return {
                "protocol_version": 1,
                "operation": self.journal.get(restore_operation["id"]) or restore_operation,
                "preview": self._public_local_preview(self.store.get("local_display_preview")),
            }

    def _start_sunshine_restart(self, source: str) -> dict[str, Any]:
        """Queue one owner-controlled Sunshine recovery operation."""
        with self._mutation_lock:
            self._reject_if_conflicting("sunshine_restart")
            state = self.monitor.refresh_now()
            if not self.monitor.provider_ready():
                raise ApiError("Sunshine monitoring/provider is unavailable", 409, "provider_unavailable")
            if state["state"] != "stopped":
                raise ApiError("Sunshine is not freshly confirmed stopped", 409, "state_not_stopped")
            operation = self.journal.internal(
                "sunshine.restart",
                {"source": source},
                owner=LOCAL_OPERATION_OWNER,
            )
            self.monitor.set_operation(operation["id"])
            self._spawn(operation["id"], lambda: self._run_sunshine_restart(operation["id"]))
            return {"protocol_version": 1, "operation": operation}

    def local_sunshine_restart(self) -> dict[str, Any]:
        """Start stopped Sunshine through the Decky-owned provider.

        This is deliberately a local Decky RPC rather than a LAN route. The
        owner bridge must be connected and a fresh provider read must confirm
        that Sunshine is stopped before the owner receives the recovery
        command.
        """
        return self._start_sunshine_restart("local_decky")

    def _run_local_display_revert(self, preview_id: str, restore_operation_id: str) -> None:
        with self._mutation_lock:
            preview = self.store.get("local_display_preview")
            if not preview or preview.get("preview_id") != preview_id:
                self.journal.update(restore_operation_id, state="failed", reason="screen preview disappeared before restoration")
                return
            try:
                result = self._send_local_restore(preview, restore_operation_id)
                self.journal.update(restore_operation_id, state="succeeded", outcome="baseline_screen_restored", restored=True, resolved_by=result["matched_by"], target={"output_key": preview["baseline_output_key"]})
                self.journal.update(preview_id, state="failed", restore_state="succeeded", restored=True, resolved_by=result["matched_by"], reason="screen preview reverted by owner")
                self._clear_local_preview_if(preview_id)
            except DisplayError as exc:
                self.store.mutate(lambda state: state.setdefault("local_display_preview", {}).update({"restore_started": False, "restore_state": "failed"}))
                self.journal.update(restore_operation_id, state="failed", reason=str(exc))
                self.journal.update(preview_id, restore_state="failed", reason=str(exc))
            except Exception as exc:
                self.store.mutate(lambda state: state.setdefault("local_display_preview", {}).update({"restore_started": False, "restore_state": "unknown"}))
                self.journal.update(restore_operation_id, state="unknown", reason=f"baseline restoration result is unknown: {str(exc)[:180]}")
                self.journal.update(preview_id, state="unknown", restore_state="unknown", reason=f"baseline restoration result is unknown: {str(exc)[:180]}")

    def _local_readback_matches(self, preview: dict[str, Any], snapshot: dict[str, Any] | None, *, target: bool) -> bool:
        if not snapshot or snapshot.get("ready") is not True:
            return False
        if snapshot.get("generation") != preview.get("generation"):
            return False
        active_key, active_state = resolve_active_output(snapshot.get("outputs", []), snapshot.get("active_output_key"))
        expected_key = preview.get("target_output_key") if target else preview.get("baseline_output_key")
        expected_identity = preview.get("target_identity") if target else preview.get("baseline_identity")
        expected_mode = preview.get("target_mode") if target else preview.get("baseline_mode")
        if active_state != "known" or active_key != expected_key:
            return False
        output = self._local_find_output(snapshot, expected_key)
        if not output or output.get("connected") is not True or not same_selection_identity(expected_identity, selection_identity(output)):
            return False
        return same_mode_profile(expected_mode, self._local_current_mode(output))

    def _send_local_restore(self, preview: dict[str, Any], operation_id: str) -> dict[str, Any]:
        snapshot, _ = self.bridge.snapshot(BRIDGE_SNAPSHOT_MAX_AGE)
        if snapshot is None or snapshot.get("ready") is not True:
            raise DisplayError("Steam display bridge is unavailable; baseline restore was not sent")
        if snapshot.get("generation") != preview.get("generation"):
            raise DisplayError("display topology changed; baseline restore was not sent")
        selection = self._local_selection_capability(snapshot)
        if not selection["can_switch_live"]:
            raise DisplayError("verified screen-selection adapter is unavailable; baseline restore was not sent")
        baseline = self._local_find_output(snapshot, preview.get("baseline_output_key"))
        if not baseline or baseline.get("connected") is not True:
            raise DisplayError("baseline screen disappeared; restore was not sent")
        if not same_selection_identity(preview.get("baseline_identity"), selection_identity(baseline)):
            raise DisplayError("baseline screen identity changed; restore was not sent")
        result = self.bridge.request("select_output", {
            "output_key": baseline["output_key"], "generation": snapshot["generation"],
        }, operation_id, timeout=8)
        if not _result_ok(result):
            raise BridgeError(str(result.get("reason", "Steam rejected baseline screen restore")))
        deadline = self.monotonic() + 5.0
        while self.monotonic() < deadline:
            latest, _ = self.bridge.snapshot(BRIDGE_SNAPSHOT_MAX_AGE)
            if self._local_readback_matches(preview, latest, target=False):
                return {"matched_by": "active_output_and_mode"}
            time.sleep(0.05)
        raise BridgeError("baseline screen restore was sent but active-output readback did not confirm it")

    def _restore_expired_local_preview(self, preview: dict[str, Any]) -> None:
        with self._mutation_lock:
            current = self.store.get("local_display_preview")
            if not current or current.get("preview_id") != preview.get("preview_id") or current.get("restore_started"):
                return
            operation_id = current.get("operation_id")
            if not operation_id:
                self._clear_local_preview_if(preview.get("preview_id"))
                return
            self.store.mutate(lambda state: state.setdefault("local_display_preview", {}).update({
                "restore_started": True, "restore_state": "dispatched", "restore_operation_id": operation_id,
            }))
            try:
                result = self._send_local_restore(current, operation_id)
                self.journal.update(operation_id, state="failed", restore_state="succeeded", restored=True, resolved_by=result["matched_by"], reason="host preview deadline expired; baseline screen restored")
                self._clear_local_preview_if(preview["preview_id"])
            except DisplayError as exc:
                self.store.mutate(lambda state: state.setdefault("local_display_preview", {}).update({"restore_started": False, "restore_state": "failed"}))
                self.journal.update(operation_id, state="unknown", restore_state="failed", reason=f"host preview deadline expired; baseline restore was not sent: {str(exc)[:180]}")
            except Exception as exc:
                self.store.mutate(lambda state: state.setdefault("local_display_preview", {}).update({"restore_started": False, "restore_state": "unknown"}))
                self.journal.update(operation_id, state="unknown", restore_state="unknown", reason=f"host preview deadline expired; baseline restore result is unknown: {str(exc)[:180]}")

    def _reconcile_local_display_preview(self) -> None:
        preview = self.store.get("local_display_preview")
        if not isinstance(preview, dict) or not preview.get("preview_id"):
            return
        operation = self.journal.get(preview["operation_id"])
        if operation and operation.get("state") in {"accepted", "dispatched", "unknown"}:
            snapshot, _ = self.bridge.snapshot(BRIDGE_SNAPSHOT_MAX_AGE)
            if self._local_readback_matches(preview, snapshot, target=True):
                if preview.get("deadline") is None:
                    self.store.mutate(lambda state: state.setdefault("local_display_preview", {}).update({"deadline": self.clock() + LOCAL_DISPLAY_PREVIEW_SECONDS}))
                self.journal.update(preview["operation_id"], state="observed_return", outcome="target_active_reconciled_after_reload", reason="Active screen readback reconciled after plugin reload")
            elif self._local_readback_matches(preview, snapshot, target=False):
                self.journal.update(preview["operation_id"], state="failed", reason="screen preview did not remain active across plugin reload")
                self._clear_local_preview_if(preview["preview_id"])

    def _clear_local_preview_if(self, preview_id: str | None) -> None:
        if not preview_id:
            return
        self.store.mutate(lambda state: state.__setitem__("local_display_preview", None) if state.get("local_display_preview", {}).get("preview_id") == preview_id else None)

    # ---- operations ----------------------------------------------------

    def _display_order_target(self, output_keys: Any, generation: Any, restart: Any) -> dict[str, Any]:
        if not isinstance(output_keys, list) or not 1 <= len(output_keys) <= 16:
            raise ApiError("output order must contain 1 to 16 screens", 400, "invalid_display_target")
        if type(restart) is not bool:
            raise ApiError("restart must be a boolean", 400, "invalid_request")
        generation = self._local_generation(generation)
        validated_keys = [self._local_output_key(value) for value in output_keys]
        if len(set(validated_keys)) != len(validated_keys):
            raise ApiError("output order contains duplicate screens", 400, "invalid_display_target")

        capability = self._local_gamescope_output_capability()
        if not capability["available"]:
            raise ApiError(capability["reason"], 409, "gamescope_output_unsupported")
        inventory = self._drm_inventory.snapshot()
        if inventory is None:
            raise ApiError("the screen inventory is unavailable", 409, "stale_display_target")
        if inventory.get("generation") != generation:
            raise ApiError("display inventory changed; refresh the order", 409, "stale_display_inventory")

        connected_outputs = [
            output
            for output in inventory.get("outputs", [])
            if isinstance(output, dict) and output.get("connected") is True
        ]
        connectors: list[str] = []
        for output_key in validated_keys:
            target = self._local_find_output(inventory, output_key)
            if target is None or target.get("connected") is not True:
                raise ApiError("a selected screen is no longer connected", 409, "stale_display_target")
            connector = target.get("connector")
            if not isinstance(connector, str):
                raise ApiError("a selected screen has no usable connector", 409, "invalid_display_target")
            if sum(1 for output in connected_outputs if output.get("connector") == connector) != 1:
                raise ApiError("a selected connector identity is ambiguous", 409, "ambiguous_display_identity")
            connectors.append(connector)
        if len(set(connectors)) != len(connectors):
            raise ApiError("output order contains duplicate connectors", 409, "ambiguous_display_identity")
        return {
            "output_keys": validated_keys,
            "connectors": connectors,
            "generation": generation,
            "restart": restart,
        }

    def _display_order(self, client: dict[str, Any], body: dict[str, Any]) -> tuple[int, dict[str, str], dict[str, Any]]:
        existing = self.journal.lookup(client["client_id"], request_id(body), body)
        if existing is not None:
            return 202, {}, {"protocol_version": 1, "operation": existing}
        with self._mutation_lock:
            existing = self.journal.lookup(client["client_id"], request_id(body), body)
            if existing is not None:
                return 202, {}, {"protocol_version": 1, "operation": existing}
            self._reject_if_conflicting("display_preference")
            target = self._display_order_target(body["output_keys"], body["generation"], body["restart"])
            created, operation = self.journal.begin(client["client_id"], request_id(body), "display.order", body)
            if not created:
                return 202, {}, {"protocol_version": 1, "operation": operation}
            operation = self.journal.update(operation["id"], target=target)
            try:
                result = self.gamescope.apply_order(target["connectors"])
            except GamescopeError as exc:
                self.journal.update(operation["id"], state="failed", reason=str(exc)[:256])
                raise ApiError(str(exc), 409, "gamescope_output_update_failed") from exc

            if target["restart"]:
                try:
                    restart_result = self.gamescope.restart_session()
                except GamescopeError as exc:
                    reason = f"Output order was saved, but Gaming Mode could not restart: {exc}"
                    operation = self.journal.update(
                        operation["id"],
                        state="failed",
                        outcome="gamescope_output_configured_restart_failed",
                        reason=reason[:256],
                    )
                    return 202, {}, {"protocol_version": 1, "operation": operation}
                result = {**result, "restart": restart_result}

            operation = self.journal.update(
                operation["id"],
                state="succeeded",
                outcome="gamescope_output_configured_and_restart_requested" if target["restart"] else "gamescope_output_configured",
                reason=(
                    "Output order saved; Gaming Mode restart requested."
                    if target["restart"]
                    else "Output order saved for the next Gaming Mode session."
                ),
            )
            return 202, {}, {"protocol_version": 1, "operation": operation}

    def _display_order_reset(self, client: dict[str, Any], body: dict[str, Any]) -> tuple[int, dict[str, str], dict[str, Any]]:
        existing = self.journal.lookup(client["client_id"], request_id(body), body)
        if existing is not None:
            return 202, {}, {"protocol_version": 1, "operation": existing}
        with self._mutation_lock:
            existing = self.journal.lookup(client["client_id"], request_id(body), body)
            if existing is not None:
                return 202, {}, {"protocol_version": 1, "operation": existing}
            self._reject_if_conflicting("display_preference")
            created, operation = self.journal.begin(client["client_id"], request_id(body), "display.order.reset", body)
            if not created:
                return 202, {}, {"protocol_version": 1, "operation": operation}
            operation = self.journal.update(operation["id"], target={"automatic": True})
            try:
                self.gamescope.clear()
            except GamescopeError as exc:
                self.journal.update(operation["id"], state="failed", reason=str(exc)[:256])
                raise ApiError(str(exc), 409, "gamescope_output_update_failed") from exc
            operation = self.journal.update(
                operation["id"],
                state="succeeded",
                outcome="gamescope_output_cleared",
                reason="Automatic display order restored for the next Gaming Mode session.",
            )
            return 202, {}, {"protocol_version": 1, "operation": operation}


    def _power(self, client: dict[str, Any], body: dict[str, Any]) -> tuple[int, dict[str, str], dict[str, Any]]:
        action = body["action"]
        existing = self.journal.lookup(client["client_id"], request_id(body), body)
        if existing is not None:
            return 202, {}, {"protocol_version": 1, "operation": existing}
        with self._mutation_lock:
            snapshot, _ = self.bridge.snapshot()
            if snapshot is None or not snapshot.get("methods", {}).get(action):
                raise ApiError(f"Steam {action} bridge is unavailable", 503, "bridge_unavailable")
            self._reject_if_conflicting("power")
            created, operation = self.journal.begin(client["client_id"], request_id(body), f"power.{action}", body)
            if created:
                self._spawn(operation["id"], lambda: self._run_power(operation["id"], action))
            return 202, {}, {"protocol_version": 1, "operation": operation}

    def _run_power(self, operation_id: str, action: str) -> None:
        with self._mutation_lock:
            self.journal.update(operation_id, state="dispatched", target={"action": action})
            try:
                result = self.bridge.request("power", {"action": action}, operation_id, timeout=8)
                if not _result_ok(result):
                    self.journal.update(operation_id, state="failed", reason=str(result.get("reason", "Steam rejected the power request")))
                    return
                self.journal.update(operation_id, state="observed_return", outcome="method_returned", reason=f"Steam returned; physical {action} transition is not confirmed by this response")
            except Exception as exc:
                self.journal.update(operation_id, state="unknown", reason=f"power result is unknown: {str(exc)[:180]}")

    def _display_preview(self, client: dict[str, Any], body: dict[str, Any]) -> tuple[int, dict[str, str], dict[str, Any]]:
        existing = self.journal.lookup(client["client_id"], request_id(body), body)
        if existing is not None:
            return 202, {}, {
                "protocol_version": 1,
                "operation": existing,
                "preview": self._public_preview(self.store.get("preview")),
            }
        with self._mutation_lock:
            snapshot, _ = self.bridge.snapshot()
            if snapshot is None or not snapshot.get("methods", {}).get("display"):
                raise ApiError("Steam display bridge is unavailable", 503, "bridge_unavailable")
            self._reject_if_conflicting("display_preview")
            output = self._find_output(snapshot, body["output_id"])
            if output is None:
                raise ApiError("output is no longer advertised", 409, "stale_target")
            if output["generation"] != body["generation"]:
                raise ApiError("display target generation is stale", 409, "stale_target")
            mode = self._find_mode(output, body["mode_id"])
            baseline = self._find_mode(output, output.get("current_mode_id"))
            if mode is None or baseline is None:
                raise ApiError("selected display mode is unavailable", 409, "stale_target")
            if mode["id"] == baseline["id"]:
                raise ApiError("selected mode is already current", 409, "no_change")
            operation_body = dict(body)
            created, operation = self.journal.begin(client["client_id"], request_id(body), "display.preview", operation_body)
            preview_id = operation["id"]
            if created:
                deadline = self.clock() + 15
                preview = {
                    "preview_id": preview_id,
                    "operation_id": preview_id,
                    "owner_client_id": client["client_id"],
                    "output_identity": display_identity(output),
                    "output_id": output["id"],
                    "generation": output["generation"],
                    "baseline_mode": baseline,
                    "target_mode": mode,
                    "deadline": deadline,
                    "restore_started": False,
                    "restore_state": None,
                    "created_at": _utc_now(),
                }
                self.store.mutate(lambda state: state.__setitem__("preview", preview))
                self.journal.update(preview_id, preview_id=preview_id, target={"output_id": output["id"], "mode_id": mode["id"], "generation": output["generation"]})
                operation = self.journal.get(preview_id, client["client_id"]) or operation
                self._spawn(preview_id, lambda: self._run_preview(preview_id))
            operation["preview_id"] = preview_id
            return 202, {}, {"protocol_version": 1, "operation": operation, "preview": self._public_preview(self.store.get("preview"))}

    def _run_preview(self, operation_id: str) -> None:
        with self._mutation_lock:
            preview = self.store.get("preview")
            if not preview or preview.get("preview_id") != operation_id:
                self.journal.update(operation_id, state="failed", reason="preview intent disappeared before dispatch")
                return
            try:
                snapshot, _ = self.bridge.snapshot()
                output = self._find_output(snapshot, preview["output_id"]) if snapshot else None
                if not snapshot or not same_display_identity(preview["output_identity"], output) or output.get("generation") != preview["generation"]:
                    raise DisplayError("display identity or generation changed; preview not sent")
                mode = self._find_mode(output, preview["target_mode"]["id"])
                if mode is None:
                    raise DisplayError("selected mode disappeared; preview not sent")
                self.journal.update(operation_id, state="dispatched")
                result = self.bridge.request("set_mode", {
                    "output_id": output["id"], "mode_id": mode["id"], "generation": output["generation"], "rgb_range": output.get("rgb_range", 0),
                }, operation_id, timeout=8)
                if not _result_ok(result):
                    raise BridgeError(str(result.get("reason", "Steam rejected display mode")))
                readback = result.get("snapshot")
                if isinstance(readback, dict):
                    def remember_readback(state: dict[str, Any]) -> None:
                        active = state.get("preview")
                        if isinstance(active, dict) and active.get("preview_id") == operation_id:
                            active["readback"] = readback
                            active["readback_at"] = _utc_now()
                    self.store.mutate(remember_readback)
                self.journal.update(operation_id, state="observed_return", outcome="mode_request_returned", reason="Preview is waiting for visible-picture confirmation or host timeout")
            except Exception as exc:
                self.journal.update(operation_id, state="failed", reason=str(exc)[:256])
                self._clear_preview_if(operation_id)

    def _display_confirm(self, client: dict[str, Any], body: dict[str, Any]) -> tuple[int, dict[str, str], dict[str, Any]]:
        existing = self.journal.lookup(client["client_id"], request_id(body), body)
        if existing is not None:
            return 202, {}, {
                "protocol_version": 1,
                "operation": existing,
                "profile": self._public_profile(self._latest_profile()),
            }
        with self._mutation_lock:
            preview = self.store.get("preview")
            if not preview or preview.get("preview_id") != body["preview_id"]:
                raise ApiError("preview is unavailable or expired", 409, "preview_not_found")
            if preview.get("owner_client_id") != client["client_id"]:
                raise ApiError("only the preview owner can confirm it", 403, "forbidden")
            if float(preview.get("deadline", 0)) <= self.clock():
                raise ApiError("preview deadline has expired", 409, "preview_expired")
            # Applying a mode may make Steam re-enumerate the same output and
            # assign new mode IDs. The target was generation-checked before
            # dispatch; confirmation relies on the stable output identity and
            # semantic mode readback instead of rejecting that expected churn.
            # Some Steam builds publish the new mode a short time after the
            # bridge command returns, so keep Save boundedly retryable rather
            # than making the user race that publication window.
            matched = False
            output = None
            current = None
            retry_deadline = min(
                self.monotonic() + 3.0,
                self.monotonic() + max(0.0, float(preview.get("deadline", 0)) - self.clock()),
            )
            while True:
                snapshot, _ = self.bridge.snapshot()
                output = self._find_output(snapshot, preview["output_id"]) if snapshot else None
                current = self._find_mode(output, output.get("current_mode_id")) if output else None
                if snapshot and same_display_identity(preview["output_identity"], output) and same_mode_profile(preview["target_mode"], current):
                    matched = True
                    break
                # Use the command-scoped readback as a fallback when it already
                # confirms the target, while still preferring a fresh snapshot.
                readback = preview.get("readback")
                readback_output = self._find_output(readback, preview["output_id"]) if isinstance(readback, dict) else None
                readback_mode = self._find_mode(readback_output, readback_output.get("current_mode_id")) if readback_output else None
                if readback and same_display_identity(preview["output_identity"], readback_output) and same_mode_profile(preview["target_mode"], readback_mode):
                    output = readback_output
                    current = readback_mode
                    matched = True
                    break
                if self.monotonic() >= retry_deadline:
                    break
                time.sleep(0.05)
            if not matched:
                raise ApiError("current Steam readback does not match the preview target", 409, "readback_not_confirmed")
            created, operation = self.journal.begin(client["client_id"], request_id(body), "display.confirm", body)
            if created:
                profile_id = opaque_id("profile-")
                profile = {
                    "id": profile_id,
                    "output_identity": preview["output_identity"],
                    "output_id": preview["output_id"],
                    "mode": current,
                    "verified_at": _utc_now(),
                    "verified_by": client["client_id"],
                }
                self.store.mutate(lambda state: (state.setdefault("profiles", {}).__setitem__(profile_id, profile), state.__setitem__("preview", None)))
                self.journal.update(operation["id"], state="succeeded", outcome="visible_picture_confirmed", target={"profile_id": profile_id})
                operation = self.journal.get(operation["id"], client["client_id"]) or operation
            return 202, {}, {"protocol_version": 1, "operation": operation, "profile": self._public_profile(self._latest_profile())}

    def _display_restore(self, client: dict[str, Any], body: dict[str, Any]) -> tuple[int, dict[str, str], dict[str, Any]]:
        existing = self.journal.lookup(client["client_id"], request_id(body), body)
        if existing is not None:
            return 202, {}, {"protocol_version": 1, "operation": existing}
        with self._mutation_lock:
            self._reject_if_conflicting("display_restore")
            preview = self.store.get("preview")
            source = body.get("source", "verified")
            if source == "preview":
                if not preview or preview.get("owner_client_id") != client["client_id"]:
                    raise ApiError("there is no restorable preview owned by this client", 409, "preview_not_found")
                if preview.get("restore_started") or float(preview.get("deadline", 0)) <= self.clock():
                    raise ApiError("preview deadline has expired; host restore is already responsible", 409, "preview_expired")
                target = {"kind": "preview", "preview_id": preview["preview_id"]}
            else:
                profile = self._profile_for_restore(body.get("profile_id"))
                target = {"kind": "verified", "profile_id": profile["id"]}
            created, operation = self.journal.begin(client["client_id"], request_id(body), "display.restore", body)
            if created:
                self._spawn(operation["id"], lambda: self._run_restore_operation(operation["id"], target))
            return 202, {}, {"protocol_version": 1, "operation": operation}

    def _display_save_current(self, client: dict[str, Any], body: dict[str, Any]) -> tuple[int, dict[str, str], dict[str, Any]]:
        """Persist the currently displayed mode after an owner assertion.

        A readback by itself is not enough to designate a recovery profile.
        ``visible=true`` is the explicit statement from the owner that the
        physical picture is good.  The exact output and generation are still
        checked against the live bridge before the profile is written.
        """
        existing = self.journal.lookup(client["client_id"], request_id(body), body)
        if existing is not None:
            target = existing.get("target") if isinstance(existing.get("target"), dict) else {}
            profiles = self.store.get("profiles", {})
            profile = profiles.get(target.get("profile_id")) if isinstance(profiles, dict) else None
            return 202, {}, {"protocol_version": 1, "operation": existing, "profile": self._public_profile(profile or self._latest_profile())}
        with self._mutation_lock:
            if body.get("visible") is not True:
                raise ApiError("visible must be true to save the current display mode", 400, "owner_confirmation_required")
            self._reject_if_conflicting("display_save_current")
            if self.store.get("preview") is not None:
                raise ApiError("a display preview is active; keep or revert it first", 409, "mutation_conflict")
            snapshot, _ = self.bridge.snapshot()
            if snapshot is None or not snapshot.get("methods", {}).get("display"):
                raise ApiError("Steam display bridge is unavailable", 503, "bridge_unavailable")
            output = self._find_output(snapshot, body["output_id"])
            if output is None:
                raise ApiError("output is no longer advertised", 409, "stale_target")
            if output.get("generation") != body["generation"]:
                raise ApiError("display target generation is stale", 409, "stale_target")
            current = self._find_mode(output, output.get("current_mode_id"))
            if current is None:
                raise ApiError("current display mode could not be read back", 409, "readback_not_confirmed")
            created, operation = self.journal.begin(client["client_id"], request_id(body), "display.save-current", body)
            if created:
                profile_id = opaque_id("profile-")
                profile = {
                    "id": profile_id,
                    "output_identity": display_identity(output),
                    "output_id": output["id"],
                    "mode": current,
                    "verified_at": _utc_now(),
                    "verified_by": client["client_id"],
                }
                self.store.mutate(lambda state: state.setdefault("profiles", {}).__setitem__(profile_id, profile))
                self.journal.update(operation["id"], state="succeeded", outcome="current_mode_visible_and_read_back", target={"profile_id": profile_id})
                operation = self.journal.get(operation["id"], client["client_id"]) or operation
            return 202, {}, {"protocol_version": 1, "operation": operation, "profile": self._public_profile(self._latest_profile())}

    def _run_restore_operation(self, operation_id: str, target: dict[str, Any]) -> None:
        with self._mutation_lock:
            preview = self.store.get("preview") if target["kind"] == "preview" else None
            if target["kind"] == "preview":
                baseline = preview.get("baseline_mode") if preview else None
                identity = preview.get("output_identity") if preview else None
                output_id = preview.get("output_id") if preview else None
            else:
                profile = self.store.get("profiles", {}).get(target["profile_id"])
                baseline = profile.get("mode") if profile else None
                identity = profile.get("output_identity") if profile else None
                output_id = profile.get("output_id") if profile else None
            try:
                if not baseline or not identity:
                    raise DisplayError("restore profile is unavailable")
                self.journal.update(operation_id, state="dispatched", target={"output_id": output_id, "mode": mode_profile(baseline)})
                result = self._send_restore(output_id, identity, baseline, operation_id)
                self.journal.update(operation_id, state="succeeded", outcome="restored_and_read_back", resolved_by=result["matched_by"], restored=True)
                if target["kind"] == "preview":
                    self._clear_preview_if(preview["preview_id"])
            except DisplayError as exc:
                self.journal.update(operation_id, state="failed", reason=str(exc))
            except Exception as exc:
                self.journal.update(operation_id, state="unknown", reason=f"restore result is unknown: {str(exc)[:180]}")

    def _send_restore(self, output_id: str, identity: dict[str, Any], baseline: dict[str, Any], operation_id: str) -> dict[str, Any]:
        snapshot, _ = self.bridge.snapshot()
        output = self._find_output(snapshot, output_id) if snapshot else None
        if not snapshot or not same_display_identity(identity, output):
            raise DisplayError("display identity changed or disappeared; restore not sent")
        mode, matched_by = resolve_restore_mode(output, baseline)
        result = self.bridge.request("set_mode", {
            "output_id": output["id"], "mode_id": mode["id"], "generation": output["generation"], "rgb_range": output.get("rgb_range", 0),
        }, operation_id, timeout=8)
        if not _result_ok(result):
            raise BridgeError(str(result.get("reason", "Steam rejected display restore")))
        deadline = self.monotonic() + 5
        while self.monotonic() < deadline:
            latest, _ = self.bridge.snapshot()
            latest_output = self._find_output(latest, output_id) if latest else None
            latest_mode = self._find_mode(latest_output, latest_output.get("current_mode_id")) if latest_output else None
            if latest_output and same_display_identity(identity, latest_output) and same_mode_profile(baseline, latest_mode):
                return {"matched_by": matched_by}
            time.sleep(0.05)
        raise BridgeError("restore was sent but readback did not confirm the working mode")

    def _sunshine_restart(self, client: dict[str, Any], body: dict[str, Any]) -> tuple[int, dict[str, str], dict[str, Any]]:
        existing = self.journal.lookup(client["client_id"], request_id(body), body)
        if existing is not None:
            return 202, {}, {"protocol_version": 1, "operation": existing}
        with self._mutation_lock:
            self._reject_if_conflicting("sunshine_restart")
            state = self.monitor.refresh_now()
            if not self.monitor.provider_ready():
                raise ApiError("Sunshine monitoring/provider is unavailable", 409, "provider_unavailable")
            if state["state"] != "stopped":
                raise ApiError("Sunshine is not freshly confirmed stopped", 409, "state_not_stopped")
            created, operation = self.journal.begin(client["client_id"], request_id(body), "sunshine.restart", body)
            if created:
                self.monitor.set_operation(operation["id"])
                self._spawn(operation["id"], lambda: self._run_sunshine_restart(operation["id"]))
            return 202, {}, {"protocol_version": 1, "operation": operation}

    def _run_sunshine_restart(self, operation_id: str) -> None:
        with self._mutation_lock:
            try:
                current = self.monitor.refresh_now(include_operation=False)
                if not self.monitor.is_enabled() or not self.monitor.provider():
                    self.journal.update(operation_id, state="failed", reason="Monitor Sunshine was disabled before dispatch")
                    return
                if current["state"] == "running":
                    self.journal.update(operation_id, state="succeeded", outcome="already_running")
                    return
                if current["state"] != "stopped":
                    self.journal.update(operation_id, state="failed", reason="live provider state was not stopped before dispatch")
                    return
                queued = self.journal.get(operation_id)
                if not queued or queued.get("state") != "accepted":
                    return
                provider = self.monitor.provider()
                if not self.monitor.is_enabled() or provider is None:
                    self.journal.update(operation_id, state="unknown", reason="provider became unavailable before dispatch")
                    return
                self.journal.update(operation_id, state="dispatched")
                result = self.monitor._call(provider.ensure_running, timeout=self.monitor.RECOVERY_TIMEOUT)
                if isinstance(result, dict) and result.get("outcome") == "already_running":
                    self.journal.update(operation_id, state="succeeded", outcome="already_running")
                    return
                deadline = self.monotonic() + 30
                while self.monotonic() < deadline:
                    if not self.monitor.is_enabled():
                        self.journal.update(operation_id, state="unknown", reason="monitor disabled after recovery dispatch; no further process command issued")
                        return
                    observed = self.monitor.refresh_now(include_operation=False)
                    if observed["state"] == "running":
                        self.journal.update(operation_id, state="succeeded", outcome="running")
                        return
                    if observed["state"] == "unavailable":
                        break
                    time.sleep(0.5)
                self.journal.update(operation_id, state="unknown", reason="provider did not confirm running before the bounded recovery deadline")
            except Exception as exc:
                self.journal.update(operation_id, state="failed", reason=str(exc)[:256])
            finally:
                self.monitor.set_operation(None)

    def _revoke_self(self, client: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
        existing = self.journal.lookup(client["client_id"], request_id(body), body)
        if existing is not None:
            return existing
        with self._mutation_lock:
            if client["client_id"] not in self.store.get("clients", {}):
                raise ApiError("credential has already been revoked", 401, "unauthorized")
            created, operation = self.journal.begin(client["client_id"], request_id(body), "pair.revoke-self", body)
            if created:
                self.store.mutate(lambda state: state.setdefault("clients", {}).pop(client["client_id"], None))
                self.journal.update(operation["id"], state="succeeded", outcome="revoked")
                self.journal.cancel_for_client(client["client_id"], "sunshine.", "cancelled because the credential was revoked")
                operation = self.journal.get(operation["id"], client["client_id"]) or operation
            return operation

    def _reject_if_conflicting(self, kind: str, ignore_id: str | None = None) -> None:
        local_preview = self.store.get("local_display_preview")
        if local_preview is not None and kind not in {"local_display_confirm", "local_display_revert"}:
            if kind == "local_display_preview":
                raise ApiError("a Gaming Mode screen preview is already active", 409, "mutation_conflict")
            raise ApiError("a Gaming Mode screen preview owns the mutation lane", 409, "mutation_conflict")
        if self.store.get("preview") is not None and kind == "display_preview":
            raise ApiError("a display preview is already active", 409, "mutation_conflict")
        if self.store.get("preview") is not None and kind not in {"display_restore"}:
            raise ApiError("a display preview owns the mutation lane", 409, "mutation_conflict")
        prefixes = {
            "power": ("power.", "display.", "sunshine."),
            "display_preview": ("power.", "display.", "sunshine."),
            "display_restore": ("power.", "display."),
            "display_preference": ("power.", "display.", "sunshine."),
            "sunshine_restart": ("power.", "display.", "sunshine."),
        }.get(kind, ())
        for operation in self.journal.active():
            if ignore_id is not None and operation.get("id") == ignore_id:
                continue
            if operation.get("kind", "").startswith(prefixes):
                raise ApiError("another mutation is still in progress", 409, "mutation_conflict")

    def _spawn(self, operation_id: str, function: Callable[[], None]) -> None:
        def run() -> None:
            try:
                function()
            finally:
                self._workers.discard(thread)

        thread = threading.Thread(target=run, name=f"steamos-companion-{operation_id}", daemon=True)
        self._workers.add(thread)
        thread.start()

    def _enforce_rate_limit(self, key: str, limit: int, window: float) -> None:
        now = self.monotonic()
        with self._rate_lock:
            started, count = self._rate_buckets.get(key, (now, 0))
            if now - started >= window:
                started, count = now, 0
            if count >= limit:
                raise ApiError("request rate limit exceeded", 429, "rate_limited")
            self._rate_buckets[key] = (started, count + 1)
            if len(self._rate_buckets) > 512:
                cutoff = now - window
                self._rate_buckets = {
                    name: bucket for name, bucket in self._rate_buckets.items() if bucket[0] >= cutoff
                }

    # ---- preview watchdog and profile helpers ------------------------

    def _start_preview_watchdog(self) -> None:
        if self._preview_thread and self._preview_thread.is_alive():
            return
        self._preview_thread = threading.Thread(target=self._preview_watchdog, name="steamos-companion-preview", daemon=True)
        self._preview_thread.start()

    def _preview_watchdog(self) -> None:
        while not self._preview_stop.wait(0.25):
            preview = self.store.get("preview")
            if preview and not preview.get("restore_started") and float(preview.get("deadline", 0)) <= self.clock():
                self._restore_expired_preview(preview)
            local_preview = self.store.get("local_display_preview")
            if local_preview and not local_preview.get("restore_started") and local_preview.get("restore_state") not in {"failed", "unknown"} and local_preview.get("deadline") is not None and float(local_preview.get("deadline", 0)) <= self.clock():
                self._restore_expired_local_preview(local_preview)

    def _restore_expired_preview(self, preview: dict[str, Any]) -> None:
        with self._mutation_lock:
            current = self.store.get("preview")
            if not current or current.get("preview_id") != preview.get("preview_id") or current.get("restore_started"):
                return
            self.store.mutate(lambda state: state.setdefault("preview", {}).update({"restore_started": True, "restore_state": "dispatched"}))
            try:
                result = self._send_restore(preview["output_id"], preview["output_identity"], preview["baseline_mode"], preview["operation_id"])
                self.journal.update(preview["operation_id"], restore_state="succeeded", restored=True, resolved_by=result["matched_by"], reason="host preview deadline expired; original mode restored")
                self._clear_preview_if(preview["preview_id"])
            except Exception as exc:
                self.store.mutate(lambda state: state.setdefault("preview", {}).update({"restore_state": "unknown"}))
                self.journal.update(preview["operation_id"], restore_state="unknown", reason=f"host preview deadline expired; restore result is unknown: {str(exc)[:160]}")

    def _clear_preview_if(self, preview_id: str) -> None:
        self.store.mutate(lambda state: state.__setitem__("preview", None) if state.get("preview", {}).get("preview_id") == preview_id else None)

    def _profile_for_restore(self, profile_id: Any) -> dict[str, Any]:
        profiles = self.store.get("profiles", {})
        if profile_id is not None:
            profile_id = identifier(profile_id, "profile_id")
            profile = profiles.get(profile_id)
        else:
            profile = max(profiles.values(), key=lambda item: item.get("verified_at", ""), default=None)
        if not profile:
            raise ApiError("no owner-verified recovery profile is available", 409, "profile_not_found")
        return profile

    def _latest_profile(self) -> dict[str, Any] | None:
        return self._profile_for_restore(None) if self.store.get("profiles", {}) else None

    @staticmethod
    def _wake_target() -> dict[str, Any]:
        return _active_wake_target()

    # ---- small data helpers -------------------------------------------

    def _find_output(self, snapshot: dict[str, Any] | None, output_id: Any) -> dict[str, Any] | None:
        if not snapshot:
            return None
        return next((output for output in snapshot.get("outputs", []) if output.get("id") == str(output_id)), None)

    @staticmethod
    def _find_mode(output: dict[str, Any] | None, mode_id: Any) -> dict[str, Any] | None:
        if not output:
            return None
        return next((mode for mode in output.get("modes", []) if mode.get("id") == str(mode_id)), None)

    @staticmethod
    def _set_pairing_status(state: dict[str, Any], pairing_id: str, status: str) -> bool:
        pairing = state.setdefault("pairings", {}).get(pairing_id)
        if not pairing:
            return False
        pairing["status"] = status
        return True

    @staticmethod
    def _public_client(value: dict[str, Any]) -> dict[str, Any]:
        return {key: value.get(key) for key in ("client_id", "name", "scopes", "created_at", "last_seen")}

    @staticmethod
    def _public_pairing(value: dict[str, Any]) -> dict[str, Any]:
        requested = value.get("requested_by_client")
        return {
            "pairing_id": value.get("pairing_id"),
            "client_name": value.get("client_name"),
            "client_id": value.get("client_id"),
            "requested_scopes": requested if isinstance(requested, list) else value.get("requested_scopes", []),
            "allowed_scopes": value.get("requested_scopes", []),
            "granted_scopes": value.get("granted_scopes"),
            "created_at": value.get("created_at"),
            "expires_at": value.get("expires_at"),
            "status": value.get("status"),
            "pairing_method": value.get("pairing_method", "payload"),
            "verification_code": (
                value.get("verification_code")
                if value.get("pairing_method") == "verification"
                and value.get("status") in {"created", "pending"}
                else None
            ),
        }

    @staticmethod
    def _public_profile(value: dict[str, Any] | None) -> dict[str, Any] | None:
        if not value:
            return None
        return {key: value.get(key) for key in ("id", "output_identity", "output_id", "mode", "verified_at")}

    @staticmethod
    def _public_remote_output(value: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value.get(key)
            for key in ("id", "name", "description", "is_internal", "current_mode_id", "modes", "generation", "rgb_range")
        }

    @staticmethod
    def _public_preview(value: dict[str, Any] | None) -> dict[str, Any] | None:
        if not value:
            return None
        return {key: value.get(key) for key in ("preview_id", "output_identity", "output_id", "generation", "baseline_mode", "target_mode", "deadline", "restore_state", "created_at")}

    def _public_local_preview(self, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if not value:
            return None
        operation = self.journal.get(value.get("operation_id")) if value.get("operation_id") else None
        return {
            "preview_id": value.get("preview_id"),
            "state": operation.get("state") if operation else None,
            "target_output_key": value.get("target_output_key"),
            "baseline_output_key": value.get("baseline_output_key"),
            "generation": value.get("generation"),
            "deadline": value.get("deadline"),
            "restore_started": value.get("restore_started") is True,
            "restore_state": value.get("restore_state"),
            "created_at": value.get("created_at"),
        }

    def _latest_local_display_operation(self) -> dict[str, Any] | None:
        operations = self.store.get("operations", {})
        candidates = [
            operation for operation in operations.values()
            if isinstance(operation, dict) and (
                operation.get("kind", "").startswith("display.selection.")
                or operation.get("kind") == "display.preference"
            )
        ]
        if not candidates:
            return None
        latest = max(candidates, key=lambda operation: operation.get("updated_at", ""))
        return self.journal.public(latest)

    def _default_advertised_host(self) -> str:
        route_host = _route_selected_ipv4()
        return route_host or "127.0.0.1"

    def _resolve_pairing_host(self, settings: dict[str, Any]) -> str:
        configured = settings.get("advertised_host")
        return validate_host(configured) if configured else self._default_advertised_host()
