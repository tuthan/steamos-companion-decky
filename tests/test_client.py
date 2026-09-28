from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from unittest import mock

from host.backend.bridge import BridgeBroker
from host.backend.client import BoundedWorkQueue, ClientService
from host.backend.client_core import ClientError, body_digest
from host.backend.coordinator import DeviceCoordinator
from host.backend.service import HostService


FINGERPRINT = "sha256:" + "a" * 64
REMOTE_CANDIDATE = {
    "endpoint": "https://192.168.50.42:18443",
    "host_id": "remote-host",
    "certificate_fingerprint": FINGERPRINT,
    "name": "Living room Deck",
}


def bridge_snapshot(current: str = "3352", generation: int = 4) -> dict:
    return {
        "ready": True,
        "methods": {"suspend": True, "restart": True, "shutdown": True, "display": True},
        "outputs": [{
            "id": "1",
            "name": "HDMI-A-1",
            "description": "Reference",
            "is_internal": False,
            "current_mode_id": current,
            "modes": [
                {"id": "3352", "width": 3440, "height": 1440, "refresh_hz": 59},
                {"id": "2", "width": 3840, "height": 2160, "refresh_hz": 60},
            ],
            "generation": generation,
            "rgb_range": 0,
        }],
        "cpu_temperature": None,
    }


class FakeRemoteCore:
    def __init__(self):
        self.approved = False
        self.cancelled = False
        self.rejected = False
        self.unknown_mutation = False
        self.mutations: list[tuple[str, dict]] = []
        self.revoke_error = False
        self.revocations = 0

    def revoke_self(self, request_id):
        self.revocations += 1
        if self.revoke_error:
            raise ClientError("device could not be reached", "network_error")
        return {"operation": {"state": "succeeded", "outcome": "revoked"}}

    def pairing_request(self, nonce, client_id, client_name, scopes, *, pairing_session=None, first_request=False):
        if first_request:
            return {
                "state": "pending",
                "pairing_id": "pair-remote-1",
                "pairing_session": "pair-session-1",
                "expires_at": time.time() + 120,
            }
        if self.cancelled:
            return {"state": "cancelled", "pairing_id": "pair-remote-1"}
        if self.rejected:
            return {"state": "rejected", "pairing_id": "pair-remote-1"}
        if not self.approved:
            return {"state": "pending", "pairing_id": "pair-remote-1", "expires_at": time.time() + 120}
        return {
            "state": "approved",
            "pairing_id": "pair-remote-1",
            "credential": {
                "token": "remote-secret-token",
                "scopes": list(scopes),
            },
            "wake_target": {"available": False, "reason": "not configured"},
        }

    def cancel_pairing(self, pairing_id, nonce, client_id, *, pairing_session=None):
        self.cancelled = True
        return {"state": "cancelled", "pairing_id": pairing_id}

    def status(self):
        return {
            "protocol_version": 1,
            "host_id": "remote-host",
            "steam_bridge": "ready",
            "capabilities": {
                "suspend": "available",
                "restart": "available",
                "shutdown": "available",
                "display_rescue": "available",
                "sunshine_restart": "disabled",
            },
            "wake_target": {"available": False},
        }

    def outputs(self):
        return {
            "available": True,
            "generation": 4,
            "outputs": bridge_snapshot()["outputs"],
            "profiles": [],
            "preview": None,
        }

    def display_order(self):
        return {
            "protocol_version": 1,
            "display_order": {
                "available": True,
                "generation": 4,
                "observed_at": "2026-09-16T00:00:00Z",
                "output_keys": ["drm:card0:DP-1", "drm:card0:HDMI-A-2"],
                "outputs": [
                    {"output_key": "drm:card0:DP-1", "display_name": "Desk monitor", "connector": "DP-1", "connected": True, "active": True},
                    {"output_key": "drm:card0:HDMI-A-2", "display_name": "Living room TV", "connector": "HDMI-A-2", "connected": True, "active": False},
                ],
                "saved_output_keys": [],
                "restart_required": True,
                "restart_available": True,
                "adapter": "gamescope-session-prefer-output",
                "unsupported": False,
                "stale": False,
                "ambiguous": False,
                "previous_reading": False,
                "reason": None,
            },
        }

    def operation(self, operation_id):
        return {"operation": {"id": operation_id, "state": "succeeded", "outcome": "observed"}}

    def mutation(self, path, body):
        self.mutations.append((path, dict(body)))
        if self.unknown_mutation:
            raise ClientError("the request result is unknown", "network_error", unknown=True)
        return {"operation": {"id": "remote-op-%d" % len(self.mutations), "state": "accepted", "outcome": None}}


