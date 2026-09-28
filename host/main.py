"""Decky entry point for the SteamOS Companion host plugin."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import decky


def _log(level: str, message: str, *args) -> None:
    """Write diagnostics to Decky's plugin log without breaking test imports."""
    logger = getattr(decky, "logger", None)
    method = getattr(logger, level, None)
    if not callable(method):
        return
    try:
        method(message, *args)
    except Exception:
        # Logging must never prevent the plugin from loading or serving RPCs.
        pass


# Decky's sandbox does not always add the installed plugin directory to
# sys.path. Make sibling packages such as backend importable regardless of
# the loader's current working directory.
PLUGIN_DIR = Path(__file__).resolve().parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))


try:
    from backend import __version__ as BACKEND_VERSION
    from backend.coordinator import DeviceCoordinator
except Exception:
    _log("exception", "SteamOS Companion failed to import backend.service")
    raise


def _state_root() -> Path:
    for name in ("DECKY_PLUGIN_SETTINGS_DIR", "DECKY_PLUGIN_RUNTIME_DIR", "DECKY_PLUGIN_DATA_DIR"):
        value = getattr(decky, name, None)
        if value:
            return Path(value) / "steamos-companion"
    return Path("/tmp") / "steamos-companion-decky-state"


def _build_service() -> DeviceCoordinator:
    root = _state_root()
    version = getattr(decky, "DECKY_PLUGIN_VERSION", None) or BACKEND_VERSION
    _log(
        "info",
        "SteamOS Companion loading version=%s coordinator state_root=%s",
        version,
        root,
    )
    try:
        service = DeviceCoordinator(root)
    except Exception:
        _log("exception", "SteamOS Companion failed to construct HostService")
        raise
    _log("info", "SteamOS Companion coordinator loaded host_id=%s mode=%s", getattr(service.host, "host_id", None), service.mode())
    return service


def _with_diagnostics(result):
    if not isinstance(result, dict):
        return result
    enriched = dict(result)
    enriched["diagnostics"] = {
        "version": getattr(decky, "DECKY_PLUGIN_VERSION", None) or BACKEND_VERSION,
        "api_mode": "coordinator",
        "log_path": getattr(decky, "DECKY_PLUGIN_LOG", None),
    }
    return enriched


async def _threaded_call(method: str, function, *args, **kwargs):
    """Run blocking service work while logging failures without request data."""
    try:
        return await asyncio.to_thread(function, *args, **kwargs)
    except Exception:
        _log("exception", "SteamOS Companion RPC failed method=%s", method)
        raise


