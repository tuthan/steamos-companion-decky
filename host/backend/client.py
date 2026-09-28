"""Client-side coordinator for a single pinned SteamOS Companion host.

The client is intentionally kept out of :mod:`service`.  Host identity,
incoming approvals, the Steam bridge, and the host operation journal remain in
their existing state file; this module owns only the outgoing role, one saved
remote credential, pending pairing state, and client-side reconciliation.
"""

from __future__ import annotations

import concurrent.futures
import copy
import hashlib
import json
import os
import re
import socket
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

from .client_core import (
    ClientError,
    ClientResponseError,
    IdentityMismatch,
    REQUEST_TIMEOUT,
    RemoteClient,
    active_route_ipv4,
    body_digest,
    discover_local_devices,
    new_verification_nonce,
    pairing_comparison_code,
    probe_device,
    send_wake_packet,
)
from .identity import opaque_id
from .protocol import ProtocolError, identifier, validate_host, validate_scopes
from .storage import StateStore


CLIENT_SCHEMA_VERSION = 2
DEFAULT_CLIENT_SCOPES = ["status.read", "power.control", "display.control"]
MAX_CLIENT_NAME = 96
MAX_ACTIONS = 64
MAX_DIAGNOSTIC_ACTIONS = 8
MAX_DISCOVERY_SCANS = 8
MAX_DISPLAY_ORDER_KEYS = 16
_DISPLAY_ORDER_OUTPUT_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:|/-]{0,127}$")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_text(value: Any, limit: int = 256) -> str:
    return str(value or "").replace("\x00", "")[:limit]


def _safe_name(value: Any) -> str:
    if not isinstance(value, str):
        raise ClientError("the client name is invalid", "invalid_client_name")
    value = " ".join(value.replace("\x00", "").split())
    if not value or len(value) > MAX_CLIENT_NAME:
        raise ClientError("the client name must be 1–96 characters", "invalid_client_name")
    return value


def default_client_name() -> str:
    try:
        name = _safe_name(socket.gethostname())
    except Exception:
        name = ""
    return name or "SteamOS handheld"


def _copy(value: Any) -> Any:
    return copy.deepcopy(value)


def _display_order_keys(value: Any) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_DISPLAY_ORDER_KEYS:
        raise ClientError("display order must contain 1 to 16 screens", "invalid_display_order")
    keys: list[str] = []
    for item in value:
        if not isinstance(item, str) or not _DISPLAY_ORDER_OUTPUT_KEY_RE.fullmatch(item):
            raise ClientError("display order contains an invalid screen", "invalid_display_order")
        keys.append(item)
    if len(set(keys)) != len(keys):
        raise ClientError("display order contains duplicate screens", "invalid_display_order")
    return keys


def _unsupported_display_order(reason: str) -> dict[str, Any]:
    return {
        "available": False,
        "generation": None,
        "observed_at": None,
        "output_keys": [],
        "outputs": [],
        "saved_output_keys": [],
        "restart_required": False,
        "restart_available": False,
        "adapter": None,
        "unsupported": True,
        "stale": False,
        "ambiguous": False,
        "previous_reading": False,
        "reason": _safe_text(reason),
    }


def _future_error(error: BaseException) -> concurrent.futures.Future:
    future: concurrent.futures.Future = concurrent.futures.Future()
    future.set_exception(error)
    return future


class Settlement:
    """Idempotent completion callback for queued work.

    A queue can report that work was rejected before a worker starts, while a
    worker can race to report an exception.  Both paths call this object; only
    the first one reaches the callback, so a UI busy flag cannot be left
    claimed or be cleared twice.
    """

    def __init__(self, callback: Callable[[Any], None] | None = None):
        self._callback = callback
        self._lock = threading.Lock()
        self._settled = False

    @property
    def settled(self) -> bool:
        with self._lock:
            return self._settled

    def settle(self, value: Any = None) -> bool:
        with self._lock:
            if self._settled:
                return False
            self._settled = True
            callback = self._callback
        if callback is not None:
            try:
                callback(value)
            except Exception:
                # Completion bookkeeping must never kill a worker.
                pass
        return True


def settle_once(callback: Callable[[Any], None] | None = None) -> Settlement:
    return Settlement(callback)


class BoundedWorkQueue:
    """Small worker pool with whole-request read deduplication.

    Reads are dropped when the pending bound is reached.  Mutations are never
    silently dropped: a rejected mutation calls its settlement callback before
    raising, which gives its owner a terminal path for its busy state.
    """

    def __init__(self, *, max_workers: int = 4, max_pending: int = 32, name: str = "steamos-companion-client"):
        self.max_workers = max(1, min(int(max_workers), 16))
        self.max_pending = max(1, min(int(max_pending), 256))
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix=name)
        self._lock = threading.RLock()
        self._pending: dict[str, concurrent.futures.Future] = {}
        self._stopped = False

    @staticmethod
    def _key(value: Any) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)

    def _submit(
        self,
        key: Any,
        function: Callable[[], Any],
        *,
        mutation: bool,
        on_settled: Callable[[Any], None] | None = None,
    ) -> concurrent.futures.Future:
        normalized_key = self._key(key)
        settlement = Settlement(on_settled)
        with self._lock:
            if self._stopped:
                error = ClientError("client work was stopped", "work_stopped")
                settlement.settle({"ok": False, "error": str(error)})
                return _future_error(error)
            if not mutation and normalized_key in self._pending:
                return self._pending[normalized_key]
            if len(self._pending) >= self.max_pending:
                error = ClientError(
                    "client work queue is full; background read was discarded" if not mutation else "client mutation could not be queued",
                    "work_queue_full",
                )
                # This callback is the mutation owner's terminal path.  The
                # returned Future also carries the same error to the caller.
                settlement.settle({"ok": False, "error": str(error)})
                return _future_error(error)
            try:
                future = self._executor.submit(function)
            except Exception as exc:
                error = ClientError("client work could not start", "work_not_started")
                settlement.settle({"ok": False, "error": str(error)})
                return _future_error(error)
            self._pending[normalized_key] = future

        def done(completed: concurrent.futures.Future) -> None:
            try:
                try:
                    value = completed.result()
                except Exception as exc:
                    value = {"ok": False, "error": _safe_text(exc)}
                settlement.settle(value)
            finally:
                with self._lock:
                    if self._pending.get(normalized_key) is completed:
                        self._pending.pop(normalized_key, None)

        future.add_done_callback(done)
        return future

    def submit_read(self, key: Any, function: Callable[[], Any]) -> concurrent.futures.Future:
        return self._submit(("read", key), function, mutation=False)

    def submit_mutation(
        self,
        key: Any,
        function: Callable[[], Any],
        *,
        on_settled: Callable[[Any], None] | None = None,
    ) -> concurrent.futures.Future:
        return self._submit(("mutation", key), function, mutation=True, on_settled=on_settled)

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def shutdown(self) -> None:
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            futures = list(self._pending.values())
        for future in futures:
            future.cancel()
        self._executor.shutdown(wait=False, cancel_futures=True)