class ClientTests(unittest.TestCase):
    def test_display_mode_preference_defaults_to_all_and_survives_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            service = ClientService(directory)
            try:
                self.assertTrue(service.public_status()["show_nonstandard_display_modes"])
                with self.assertRaises(ClientError) as invalid:
                    service.set_display_preferences("yes")
                self.assertEqual(invalid.exception.code, "invalid_display_preferences")
                hidden = service.set_display_preferences(False)
                self.assertFalse(hidden["show_nonstandard_display_modes"])
            finally:
                service.stop()

            restored = ClientService(directory)
            try:
                self.assertFalse(restored.public_status()["show_nonstandard_display_modes"])
            finally:
                restored.stop()

    def test_upgrade_reveals_modes_hidden_by_old_default(self):
        with tempfile.TemporaryDirectory() as directory:
            service = ClientService(directory)
            service.store.mutate(lambda state: state.update(schema_version=1, show_nonstandard_display_modes=False))
            service.stop()
            upgraded = ClientService(directory)
            try:
                self.assertTrue(upgraded.public_status()["show_nonstandard_display_modes"])
            finally:
                upgraded.stop()

    def test_discard_staged_pairing_revokes_new_host_and_keeps_old_host(self):
        with tempfile.TemporaryDirectory() as directory:
            core = FakeRemoteCore()
            service = ClientService(directory, core_factory=lambda *args, **kwargs: core)
            old = {**REMOTE_CANDIDATE, "host_id": "old-host", "token": "old-secret"}
            staged = {**REMOTE_CANDIDATE, "host_id": "new-host", "token": "new-secret"}
            service.store.mutate(lambda state: state.update(remote=old, staged_remote=staged))
            try:
                result = service.use_staged_remote(False)
                self.assertEqual(core.revocations, 1)
                self.assertIn("removed", result["server_cleanup"])
                self.assertEqual(service.store.get("remote")["token"], "old-secret")
                self.assertIsNone(service.store.get("staged_remote"))

                core.revoke_error = True
                service.store.mutate(lambda state: state.__setitem__("staged_remote", staged))
                result = service.use_staged_remote(False)
                self.assertIn("Could not confirm", result["server_cleanup"])
                self.assertEqual(service.store.get("remote")["token"], "old-secret")
                self.assertIsNone(service.store.get("staged_remote"))
            finally:
                service.stop()

    def test_revoke_keeps_local_pairing_until_host_confirms(self):
        with tempfile.TemporaryDirectory() as directory:
            core = FakeRemoteCore()
            service = ClientService(directory, core_factory=lambda *args, **kwargs: core)
            service.store.mutate(lambda state: state.__setitem__("remote", {**REMOTE_CANDIDATE, "token": "secret"}))
            try:
                core.revoke_error = True
                with self.assertRaises(ClientError):
                    service.forget_remote(revoke=True)
                self.assertIsNotNone(service.store.get("remote"))
                core.revoke_error = False
                self.assertTrue(service.forget_remote(revoke=True)["revoked"])
                self.assertIsNone(service.store.get("remote"))
            finally:
                service.stop()

    def test_diagnostics_omits_secrets_and_untrusted_error_text(self):
        with tempfile.TemporaryDirectory() as directory:
            service = ClientService(directory)
            secret = "seeded-bearer-and-qr-secret"
            service.store.mutate(lambda state: state.update(
                remote={**REMOTE_CANDIDATE, "token": secret, "last_error": secret, "name": secret,
                        "outputs": [{"id": "1"}], "profiles": []},
                operations={"one": {"action": "restore", "state": "failed", "reason": secret,
                                    "body": {"token": secret}, "updated_at": "2026-09-28T00:00:00Z"}},
            ))
            try:
                report = service.diagnostics()
                self.assertEqual(report["display_output_count"], 1)
                self.assertEqual(report["operations"][0]["action"], "restore")
                self.assertNotIn(secret, json.dumps(report))
                self.assertLess(len(json.dumps(report)), 8192)
            finally:
                service.stop()

    def test_verified_restore_is_available_only_during_owned_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            service = ClientService(directory)
            remote = {**REMOTE_CANDIDATE, "token": "secret", "scopes": ["display.control"],
                      "status": {"capabilities": {"display_rescue": "available"}},
                      "profiles": [{"id": "saved"}], "preview": {"preview_id": "preview-1"}}
            preview_action = {"action": "preview", "state": "accepted", "remote_operation_id": "preview-1",
                              "target": {"host_id": "remote-host"}}
            service.store.mutate(lambda state: state.update(remote=remote, operations={"one": preview_action}))
            try:
                self.assertTrue(service.action_availability("restore")["available"])
                self.assertFalse(service.action_availability("save_current")["available"])
                service.store.mutate(lambda state: state["operations"]["one"].__setitem__("remote_operation_id", "other-preview"))
                self.assertFalse(service.action_availability("restore")["available"])
            finally:
                service.stop()

    def test_failed_wake_replaces_the_previous_sent_packet_fact(self):
        with tempfile.TemporaryDirectory() as directory:
            service = ClientService(directory)
            service.store.mutate(lambda state: state.__setitem__("remote", {
                **REMOTE_CANDIDATE,
                "wake_target": {"available": True, "mac": "001122334455"},
            }))
            try:
                with mock.patch("host.backend.client.active_route_ipv4", return_value="192.168.50.10"), mock.patch(
                    "host.backend.client.send_wake_packet",
                    side_effect=[{"destinations": ["192.168.50.255"]}, ClientError("packet send failed", "network_error")],
                ):
                    self.assertTrue(service.wake()["sent"])
                    self.assertEqual(service.public_status()["wake"]["state"], "sent")
                    with self.assertRaises(ClientError):
                        service.wake()
                self.assertEqual(service.public_status()["wake"]["state"], "failed")
                self.assertEqual(service.public_status()["remote"]["wake"]["state"], "failed")
            finally:
                service.stop()

    def test_bounded_queue_deduplicates_reads_and_settles_rejected_mutations(self):
        queue = BoundedWorkQueue(max_workers=1, max_pending=2)
        started = threading.Event()
        release = threading.Event()
        settled: list[dict] = []
        try:
            def slow_read():
                started.set()
                release.wait(1)
                return "read"

            first = queue.submit_read("status", slow_read)
            self.assertTrue(started.wait(1))
            duplicate = queue.submit_read("status", lambda: "not used")
            self.assertIs(first, duplicate)
            queued = queue.submit_read("outputs", lambda: "outputs")
            rejected = queue.submit_mutation("power", lambda: "not used", on_settled=settled.append)
            with self.assertRaises(ClientError) as error:
                rejected.result()
            self.assertEqual(error.exception.code, "work_queue_full")
            self.assertTrue(settled)
            release.set()
            self.assertEqual(first.result(1), "read")
            self.assertEqual(queued.result(1), "outputs")
        finally:
            release.set()
            queue.shutdown()

    def test_pending_pairing_survives_reload_without_leaking_private_material(self):
        with tempfile.TemporaryDirectory() as directory:
            core = FakeRemoteCore()
            factory = lambda *args, **kwargs: core
            service = ClientService(
                directory,
                local_host_id="local-host",
                local_certificate_fingerprint="sha256:" + "b" * 64,
                core_factory=factory,
            )
            service.start()
            try:
                pending = service.request_pairing(REMOTE_CANDIDATE)
                self.assertEqual(pending["status"], "pending")
                self.assertNotIn("nonce", pending)
                self.assertNotIn("pairing_session", pending)
                private_pending = service.store.get("pending_pairing")
                self.assertTrue(private_pending["nonce"])
                pending_id = pending["id"]
            finally:
                service.stop()

            restored = ClientService(
                directory,
                local_host_id="local-host",
                local_certificate_fingerprint="sha256:" + "b" * 64,
                core_factory=factory,
            )
            restored.start()
            try:
                self.assertEqual(restored.public_status()["pending_pairing"]["id"], pending_id)
                core.approved = True
                approved = restored.poll_pairing(pending_id)
                self.assertEqual(approved["state"], "approved")
                self.assertFalse(approved["needs_confirmation"])
                public = restored.public_status()
                self.assertEqual(public["remote"]["host_id"], "remote-host")
                self.assertNotIn("token", public["remote"])
                self.assertNotIn("remote-secret-token", json.dumps(public))
            finally:
                restored.stop()

    def test_cancel_pairing_waits_for_remote_ack_before_clearing_local_state(self):
        with tempfile.TemporaryDirectory() as directory:
            core = FakeRemoteCore()
            service = ClientService(directory, core_factory=lambda *args, **kwargs: core)
            service.start()
            try:
                pending = service.request_pairing(REMOTE_CANDIDATE)
                cancelled = service.cancel_pairing(pending["id"])
                self.assertTrue(cancelled["cancelled"])
                self.assertTrue(cancelled["acknowledged"])
                self.assertEqual(cancelled["state"], "cancelled")
                self.assertIsNone(service.public_status()["pending_pairing"])
                self.assertTrue(core.cancelled)
            finally:
                service.stop()

    def test_rejected_pairing_is_kept_as_an_explicit_terminal_result(self):
        with tempfile.TemporaryDirectory() as directory:
            core = FakeRemoteCore()
            service = ClientService(directory, core_factory=lambda *args, **kwargs: core)
            service.start()
            try:
                pending = service.request_pairing(REMOTE_CANDIDATE)
                core.rejected = True
                rejected = service.poll_pairing(pending["id"])
                self.assertEqual(rejected["status"], "rejected")
                self.assertEqual(rejected["last_error"], "Pairing was declined on the other device.")
            finally:
                service.stop()

    def test_discovery_deduplicates_identity_and_reports_scan_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            candidates = [
                REMOTE_CANDIDATE,
                dict(REMOTE_CANDIDATE),
                {**REMOTE_CANDIDATE, "host_id": "local-host"},
                {**REMOTE_CANDIDATE, "certificate_fingerprint": "not-a-pin"},
            ]
            service = ClientService(
                directory,
                local_host_id="local-host",
                local_certificate_fingerprint="sha256:" + "b" * 64,
                discovery_function=lambda **kwargs: candidates,
            )
            service.start()
            try:
                found = service.discover()
                self.assertEqual(len(found), 1)
                self.assertEqual(found[0]["host_id"], "remote-host")
            finally:
                service.stop()

            failed = ClientService(
                directory + "-failed",
                discovery_function=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("scan failed")),
            )
            failed.start()
            try:
                scan = failed.begin_discovery()
                deadline = time.time() + 1
                result = None
                while time.time() < deadline:
                    result = failed.poll_discovery(scan["scan_id"])
                    if result["state"] != "searching":
                        break
                    time.sleep(0.01)
                self.assertEqual(result["state"], "failed")
                self.assertIn("scan failed", result["error"])
            finally:
                failed.stop()

    def test_client_action_persists_exact_body_and_requires_explicit_unknown_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            core = FakeRemoteCore()
            service = ClientService(directory, core_factory=lambda *args, **kwargs: core)
            service.store.mutate(lambda state: state.__setitem__("remote", {
                **REMOTE_CANDIDATE,
                "token": "remote-secret-token",
                "scopes": ["status.read", "power.control", "display.control"],
                "status": None,
                "outputs": [],
                "profiles": [],
                "preview": None,
            }))
            service.start()
            try:
                service.read_status()
                accepted = service.power("suspend")
                self.assertEqual(accepted["state"], "accepted")
                action_id = accepted["id"]
                action = service.store.get("operations", {})[action_id]
                self.assertEqual(core.mutations[0][0], "/v1/power")
                self.assertEqual(core.mutations[0][1], action["body"])
                self.assertEqual(action["body_digest"], body_digest(action["body"]))

                checked = service.check_operation(action_id)
                self.assertEqual(checked["state"], "succeeded")
                self.assertTrue(checked["read_only"])

                core.unknown_mutation = True
                with self.assertRaises(ClientError) as unknown:
                    service.power("restart")
                self.assertTrue(unknown.exception.unknown)
                unknown_action = service.public_status()["last_action"]
                self.assertEqual(unknown_action["state"], "unknown")
                self.assertTrue(unknown_action["retry_allowed"])
                with self.assertRaises(ClientError) as ack:
                    service.resend_action(unknown_action["id"])
                self.assertEqual(ack.exception.code, "resend_ack_required")

                core.unknown_mutation = False
                resent = service.resend_action(unknown_action["id"], acknowledge_earlier_may_have_run=True)
                self.assertEqual(resent["state"], "accepted")
                self.assertEqual(core.mutations[-1][1], service.store.get("operations", {})[unknown_action["id"]]["body"])
                public_json = json.dumps(service.public_status())
                self.assertNotIn("remote-secret-token", public_json)
                self.assertNotIn('"body"', public_json)
            finally:
                service.stop()

    def test_client_controls_remote_display_order_with_persisted_operations(self):
        with tempfile.TemporaryDirectory() as directory:
            core = FakeRemoteCore()
            service = ClientService(directory, core_factory=lambda *args, **kwargs: core)
            service.store.mutate(lambda state: state.__setitem__("remote", {
                **REMOTE_CANDIDATE,
                "token": "remote-secret-token",
                "scopes": ["status.read", "power.control", "display.control"],
                "status": bridge_snapshot(),
                "outputs": [],
                "display_order": None,
                "profiles": [],
                "preview": None,
            }))
            service.start()
            try:
                order = service.read_display_order()
                self.assertTrue(order["display_order"]["available"])
                self.assertEqual(service.public_status()["remote"]["display_order"]["generation"], 4)

                saved = service.save_display_order(
                    ["drm:card0:HDMI-A-2", "drm:card0:DP-1"],
                    4,
                    restart=True,
                )
                self.assertEqual(saved["state"], "accepted")
                self.assertEqual(core.mutations[-1], (
                    "/v1/display/order",
                    {
                        "request_id": service.store.get("operations", {})[saved["id"]]["request_id"],
                        "output_keys": ["drm:card0:HDMI-A-2", "drm:card0:DP-1"],
                        "generation": 4,
                        "restart": True,
                    },
                ))
                service.check_operation(saved["id"])

                reset = service.reset_display_order()
                self.assertEqual(reset["state"], "accepted")
                self.assertEqual(core.mutations[-1][0], "/v1/display/order/automatic")
            finally:
                service.stop()

    def test_reload_turns_an_unsettled_send_into_an_explicit_unknown_result(self):
        with tempfile.TemporaryDirectory() as directory:
            service = ClientService(directory)
            service.store.mutate(lambda state: state.__setitem__("remote", {
                **REMOTE_CANDIDATE,
                "token": "remote-secret-token",
                "scopes": ["status.read", "power.control"],
            }))
            action = service._new_action("restart", "/v1/power", {"action": "restart"})
            service.stop()

            restored = ClientService(directory)
            try:
                visible = restored.public_status()["last_action"]
                self.assertEqual(visible["id"], action["id"])
                self.assertEqual(visible["state"], "unknown")
                self.assertTrue(visible["retry_allowed"])
                self.assertIn("reloaded", visible["reason"])
            finally:
                restored.stop()

    def test_save_current_requires_owner_confirmation_and_verified_readback(self):
        temp = tempfile.TemporaryDirectory()
        bridge = BridgeBroker()
        bridge.report_snapshot(bridge_snapshot())
        service = HostService(temp.name, bridge=bridge)
        service._tls = {"ready": True, "fingerprint": FINGERPRINT}
        try:
            created = service.create_pairing(["status.read", "display.control"])
            from host.backend.pairing import decode_payload

            payload = decode_payload(created["payload"])
            request = {
                "pairing_id": payload["pairing_id"],
                "secret": payload["secret"],
                "client_name": "Client",
                "client_id": "client-save-current",
                "scopes": ["status.read", "display.control"],
            }
            service.handle_http("POST", "/v1/pair/request", {}, request)
            service.approve_pairing(payload["pairing_id"])
            credential = service.handle_http("POST", "/v1/pair/request", {}, request)[2]["credential"]
            headers = {"Authorization": "Bearer " + credential["token"]}
            body = {"request_id": "save-current-1", "output_id": "1", "generation": 4, "visible": True}
            with self.assertRaises(Exception) as not_confirmed:
                service.handle_http("POST", "/v1/display/save-current", headers, {**body, "visible": False})
            self.assertEqual(not_confirmed.exception.code, "invalid_request")
            saved = service.handle_http("POST", "/v1/display/save-current", headers, body)[2]
            self.assertEqual(saved["operation"]["state"], "succeeded")
            self.assertEqual(saved["profile"]["mode"]["id"], "3352")
            self.assertEqual(len(service.store.get("profiles", {})), 1)

            service.store.mutate(lambda state: state.__setitem__("preview", {"preview_id": "preview-1"}))
            with self.assertRaises(Exception) as preview_active:
                service.handle_http("POST", "/v1/display/save-current", headers, {**body, "request_id": "save-current-2"})
            self.assertEqual(preview_active.exception.code, "mutation_conflict")
        finally:
            service.stop()
            temp.cleanup()

    def test_coordinator_switches_roles_without_replacing_host_state(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator = DeviceCoordinator(directory)
            self.assertFalse(coordinator.get_settings()["setup_complete"])
            self.assertIsNotNone(coordinator.host)
            coordinator.host.update_settings({"listen_enabled": False})
            coordinator.start()

            def wait_for(mode: str):
                deadline = time.time() + 2
                while time.time() < deadline:
                    status = coordinator.get_settings()
                    if status["mode"]["selected"] == mode and status["mode"]["transition"] is None:
                        return status
                    time.sleep(0.01)
                self.fail("mode transition did not finish: %r" % coordinator.get_settings())

            coordinator.set_mode("client", client_name="Remote controller")
            client_status = wait_for("client")
            self.assertTrue(client_status["roles"]["client"]["running"])
            self.assertFalse(client_status["roles"]["server"]["running"])
            local_order = coordinator.local_display_order()
            self.assertIn("display_order", local_order)
            host_id = coordinator.host.host_id

            coordinator.set_mode("both")
            both_status = wait_for("both")
            self.assertTrue(both_status["roles"]["client"]["running"])
            self.assertTrue(both_status["roles"]["server"]["running"])

            coordinator.set_mode("server")
            server_status = wait_for("server")
            self.assertFalse(server_status["roles"]["client"]["running"])
            self.assertTrue(server_status["roles"]["server"]["running"])
            self.assertEqual(coordinator.host.host_id, host_id)

            coordinator.set_mode("client")
            final_status = wait_for("client")
            self.assertTrue(final_status["roles"]["client"]["running"])
            self.assertFalse(final_status["roles"]["server"]["running"])
            coordinator.stop()


if __name__ == "__main__":
    unittest.main()
