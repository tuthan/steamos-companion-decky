"""Role-aware lifecycle coordinator for Server, Client, and Both modes."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any

from .client import ClientService
from .client_core import ClientError
from .service import HostService
from .storage import StateError


MODES = frozenset({"client", "server", "both"})


class CoordinatorError(ClientError):
    pass


class DeviceCoordinator:
    """Own the selected role and independently start/stop its services.

    ``HostService`` keeps its original state path and data shape.  Outgoing
    client state is stored below ``client/`` so changing roles cannot overwrite
    the host certificate, incoming clients, profiles, or host journal.
    """

    def __init__(self, state_root: str | os.PathLike[str]):
        self.root = Path(state_root)
        self._lock = threading.RLock()
        self._transition_thread: threading.Thread | None = None
        self._transition_cancel = threading.Event()
        self._host_started = False
        self._client_started = False
        self._recovery_error: str | None = None
        self.host: HostService | None = None

        legacy_state_exists = os.path.lexists(self.root / "state.json")
        try:
            self.host = HostService(self.root)
        except Exception as exc:
            # A corrupt or unreadable existing state must be visible as a
            # recovery error.  Never create a replacement identity here.
            self._recovery_error = f"SteamOS Companion state needs recovery: {str(exc)[:220]}"

        try:
            local_host_id = self.host.host_id if self.host is not None else None
            local_fingerprint = None
            if self.host is not None:
                tls = self.host.store.get("tls", {})
                if isinstance(tls, dict):
                    local_fingerprint = tls.get("fingerprint")
            client_state_exists = os.path.lexists(self.root / "client" / "state.json")
            self.client = ClientService(
                self.root / "client",
                local_host_id=local_host_id,
                local_certificate_fingerprint=local_fingerprint,
            )
            if legacy_state_exists and not client_state_exists:
                # v0.4.0 was host-only. Existing installations are servers and
                # retain their exact listener settings.
                self.client.set_mode("server", setup_complete=True)
                self.client.store.mutate(lambda state: state.__setitem__("upgrade_notice", True))
            self._recover_interrupted_transition()
        except Exception as exc:
            self.client = None
            if self._recovery_error is None:
                self._recovery_error = f"SteamOS Companion client state needs recovery: {str(exc)[:220]}"

    # ---- lifecycle ----------------------------------------------------

    def _state(self) -> dict[str, Any]:
        return self.client.store.snapshot() if self.client is not None else {}

    def mode(self) -> str | None:
        value = self._state().get("device_mode")
        return value if value in MODES else None

    def setup_complete(self) -> bool:
        return self._state().get("setup_complete") is True and self.mode() in MODES

    def _recover_interrupted_transition(self) -> None:
        """Clear a transition that lost its worker during a plugin reload."""
        if self.client is None:
            return
        transition = self.client.store.get("mode_transition")
        if not isinstance(transition, dict) or transition.get("state") in {"cancelled", "complete", "failed"}:
            return
        reason = "Mode change interrupted by a plugin reload; the previous mode is still active."
        self.client.store.mutate(lambda state: (
            state.__setitem__("mode_transition", None),
            state.setdefault("last_service_errors", {}).__setitem__("transition", reason),
        ))

    @staticmethod
    def _has_server(mode: str | None) -> bool:
        return mode in {"server", "both"}

    @staticmethod
    def _has_client(mode: str | None) -> bool:
        return mode in {"client", "both"}

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._recovery_error:
                return self.get_settings()
            mode = self.mode()
            if self._has_server(mode) and self.host is not None and not self._host_started:
                self.host.start(start_server=True)
                self._host_started = True
            if self._has_client(mode) and self.client is not None and not self._client_started:
                self.client.start()
                self._client_started = True
            return self.get_settings()

    def stop(self) -> None:
        with self._lock:
            host_started = self._host_started
            client_started = self._client_started
            self._host_started = False
            self._client_started = False
        if client_started and self.client is not None:
            self.client.stop()
        if host_started and self.host is not None:
            self.host.stop()

    # ---- public state -------------------------------------------------

    def get_settings(self) -> dict[str, Any]:
        client_status = self.client.public_status() if self.client is not None else {
            "setup_complete": False, "device_mode": None, "running": False, "remote": None,
            "pending_pairing": None, "operations": [],
        }
        server_status = self.host.get_local_status() if self.host is not None else {
            "protocol_version": 1, "settings": {}, "listener": {"running": False, "error": self._recovery_error},
            "pending_pairings": [], "clients": [], "bridge": {"ready": False},
        }
        transition = client_status.get("mode_transition")
        mode = client_status.get("device_mode")
        effective_mode = transition.get("effective", mode) if isinstance(transition, dict) else mode
        service_errors: dict[str, str] = {}
        if isinstance(server_status.get("listener"), dict) and server_status["listener"].get("error"):
            service_errors["server"] = str(server_status["listener"]["error"])[:256]
        if self._recovery_error:
            service_errors["state"] = self._recovery_error
        stored_service_errors = client_status.get("last_service_errors")
        if isinstance(stored_service_errors, dict):
            service_errors.update({str(key): str(value)[:256] for key, value in stored_service_errors.items()})
        return {
            # These top-level fields preserve the v0.4 RPC shape for existing
            # Decky integrations while the nested role views are additive.
            **server_status,
            "device_mode": effective_mode,
            "setup_complete": client_status.get("setup_complete") is True and mode in MODES,
            "mode": {
                "selected": mode,
                "effective": effective_mode,
                "setup_complete": client_status.get("setup_complete") is True and mode in MODES,
                "transition": transition,
            },
            "roles": {
                "client": {"enabled": self._has_client(effective_mode), "running": self._client_started, "state": "ready" if self._client_started else "stopped"},
                "server": {"enabled": self._has_server(effective_mode), "running": self._host_started, "state": "ready" if self._host_started else "stopped"},
            },
            "client": client_status,
            "server": server_status,
            "service_errors": service_errors,
            "recovery_error": self._recovery_error,
        }

    get_local_status = get_settings

    # ---- mode transitions --------------------------------------------

    def set_mode(self, mode: str, *, client_name: str | None = None) -> dict[str, Any]:
        if mode not in MODES:
            raise CoordinatorError("device mode must be client, server, or both", "invalid_device_mode")
        if self._recovery_error or self.client is None:
            raise CoordinatorError(self._recovery_error or "client state is unavailable", "state_recovery_required")
        if client_name is not None:
            self.client.set_client_name(client_name)
        with self._lock:
            current = self.mode()
            transition = self._state().get("mode_transition")
            if current == mode and not transition:
                if self._has_server(mode) and self.host is not None and not self._host_started:
                    self.host.start(start_server=True)
                    self._host_started = True
                if not self._has_client(mode) and self._client_started:
                    self.client.stop()
                    self._client_started = False
                if self._has_client(mode) and not self._client_started:
                    self.client.start()
                    self._client_started = True
                return self.get_settings()
            if transition and transition.get("state") not in {"cancelled", "complete"}:
                raise CoordinatorError("another mode change is already in progress", "mode_change_in_progress")
            transition_id = f"mode-{int(time.time() * 1000)}-{threading.get_ident()}"
            record = {
                "id": transition_id,
                "from": current,
                "requested": mode,
                "effective": current,
                "state": "applying" if current is None or not self._departing_server(current, mode) else "waiting",
                "reason": "Waiting for display recovery" if current and self._departing_server(current, mode) and self._server_work_active() else None,
                "started_at": time.time(),
                "elapsed_seconds": 0,
                "cancel_requested": False,
            }
            self.client.store.mutate(lambda state: state.__setitem__("mode_transition", record))
            self._transition_cancel.clear()
            thread = threading.Thread(target=self._apply_mode, args=(transition_id, mode), name="steamos-companion-mode", daemon=True)
            self._transition_thread = thread
            thread.start()
        return self.get_settings()

    def _departing_server(self, current: str | None, target: str) -> bool:
        return self._has_server(current) and not self._has_server(target)

    def _server_work_active(self) -> bool:
        if self.host is None:
            return False
        if self.host.store.get("preview") is not None:
            return True
        if self.host.store.get("local_display_preview") is not None:
            return True
        return bool(self.host.journal.active())

    def _update_transition(self, transition_id: str, **changes: Any) -> bool:
        if self.client is None:
            return False
        changed = False
        def update(state: dict[str, Any]) -> None:
            nonlocal changed
            transition = state.get("mode_transition")
            if isinstance(transition, dict) and transition.get("id") == transition_id:
                transition.update(changes)
                if "started_at" in transition:
                    transition["elapsed_seconds"] = max(0, int(time.time() - float(transition["started_at"])))
                changed = True
        self.client.store.mutate(update)
        return changed

    def _apply_mode(self, transition_id: str, target: str) -> None:
        current = self.mode()
        try:
            deadline = time.monotonic() + 30
            while self._departing_server(current, target) and self._server_work_active():
                if self._transition_cancel.is_set():
                    self._update_transition(transition_id, state="cancelled", reason="Mode change cancelled")
                    self._clear_transition(transition_id)
                    return
                self._update_transition(transition_id, state="waiting", reason="Waiting for display recovery")
                if time.monotonic() >= deadline:
                    self._update_transition(transition_id, reason="Display recovery is still active; open status/details or cancel the mode change")
                    deadline = time.monotonic() + 1
                time.sleep(0.1)
            if self._transition_cancel.is_set():
                self._clear_transition(transition_id)
                return
            errors: dict[str, str] = {}
            if self._has_server(target):
                if self.host is not None and not self._host_started:
                    try:
                        self.host.start(start_server=True)
                        self._host_started = True
                    except Exception as exc:
                        errors["server"] = str(exc)[:256]
            elif self._host_started and self.host is not None:
                if self.host is not None:
                    self.host.expire_pending_pairings()
                    self.host.stop()
                self._host_started = False
            if self._has_client(target):
                if self.client is not None and not self._client_started:
                    self.client.start()
                    self._client_started = True
            elif self._client_started and self.client is not None:
                self.client.stop()
                self._client_started = False
            if self.client is not None:
                def commit(state: dict[str, Any]) -> None:
                    state["device_mode"] = target
                    state["setup_complete"] = True
                    state["mode_transition"] = None
                    state["last_service_errors"] = errors
                self.client.store.mutate(commit)
        except Exception as exc:
            # The requested role is still published only after the old role is
            # stopped.  A startup error is partial service state, not a reason
            # to roll back a completed local transition.
            if self.client is not None:
                self._update_transition(transition_id, state="failed", reason=str(exc)[:256], errors={"transition": str(exc)[:256]})
                reason = str(exc)[:256]
                self.client.store.mutate(lambda state: (
                    state.__setitem__("mode_transition", None),
                    state.setdefault("last_service_errors", {}).__setitem__("transition", reason),
                ))

    def cancel_mode_change(self) -> dict[str, Any]:
        if self.client is None:
            return {"cancelled": False, "reason": self._recovery_error or "client state is unavailable"}
        transition = self._state().get("mode_transition")
        if not isinstance(transition, dict):
            return {"cancelled": False, "reason": "No mode change is in progress"}
        self._transition_cancel.set()
        self._update_transition(transition.get("id", ""), cancel_requested=True, reason="Cancelling mode change")
        return {"cancelled": True, "mode": self.mode(), "transition": self._state().get("mode_transition")}

    def dismiss_upgrade_notice(self) -> dict[str, Any]:
        if self.client is None:
            return {"dismissed": False, "reason": self._recovery_error or "client state is unavailable"}
        self.client.dismiss_upgrade_notice()
        return {"dismissed": True}

    def _clear_transition(self, transition_id: str) -> None:
        if self.client is not None:
            def clear(state: dict[str, Any]) -> None:
                transition = state.get("mode_transition")
                if isinstance(transition, dict) and transition.get("id") == transition_id:
                    state["mode_transition"] = None
            self.client.store.mutate(clear)

    # ---- role guards and delegated host RPCs -------------------------

    def _role_mode(self) -> str | None:
        """Use the requested role while a transition is committing."""
        transition = self._state().get("mode_transition")
        if isinstance(transition, dict) and transition.get("requested") in MODES:
            return transition["requested"]
        return self.mode()

    def _require_server(self) -> HostService:
        if not self._has_server(self._role_mode()) or self.host is None:
            raise CoordinatorError("This device is not accepting remote connections in Client mode", "server_role_required")
        return self.host

    def _require_client(self) -> ClientService:
        if not self._has_client(self._role_mode()) or self.client is None:
            raise CoordinatorError("Client mode is not enabled", "client_role_required")
        return self.client

    def _require_local_display(self) -> HostService:
        """Return the local display service in any configured device role."""
        if self.host is None:
            raise CoordinatorError(self._recovery_error or "host state is unavailable", "state_recovery_required")
        if self._role_mode() not in MODES:
            raise CoordinatorError("Choose a device mode before changing local displays", "device_mode_required")
        return self.host

    def update_settings(self, changes: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(changes, dict):
            raise CoordinatorError("settings must be an object", "invalid_request")
        if "changes" in changes and isinstance(changes["changes"], dict):
            changes = changes["changes"]
        if "client_name" in changes:
            if self.client is None:
                raise CoordinatorError(self._recovery_error or "client state is unavailable", "state_recovery_required")
            # The chooser saves the name in the same transaction as the first
            # role, so Client mode is not required to be active yet.
            self.client.set_client_name(changes["client_name"])
        if "show_nonstandard_display_modes" in changes:
            if self.client is None:
                raise CoordinatorError(self._recovery_error or "client state is unavailable", "state_recovery_required")
            self.client.set_display_preferences(changes["show_nonstandard_display_modes"])
        if "device_mode" in changes or "mode" in changes:
            await_mode = changes.get("device_mode", changes.get("mode"))
            result = self.set_mode(await_mode, client_name=changes.get("client_name"))
        else:
            result = self.get_settings()
        server_keys = {"listen_enabled", "listen_address", "listen_port", "advertised_host", "monitor_sunshine", "auto_recover_sunshine"}
        server_changes = {key: changes[key] for key in server_keys if key in changes}
        if server_changes:
            # Listener configuration is local settings and may be edited before
            # enabling Server; the actual listener still requires the role.
            if self.host is None:
                raise CoordinatorError(self._recovery_error or "host state is unavailable", "state_recovery_required")
            self.host.update_settings(server_changes)
            result = self.get_settings()
        return result

    # Host-side pairing/settings methods.
    def create_pairing(self, requested_scopes: Any = None) -> dict[str, Any]:
        return self._require_server().create_pairing(requested_scopes)

    def create_pairing_code(self, requested_scopes: Any = None) -> dict[str, Any]:
        return self._require_server().create_pairing_code(requested_scopes)

    def list_pairings(self) -> list[dict[str, Any]]:
        return self._require_server().list_pairings()

    def approve_pairing(self, pairing_id: str, scopes: Any = None) -> dict[str, Any]:
        return self._require_server().approve_pairing(pairing_id, scopes)

    def reject_pairing(self, pairing_id: str) -> dict[str, Any]:
        return self._require_server().reject_pairing(pairing_id)

    def revoke_client(self, client_id: str) -> dict[str, Any]:
        return self._require_server().revoke_client(client_id)

    def set_sunshine_provider(self, provider: Any) -> dict[str, Any]:
        return self._require_server().set_sunshine_provider(provider)

    def report_sunshine_owner(self, report: Any) -> dict[str, Any]:
        return self._require_server().report_sunshine_owner(report)

    def next_bridge_command(self) -> dict[str, Any] | None:
        return self._require_server().next_bridge_command()

    def report_bridge_result(self, command_id: str, result: dict[str, Any]) -> dict[str, Any]:
        return self._require_server().report_bridge_result(command_id, result)

    def report_bridge_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        return self._require_server().report_bridge_snapshot(snapshot)

    def local_display_outputs(self) -> dict[str, Any]:
        return self._require_server().local_display_outputs()

    def local_display_preview(self, output_key: str, generation: int) -> dict[str, Any]:
        return self._require_server().local_display_preview(output_key, generation)

    def local_display_confirm(self, preview_id: str) -> dict[str, Any]:
        return self._require_server().local_display_confirm(preview_id)

    def local_display_revert(self, preview_id: str) -> dict[str, Any]:
        return self._require_server().local_display_revert(preview_id)

    def local_gamescope_output(self, output_key: str) -> dict[str, Any]:
        return self._require_server().local_gamescope_output(output_key)

    def local_gamescope_outputs(self, output_keys: list[str], generation: int, restart: bool = False) -> dict[str, Any]:
        return self._require_server().local_gamescope_outputs(output_keys, generation, restart)

    def local_clear_gamescope_output(self) -> dict[str, Any]:
        return self._require_server().local_clear_gamescope_output()

    def local_gamescope_restart(self) -> dict[str, Any]:
        return self._require_server().local_gamescope_restart()

    # Compatibility aliases for the v0.5.7 frontend/backend method names.
    def local_preferred_monitor(self, output_key: str) -> dict[str, Any]:
        return self.local_gamescope_output(output_key)

    def local_clear_preferred_monitor(self) -> dict[str, Any]:
        return self.local_clear_gamescope_output()

    def local_sunshine_restart(self) -> dict[str, Any]:
        return self._require_server().local_sunshine_restart()

    # Local Gaming Mode output ordering is available even in Client mode. It
    # operates on this handheld's DRM/Gamescope state and never targets the
    # paired remote device.
    def local_display_order(self) -> dict[str, Any]:
        return self._require_local_display().display_order("decky-local")

    def local_display_order_save(self, output_keys: list[str], generation: int, restart: bool = False) -> dict[str, Any]:
        return self._require_local_display().local_gamescope_outputs(output_keys, generation, restart)

    def local_display_order_reset(self) -> dict[str, Any]:
        return self._require_local_display().local_clear_gamescope_output()

    # Client-side RPCs.
    def discover_remote_devices(self, port: int = 18443, endpoints: list[str] | None = None) -> list[dict[str, Any]]:
        return self._require_client().discover(port=port, endpoints=endpoints)

    def begin_discovery(self, port: int = 18443, endpoints: list[str] | None = None) -> dict[str, Any]:
        return self._require_client().begin_discovery(port=port, endpoints=endpoints)

    def poll_discovery(self, scan_id: str) -> dict[str, Any]:
        return self._require_client().poll_discovery(scan_id)

    def cancel_discovery(self, scan_id: str) -> dict[str, Any]:
        return self._require_client().cancel_discovery(scan_id)

    def check_remote_device(self, host: str, port: int = 18443) -> dict[str, Any]:
        return self._require_client().check_device(host, port)

    def request_remote_pairing(self, candidate: dict[str, Any], requested_scopes: Any = None, replace_existing: bool = False) -> dict[str, Any]:
        return self._require_client().request_pairing(candidate, requested_scopes, replace_existing=replace_existing)

    def poll_remote_pairing(self, pending_id: str | None = None) -> dict[str, Any]:
        return self._require_client().poll_pairing(pending_id)

    def cancel_remote_pairing(self, pending_id: str | None = None) -> dict[str, Any]:
        return self._require_client().cancel_pairing(pending_id)

    def use_staged_remote(self, use: bool) -> dict[str, Any]:
        return self._require_client().use_staged_remote(use)

    def remote_status(self) -> dict[str, Any]:
        return self._require_client().read_status()

    def remote_outputs(self) -> dict[str, Any]:
        return self._require_client().read_outputs()

    def remote_display_order(self) -> dict[str, Any]:
        return self._require_client().read_display_order()

    def remote_display_order_save(self, output_keys: list[str], generation: int, restart: bool = False) -> dict[str, Any]:
        return self._require_client().save_display_order(output_keys, generation, restart=restart)

    def remote_display_order_reset(self) -> dict[str, Any]:
        return self._require_client().reset_display_order()

    def remote_action_availability(
        self,
        action: str,
        output_id: str | None = None,
        mode_id: str | None = None,
        output_keys: list[str] | None = None,
        generation: int | None = None,
    ) -> dict[str, Any]:
        return self._require_client().action_availability(
            action,
            output_id=output_id,
            mode_id=mode_id,
            output_keys=output_keys,
            generation=generation,
        )

    def remote_power(self, action: str) -> dict[str, Any]:
        return self._require_client().power(action)

    def remote_preview(self, output_id: str, mode_id: str, generation: int) -> dict[str, Any]:
        return self._require_client().preview(output_id, mode_id, generation)

    def remote_confirm_preview(self, preview_id: str, visible: bool = True) -> dict[str, Any]:
        return self._require_client().confirm_preview(preview_id, visible=visible)

    def remote_restore(self, source: str = "verified", profile_id: str | None = None) -> dict[str, Any]:
        return self._require_client().restore(source=source, profile_id=profile_id)

    def remote_save_current(self, output_id: str, generation: int) -> dict[str, Any]:
        return self._require_client().save_current(output_id, generation)

    def remote_sunshine_restart(self) -> dict[str, Any]:
        return self._require_client().sunshine_restart()

    def check_remote_operation(self, action_id: str) -> dict[str, Any]:
        return self._require_client().check_operation(action_id)

    def resend_remote_operation(self, action_id: str, acknowledge_earlier_may_have_run: bool = False) -> dict[str, Any]:
        return self._require_client().resend_action(action_id, acknowledge_earlier_may_have_run=acknowledge_earlier_may_have_run)

    def wake_remote(self) -> dict[str, Any]:
        return self._require_client().wake()

    def client_diagnostics(self) -> dict[str, Any]:
        return self._require_client().diagnostics()

    def rename_remote(self, alias: str) -> dict[str, Any]:
        return self._require_client().rename_remote(alias)

    def update_remote_endpoint(self, candidate: dict[str, Any]) -> dict[str, Any]:
        return self._require_client().update_remote_endpoint(candidate)

    def forget_remote(self, revoke: bool = False) -> dict[str, Any]:
        return self._require_client().forget_remote(revoke=revoke)


Coordinator = DeviceCoordinator