class ClientService:
    """Persistent, single-remote client service used by Decky RPCs."""

    def __init__(
        self,
        state_root: str | os.PathLike[str],
        *,
        local_host_id: str | None = None,
        local_certificate_fingerprint: str | None = None,
        core_factory: Callable[..., Any] = RemoteClient,
        discovery_function: Callable[..., list[dict[str, Any]]] = discover_local_devices,
        probe_function: Callable[[str], dict[str, Any]] = probe_device,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.clock = clock
        self.monotonic = monotonic
        self.local_host_id = local_host_id
        self.local_certificate_fingerprint = local_certificate_fingerprint
        self.core_factory = core_factory
        self.discovery_function = discovery_function
        self.probe_function = probe_function
        self.store = StateStore(state_root, self._initial_state)
        self._migrate_state()
        self._lock = threading.RLock()
        self._mutation_lock = threading.RLock()
        self._queue = BoundedWorkQueue()
        self._running = False
        self._scans: dict[str, dict[str, Any]] = {}
        self._scan_lock = threading.RLock()
        self._scan_generation = 0

    def _initial_state(self) -> dict[str, Any]:
        return {
            "schema_version": CLIENT_SCHEMA_VERSION,
            "setup_complete": False,
            "device_mode": None,
            "client_id": opaque_id("client-") ,
            "client_name": default_client_name(),
            "upgrade_notice": False,
            "show_nonstandard_display_modes": True,
            "remote": None,
            "staged_remote": None,
            "pending_pairing": None,
            "operations": {},
            "wake": None,
        }

    def _migrate_state(self) -> None:
        state = self.store.snapshot()
        changed = False
        previous_schema = state.get("schema_version")
        if state.get("schema_version") != CLIENT_SCHEMA_VERSION:
            state["schema_version"] = CLIENT_SCHEMA_VERSION
            changed = True
        if previous_schema == 1 and state.get("show_nonstandard_display_modes") is False:
            # The old default hid valid recovery modes. Existing users can
            # explicitly hide them again in Settings after this upgrade.
            state["show_nonstandard_display_modes"] = True
            changed = True
        if not isinstance(state.get("client_id"), str) or not state.get("client_id"):
            state["client_id"] = opaque_id("client-")
            changed = True
        try:
            state["client_name"] = _safe_name(state.get("client_name") or default_client_name())
        except ClientError:
            state["client_name"] = default_client_name()
            changed = True
        for key, default in (("setup_complete", False), ("device_mode", None), ("upgrade_notice", False), ("show_nonstandard_display_modes", True), ("remote", None), ("staged_remote", None), ("pending_pairing", None), ("operations", {}), ("wake", None)):
            if key not in state:
                state[key] = default
                changed = True
        for key, default in (("mode_transition", None), ("last_service_errors", {})):
            if key not in state:
                state[key] = default
                changed = True
        if not isinstance(state.get("operations"), dict):
            state["operations"] = {}
            changed = True
        for operation in state["operations"].values():
            if isinstance(operation, dict) and operation.get("state") == "sending":
                # A plugin reload can interrupt the local send before a
                # remote operation ID exists. Preserve the exact body but
                # expose an explicit, user-acknowledged retry path instead of
                # leaving the mutation permanently busy or replaying it.
                operation["state"] = "unknown"
                operation["reason"] = "The client reloaded before the result was confirmed."
                operation["retry_allowed"] = True
                operation["updated_at"] = _utc_now()
                changed = True
        if state.get("device_mode") not in {"client", "server", "both", None}:
            state["device_mode"] = None
            state["setup_complete"] = False
            changed = True
        if not isinstance(state.get("setup_complete"), bool):
            state["setup_complete"] = False
            changed = True
        if not isinstance(state.get("upgrade_notice"), bool):
            state["upgrade_notice"] = False
            changed = True
        if not isinstance(state.get("show_nonstandard_display_modes"), bool):
            state["show_nonstandard_display_modes"] = False
            changed = True
        if not isinstance(state.get("mode_transition"), (dict, type(None))):
            state["mode_transition"] = None
            changed = True
        if not isinstance(state.get("last_service_errors"), dict):
            state["last_service_errors"] = {}
            changed = True
        if changed:
            self.store.replace(state)

    # ---- lifecycle and local state -----------------------------------

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._running:
                return self.public_status()
            self._queue = BoundedWorkQueue()
            self._running = True
        return self.public_status()

    def stop(self) -> None:
        with self._lock:
            self._running = False
            queue = self._queue
            self._queue = None
        if queue is not None:
            queue.shutdown()

    close = stop

    def _state(self) -> dict[str, Any]:
        return self.store.snapshot()

    def _work_queue(self) -> BoundedWorkQueue:
        queue = self._queue
        if queue is None:
            raise ClientError("client work is stopped", "work_stopped")
        return queue

    def set_mode(self, mode: str | None, *, setup_complete: bool = True) -> dict[str, Any]:
        if mode not in {"client", "server", "both", None}:
            raise ClientError("device mode is invalid", "invalid_device_mode")
        self.store.mutate(lambda state: (state.__setitem__("device_mode", mode), state.__setitem__("setup_complete", bool(setup_complete))))
        return self.public_status()

    def set_client_name(self, name: str) -> dict[str, Any]:
        normalized = _safe_name(name)
        self.store.mutate(lambda state: state.__setitem__("client_name", normalized))
        return self.public_status()

    def set_display_preferences(self, show_nonstandard_display_modes: Any) -> dict[str, Any]:
        if not isinstance(show_nonstandard_display_modes, bool):
            raise ClientError("show_nonstandard_display_modes must be boolean", "invalid_display_preferences")
        self.store.mutate(lambda state: state.__setitem__("show_nonstandard_display_modes", show_nonstandard_display_modes))
        return self.public_status()

    def dismiss_upgrade_notice(self) -> dict[str, Any]:
        self.store.mutate(lambda state: state.__setitem__("upgrade_notice", False))
        return self.public_status()

    def public_status(self) -> dict[str, Any]:
        state = self._state()
        return {
            "setup_complete": state.get("setup_complete") is True,
            "device_mode": state.get("device_mode"),
            "client_id": state.get("client_id"),
            "client_name": state.get("client_name") or default_client_name(),
            "upgrade_notice": state.get("upgrade_notice") is True,
            "show_nonstandard_display_modes": state.get("show_nonstandard_display_modes") is True,
            "running": self._running,
            "mode_transition": _copy(state.get("mode_transition")) if isinstance(state.get("mode_transition"), dict) else None,
            "last_service_errors": _copy(state.get("last_service_errors", {})),
            "remote": self._public_remote(state.get("remote")),
            "staged_remote": self._public_remote(state.get("staged_remote")),
            "pending_pairing": self._public_pending(state.get("pending_pairing")),
            "operations": [self._public_action(value) for value in state.get("operations", {}).values() if isinstance(value, dict)],
            "last_action": self._latest_action(state.get("operations", {})),
            "wake": _copy(state.get("wake")) if isinstance(state.get("wake"), dict) else None,
        }

    def diagnostics(self) -> dict[str, Any]:
        """Allowlisted local facts for copying; never serialize private records."""
        state = self._state()
        remote = state.get("remote") if isinstance(state.get("remote"), dict) else {}
        operations = state.get("operations") if isinstance(state.get("operations"), dict) else {}
        allowed_actions = {
            "suspend", "restart", "shutdown", "preview", "confirm", "restore",
            "confirm_preview", "restore_preview", "save_current", "sunshine_restart",
            "display_order", "display_order_reset",
        }
        allowed_states = {"sending", "accepted", "dispatched", "observed_return", "succeeded", "failed", "unknown"}
        recent = sorted(
            (item for item in operations.values() if isinstance(item, dict)),
            key=lambda item: str(item.get("updated_at", "")),
            reverse=True,
        )[:MAX_DIAGNOSTIC_ACTIONS]
        return {
            "format": "steamos-companion-decky-diagnostics-v1",
            "client_schema_version": CLIENT_SCHEMA_VERSION,
            "device_mode": state.get("device_mode") if state.get("device_mode") in {"client", "server", "both"} else "unset",
            "remote_paired": bool(remote),
            "remote_connected": remote.get("connection", {}).get("reachable") is True if isinstance(remote.get("connection"), dict) else False,
            "display_output_count": min(len(remote.get("outputs", [])), 256) if isinstance(remote.get("outputs"), list) else 0,
            "saved_profile_count": min(len(remote.get("profiles", [])), 256) if isinstance(remote.get("profiles"), list) else 0,
            "staged_pairing_present": isinstance(state.get("staged_remote"), dict),
            "operations": [
                {
                    "action": item.get("action") if isinstance(item.get("action"), str) and item.get("action") in allowed_actions else "other",
                    "state": item.get("state") if isinstance(item.get("state"), str) and item.get("state") in allowed_states else "unknown",
                    "host_operation_id_known": isinstance(item.get("remote_operation_id"), str),
                }
                for item in recent
            ],
        }

    @staticmethod
    def _public_remote(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        result = {
            key: _copy(value.get(key))
            for key in (
                "host_id", "endpoint", "certificate_fingerprint", "name", "scopes", "last_connected_at",
                "last_checked_at", "last_error", "retry_after", "status", "outputs", "display_order", "profiles", "preview", "wake_target",
                "wake", "alias",
            )
            if key in value
        }
        connection = value.get("connection")
        if isinstance(connection, dict):
            result["connection"] = _copy(connection)
        return result

    @staticmethod
    def _public_pending(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        return {
            key: _copy(value.get(key))
            for key in (
                "id", "pairing_id", "endpoint", "host_id", "certificate_fingerprint", "name", "scopes",
                "comparison_code", "expires_at", "status", "created_at", "last_error", "retry_after", "staged",
            )
            if key in value
        }

    @staticmethod
    def _public_action(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        return {
            key: _copy(value.get(key))
            for key in (
                "id", "request_id", "action", "path", "target", "body_digest", "remote_operation_id",
                "state", "outcome", "reason", "created_at", "updated_at", "read_only", "retry_allowed",
            )
            if key in value
        }

    @staticmethod
    def _latest_action(operations: Any) -> dict[str, Any] | None:
        if not isinstance(operations, dict):
            return None
        values = [value for value in operations.values() if isinstance(value, dict)]
        if not values:
            return None
        return ClientService._public_action(max(values, key=lambda item: item.get("updated_at", "")))

    # ---- discovery and pairing ---------------------------------------

    def _reject_self(self, candidate: dict[str, Any]) -> None:
        if self.local_host_id and candidate.get("host_id") == self.local_host_id:
            raise ClientError("This device is not a remote target", "self_device")
        if self.local_certificate_fingerprint and candidate.get("certificate_fingerprint") == self.local_certificate_fingerprint:
            raise ClientError("This device is not a remote target", "self_device")

    @staticmethod
    def _candidate(value: Any, *, port: int | None = None) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ClientError("the remote device candidate is invalid", "invalid_candidate")
        endpoint = value.get("endpoint")
        if not isinstance(endpoint, str) and isinstance(value.get("host"), str):
            try:
                host = validate_host(value["host"])
            except ProtocolError as exc:
                raise ClientError("the device address is invalid", "invalid_endpoint") from exc
            candidate_port = value.get("port", 18443 if port is None else port)
            if not isinstance(candidate_port, int) or isinstance(candidate_port, bool) or not 1 <= candidate_port <= 65535:
                raise ClientError("the device port is invalid", "invalid_endpoint")
            rendered = f"[{host}]" if ":" in host else host
            endpoint = f"https://{rendered}:{candidate_port}"
        if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
            raise ClientError("the device address is invalid", "invalid_endpoint")
        host_id = value.get("host_id")
        fingerprint = value.get("certificate_fingerprint")
        if not isinstance(host_id, str) or not host_id or not isinstance(fingerprint, str):
            raise ClientError("the device identity is incomplete", "identity_mismatch")
        # Reuse the transport boundary for endpoint canonicalization and pin
        # validation before a discovered/manual candidate can be stored or
        # used for pairing. This rejects an overlong host ID instead of
        # silently truncating the identity being approved.
        validated = RemoteClient(endpoint, host_id, fingerprint)
        return {
            "endpoint": validated.endpoint,
            "host_id": validated.host_id,
            "certificate_fingerprint": validated.certificate_fingerprint,
            "name": _safe_text(value.get("name"), 96) or None,
            "service": "steamos-companion",
        }

    def discover(self, *, port: int = 18443, endpoints: list[str] | None = None) -> list[dict[str, Any]]:
        values = self.discovery_function(port=port, endpoints=endpoints)
        results: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for value in values or []:
            try:
                candidate = self._candidate(value)
                self._reject_self(candidate)
            except ClientError:
                continue
            key = (candidate["host_id"], candidate["certificate_fingerprint"])
            if key in seen:
                continue
            seen.add(key)
            results.append(candidate)
        return results

    def begin_discovery(self, *, port: int = 18443, endpoints: list[str] | None = None) -> dict[str, Any]:
        with self._scan_lock:
            self._scan_generation += 1
            scan_id = opaque_id("scan-")
            self._scans[scan_id] = {"state": "searching", "results": [], "started_at": self.monotonic(), "cancelled": False}
            while len(self._scans) > MAX_DISCOVERY_SCANS:
                self._scans.pop(next(iter(self._scans)))

        def run() -> list[dict[str, Any]]:
            try:
                return self.discover(port=port, endpoints=endpoints)
            except Exception as exc:
                with self._scan_lock:
                    scan = self._scans.get(scan_id)
                    if scan and not scan["cancelled"]:
                        scan.update({"state": "failed", "error": _safe_text(exc)})
                raise

        try:
            future = self._work_queue().submit_read(("discovery", scan_id, port, endpoints or []), run)
        except Exception as exc:
            with self._scan_lock:
                scan = self._scans.get(scan_id)
                if scan is not None:
                    scan.update({"state": "failed", "error": _safe_text(exc)})
            raise

        def done(completed: concurrent.futures.Future) -> None:
            try:
                results = completed.result()
            except Exception as exc:
                results = []
                error = _safe_text(exc)
            else:
                error = ""
            with self._scan_lock:
                scan = self._scans.get(scan_id)
                if scan is None or scan["cancelled"]:
                    return
                scan.update({"state": "complete" if not error else "failed", "results": results, "error": error})

        future.add_done_callback(done)
        return {"scan_id": scan_id, "state": "searching"}

    def poll_discovery(self, scan_id: str) -> dict[str, Any]:
        scan_id = identifier(scan_id, "scan_id")
        with self._scan_lock:
            scan = self._scans.get(scan_id)
            if scan is None:
                raise ClientError("device scan is not available", "scan_not_found")
            return {"scan_id": scan_id, "state": scan["state"], "results": _copy(scan.get("results", [])), "error": scan.get("error")}

    def cancel_discovery(self, scan_id: str) -> dict[str, Any]:
        scan_id = identifier(scan_id, "scan_id")
        with self._scan_lock:
            scan = self._scans.get(scan_id)
            if scan is None:
                return {"scan_id": scan_id, "cancelled": True}
            scan["cancelled"] = True
            scan["state"] = "cancelled"
            scan["results"] = []
        return {"scan_id": scan_id, "cancelled": True}

    def check_device(self, host: str, port: int = 18443) -> dict[str, Any]:
        try:
            host = validate_host(host)
        except ProtocolError as exc:
            raise ClientError("the device address is invalid", "invalid_endpoint") from exc
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            raise ClientError("the device port is invalid", "invalid_endpoint")
        rendered = f"[{host}]" if ":" in host else host
        candidate = self._candidate(self.probe_function(f"https://{rendered}:{port}"))
        self._reject_self(candidate)
        return candidate

    def _core(self, remote: dict[str, Any] | None = None) -> Any:
        value = remote or self._state().get("remote")
        if not isinstance(value, dict):
            raise ClientError("Pair a remote device first", "not_paired")
        try:
            return self.core_factory(
                value["endpoint"], value["host_id"], value["certificate_fingerprint"], token=value.get("token")
            )
        except TypeError:
            return self.core_factory(value)

    def request_pairing(
        self,
        candidate: dict[str, Any],
        requested_scopes: Any = None,
        *,
        replace_existing: bool = False,
        client_name: str | None = None,
    ) -> dict[str, Any]:
        candidate = self._candidate(candidate)
        self._reject_self(candidate)
        scopes = validate_scopes(requested_scopes or DEFAULT_CLIENT_SCOPES)
        name = _safe_name(client_name or self._state().get("client_name") or default_client_name())
        pending_id = opaque_id("pair-request-")
        nonce = new_verification_nonce()
        existing = self._state().get("remote")
        record = {
            "id": pending_id,
            "pairing_id": None,
            "endpoint": candidate["endpoint"],
            "host_id": candidate["host_id"],
            "certificate_fingerprint": candidate["certificate_fingerprint"],
            "name": candidate.get("name"),
            "scopes": scopes,
            "nonce": nonce,
            "pairing_session": None,
            "client_id": self._state().get("client_id"),
            "client_name": name,
            "expires_at": self.clock() + 120,
            "status": "sending",
            "created_at": _utc_now(),
            "staged": bool(existing) or bool(replace_existing),
            "last_error": None,
            "retry_after": None,
        }
        # The nonce is private state; the public projection never returns it.
        self.store.mutate(lambda state: state.__setitem__("pending_pairing", record))
        try:
            response = self._core(record).pairing_request(
                nonce,
                record["client_id"],
                name,
                scopes,
                first_request=True,
            )
            if not isinstance(response, dict):
                raise ClientError("the device returned an invalid pairing response", "invalid_response")
            comparison = pairing_comparison_code(nonce, candidate["certificate_fingerprint"])
            def update(state: dict[str, Any]) -> None:
                pending = state.get("pending_pairing")
                if not isinstance(pending, dict) or pending.get("id") != pending_id:
                    return
                pending.update({
                    "pairing_id": response.get("pairing_id"),
                    "pairing_session": response.get("pairing_session"),
                    "comparison_code": comparison,
                    "expires_at": min(float(pending["expires_at"]), float(response.get("expires_at", pending["expires_at"]))),
                    "status": response.get("state", "pending"),
                    "last_error": None,
                })
            self.store.mutate(update)
            return self._public_pending(self.store.get("pending_pairing")) or {}
        except Exception as exc:
            self._mark_pending_error(pending_id, exc)
            raise

    def poll_pairing(self, pending_id: str | None = None) -> dict[str, Any]:
        pending = self._state().get("pending_pairing")
        if not isinstance(pending, dict):
            raise ClientError("there is no pending pairing request", "pairing_not_found")
        if pending_id is not None and pending.get("id") != pending_id:
            raise ClientError("the pairing request is no longer current", "pairing_stale")
        if float(pending.get("expires_at", 0)) <= self.clock():
            self._update_pending(pending["id"], {"status": "expired", "last_error": "This pairing request expired."})
            return self._public_pending(self.store.get("pending_pairing")) or {}
        try:
            response = self._core(pending).pairing_request(
                pending["nonce"], pending["client_id"], pending["client_name"], pending["scopes"],
                pairing_session=pending.get("pairing_session"), first_request=False,
            )
            if not isinstance(response, dict):
                raise ClientError("the device returned an invalid pairing response", "invalid_response")
        except ClientResponseError as exc:
            if exc.code in {"pairing_expired", "pairing_not_found", "pairing_consumed"}:
                self._update_pending(pending["id"], {"status": "expired", "last_error": "This pairing request expired."})
                return self._public_pending(self.store.get("pending_pairing")) or {}
            if exc.code in {"pairing_rejected", "rejected"}:
                self._update_pending(pending["id"], {"status": "rejected", "last_error": "Pairing was declined on the other device."})
                return self._public_pending(self.store.get("pending_pairing")) or {}
            if exc.code in {"pairing_cancelled", "cancelled"}:
                self._update_pending(pending["id"], {"status": "cancelled", "last_error": "Pairing was cancelled on the other device."})
                return self._public_pending(self.store.get("pending_pairing")) or {}
            self._update_pending(pending["id"], {"status": "waiting", "last_error": _safe_text(exc), "retry_after": exc.retry_after})
            return self._public_pending(self.store.get("pending_pairing")) or {}
        except IdentityMismatch as exc:
            self._update_pending(pending["id"], {"status": "identity_mismatch", "last_error": "The device's identity changed. Check the device before pairing again."})
            raise exc
        except ClientError as exc:
            self._update_pending(pending["id"], {"status": "waiting", "last_error": _safe_text(exc), "retry_after": exc.retry_after})
            return self._public_pending(self.store.get("pending_pairing")) or {}
        state = str(response.get("state", "pending"))
        if state == "rejected":
            self._update_pending(pending["id"], {"status": "rejected", "last_error": "Pairing was declined on the other device.", "retry_after": None})
            return self._public_pending(self.store.get("pending_pairing")) or {}
        if state == "cancelled":
            self._update_pending(pending["id"], {"status": "cancelled", "last_error": "Pairing was cancelled on the other device.", "retry_after": None})
            return self._public_pending(self.store.get("pending_pairing")) or {}
        if state == "expired":
            self._update_pending(pending["id"], {"status": "expired", "last_error": "This pairing request expired.", "retry_after": None})
            return self._public_pending(self.store.get("pending_pairing")) or {}
        if state != "approved":
            self._update_pending(pending["id"], {"status": state if state in {"pending", "waiting"} else "pending", "last_error": None, "retry_after": None})
            return self._public_pending(self.store.get("pending_pairing")) or {}
        credential = response.get("credential")
        if not isinstance(credential, dict) or not isinstance(credential.get("token"), str) or not credential.get("token"):
            self._update_pending(pending["id"], {"status": "failed", "last_error": "The device returned no usable credential."})
            raise ClientError("the device returned no usable credential", "invalid_response")
        try:
            granted_scopes = validate_scopes(credential.get("scopes") if isinstance(credential.get("scopes"), list) else pending["scopes"])
        except ProtocolError as exc:
            self._update_pending(pending["id"], {"status": "failed", "last_error": "The device returned invalid permissions."})
            raise ClientError("the device returned invalid permissions", "invalid_response") from exc
        remote = {
            "endpoint": pending["endpoint"],
            "host_id": pending["host_id"],
            "certificate_fingerprint": pending["certificate_fingerprint"],
            "name": pending.get("name"),
            "alias": pending.get("name"),
            "scopes": granted_scopes,
            "token": credential["token"],
            "wake_target": response.get("wake_target"),
            "last_connected_at": None,
            "last_checked_at": None,
            "last_error": None,
            "status": None,
            "outputs": [],
            "profiles": [],
            "preview": None,
        }
        current = self._state().get("remote")
        staged = bool(pending.get("staged")) or isinstance(current, dict)
        def commit(state: dict[str, Any]) -> None:
            if not isinstance(state.get("pending_pairing"), dict) or state["pending_pairing"].get("id") != pending["id"]:
                return
            if staged:
                state["staged_remote"] = remote
            else:
                state["remote"] = remote
            state["pending_pairing"] = None
        self.store.mutate(commit)
        if staged:
            return {"state": "approved", "needs_confirmation": True, "remote": self._public_remote(remote), "old_remote": self._public_remote(current)}
        return {"state": "approved", "needs_confirmation": False, "remote": self._public_remote(remote)}

    def cancel_pairing(self, pending_id: str | None = None) -> dict[str, Any]:
        if pending_id is not None:
            pending_id = identifier(pending_id, "pending_id")
        pending = self._state().get("pending_pairing")
        if isinstance(pending, dict) and (pending_id is None or pending.get("id") == pending_id):
            pairing_id = pending.get("pairing_id")
            nonce = pending.get("nonce")
            client_id = pending.get("client_id")
            if not all(isinstance(value, str) and value for value in (pairing_id, nonce, client_id)):
                # A request that failed before returning its host pairing ID
                # cannot be cancelled remotely. Keep the old best-effort
                # result explicit rather than claiming that the host acked it.
                self.store.mutate(lambda state: state.__setitem__("pending_pairing", None))
                return {"cancelled": True, "acknowledged": False, "server_request_may_remain": True}
            try:
                cancel = getattr(self._core(pending), "cancel_pairing", None)
                if not callable(cancel):
                    raise ClientError("the remote device cannot cancel this pairing request", "client_incompatible")
                response = cancel(
                    pairing_id,
                    nonce,
                    client_id,
                    pairing_session=pending.get("pairing_session"),
                )
                if not isinstance(response, dict):
                    raise ClientError("the device returned an invalid pairing cancellation response", "invalid_response")
            except ClientResponseError as exc:
                if exc.code in {"pairing_not_found", "pairing_expired", "pairing_consumed"}:
                    state = "expired"
                elif exc.code in {"pairing_rejected", "rejected"}:
                    state = "rejected"
                elif exc.code in {"pairing_cancelled", "cancelled"}:
                    state = "cancelled"
                else:
                    self._update_pending(pending["id"], {"status": "waiting", "last_error": _safe_text(exc), "retry_after": exc.retry_after})
                    raise
            except Exception as exc:
                self._update_pending(pending["id"], {"status": "waiting", "last_error": _safe_text(exc), "retry_after": getattr(exc, "retry_after", None)})
                raise
            else:
                state = str(response.get("state", "cancelled"))
                if state == "approved":
                    self._update_pending(pending["id"], {"status": "waiting", "last_error": "Pairing was approved before it could be cancelled."})
                    raise ClientError("the pairing request was already approved", "pairing_already_approved")
                if state not in {"cancelled", "rejected", "expired"}:
                    self._update_pending(pending["id"], {"status": "waiting", "last_error": "The device returned an invalid pairing cancellation state."})
                    raise ClientError("the device returned an invalid pairing cancellation state", "invalid_response")
            self.store.mutate(
                lambda state: state.__setitem__("pending_pairing", None)
                if isinstance(state.get("pending_pairing"), dict) and state["pending_pairing"].get("id") == pending["id"]
                else None
            )
            return {"cancelled": True, "acknowledged": True, "state": state, "server_request_may_remain": False}
        return {"cancelled": True, "server_request_may_remain": False}

    def use_staged_remote(self, use: bool) -> dict[str, Any]:
        staged = self._state().get("staged_remote")
        if not isinstance(staged, dict):
            raise ClientError("there is no staged remote device", "staged_remote_not_found")
        old = self._state().get("remote")
        if use:
            self.store.mutate(lambda state: (state.__setitem__("remote", state.get("staged_remote")), state.__setitem__("staged_remote", None)))
            return {"used": True, "remote": self._public_remote(self._state().get("remote")), "old_remote": self._public_remote(old)}
        cleanup = "The new device removed this client."
        try:
            response = self._core(staged).revoke_self(opaque_id("revoke-"))
            operation = response.get("operation") if isinstance(response, dict) else None
            if not isinstance(operation, dict) or operation.get("state") != "succeeded":
                cleanup = "Removal on the new device was not confirmed; remove this client there if it remains listed."
        except Exception:
            cleanup = "Could not confirm removal on the new device; remove this client there if it remains listed."
        self.store.mutate(lambda state: state.__setitem__("staged_remote", None))
        return {"used": False, "remote": self._public_remote(old), "server_cleanup": cleanup}

    def _mark_pending_error(self, pending_id: str, error: BaseException) -> None:
        if isinstance(error, IdentityMismatch):
            status = "identity_mismatch"
        elif isinstance(error, ClientResponseError) and error.code in {"pairing_expired", "pairing_not_found"}:
            status = "expired"
        elif isinstance(error, ClientResponseError) and error.code in {"pairing_rejected", "rejected"}:
            status = "rejected"
        elif isinstance(error, ClientResponseError) and error.code in {"pairing_cancelled", "cancelled"}:
            status = "cancelled"
        else:
            status = "waiting"
        self._update_pending(pending_id, {"status": status, "last_error": _safe_text(error), "retry_after": getattr(error, "retry_after", None)})

    def _update_pending(self, pending_id: str, changes: dict[str, Any]) -> None:
        def update(state: dict[str, Any]) -> None:
            pending = state.get("pending_pairing")
            if isinstance(pending, dict) and pending.get("id") == pending_id:
                pending.update({key: _safe_text(value) if key == "last_error" else value for key, value in changes.items()})
        self.store.mutate(update)

    # ---- remote reads -------------------------------------------------

    def _remote_matches(self, expected: dict[str, Any]) -> bool:
        current = self.store.get("remote")
        return isinstance(current, dict) and current.get("host_id") == expected.get("host_id") and current.get("endpoint") == expected.get("endpoint")

    def _update_remote(self, expected: dict[str, Any], changes: dict[str, Any]) -> None:
        def update(state: dict[str, Any]) -> None:
            current = state.get("remote")
            if not isinstance(current, dict) or current.get("host_id") != expected.get("host_id") or current.get("endpoint") != expected.get("endpoint"):
                return
            current.update(_copy(changes))
        self.store.mutate(update)

    def read_status(self, *, force: bool = False) -> dict[str, Any]:
        remote = self._state().get("remote")
        if not isinstance(remote, dict):
            raise ClientError("Pair a remote device first", "not_paired")
        key = ("status", remote.get("host_id"), remote.get("endpoint"))
        future = self._work_queue().submit_read(key, lambda: self._core(remote).status())
        try:
            value = future.result(timeout=REQUEST_TIMEOUT + 3)
        except Exception as exc:
            error = exc if isinstance(exc, ClientError) else ClientError("Can't reach the remote device", "network_error")
            self._update_remote(remote, {
                "connection": {"reachable": False, "last_error": _safe_text(error)},
                "last_error": _safe_text(error),
                "retry_after": getattr(error, "retry_after", None),
                "last_checked_at": _utc_now(),
            })
            raise error
        checked = _utc_now()
        self._update_remote(remote, {
            "connection": {"reachable": True, "last_error": None},
            "last_connected_at": checked,
            "last_checked_at": checked,
            "last_error": None,
            "retry_after": None,
            "status": _copy(value),
            "wake_target": value.get("wake_target") if isinstance(value.get("wake_target"), dict) else remote.get("wake_target"),
        })
        return {
            "connection": "connected",
            "checked_at": checked,
            "status": value,
            "remote": self._public_remote(self.store.get("remote")),
            "last_action": self._latest_action(self._state().get("operations", {})),
        }

    get_remote_status = read_status

    def read_outputs(self) -> dict[str, Any]:
        remote = self._state().get("remote")
        if not isinstance(remote, dict):
            raise ClientError("Pair a remote device first", "not_paired")
        key = ("outputs", remote.get("host_id"), remote.get("endpoint"))
        future = self._work_queue().submit_read(key, lambda: self._core(remote).outputs())
        try:
            value = future.result(timeout=REQUEST_TIMEOUT + 3)
        except Exception as exc:
            error = exc if isinstance(exc, ClientError) else ClientError("Can't reach the remote device", "network_error")
            self._update_remote(remote, {"last_error": _safe_text(error), "retry_after": getattr(error, "retry_after", None), "last_checked_at": _utc_now()})
            raise error
        if not isinstance(value, dict):
            raise ClientError("the device returned invalid display data", "invalid_response")
        self._update_remote(remote, {
            "outputs": value.get("outputs", []) if isinstance(value.get("outputs"), list) else [],
            "profiles": value.get("profiles", []) if isinstance(value.get("profiles"), list) else [],
            "preview": value.get("preview"),
        })
        return value

    get_remote_outputs = read_outputs

    def read_display_order(self) -> dict[str, Any]:
        """Read the remote Gaming Mode order without breaking older hosts."""
        remote = self._state().get("remote")
        if not isinstance(remote, dict):
            raise ClientError("Pair a remote device first", "not_paired")
        key = ("display-order", remote.get("host_id"), remote.get("endpoint"))
        future = self._work_queue().submit_read(key, lambda: self._core(remote).display_order())
        try:
            value = future.result(timeout=REQUEST_TIMEOUT + 3)
        except ClientResponseError as exc:
            # Display ordering is additive to v1. A paired host from before
            # this route existed should remain fully usable for its other
            # controls; represent the missing capability as a resource state.
            if exc.status == 404 or exc.code == "not_found":
                value = {
                    "protocol_version": 1,
                    "display_order": _unsupported_display_order(
                        "This remote device does not expose Gaming Mode display ordering."
                    ),
                }
            else:
                self._update_remote(remote, {
                    "last_error": _safe_text(exc),
                    "retry_after": exc.retry_after,
                    "last_checked_at": _utc_now(),
                })
                raise
        except Exception as exc:
            error = exc if isinstance(exc, ClientError) else ClientError("Can't reach the remote device", "network_error")
            self._update_remote(remote, {
                "last_error": _safe_text(error),
                "retry_after": getattr(error, "retry_after", None),
                "last_checked_at": _utc_now(),
            })
            raise error
        if not isinstance(value, dict) or not isinstance(value.get("display_order"), dict):
            raise ClientError("the device returned invalid display-order data", "invalid_response")
        self._update_remote(remote, {"display_order": value["display_order"]})
        return value

    get_remote_display_order = read_display_order

    def refresh_remote(self) -> dict[str, Any]:
        status = self.read_status()
        try:
            outputs = self.read_outputs()
        except ClientError:
            outputs = None
        try:
            display_order = self.read_display_order()
        except ClientError:
            display_order = None
        self.reconcile_operations()
        return {
            "status": status,
            "outputs": outputs,
            "display_order": display_order,
            "remote": self._public_remote(self.store.get("remote")),
        }

    # ---- availability and actions ------------------------------------

    def action_availability(
        self,
        action: str,
        *,
        output_id: str | None = None,
        mode_id: str | None = None,
        preview_id: str | None = None,
        output_keys: list[str] | None = None,
        generation: int | None = None,
    ) -> dict[str, Any]:
        action = _safe_text(action, 64)
        if action not in {
            "suspend", "restart", "shutdown", "preview", "confirm", "restore", "restore_preview",
            "save_current", "sunshine_restart", "display_order", "display_order_reset",
        }:
            return {"available": False, "reason": "This action is unsupported"}
        state = self._state()
        remote = state.get("remote")
        if not isinstance(remote, dict):
            return {"available": False, "reason": "Pair a remote device first"}
        status = remote.get("status") if isinstance(remote.get("status"), dict) else {}
        capabilities = status.get("capabilities") if isinstance(status.get("capabilities"), dict) else {}
        if action in {"suspend", "restart", "shutdown"}:
            capability = capabilities.get(action)
            if capability != "available":
                return {"available": False, "reason": f"This pairing cannot {action} the remote device"}
        if action == "sunshine_restart" and capabilities.get("sunshine_restart") != "available":
            return {"available": False, "reason": "Sunshine recovery is unavailable on the remote device"}
        if action in {"display_order", "display_order_reset"}:
            if "display.control" not in set(remote.get("scopes", [])):
                return {"available": False, "reason": "This pairing cannot change the remote display order"}
            order = remote.get("display_order") if isinstance(remote.get("display_order"), dict) else None
            if order is None:
                return {"available": False, "reason": "Refresh the remote display order first"}
            if order.get("unsupported") is True:
                return {"available": False, "reason": order.get("reason") or "Gaming Mode display ordering is unsupported on the remote device"}
            if action != "display_order_reset":
                if order.get("available") is not True:
                    return {"available": False, "reason": order.get("reason") or "Gaming Mode display ordering is unavailable on the remote device"}
                try:
                    keys = _display_order_keys(output_keys)
                except ClientError as exc:
                    return {"available": False, "reason": str(exc)}
                if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0 or generation > 2_147_483_647:
                    return {"available": False, "reason": "The remote display inventory is stale; refresh it before saving"}
                if order.get("generation") != generation:
                    return {"available": False, "reason": "The remote display inventory changed; refresh the order"}
                outputs = order.get("outputs") if isinstance(order.get("outputs"), list) else []
                by_key = {item.get("output_key"): item for item in outputs if isinstance(item, dict)}
                if any(by_key.get(key, {}).get("connected") is not True for key in keys):
                    return {"available": False, "reason": "A selected remote screen is no longer connected"}
        if action in {"preview", "confirm", "restore", "restore_preview", "save_current"} and capabilities.get("display_rescue") != "available":
            return {"available": False, "reason": "Steam display controls are unavailable on the remote device"}
        if action == "restore" and not (remote.get("profiles") or status.get("recovery_profile")):
            return {"available": False, "reason": "No recovery mode saved"}
        if action == "confirm" and not remote.get("preview"):
            return {"available": False, "reason": "There is no active display preview to confirm"}
        if action == "restore_preview" and not remote.get("preview"):
            return {"available": False, "reason": "There is no active display preview to revert"}
        preview = remote.get("preview") if isinstance(remote.get("preview"), dict) else None
        owns_preview = bool(preview and any(
            isinstance(value, dict)
            and value.get("action") == "preview"
            and value.get("remote_operation_id") == preview.get("preview_id")
            and value.get("target", {}).get("host_id") == remote.get("host_id")
            for value in state.get("operations", {}).values()
        ))
        if preview and action in {"restore", "restore_preview", "confirm"} and not owns_preview:
            return {"available": False, "reason": "This display preview belongs to another client"}
        for value in state.get("operations", {}).values():
            if not isinstance(value, dict) or value.get("state") not in {"sending", "accepted", "dispatched"}:
                continue
            # Only the owner can confirm or restore while its own preview
            # occupies the mutation lane. A verified profile is a distinct
            # recovery target from the preview baseline.
            recovery_exit = action in {"confirm", "restore_preview", "restore"} and owns_preview and value.get("action") == "preview" and value.get("remote_operation_id") == preview.get("preview_id")
            if not recovery_exit:
                return {"available": False, "reason": "Another operation is in progress on the remote device", "operation_id": value.get("id")}
        if preview and action not in {"confirm", "restore_preview", "restore"}:
            return {"available": False, "reason": "Another display preview is in progress on the remote device"}
        if action == "preview":
            outputs = remote.get("outputs") or []
            output = next((value for value in outputs if value.get("id") == output_id), None)
            mode = next((value for value in output.get("modes", []) if value.get("id") == mode_id), None) if output else None
            if not output or not mode:
                return {"available": False, "reason": "Select an advertised display mode first"}
            if output.get("current_mode_id") == mode_id:
                return {"available": False, "reason": "The selected mode is already current"}
        return {"available": True, "reason": None}

    def _new_action(self, action: str, path: str, fields: dict[str, Any]) -> dict[str, Any]:
        remote = self._state().get("remote")
        if not isinstance(remote, dict):
            raise ClientError("Pair a remote device first", "not_paired")
        body = {"request_id": opaque_id("request-"), **_copy(fields)}
        digest = body_digest(body)
        action_id = opaque_id("action-")
        stamp = _utc_now()
        record = {
            "id": action_id,
            "request_id": body["request_id"],
            "action": action,
            "path": path,
            "target": {"host_id": remote["host_id"], "endpoint": remote["endpoint"], "name": remote.get("alias") or remote.get("name")},
            "body": body,
            "body_digest": digest,
            "remote_operation_id": None,
            "state": "sending",
            "outcome": None,
            "reason": None,
            "created_at": stamp,
            "updated_at": stamp,
            "read_only": False,
            "retry_allowed": False,
        }
        def save(state: dict[str, Any]) -> None:
            actions = state.setdefault("operations", {})
            actions[action_id] = record
            if len(actions) > MAX_ACTIONS:
                terminal = [item for item in actions.values() if isinstance(item, dict) and item.get("state") in {"succeeded", "failed", "unknown", "observed_return"}]
                for old in sorted(terminal, key=lambda item: item.get("updated_at", ""))[: max(0, len(actions) - MAX_ACTIONS)]:
                    actions.pop(old.get("id"), None)
        self.store.mutate(save)
        return record

    def _update_action(self, action_id: str, **changes: Any) -> dict[str, Any]:
        def update(state: dict[str, Any]) -> dict[str, Any]:
            action = state.setdefault("operations", {}).get(action_id)
            if not isinstance(action, dict):
                raise ClientError("action is no longer available", "action_not_found")
            allowed = {"remote_operation_id", "state", "outcome", "reason", "read_only", "retry_allowed"}
            for key, value in changes.items():
                if key in allowed:
                    action[key] = _safe_text(value) if key in {"outcome", "reason"} and value is not None else value
            action["updated_at"] = _utc_now()
            return self._public_action(action)
        return self.store.mutate(update)

    def _dispatch_action(self, record: dict[str, Any]) -> dict[str, Any]:
        remote = self._state().get("remote")
        if not isinstance(remote, dict) or remote.get("host_id") != record["target"]["host_id"]:
            self._update_action(record["id"], state="failed", reason="the selected remote device changed")
            raise ClientError("the selected remote device changed", "stale_target")
        try:
            value = self._core(remote).mutation(record["path"], record["body"])
        except IdentityMismatch:
            self._update_action(record["id"], state="failed", reason="The device's identity changed", retry_allowed=False)
            raise
        except ClientResponseError as exc:
            self._update_action(record["id"], state="failed", reason=_safe_text(exc), retry_allowed=True)
            raise
        except ClientError as exc:
            self._update_action(record["id"], state="unknown" if exc.unknown else "failed", reason=_safe_text(exc), retry_allowed=bool(exc.unknown))
            raise
        except Exception as exc:
            self._update_action(record["id"], state="unknown", reason="Result not confirmed", retry_allowed=True)
            raise ClientError("Result not confirmed", "operation_unknown", unknown=True) from exc
        if not isinstance(value, dict) or not isinstance(value.get("operation"), dict):
            self._update_action(record["id"], state="unknown", reason="The device returned no operation result", retry_allowed=True)
            raise ClientError("The device returned no operation result", "invalid_response", unknown=True)
        operation = value["operation"]
        try:
            remote_operation_id = identifier(operation.get("id"), "operation_id")
        except ProtocolError as exc:
            self._update_action(record["id"], state="unknown", reason="The device returned an invalid operation identity", retry_allowed=True)
            raise ClientError("The device returned an invalid operation identity", "invalid_response", unknown=True) from exc
        remote_state = operation.get("state")
        valid_states = {"accepted", "dispatched", "observed_return", "succeeded", "failed", "unknown"}
        if remote_state not in valid_states:
            self._update_action(record["id"], remote_operation_id=remote_operation_id, state="unknown", reason="The device returned an invalid operation state", retry_allowed=True)
            raise ClientError("The device returned an invalid operation state", "invalid_response", unknown=True)
        mapped = remote_state
        outcome = operation.get("outcome") if isinstance(operation, dict) else None
        self._update_action(record["id"], remote_operation_id=remote_operation_id, state=mapped, outcome=outcome, reason=operation.get("reason") if isinstance(operation, dict) else None, retry_allowed=mapped in {"failed", "unknown"})
        return self._public_action(self.store.get("operations", {}).get(record["id"]))

    def _run_action(self, record: dict[str, Any]) -> dict[str, Any]:
        with self._mutation_lock:
            return self._dispatch_action(record)

    def _action(self, action: str, path: str, fields: dict[str, Any]) -> dict[str, Any]:
        record = self._new_action(action, path, fields)
        settled = Settlement(lambda value: self._settle_unstarted(record["id"], value))
        try:
            queue = self._work_queue()
            future = queue.submit_mutation(record["id"], lambda: self._run_action(record), on_settled=settled.settle)
        except Exception as exc:
            # A mode transition can stop the client queue while this action is
            # being prepared. Persist a terminal result so the originating UI
            # control cannot remain busy forever.
            self._settle_unstarted(record["id"], {"ok": False, "error": _safe_text(exc)})
            raise
        try:
            return future.result(timeout=REQUEST_TIMEOUT + 5)
        except concurrent.futures.TimeoutError as exc:
            self._update_action(record["id"], state="unknown", reason="Result not confirmed", retry_allowed=True)
            raise ClientError("Result not confirmed", "operation_unknown", unknown=True) from exc
        except Exception:
            raise

    def _settle_unstarted(self, action_id: str, value: Any) -> None:
        current = self.store.get("operations", {}).get(action_id)
        if isinstance(current, dict) and current.get("state") == "sending":
            self._update_action(action_id, state="failed", reason="The request could not be started", retry_allowed=True)

    def power(self, action: str) -> dict[str, Any]:
        if action not in {"suspend", "restart", "shutdown"}:
            raise ClientError("power action is unsupported", "invalid_action")
        available = self.action_availability(action)
        if not available["available"]:
            raise ClientError(available["reason"], "action_unavailable")
        return self._action(action, "/v1/power", {"action": action})

    def preview(self, output_id: str, mode_id: str, generation: int) -> dict[str, Any]:
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0 or generation > 2_147_483_647:
            raise ClientError("display generation is invalid", "invalid_request")
        available = self.action_availability("preview", output_id=output_id, mode_id=mode_id)
        if not available["available"]:
            raise ClientError(available["reason"], "action_unavailable")
        return self._action("preview", "/v1/display/preview", {"output_id": identifier(output_id, "output_id"), "mode_id": identifier(mode_id, "mode_id"), "generation": generation})

    def confirm_preview(self, preview_id: str, *, visible: bool = True) -> dict[str, Any]:
        if visible is not True:
            raise ClientError("visible confirmation is required", "invalid_request")
        available = self.action_availability("confirm", preview_id=preview_id)
        if not available["available"]:
            raise ClientError(available["reason"], "action_unavailable")
        return self._action("confirm_preview", "/v1/display/confirm", {"preview_id": identifier(preview_id, "preview_id"), "visible": True})

    def restore(self, *, source: str = "verified", profile_id: str | None = None) -> dict[str, Any]:
        if source not in {"verified", "preview"}:
            raise ClientError("restore source is unsupported", "invalid_request")
        action = "restore_preview" if source == "preview" else "restore"
        available = self.action_availability(action)
        if not available["available"]:
            raise ClientError(available["reason"], "action_unavailable")
        fields: dict[str, Any] = {"source": source}
        if profile_id is not None:
            fields["profile_id"] = identifier(profile_id, "profile_id")
        return self._action(action, "/v1/display/restore", fields)

    def save_current(self, output_id: str, generation: int) -> dict[str, Any]:
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0 or generation > 2_147_483_647:
            raise ClientError("display generation is invalid", "invalid_request")
        available = self.action_availability("save_current")
        if not available["available"]:
            raise ClientError(available["reason"], "action_unavailable")
        return self._action("save_current", "/v1/display/save-current", {"output_id": identifier(output_id, "output_id"), "generation": generation, "visible": True})

    def save_display_order(self, output_keys: Any, generation: Any, *, restart: bool = False) -> dict[str, Any]:
        """Save the remote host's ordered Gamescope output preference."""
        keys = _display_order_keys(output_keys)
        if not isinstance(generation, int) or isinstance(generation, bool) or not 0 <= generation <= 2_147_483_647:
            raise ClientError("display order generation is invalid", "invalid_display_order")
        if type(restart) is not bool:
            raise ClientError("display order restart must be boolean", "invalid_display_order")
        available = self.action_availability("display_order", output_keys=keys, generation=generation)
        if not available["available"]:
            raise ClientError(available["reason"], "action_unavailable")
        return self._action(
            "display_order",
            "/v1/display/order",
            {"output_keys": keys, "generation": generation, "restart": restart},
        )

    def reset_display_order(self) -> dict[str, Any]:
        """Remove only the remote host's plugin-owned output preference."""
        available = self.action_availability("display_order_reset")
        if not available["available"]:
            raise ClientError(available["reason"], "action_unavailable")
        return self._action("display_order_reset", "/v1/display/order/automatic", {})

    def sunshine_restart(self) -> dict[str, Any]:
        available = self.action_availability("sunshine_restart")
        if not available["available"]:
            raise ClientError(available["reason"], "action_unavailable")
        return self._action("sunshine_restart", "/v1/sunshine/restart", {})

    # ---- action reconciliation ---------------------------------------

    @staticmethod
    def _remote_state_to_local(value: Any) -> str:
        if isinstance(value, str) and value in {"accepted", "dispatched", "observed_return", "succeeded", "failed", "unknown"}:
            return value
        return "unknown"

    def check_operation(self, action_id: str) -> dict[str, Any]:
        action_id = identifier(action_id, "action_id")
        action = self.store.get("operations", {}).get(action_id)
        if not isinstance(action, dict):
            raise ClientError("action is no longer available", "action_not_found")
        remote_operation_id = action.get("remote_operation_id")
        if not remote_operation_id:
            return self._update_action(action_id, state="unknown", reason="No remote operation ID was returned; result cannot be confirmed by a read-only check.", read_only=True, retry_allowed=True)
        remote = self._state().get("remote")
        if not isinstance(remote, dict) or remote.get("host_id") != action.get("target", {}).get("host_id"):
            return self._update_action(action_id, state="unknown", reason="The original remote device is no longer selected.", read_only=True, retry_allowed=True)
        try:
            operation_id = identifier(remote_operation_id, "operation_id")
            value = self._core(remote).operation(operation_id)
        except (ClientError, ProtocolError) as exc:
            self._update_action(action_id, state="unknown", reason=_safe_text(exc), read_only=True, retry_allowed=not isinstance(exc, IdentityMismatch))
            return self._public_action(self.store.get("operations", {}).get(action_id))
        operation = value.get("operation") if isinstance(value, dict) else None
        if not isinstance(operation, dict):
            self._update_action(action_id, state="unknown", reason="The remote returned no operation result", read_only=True, retry_allowed=True)
            return self._public_action(self.store.get("operations", {}).get(action_id))
        operation_state = self._remote_state_to_local(operation.get("state"))
        self._update_action(action_id, state=operation_state, outcome=operation.get("outcome"), reason=operation.get("reason"), read_only=True, retry_allowed=operation_state in {"failed", "unknown"})
        return self._public_action(self.store.get("operations", {}).get(action_id))

    def reconcile_operations(self) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        for action in self._state().get("operations", {}).values():
            if isinstance(action, dict) and action.get("remote_operation_id") and action.get("state") in {"sending", "accepted", "dispatched", "observed_return", "unknown"}:
                try:
                    values.append(self.check_operation(action["id"]))
                except ClientError:
                    continue
        return values

    def resend_action(self, action_id: str, *, acknowledge_earlier_may_have_run: bool = False) -> dict[str, Any]:
        action_id = identifier(action_id, "action_id")
        if not acknowledge_earlier_may_have_run:
            raise ClientError("Acknowledge that the earlier request may already have run", "resend_ack_required")
        action = self.store.get("operations", {}).get(action_id)
        if not isinstance(action, dict) or action.get("state") not in {"unknown", "failed"} or action.get("retry_allowed") is not True:
            raise ClientError("this action is not eligible for an explicit resend", "resend_unavailable")
        remote = self._state().get("remote")
        if not isinstance(remote, dict) or remote.get("host_id") != action.get("target", {}).get("host_id"):
            raise ClientError("the original remote device is no longer selected", "stale_target")
        # Reuse the persisted exact body and original request ID.  The digest
        # alone is intentionally never used to reconstruct a mutation.
        record = _copy(action)
        self._update_action(action_id, state="sending", reason="The earlier request may already have run; sending the explicit retry.", retry_allowed=False, read_only=False)
        settled = Settlement(lambda value: self._settle_unstarted(action_id, value))
        try:
            future = self._work_queue().submit_mutation(("resend", action_id), lambda: self._run_action(record), on_settled=settled.settle)
        except Exception as exc:
            self._settle_unstarted(action_id, {"ok": False, "error": _safe_text(exc)})
            raise
        try:
            return future.result(timeout=REQUEST_TIMEOUT + 5)
        except concurrent.futures.TimeoutError as exc:
            self._update_action(action_id, state="unknown", reason="Result not confirmed", retry_allowed=True)
            raise ClientError("Result not confirmed", "operation_unknown", unknown=True) from exc

    # ---- wake and pairing/settings maintenance -----------------------

    def wake(self) -> dict[str, Any]:
        remote = self._state().get("remote")
        if not isinstance(remote, dict):
            raise ClientError("Pair a remote device first", "not_paired")
        target = remote.get("wake_target")
        if not isinstance(target, dict) or not target.get("available") or not target.get("mac"):
            return {"sent": False, "state": "unavailable", "message": "Wake isn't configured for this device"}
        try:
            result = send_wake_packet(str(target["mac"]), source_address=active_route_ipv4())
        except ClientError as exc:
            failure = {"state": "failed", "reason": _safe_text(exc), "at": _utc_now()}
            self._update_remote(remote, {"wake": failure})
            self.store.mutate(lambda state: state.__setitem__("wake", {"remote_host_id": remote["host_id"], **failure}))
            raise
        wake = {"state": "sent", "sent_at": _utc_now(), **result}
        self._update_remote(remote, {"wake": wake})
        self.store.mutate(lambda state: state.__setitem__("wake", {"remote_host_id": remote["host_id"], **wake}))
        return {"sent": True, "state": "sent", "message": "Wake packet sent · Waiting for connection…", "wake": wake}

    def rename_remote(self, alias: str) -> dict[str, Any]:
        alias = _safe_name(alias)
        if not isinstance(self._state().get("remote"), dict):
            raise ClientError("Pair a remote device first", "not_paired")
        self.store.mutate(lambda state: state["remote"].__setitem__("alias", alias))
        return self.public_status()

    def update_remote_endpoint(self, candidate: dict[str, Any]) -> dict[str, Any]:
        candidate = self._candidate(candidate)
        self._reject_self(candidate)
        remote = self._state().get("remote")
        if not isinstance(remote, dict):
            raise ClientError("Pair a remote device first", "not_paired")
        if candidate["host_id"] != remote.get("host_id") or candidate["certificate_fingerprint"] != remote.get("certificate_fingerprint"):
            raise IdentityMismatch("Device identity changed; pair again before using this address")
        self.store.mutate(lambda state: (state["remote"].__setitem__("endpoint", candidate["endpoint"]), state["remote"].__setitem__("last_error", None)))
        return self.public_status()

    def forget_remote(self, *, revoke: bool = False) -> dict[str, Any]:
        remote = self._state().get("remote")
        if not isinstance(remote, dict):
            return {"forgotten": True, "server_may_list_client": False}
        if revoke:
            try:
                response = self._core(remote).revoke_self(opaque_id("revoke-"))
                operation = response.get("operation") if isinstance(response, dict) else None
                if not isinstance(operation, dict) or operation.get("state") != "succeeded":
                    raise ClientError("host did not confirm credential removal", "revoke_unconfirmed")
            except ClientError as exc:
                raise ClientError("Could not remove access on the remote device; the local pairing was kept", "revoke_failed") from exc
        self.store.mutate(lambda state: state.__setitem__("remote", None))
        return {"forgotten": True, "revoked": revoke, "server_may_list_client": not revoke}