class Plugin:
    # Keep the class attribute for API-v0 installations that pass the Plugin
    # class as self. API-v1 instantiates this class; the coordinator itself is
    # API-version agnostic and gates host/client lifecycles.
    service = _build_service()

    async def _main(self):
        _log("info", "SteamOS Companion backend startup begin")
        try:
            status = await _threaded_call("startup", self.service.start)
        except Exception:
            _log("exception", "SteamOS Companion backend startup failed")
            return
        listener = status.get("listener", {}) if isinstance(status, dict) else {}
        tls = status.get("tls", {}) if isinstance(status, dict) else {}
        _log(
            "info",
            "SteamOS Companion backend startup complete listener_running=%s listener_error=%s tls_ready=%s",
            listener.get("running"),
            listener.get("error"),
            tls.get("ready"),
        )

    async def get_settings(self):
        result = await _threaded_call("get_settings", self.service.get_settings)
        return _with_diagnostics(result)

    async def update_settings(self, changes):
        return await _threaded_call("update_settings", self.service.update_settings, changes)

    async def set_device_mode(self, mode, client_name=None):
        return await _threaded_call("set_device_mode", self.service.set_mode, mode, client_name=client_name)

    async def cancel_mode_change(self):
        return await _threaded_call("cancel_mode_change", self.service.cancel_mode_change)

    async def dismiss_upgrade_notice(self):
        return await _threaded_call("dismiss_upgrade_notice", self.service.dismiss_upgrade_notice)

    async def create_pairing(self, requested_scopes=None):
        return await _threaded_call("create_pairing", self.service.create_pairing, requested_scopes)

    async def create_pairing_code(self, requested_scopes=None):
        return await _threaded_call("create_pairing_code", self.service.create_pairing_code, requested_scopes)

    async def list_pairings(self):
        return await _threaded_call("list_pairings", self.service.list_pairings)

    async def approve_pairing(self, pairing_id, scopes=None):
        return await _threaded_call("approve_pairing", self.service.approve_pairing, pairing_id, scopes)

    async def reject_pairing(self, pairing_id):
        return await _threaded_call("reject_pairing", self.service.reject_pairing, pairing_id)

    async def revoke_client(self, client_id):
        return await _threaded_call("revoke_client", self.service.revoke_client, client_id)

    async def set_sunshine_provider(self, provider):
        return await _threaded_call("set_sunshine_provider", self.service.set_sunshine_provider, provider)

    async def report_sunshine_owner(self, report):
        return await _threaded_call("report_sunshine_owner", self.service.report_sunshine_owner, report)

    async def next_bridge_command(self):
        return await _threaded_call("next_bridge_command", self.service.next_bridge_command)

    async def report_bridge_result(self, command_id, result):
        return await _threaded_call("report_bridge_result", self.service.report_bridge_result, command_id, result)

    async def report_bridge_snapshot(self, snapshot):
        return await _threaded_call("report_bridge_snapshot", self.service.report_bridge_snapshot, snapshot)

    async def local_display_outputs(self):
        return await _threaded_call("local_display_outputs", self.service.local_display_outputs)

    async def local_display_preview(self, output_key, generation):
        return await _threaded_call("local_display_preview", self.service.local_display_preview, output_key, generation)

    async def local_display_confirm(self, preview_id):
        return await _threaded_call("local_display_confirm", self.service.local_display_confirm, preview_id)

    async def local_display_revert(self, preview_id):
        return await _threaded_call("local_display_revert", self.service.local_display_revert, preview_id)

    async def local_gamescope_output(self, output_key):
        return await _threaded_call("local_gamescope_output", self.service.local_gamescope_output, output_key)

    async def local_gamescope_outputs(self, output_keys, generation, restart=False):
        return await _threaded_call("local_gamescope_outputs", self.service.local_gamescope_outputs, output_keys, generation, restart)

    async def local_clear_gamescope_output(self):
        return await _threaded_call("local_clear_gamescope_output", self.service.local_clear_gamescope_output)

    async def local_gamescope_restart(self):
        return await _threaded_call("local_gamescope_restart", self.service.local_gamescope_restart)

    # Compatibility aliases for a frontend that was loaded before the plugin
    # backend was reloaded to v0.5.13.
    async def local_preferred_monitor(self, output_key):
        return await _threaded_call("local_preferred_monitor", self.service.local_preferred_monitor, output_key)

    async def local_clear_preferred_monitor(self):
        return await _threaded_call("local_clear_preferred_monitor", self.service.local_clear_preferred_monitor)

    async def local_sunshine_restart(self):
        return await _threaded_call("local_sunshine_restart", self.service.local_sunshine_restart)

    async def local_display_order(self):
        return await _threaded_call("local_display_order", self.service.local_display_order)

    async def local_display_order_save(self, output_keys, generation, restart=False):
        return await _threaded_call("local_display_order_save", self.service.local_display_order_save, output_keys, generation, restart)

    async def local_display_order_reset(self):
        return await _threaded_call("local_display_order_reset", self.service.local_display_order_reset)

    # Outgoing client RPCs.  Every network method runs in a bounded worker so
    # a slow remote host cannot block the incoming Steam bridge.
    async def discover_remote_devices(self, port=18443, endpoints=None):
        return await _threaded_call("discover_remote_devices", self.service.discover_remote_devices, port, endpoints)

    async def begin_discovery(self, port=18443, endpoints=None):
        return await _threaded_call("begin_discovery", self.service.begin_discovery, port, endpoints)

    async def poll_discovery(self, scan_id):
        return await _threaded_call("poll_discovery", self.service.poll_discovery, scan_id)

    async def cancel_discovery(self, scan_id):
        return await _threaded_call("cancel_discovery", self.service.cancel_discovery, scan_id)

    async def check_remote_device(self, host, port=18443):
        return await _threaded_call("check_remote_device", self.service.check_remote_device, host, port)

    async def request_remote_pairing(self, candidate, requested_scopes=None, replace_existing=False):
        return await _threaded_call("request_remote_pairing", self.service.request_remote_pairing, candidate, requested_scopes, replace_existing)

    async def poll_remote_pairing(self, pending_id=None):
        return await _threaded_call("poll_remote_pairing", self.service.poll_remote_pairing, pending_id)

    async def cancel_remote_pairing(self, pending_id=None):
        return await _threaded_call("cancel_remote_pairing", self.service.cancel_remote_pairing, pending_id)

    async def use_staged_remote(self, use):
        return await _threaded_call("use_staged_remote", self.service.use_staged_remote, use)

    async def remote_status(self):
        return await _threaded_call("remote_status", self.service.remote_status)

    async def remote_outputs(self):
        return await _threaded_call("remote_outputs", self.service.remote_outputs)

    async def remote_display_order(self):
        return await _threaded_call("remote_display_order", self.service.remote_display_order)

    async def remote_display_order_save(self, output_keys, generation, restart=False):
        return await _threaded_call("remote_display_order_save", self.service.remote_display_order_save, output_keys, generation, restart)

    async def remote_display_order_reset(self):
        return await _threaded_call("remote_display_order_reset", self.service.remote_display_order_reset)

    async def remote_action_availability(self, action, output_id=None, mode_id=None, output_keys=None, generation=None):
        return await _threaded_call("remote_action_availability", self.service.remote_action_availability, action, output_id, mode_id, output_keys, generation)

    async def remote_power(self, action):
        return await _threaded_call("remote_power", self.service.remote_power, action)

    async def remote_preview(self, output_id, mode_id, generation):
        return await _threaded_call("remote_preview", self.service.remote_preview, output_id, mode_id, generation)

    async def remote_confirm_preview(self, preview_id, visible=True):
        return await _threaded_call("remote_confirm_preview", self.service.remote_confirm_preview, preview_id, visible)

    async def remote_restore(self, source="verified", profile_id=None):
        return await _threaded_call("remote_restore", self.service.remote_restore, source, profile_id)

    async def remote_save_current(self, output_id, generation):
        return await _threaded_call("remote_save_current", self.service.remote_save_current, output_id, generation)

    async def remote_sunshine_restart(self):
        return await _threaded_call("remote_sunshine_restart", self.service.remote_sunshine_restart)

    async def check_remote_operation(self, action_id):
        return await _threaded_call("check_remote_operation", self.service.check_remote_operation, action_id)

    async def resend_remote_operation(self, action_id, acknowledge_earlier_may_have_run=False):
        return await _threaded_call("resend_remote_operation", self.service.resend_remote_operation, action_id, acknowledge_earlier_may_have_run)

    async def wake_remote(self):
        return await _threaded_call("wake_remote", self.service.wake_remote)

    async def client_diagnostics(self):
        return await _threaded_call("client_diagnostics", self.service.client_diagnostics)

    async def rename_remote(self, alias):
        return await _threaded_call("rename_remote", self.service.rename_remote, alias)

    async def update_remote_endpoint(self, candidate):
        return await _threaded_call("update_remote_endpoint", self.service.update_remote_endpoint, candidate)

    async def forget_remote(self, revoke=False):
        return await _threaded_call("forget_remote", self.service.forget_remote, revoke)

    async def local_status(self):
        result = await _threaded_call("local_status", self.service.get_local_status)
        return _with_diagnostics(result)

    async def _unload(self):
        _log("info", "SteamOS Companion backend shutdown begin")
        try:
            await _threaded_call("shutdown", self.service.stop)
        finally:
            _log("info", "SteamOS Companion backend shutdown complete")
