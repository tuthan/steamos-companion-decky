from __future__ import annotations

import json
import base64
import http
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from host.backend.bridge import BridgeBroker
from host.backend.coordinator import CoordinatorError, DeviceCoordinator
from host.backend.display import DisplayError, normalize_snapshot, resolve_active_output, resolve_restore_mode, same_mode_profile, same_selection_identity
from host.backend.drm import DrmInventory
from host.backend.gamescope import GamescopeOutputManager
from host.backend.identity import ensure_tls_material, read_cpu_temperature
from host.backend.pairing import decode_payload, derive_pairing_code
from host.backend.provider import BridgeSunshineProvider, DeckySunshineProcessObserver, ProviderError
from host.backend.service import HostService, MAX_CLIENTS, MAX_PAIRING_RECORDS, PAIRING_RETENTION_SECONDS
from host.backend.server import CONNECTION_LIMIT, REQUEST_TIMEOUT, _Handler, _ThreadingHTTPServer, _restore_system_http_package_path
from host.backend.storage import StateError, StateStore


# base64url of 16 fixed bytes; shared with tests/test_protocol.py.
VERIFICATION_NONCE = "AAECAwQFBgcICQoLDA0ODw"
LOCAL_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "local_display"


def snapshot(current="3352", generation=4):
    return {
        "ready": True,
        "methods": {"suspend": True, "restart": True, "shutdown": True, "display": True},
        "outputs": [{
            "id": "1", "name": "HDMI-A-1", "description": "Reference", "is_internal": False,
            "current_mode_id": current,
            "modes": [
                {"id": "3352", "width": 3440, "height": 1440, "refresh_hz": 59},
                {"id": "2", "width": 3840, "height": 2160, "refresh_hz": 60},
                {"id": "0", "width": 1920, "height": 1080, "refresh_hz": 60},
            ],
            "generation": generation, "rgb_range": 0,
        }],
        "cpu_temperature": None,
    }


def local_fixture(name):
    return json.loads((LOCAL_FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def local_switched_snapshot(active_key):
    value = local_fixture("two-screens.json")
    value["active_output_key"] = active_key
    for output in value["outputs"]:
        output["active"] = output["output_key"] == active_key
    return value


def wait_for_bridge_command(bridge, timeout=2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        command = bridge.next_command()
        if command is not None:
            return command
        time.sleep(0.005)
    return None


def wait_for_terminal_operation(service, operation_id, timeout=2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        operation = service.journal.get(operation_id)
        if operation and operation["state"] in {"observed_return", "succeeded", "failed", "unknown"}:
            return operation
        time.sleep(0.01)
    return service.journal.get(operation_id)


class HostTests(unittest.TestCase):
    def make_service(self):
        temp = tempfile.TemporaryDirectory()
        bridge = BridgeBroker()
        bridge.report_snapshot(snapshot())
        drm_root = Path(temp.name) / "drm"
        drm_root.mkdir()
        service = HostService(temp.name, bridge=bridge, drm_root=drm_root)
        service._tls = {"ready": True, "fingerprint": "sha256:" + "a" * 64}
        return temp, service

    def pair(self, service, scopes=None):
        created = service.create_pairing(scopes)
        payload = decode_payload(created["payload"])
        pending = service.handle_http("POST", "/v1/pair/request", {}, {
            "pairing_id": payload["pairing_id"], "secret": payload["secret"],
            "client_name": "Test Omarchy", "client_id": "client-test",
            "scopes": scopes or ["status.read", "power.control", "display.control"],
        })[2]
        self.assertEqual(pending["state"], "pending")
        service.approve_pairing(payload["pairing_id"], scopes)
        approved = service.handle_http("POST", "/v1/pair/request", {}, {
            "pairing_id": payload["pairing_id"], "secret": payload["secret"],
            "client_name": "Test Omarchy", "client_id": "client-test",
            "scopes": scopes or ["status.read", "power.control", "display.control"],
        })[2]
        return approved["credential"]

    def test_pairing_requires_local_approval_and_returns_token_once(self):
        temp, service = self.make_service()
        try:
            created = service.create_pairing()
            with self.assertRaises(Exception) as early:
                service.approve_pairing(created["pairing_id"])
            self.assertEqual(early.exception.status, 409)
            credential = self.pair(service)
            self.assertTrue(credential["token"])
            self.assertEqual(service.store.path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(Exception):
                service.handle_http("POST", "/v1/pair/request", {}, {
                    "pairing_id": service.list_pairings()[0]["pairing_id"], "secret": "wrong",
                    "client_name": "Test", "scopes": ["status.read"],
                })
        finally:
            temp.cleanup()

    def test_short_pairing_code_still_requires_decky_approval_and_is_one_time(self):
        temp, service = self.make_service()
        try:
            created = service.create_pairing_code(["status.read", "power.control"])
            self.assertRegex(created["pairing_code"], r"^[0-9]{4}-[0-9]{4}$")
            self.assertNotIn("payload", created)
            stored = service.store.get("pairings", {})[created["pairing_id"]]
            self.assertNotIn("pairing_code", stored)
            self.assertTrue(stored["pairing_code_hash"])
            with self.assertRaises(Exception) as early:
                service.approve_pairing(created["pairing_id"])
            self.assertEqual(early.exception.status, 409)
            request = {
                "pairing_code": created["pairing_code"],
                "client_name": "Code Client",
                "client_id": "client-code",
                "scopes": ["status.read", "power.control"],
            }
            pending = service.handle_http("POST", "/v1/pair/request", {}, request)[2]
            self.assertEqual(pending["state"], "pending")
            listed = service.get_local_status()["pending_pairings"]
            self.assertEqual(listed[0]["pairing_method"], "code")
            service.approve_pairing(created["pairing_id"])
            approved = service.handle_http("POST", "/v1/pair/request", {}, request)[2]
            self.assertEqual(approved["state"], "approved")
            self.assertEqual(approved["host_id"], service.host_id)
            self.assertEqual(approved["credential"]["host_id"], service.host_id)
            with self.assertRaises(Exception) as consumed:
                service.handle_http("POST", "/v1/pair/request", {}, request)
            self.assertEqual(consumed.exception.status, 404)
        finally:
            service.stop()
            temp.cleanup()

    def test_certificate_bound_verification_code_is_shown_for_visual_approval(self):
        temp, service = self.make_service()
        try:
            request = {
                "verification_nonce": VERIFICATION_NONCE,
                "client_name": "Omarchy Deck",
                "client_id": "client-verification",
                "scopes": ["status.read", "power.control"],
            }
            expected = derive_pairing_code(VERIFICATION_NONCE, service._tls["fingerprint"])
            pending = service.handle_http("POST", "/v1/pair/request", {}, request)[2]
            self.assertEqual(pending["state"], "pending")
            visible = service.get_local_status()["pending_pairings"]
            self.assertEqual(visible[0]["verification_code"], f"{expected[:4]}-{expected[4:]}")
            self.assertEqual(visible[0]["pairing_method"], "verification")
            stored = service.store.get("pairings", {})[pending["pairing_id"]]
            # The nonce itself is never retained in the clear, and the legacy
            # client-chosen code hash is gone.
            self.assertNotIn("verification_code_hash", stored)
            self.assertTrue(stored["verification_nonce_hash"])
            self.assertNotIn(VERIFICATION_NONCE, json.dumps(stored))
            wake_target = {
                "available": True, "mac": "001122334455", "interface": "enp5s0",
                "source_address": "192.168.50.24", "reason": None,
            }
            with mock.patch.object(service, "_wake_target", return_value=wake_target):
                service.approve_pairing(pending["pairing_id"])
                approved = service.handle_http("POST", "/v1/pair/request", {}, request)[2]
            self.assertEqual(approved["state"], "approved")
            self.assertEqual(approved["host_id"], service.host_id)
            self.assertEqual(approved["wake_target"]["mac"], "001122334455")
        finally:
            service.stop()
            temp.cleanup()

    def test_pairing_rejection_is_acknowledged_by_the_requesting_client(self):
        temp, service = self.make_service()
        try:
            request = {
                "verification_nonce": VERIFICATION_NONCE,
                "client_name": "Reject me",
                "client_id": "client-rejection",
                "scopes": ["status.read"],
            }
            pending = service.handle_http("POST", "/v1/pair/request", {}, request)[2]
            service.reject_pairing(pending["pairing_id"])
            with self.assertRaises(Exception) as rejected:
                service.handle_http("POST", "/v1/pair/request", {}, request)
            self.assertEqual(rejected.exception.status, 409)
            self.assertEqual(rejected.exception.code, "pairing_rejected")
            self.assertFalse(service.get_local_status()["pending_pairings"])
        finally:
            service.stop()
            temp.cleanup()

    def test_pairing_cancellation_is_acknowledged_and_idempotent(self):
        temp, service = self.make_service()
        try:
            request = {
                "verification_nonce": VERIFICATION_NONCE,
                "client_name": "Cancel me",
                "client_id": "client-cancellation",
                "scopes": ["status.read"],
            }
            pending = service.handle_http("POST", "/v1/pair/request", {}, request)[2]
            cancel = {
                "pairing_id": pending["pairing_id"],
                "verification_nonce": VERIFICATION_NONCE,
                "client_id": "client-cancellation",
            }
            status, _, response = service.handle_http("POST", "/v1/pair/cancel", {}, cancel)
            self.assertEqual(status, 200)
            self.assertEqual(response["state"], "cancelled")
            self.assertFalse(service.get_local_status()["pending_pairings"])
            self.assertEqual(service.handle_http("POST", "/v1/pair/cancel", {}, cancel)[2]["state"], "cancelled")
            with self.assertRaises(Exception) as cancelled:
                service.handle_http("POST", "/v1/pair/request", {}, request)
            self.assertEqual(cancelled.exception.code, "pairing_cancelled")
        finally:
            service.stop()
            temp.cleanup()

    def test_relay_with_its_own_certificate_cannot_match_the_displayed_code(self):
        """A rogue listener that re-submits the nonce shows different digits."""
        temp, service = self.make_service()
        try:
            rogue_fingerprint = "sha256:" + "b" * 64
            self.assertNotEqual(service._tls["fingerprint"], rogue_fingerprint)
            # What the client would show if it had pinned the rogue's cert.
            rogue_code = derive_pairing_code(VERIFICATION_NONCE, rogue_fingerprint)
            pending = service.handle_http("POST", "/v1/pair/request", {}, {
                "verification_nonce": VERIFICATION_NONCE,
                "client_name": "Relayed client",
                "client_id": "client-relayed",
                "scopes": ["status.read"],
            })[2]
            shown = service.get_local_status()["pending_pairings"][0]["verification_code"]
            self.assertEqual(shown, "{0}-{1}".format(
                derive_pairing_code(VERIFICATION_NONCE, service._tls["fingerprint"])[:4],
                derive_pairing_code(VERIFICATION_NONCE, service._tls["fingerprint"])[4:],
            ))
            self.assertNotEqual(shown.replace("-", ""), rogue_code)
            self.assertEqual(pending["state"], "pending")
        finally:
            service.stop()
            temp.cleanup()

    def test_pair_request_without_host_certificate_is_unavailable(self):
        temp, service = self.make_service()
        try:
            service._tls = {}
            service.store.mutate(lambda state: state.pop("tls", None))
            with self.assertRaises(Exception) as missing:
                service.handle_http("POST", "/v1/pair/request", {}, {
                    "verification_nonce": VERIFICATION_NONCE,
                    "client_name": "No TLS", "client_id": "client-no-tls",
                    "scopes": ["status.read"],
                })
            self.assertEqual(missing.exception.status, 503)
            self.assertEqual(missing.exception.code, "tls_unavailable")
        finally:
            service.stop()
            temp.cleanup()

    def test_pairing_requires_matching_tls_channel_binding_and_returns_session(self):
        temp, service = self.make_service()
        try:
            binding = base64.urlsafe_b64encode(b"client-and-host-channel").decode("ascii")
            request = {
                "verification_nonce": VERIFICATION_NONCE,
                "client_name": "Bound client",
                "client_id": "client-bound",
                "scopes": ["status.read"],
            }
            with self.assertRaises(Exception) as mismatch:
                service.handle_http(
                    "POST", "/v1/pair/request",
                    {"X-SteamOS-Companion-TLS-Binding": base64.urlsafe_b64encode(b"different-channel").decode("ascii")},
                    request,
                    peer_address="192.168.50.20",
                    channel_binding=binding,
                )
            self.assertEqual(mismatch.exception.code, "pairing_channel_mismatch")
            pending = service.handle_http(
                "POST", "/v1/pair/request",
                {"X-SteamOS-Companion-TLS-Binding": binding},
                request,
                peer_address="192.168.50.20",
                channel_binding=binding,
            )[2]
            self.assertTrue(pending.get("pairing_session"))
            follow_up = {**request, "pairing_session": pending["pairing_session"]}
            pending_again = service.handle_http(
                "POST", "/v1/pair/request", {}, follow_up,
                peer_address="192.168.50.20",
                channel_binding="a-different-tls-channel",
            )[2]
            self.assertEqual(pending_again["state"], "pending")
        finally:
            temp.cleanup()

    def test_fabricated_pairing_sessions_do_not_fill_queue_or_get_poll_rate(self):
        now = [10_000.0]
        temp = tempfile.TemporaryDirectory()
        service = HostService(
            temp.name,
            bridge=BridgeBroker(),
            clock=lambda: now[0],
            monotonic=lambda: now[0],
        )
        service._tls = {"ready": True, "fingerprint": "sha256:" + "a" * 64}
        try:
            for index in range(13):
                nonce = base64.urlsafe_b64encode(bytes([index]) * 16).decode().rstrip("=")
                with self.assertRaises(Exception) as rejected:
                    service.handle_http("POST", "/v1/pair/request", {}, {
                        "verification_nonce": nonce,
                        "pairing_session": "f" * 16,
                        "client_name": "Rogue client",
                        "client_id": f"rogue-{index}",
                        "scopes": ["status.read"],
                    })
                if index == 12:
                    self.assertEqual(rejected.exception.code, "rate_limited")
            self.assertEqual(service._rate_buckets["pair:direct"][1], 12)
            self.assertFalse(service.store.get("pairings"))

            now[0] += 60
            pending = service.handle_http("POST", "/v1/pair/request", {}, {
                "verification_nonce": VERIFICATION_NONCE,
                "client_name": "Legitimate client",
                "client_id": "client-legitimate",
                "scopes": ["status.read"],
            })[2]
            self.assertEqual(pending["state"], "pending")
            self.assertEqual(len(service.store.get("pairings", {})), 1)
        finally:
            service.stop()
            temp.cleanup()

    def test_approved_pairing_expires_before_credential_delivery(self):
        now = [10_000.0]
        temp = tempfile.TemporaryDirectory()
        service = HostService(temp.name, bridge=BridgeBroker(), clock=lambda: now[0])
        service._tls = {"ready": True, "fingerprint": "sha256:" + "b" * 64}
        request = {
            "verification_nonce": VERIFICATION_NONCE,
            "client_name": "Expiry client",
            "client_id": "client-expiry",
            "scopes": ["status.read"],
        }
        try:
            pending = service.handle_http("POST", "/v1/pair/request", {}, request)[2]
            pairing_id = pending["pairing_id"]
            service.approve_pairing(pairing_id)
            self.assertIn(pairing_id, service._approved_tokens)

            now[0] += 600
            with self.assertRaises(Exception) as expired:
                service.handle_http("POST", "/v1/pair/request", {}, request)
            self.assertEqual(expired.exception.code, "pairing_expired")
            self.assertEqual(service.store.get("pairings", {})[pairing_id]["status"], "expired")
            self.assertNotIn(pairing_id, service._approved_tokens)
        finally:
            service.stop()
            temp.cleanup()

    def test_http_server_has_bounded_threaded_accept_path(self):
        from socketserver import ThreadingMixIn

        self.assertTrue(issubclass(_ThreadingHTTPServer, ThreadingMixIn))
        self.assertEqual(_ThreadingHTTPServer.request_queue_size, CONNECTION_LIMIT)

    def test_http_handler_arms_an_absolute_request_deadline(self):
        with mock.patch("host.backend.server.BaseHTTPRequestHandler.__init__", return_value=None):
            with mock.patch("host.backend.server.threading.Timer") as timer_class:
                _Handler("request", ("127.0.0.1", 1), object())
        timer_class.assert_called_once()
        self.assertEqual(timer_class.call_args.args[0], REQUEST_TIMEOUT)
        timer_class.return_value.start.assert_called_once_with()
        timer_class.return_value.cancel.assert_called_once_with()

        handler = object.__new__(_Handler)
        handler.connection = mock.Mock()
        handler.close_connection = False
        handler._abort_request()
        self.assertTrue(handler.close_connection)
        handler.connection.shutdown.assert_called_once_with(socket.SHUT_RDWR)
        handler.connection.close.assert_called_once_with()

    def test_decky_frozen_http_package_can_reach_system_server_module(self):
        with tempfile.TemporaryDirectory() as root:
            package = Path(root) / "http"
            package.mkdir()
            (package / "server.py").write_text("# stdlib fixture\n", encoding="utf-8")
            with mock.patch.object(http, "__path__", []), mock.patch.object(sys, "path", [root]):
                _restore_system_http_package_path()
                self.assertEqual(http.__path__, [str(package)])

    def test_pairing_state_is_pruned_and_hard_bounded(self):
        now = [10_000.0]
        temp = tempfile.TemporaryDirectory()
        bridge = BridgeBroker()
        service = HostService(temp.name, bridge=bridge, clock=lambda: now[0])
        try:
            def seed(state):
                records = state.setdefault("pairings", {})
                records["pair-ancient"] = {
                    "pairing_id": "pair-ancient", "status": "approved",
                    "expires_at": now[0] - PAIRING_RETENTION_SECONDS - 1,
                    "created_at": "0000",
                }
                records["pair-expired"] = {
                    "pairing_id": "pair-expired", "status": "pending",
                    "expires_at": now[0] - 1, "created_at": "0001",
                }
                records["pair-malformed"] = {
                    "pairing_id": "pair-malformed", "status": "rejected",
                    "expires_at": "not-a-number", "created_at": "0002",
                }
                for index in range(MAX_PAIRING_RECORDS + 20):
                    pairing_id = f"pair-{index:03d}"
                    records[pairing_id] = {
                        "pairing_id": pairing_id, "status": "rejected",
                        "expires_at": now[0] + 60, "created_at": f"{index:04d}",
                    }
            service.store.mutate(seed)
            service._approved_tokens["pair-ancient"] = "discard-me"
            service._prune_pairings()
            pairings = service.store.get("pairings", {})
            self.assertLessEqual(len(pairings), MAX_PAIRING_RECORDS)
            self.assertNotIn("pair-ancient", pairings)
            self.assertNotIn("pair-malformed", pairings)
            self.assertNotIn("pair-ancient", service._approved_tokens)
            self.assertTrue(all(
                not (
                    value.get("status") in {"created", "pending"}
                    and float(value.get("expires_at", 0)) <= now[0]
                )
                for value in pairings.values()
            ))
        finally:
            service.stop()
            temp.cleanup()

    def test_pairing_approval_enforces_client_limit(self):
        temp, service = self.make_service()
        try:
            service.store.mutate(lambda state: state.setdefault("clients", {}).update({
                f"client-{index}": {"client_id": f"client-{index}"}
                for index in range(MAX_CLIENTS)
            }))
            created = service.create_pairing(["status.read"])
            payload = decode_payload(created["payload"])
            pending = service.handle_http("POST", "/v1/pair/request", {}, {
                "pairing_id": payload["pairing_id"], "secret": payload["secret"],
                "client_name": "Over-limit client", "client_id": "client-new",
                "scopes": ["status.read"],
            })[2]
            self.assertEqual(pending["state"], "pending")
            with self.assertRaises(Exception) as error:
                service.approve_pairing(created["pairing_id"])
            self.assertEqual(error.exception.status, 429)
            self.assertEqual(error.exception.code, "client_limit_reached")
        finally:
            service.stop()
            temp.cleanup()

    def test_status_is_authenticated_and_sunshine_is_disabled_by_default(self):
        temp, service = self.make_service()
        try:
            credential = self.pair(service)
            status, _, value = service.handle_http("GET", "/v1/status", {"Authorization": "Bearer " + credential["token"]})
            self.assertEqual(status, 200)
            self.assertEqual(value["sunshine"]["state"], "disabled")
            self.assertTrue(service.get_local_status()["settings"]["auto_recover_sunshine"])
            self.assertEqual(value["capabilities"]["suspend"], "available")
            self.assertEqual(value["capabilities"]["restart"], "available")
            self.assertEqual(value["capabilities"]["shutdown"], "available")
            with self.assertRaises(Exception):
                service.handle_http("GET", "/v1/status", {})
        finally:
            temp.cleanup()

    def test_disabled_sunshine_monitor_does_not_poll_its_provider(self):
        class FakeSunshine:
            provider_name = "fake-sunshine"
            contract_version = "test-1"

            def __init__(self):
                self.status_calls = 0

            def get_status(self):
                self.status_calls += 1
                return {"running": True}

            def ensure_running(self):
                raise AssertionError("disabled monitoring must not recover Sunshine")

        temp, service = self.make_service()
        provider = FakeSunshine()
        try:
            service.set_sunshine_provider(provider)
            credential = self.pair(service)
            headers = {"Authorization": "Bearer " + credential["token"]}
            status, _, value = service.handle_http("GET", "/v1/status", headers)
            self.assertEqual(status, 200)
            self.assertFalse(service.monitor.is_enabled())
            self.assertEqual(value["sunshine"]["state"], "disabled")
            self.assertEqual(provider.status_calls, 0)
        finally:
            service.stop()
            temp.cleanup()

    def test_client_can_revoke_its_credential(self):
        temp, service = self.make_service()
        try:
            credential = self.pair(service)
            headers = {"Authorization": "Bearer " + credential["token"]}
            response = service.handle_http("POST", "/v1/pair/revoke-self", headers, {"request_id": "revoke-self"})
            self.assertEqual(response[0], 202)
            self.assertEqual(response[2]["operation"]["outcome"], "revoked")
            with self.assertRaises(Exception) as caught:
                service.handle_http("GET", "/v1/status", headers)
            self.assertEqual(caught.exception.status, 401)
        finally:
            service.stop()
            temp.cleanup()

    def test_pairing_uses_the_os_selected_active_route_address(self):
        temp, service = self.make_service()
        probe = mock.MagicMock()
        probe.__enter__.return_value = probe
        probe.__exit__.return_value = False
        probe.getsockname.return_value = ("192.168.50.24", 0)
        try:
            with mock.patch("host.backend.service.socket.socket", return_value=probe), \
                 mock.patch("host.backend.service.socket.getaddrinfo") as getaddrinfo:
                created = service.create_pairing()
                status = service.get_local_status()
            payload = decode_payload(created["payload"])
            self.assertEqual(payload["endpoint"], "https://192.168.50.24:18443")
            self.assertEqual(status["pairing_host"], "192.168.50.24")
            self.assertTrue(status["pairing_host_auto_detected"])
            getaddrinfo.assert_not_called()
            probe.connect.assert_called_with(("192.0.2.1", 9))
        finally:
            service.stop()
            temp.cleanup()

    def test_first_status_initializes_and_reuses_host_identity_and_tls(self):
        with tempfile.TemporaryDirectory() as directory:
            first = HostService(directory)
            try:
                first.start(start_server=False)
                initial = first.get_local_status()
                self.assertTrue(initial["host_id"].startswith("host-"))
                self.assertTrue(initial["tls"]["ready"], initial["tls"])
                fingerprint = initial["tls"]["fingerprint"]
                self.assertTrue((Path(directory) / "host-cert.pem").is_file())
                self.assertTrue((Path(directory) / "host-key.pem").is_file())
            finally:
                first.stop()

            second = HostService(directory)
            try:
                restored = second.get_local_status()
                self.assertEqual(restored["host_id"], initial["host_id"])
                self.assertEqual(restored["tls"]["fingerprint"], fingerprint)
            finally:
                second.stop()

    def test_tls_generation_does_not_inherit_decky_bundled_library_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            failure = subprocess.CalledProcessError(
                1,
                ["/usr/bin/openssl"],
                stderr=b"bundled libssl mismatch",
            )
            with mock.patch.dict(os.environ, {
                "LD_LIBRARY_PATH": "/tmp/decky-bundled-libs",
                "LD_PRELOAD": "/tmp/decky-bundled-libs/libssl.so.3",
                "OPENSSL_CONF": "/tmp/decky-openssl.cnf",
            }, clear=False), mock.patch(
                "host.backend.identity.shutil.which", return_value="/usr/bin/openssl"
            ), mock.patch(
                "host.backend.identity.subprocess.run", side_effect=failure
            ) as run:
                result = ensure_tls_material(directory, "host-test")
            self.assertFalse(result["ready"])
            self.assertIn("bundled libssl mismatch", result["reason"])
            self.assertEqual(run.call_count, 2)
            for call in run.call_args_list:
                environment = call.kwargs["env"]
                self.assertNotIn("LD_LIBRARY_PATH", environment)
                self.assertNotIn("LD_PRELOAD", environment)
                self.assertNotIn("OPENSSL_CONF", environment)

    def test_sunshine_restart_requires_decky_monitoring_not_a_pairing_toggle(self):
        temp, service = self.make_service()
        try:
            credential = self.pair(service)
            service.update_settings({"monitor_sunshine": True})
            with self.assertRaises(Exception) as caught:
                service.handle_http("POST", "/v1/sunshine/restart", {"Authorization": "Bearer " + credential["token"]}, {"request_id": "sunshine-test"})
            self.assertEqual(caught.exception.status, 409)
            self.assertEqual(caught.exception.code, "provider_unavailable")
        finally:
            temp.cleanup()

    def test_operation_request_id_is_idempotent_and_changed_body_conflicts(self):
        temp, service = self.make_service()
        try:
            credential = self.pair(service)
            headers = {"Authorization": "Bearer " + credential["token"]}
            first = service.handle_http("POST", "/v1/power", headers, {"request_id": "same", "action": "suspend"})[2]["operation"]["id"]
            second = service.handle_http("POST", "/v1/power", headers, {"request_id": "same", "action": "suspend"})[2]["operation"]["id"]
            self.assertEqual(first, second)
            with self.assertRaises(Exception) as caught:
                service.handle_http("POST", "/v1/power", headers, {"request_id": "same", "action": "restart"})
            self.assertEqual(caught.exception.status, 409)
        finally:
            service.stop()
            temp.cleanup()

    def test_internal_operations_are_pruned_to_the_shared_retention_bound(self):
        temp, service = self.make_service()
        try:
            created = []
            for index in range(service.journal.MAX_OPERATIONS + 14):
                operation = service.journal.internal("local.test", {"index": index})
                created.append(operation["id"])
                service.journal.update(operation["id"], state="succeeded", outcome="done")
            operations = service.store.get("operations", {})
            self.assertEqual(len(operations), service.journal.MAX_OPERATIONS)
            self.assertNotIn(created[0], operations)
            self.assertIn(created[-1], operations)
        finally:
            service.stop()
            temp.cleanup()

    def test_power_routes_dispatch_each_validated_system_action(self):
        temp, service = self.make_service()
        try:
            credential = self.pair(service)
            headers = {"Authorization": "Bearer " + credential["token"]}
            with mock.patch.object(service, "_spawn"):
                for action in ("suspend", "restart", "shutdown"):
                    response = service.handle_http("POST", "/v1/power", headers, {
                        "request_id": "power-" + action,
                        "action": action,
                    })
                    operation_id = response[2]["operation"]["id"]
                    self.assertEqual(response[2]["operation"]["kind"], "power." + action)
                    service.journal.update(operation_id, state="succeeded", outcome="test-complete")
        finally:
            service.stop()
            temp.cleanup()

    def test_display_restore_resolves_dynamic_id_by_stable_properties(self):
        baseline = {"id": "3352", "width": 3440, "height": 1440, "refresh_hz": 59}
        output = snapshot()["outputs"][0]
        output["modes"] = [
            {"id": "6001", "width": 3440, "height": 1440, "refresh_hz": 59},
            {"id": "2", "width": 3840, "height": 2160, "refresh_hz": 60},
        ]
        mode, matched_by = resolve_restore_mode(output, baseline)
        self.assertEqual(mode["id"], "6001")
        self.assertEqual(matched_by, "mode_properties")

    def test_display_profile_accepts_steam_refresh_rounding(self):
        self.assertTrue(same_mode_profile(
            {"width": 3440, "height": 1440, "refresh_hz": 60},
            {"width": 3440, "height": 1440, "refresh_hz": 59},
        ))
        self.assertFalse(same_mode_profile(
            {"width": 1920, "height": 1080, "refresh_hz": 60},
            {"width": 1920, "height": 1080, "refresh_hz": 75},
        ))
        self.assertFalse(same_mode_profile(
            {"width": 1280, "height": 1024, "refresh_hz": 60},
            {"width": 1280, "height": 1024, "refresh_hz": 61},
        ))

    def test_display_restore_resolves_steam_rounded_refresh(self):
        baseline = {"id": "4", "width": 3440, "height": 1440, "refresh_hz": 60}
        output = snapshot()["outputs"][0]
        output["modes"] = [{"id": "7575", "width": 3440, "height": 1440, "refresh_hz": 59}]
        mode, matched_by = resolve_restore_mode(output, baseline)
        self.assertEqual(mode["id"], "7575")
        self.assertEqual(matched_by, "mode_properties")

    def test_display_preview_confirm_and_verified_restore(self):
        temp, service = self.make_service()
        state = snapshot()
        service.store.mutate(lambda value: value.setdefault("profiles", {}).update({
            "profile-seed": {
                "id": "profile-seed",
                "output_identity": {"id": "1", "name": "HDMI-A-1", "description": "Reference", "is_internal": False},
                "output_id": "1",
                "mode": state["outputs"][0]["modes"][0],
                "verified_at": "2026-09-13T00:00:00Z",
                "verified_by": "owner",
            }
        }))
        credential = self.pair(service, ["status.read", "display.control"])
        headers = {"Authorization": "Bearer " + credential["token"]}
        stop = threading.Event()

        def frontend():
            while not stop.is_set():
                command = service.next_bridge_command()
                if command:
                    readback = json.loads(json.dumps(state))
                    service.report_bridge_result(command["command_id"], {"ok": True, "snapshot": readback})
                    time.sleep(0.15)
                    state["outputs"][0]["current_mode_id"] = command["payload"]["mode_id"]
                    state["outputs"][0]["generation"] += 1
                    service.bridge.report_snapshot(state)
                else:
                    time.sleep(0.005)

        worker = threading.Thread(target=frontend, daemon=True)
        worker.start()
        try:
            preview = service.handle_http("POST", "/v1/display/preview", headers, {"request_id": "preview-1", "output_id": "1", "mode_id": "2", "generation": 4})[2]
            deadline = time.time() + 2
            while time.time() < deadline:
                operation = service.journal.get(preview["operation"]["id"], credential["client_id"])
                if operation and operation["state"] in {"observed_return", "failed", "unknown"}:
                    break
                time.sleep(0.01)
            self.assertEqual(service.journal.get(preview["operation"]["id"], credential["client_id"])["state"], "observed_return")
            confirmed = service.handle_http("POST", "/v1/display/confirm", headers, {"request_id": "confirm-1", "preview_id": preview["preview"]["preview_id"], "visible": True})[2]
            self.assertEqual(confirmed["operation"]["state"], "succeeded")
            restore = service.handle_http("POST", "/v1/display/restore", headers, {"request_id": "restore-1", "source": "verified", "profile_id": "profile-seed"})[2]
            deadline = time.time() + 2
            while time.time() < deadline:
                operation = service.journal.get(restore["operation"]["id"], credential["client_id"])
                if operation and operation["state"] in {"succeeded", "failed", "unknown"}:
                    break
                time.sleep(0.01)
            self.assertEqual(service.journal.get(restore["operation"]["id"], credential["client_id"])["state"], "succeeded")
            self.assertEqual(state["outputs"][0]["current_mode_id"], "3352")
        finally:
            stop.set()
            service.stop()
            temp.cleanup()

    def test_local_display_inventory_is_explicitly_read_only_without_verified_adapter(self):
        temp, service = self.make_service()
        try:
            local = service.local_display_outputs()
            self.assertTrue(local["available"])
            self.assertEqual(local["active_state"], "unknown")
            self.assertFalse(local["selection"]["can_switch_live"])
            self.assertFalse(local["selection"]["can_set_startup_preference"])
            self.assertIn("not verified", local["selection"]["reason"])
            remote = service.display_outputs("client-test")
            self.assertNotIn("output_key", remote["outputs"][0])
            with self.assertRaises(Exception) as error:
                service.local_display_preview("1", local["generation"])
            self.assertEqual(error.exception.code, "active_output_unknown")
        finally:
            service.stop()
            temp.cleanup()

    def test_drm_inventory_reads_physical_connectors_and_tracks_topology(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def connector(name, status, modes="", edid=None):
                path = root / name
                path.mkdir()
                (path / "status").write_text(status, encoding="ascii")
                if modes:
                    (path / "modes").write_text(modes, encoding="ascii")
                if edid is not None:
                    (path / "edid").write_bytes(edid)

            def named_edid(vendor, product_id, name):
                data = bytearray(128)
                data[:8] = b"\x00\xff\xff\xff\xff\xff\xff\x00"
                manufacturer = ((ord(vendor[0]) - 64) << 10) | ((ord(vendor[1]) - 64) << 5) | (ord(vendor[2]) - 64)
                data[8:10] = manufacturer.to_bytes(2, "big")
                data[10:12] = product_id.to_bytes(2, "little")
                descriptor = bytearray(18)
                descriptor[3] = 0xFC
                descriptor[5:18] = name.encode("ascii")[:13].ljust(13, b" ")
                data[54:72] = descriptor
                return bytes(data)

            connector("card0-HDMI-A-1", "connected", "1920x1080\n1920x1080\n3840x2160\n")
            connector("card0-HDMI-A-2", "disconnected")
            connector("card0-DP-1", "connected", "2560x1440\n", named_edid("WAM", 9984, "F270iPRO"))
            connector("card0-Writeback-1", "connected")

            inventory = DrmInventory(root)
            first = inventory.snapshot()
            self.assertIsNotNone(first)
            self.assertEqual([output["connector"] for output in first["outputs"]], ["DP-1", "HDMI-A-1", "HDMI-A-2"])
            self.assertEqual(first["outputs"][0]["display_name"], "F270iPRO")
            self.assertEqual(first["outputs"][0]["monitor_vendor"], "WAM")
            self.assertEqual(first["outputs"][0]["monitor_product_id"], 9984)
            self.assertEqual(first["outputs"][0]["description"], "WAM · model 9984")
            self.assertEqual(first["connected_count"] if "connected_count" in first else sum(output["connected"] for output in first["outputs"]), 2)
            self.assertEqual(first["outputs"][1]["modes"], [
                {"id": "drm:card0:HDMI-A-1:mode:1920x1080", "width": 1920, "height": 1080, "refresh_hz": None},
                {"id": "drm:card0:HDMI-A-1:mode:3840x2160", "width": 3840, "height": 2160, "refresh_hz": None},
            ])
            self.assertIsNone(first["active_output_key"])
            self.assertFalse(first["selection"]["can_switch_live"])
            initial_generation = first["generation"]
            self.assertEqual(inventory.snapshot()["generation"], initial_generation)

            (root / "card0-HDMI-A-2" / "status").write_text("connected", encoding="ascii")
            changed = inventory.snapshot()
            self.assertGreater(changed["generation"], initial_generation)
            self.assertTrue(next(output for output in changed["outputs"] if output["connector"] == "HDMI-A-2")["connected"])

    def test_drm_inventory_holds_a_transient_usb_c_disconnect(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("card0-eDP-1", "card0-DP-1"):
                connector = root / name
                connector.mkdir()
                (connector / "status").write_text("connected", encoding="ascii")
            current_time = [0.0]
            inventory = DrmInventory(root, clock=lambda: current_time[0], disconnect_hold_seconds=3.0)
            first = inventory.snapshot()
            self.assertEqual(first["generation"], 1)

            (root / "card0-DP-1" / "status").write_text("unknown", encoding="ascii")
            current_time[0] = 1.0
            held = inventory.snapshot()
            self.assertEqual(held["generation"], first["generation"])
            self.assertIn("DP-1", [output["connector"] for output in held["outputs"]])

            (root / "card0-DP-1" / "status").write_text("connected", encoding="ascii")
            current_time[0] = 2.0
            restored = inventory.snapshot()
            self.assertEqual(restored["generation"], first["generation"])
            self.assertIn("DP-1", [output["connector"] for output in restored["outputs"]])

            (root / "card0-DP-1" / "status").write_text("unknown", encoding="ascii")
            current_time[0] = 2.5
            held_again = inventory.snapshot()
            self.assertEqual(held_again["generation"], first["generation"])

            current_time[0] = 5.5
            gone = inventory.snapshot()
            self.assertGreater(gone["generation"], first["generation"])
            self.assertNotIn("DP-1", [output["connector"] for output in gone["outputs"]])

    def test_drm_inventory_retains_internal_panel_after_eight_displayport_connectors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(1, 9):
                connector = root / f"card0-DP-{index}"
                connector.mkdir()
                (connector / "status").write_text("connected" if index == 2 else "disconnected", encoding="ascii")
            internal = root / "card0-eDP-1"
            internal.mkdir()
            (internal / "status").write_text("connected", encoding="ascii")

            inventory = DrmInventory(root)
            snapshot = inventory.snapshot()

            self.assertEqual(len(snapshot["outputs"]), 9)
            self.assertEqual(snapshot["connected_count"], 2)
            self.assertEqual(
                [output["connector"] for output in snapshot["outputs"] if output["connected"]],
                ["DP-2", "eDP-1"],
            )
            self.assertTrue(next(output for output in snapshot["outputs"] if output["connector"] == "eDP-1")["is_internal"])

    def test_local_display_uses_drm_connectors_when_steam_reports_gamescope_only(self):
        with tempfile.TemporaryDirectory() as state_directory, tempfile.TemporaryDirectory() as drm_directory:
            drm_root = Path(drm_directory)
            for name, status in (
                ("card0-HDMI-A-1", "connected"),
                ("card0-HDMI-A-2", "connected"),
                ("card0-DP-1", "connected"),
            ):
                path = drm_root / name
                path.mkdir()
                (path / "status").write_text(status, encoding="ascii")
            bridge = BridgeBroker()
            bridge.report_snapshot(snapshot())
            service = HostService(state_directory, bridge=bridge, drm_root=drm_root)
            try:
                local = service.local_display_outputs()
                self.assertEqual(local["source"], "linux-drm-sysfs")
                self.assertEqual(local["connected_count"], 3)
                self.assertEqual([output["connector"] for output in local["outputs"]], ["DP-1", "HDMI-A-1", "HDMI-A-2"])
                self.assertEqual(local["active_state"], "unknown")
                self.assertFalse(local["selection"]["can_switch_live"])
            finally:
                service.stop()

    def test_local_gamescope_output_routes_a_drm_connector_and_can_clear_it(self):
        with tempfile.TemporaryDirectory() as state_directory, tempfile.TemporaryDirectory() as drm_directory, tempfile.TemporaryDirectory() as user_directory, tempfile.TemporaryDirectory() as vendor_directory:
            drm_root = Path(drm_directory)
            connector = drm_root / "card0-DP-1"
            connector.mkdir()
            (connector / "status").write_text("connected", encoding="ascii")
            hdmi_connector = drm_root / "card0-HDMI-A-2"
            hdmi_connector.mkdir()
            (hdmi_connector / "status").write_text("connected", encoding="ascii")
            original_script = Path(vendor_directory) / "gamescope-session"
            original_script.write_text("#!/usr/bin/env bash\nexec gamescope -O '*',eDP-1\n", encoding="utf-8")
            systemctl_calls = []
            gamescope = GamescopeOutputManager(
                state_directory,
                user_root=user_directory,
                original_script=original_script,
                gamescopectl=Path(vendor_directory) / "gamescopectl",
                systemctl_runner=lambda arguments: systemctl_calls.append(arguments),
            )
            bridge = BridgeBroker()
            bridge.report_snapshot(snapshot())
            service = HostService(state_directory, bridge=bridge, drm_root=drm_root, gamescope=gamescope)
            try:
                response = service.local_gamescope_output("drm:card0:DP-1")
                operation = response["operation"]
                self.assertEqual(operation["state"], "succeeded")
                self.assertEqual(operation["outcome"], "gamescope_output_configured")
                self.assertEqual(gamescope.configured_connector(), "DP-1")
                self.assertIn('GAME_MODE_DISPLAY_ORDER="DP-1"', gamescope.script_path.read_text(encoding="utf-8"))
                self.assertEqual(systemctl_calls, [["daemon-reload"]])
                self.assertIsNone(bridge.next_command())
                local = service.local_display_outputs()
                self.assertEqual(local["monitor_switch"]["configured_connector"], "DP-1")
                self.assertEqual(local["monitor_switch"]["configured_connectors"], ["DP-1"])

                ordered = service.local_gamescope_outputs(
                    ["drm:card0:DP-1", "drm:card0:HDMI-A-2"],
                    local["generation"],
                    restart=True,
                )
                self.assertEqual(ordered["operation"]["state"], "succeeded")
                self.assertEqual(ordered["operation"]["outcome"], "gamescope_output_configured_and_restart_requested")
                self.assertEqual(gamescope.configured_connectors(), ["DP-1", "HDMI-A-2"])
                self.assertEqual(systemctl_calls[-1], ["--no-block", "restart", "gamescope-session.target"])

                cleared = service.local_clear_gamescope_output()
                self.assertEqual(cleared["operation"]["state"], "succeeded")
                self.assertEqual(cleared["operation"]["outcome"], "gamescope_output_cleared")
                self.assertIsNone(gamescope.configured_connector())
                self.assertEqual(systemctl_calls, [["daemon-reload"], ["daemon-reload"], ["--no-block", "restart", "gamescope-session.target"], ["daemon-reload"]])
            finally:
                service.stop()

    def test_remote_display_order_routes_round_trip_through_gamescope(self):
        with tempfile.TemporaryDirectory() as state_directory, tempfile.TemporaryDirectory() as drm_directory, tempfile.TemporaryDirectory() as user_directory, tempfile.TemporaryDirectory() as vendor_directory:
            drm_root = Path(drm_directory)
            for name in ("card0-DP-1", "card0-HDMI-A-2"):
                connector = drm_root / name
                connector.mkdir()
                (connector / "status").write_text("connected", encoding="ascii")
            original_script = Path(vendor_directory) / "gamescope-session"
            original_script.write_text("#!/usr/bin/env bash\nexec gamescope -O '*',eDP-1\n", encoding="utf-8")
            systemctl_calls = []
            gamescope = GamescopeOutputManager(
                state_directory,
                user_root=user_directory,
                original_script=original_script,
                gamescopectl=Path(vendor_directory) / "gamescopectl",
                systemctl_runner=lambda arguments: systemctl_calls.append(arguments),
            )
            bridge = BridgeBroker()
            bridge.report_snapshot(snapshot())
            service = HostService(state_directory, bridge=bridge, drm_root=drm_root, gamescope=gamescope)
            service._tls = {"ready": True, "fingerprint": "sha256:" + "a" * 64}
            try:
                credential = self.pair(service)
                headers = {"Authorization": "Bearer " + credential["token"]}

                status, _, payload = service.handle_http("GET", "/v1/display/order", headers)
                self.assertEqual(status, 200)
                order = payload["display_order"]
                self.assertTrue(order["available"])
                self.assertEqual(order["output_keys"], ["drm:card0:DP-1", "drm:card0:HDMI-A-2"])
                generation = order["generation"]

                body = {
                    "request_id": "remote-order-1",
                    "output_keys": ["drm:card0:DP-1", "drm:card0:HDMI-A-2"],
                    "generation": generation,
                    "restart": False,
                }
                status, _, first_payload = service.handle_http("POST", "/v1/display/order", headers, body)
                first_operation = first_payload["operation"]
                self.assertEqual(status, 202)
                self.assertEqual(first_operation["kind"], "display.order")
                self.assertEqual(first_operation["state"], "succeeded")
                self.assertEqual(first_operation["target"]["output_keys"], body["output_keys"])
                self.assertEqual(gamescope.configured_connectors(), ["DP-1", "HDMI-A-2"])
                self.assertEqual(systemctl_calls, [["daemon-reload"]])

                status, _, repeat_payload = service.handle_http("POST", "/v1/display/order", headers, body)
                self.assertEqual(status, 202)
                self.assertEqual(repeat_payload["operation"]["id"], first_operation["id"])
                self.assertEqual(systemctl_calls, [["daemon-reload"]])

                status, _, payload = service.handle_http("GET", "/v1/display/order", headers)
                self.assertEqual(status, 200)
                self.assertEqual(payload["display_order"]["saved_output_keys"], body["output_keys"])

                restart_body = {**body, "request_id": "remote-order-restart", "restart": True}
                status, _, restart_payload = service.handle_http("POST", "/v1/display/order", headers, restart_body)
                self.assertEqual(status, 202)
                self.assertEqual(restart_payload["operation"]["outcome"], "gamescope_output_configured_and_restart_requested")
                self.assertEqual(systemctl_calls[-1], ["--no-block", "restart", "gamescope-session.target"])

                reset_body = {"request_id": "remote-reset-1"}
                status, _, reset_payload = service.handle_http("POST", "/v1/display/order/automatic", headers, reset_body)
                self.assertEqual(status, 202)
                self.assertEqual(reset_payload["operation"]["kind"], "display.order.reset")
                self.assertEqual(reset_payload["operation"]["state"], "succeeded")
                self.assertEqual(reset_payload["operation"]["outcome"], "gamescope_output_cleared")
                self.assertEqual(gamescope.configured_connectors(), [])
                self.assertEqual(systemctl_calls[-1], ["daemon-reload"])
            finally:
                service.stop()

    def test_remote_display_order_reports_unsupported_hosts_as_resource(self):
        with tempfile.TemporaryDirectory() as state_directory, tempfile.TemporaryDirectory() as drm_directory, tempfile.TemporaryDirectory() as user_directory, tempfile.TemporaryDirectory() as vendor_directory:
            drm_root = Path(drm_directory)
            connector = drm_root / "card0-DP-1"
            connector.mkdir()
            (connector / "status").write_text("connected", encoding="ascii")
            original_script = Path(vendor_directory) / "gamescope-session"
            original_script.write_text("#!/usr/bin/env bash\nexec gamescope --changed-format\n", encoding="utf-8")
            gamescope = GamescopeOutputManager(
                state_directory,
                user_root=user_directory,
                original_script=original_script,
                gamescopectl=Path(vendor_directory) / "gamescopectl",
                systemctl_runner=lambda arguments: None,
            )
            bridge = BridgeBroker()
            bridge.report_snapshot(snapshot())
            service = HostService(state_directory, bridge=bridge, drm_root=drm_root, gamescope=gamescope)
            service._tls = {"ready": True, "fingerprint": "sha256:" + "a" * 64}
            try:
                credential = self.pair(service)
                status, _, payload = service.handle_http(
                    "GET",
                    "/v1/display/order",
                    {"Authorization": "Bearer " + credential["token"]},
                )
                order = payload["display_order"]
                self.assertEqual(status, 200)
                self.assertFalse(order["available"])
                self.assertTrue(order["unsupported"])
                self.assertEqual(order["reason"], "This SteamOS gamescope-session format is not supported safely")
                self.assertFalse(order["restart_available"])
            finally:
                service.stop()


    def test_local_display_contract_does_not_infer_active_screen(self):
        known = normalize_snapshot(local_fixture("two-screens.json"))
        self.assertEqual(resolve_active_output(known["outputs"], known["active_output_key"]), ("output:hdmi-a-1", "known"))
        unknown = normalize_snapshot(local_fixture("unknown-active.json"))
        self.assertEqual(resolve_active_output(unknown["outputs"], unknown.get("active_output_key")), (None, "unknown"))
        ambiguous = normalize_snapshot(local_fixture("ambiguous-active.json"))
        self.assertEqual(resolve_active_output(ambiguous["outputs"], ambiguous.get("active_output_key")), (None, "ambiguous"))
        self.assertFalse(same_selection_identity(
            {"output_key": "output:one", "connector": "HDMI-A-1", "gpu_id": "gpu-0", "identity_confidence": "ambiguous"},
            {"output_key": "output:one", "connector": "HDMI-A-1", "gpu_id": "gpu-0", "identity_confidence": "ambiguous"},
        ))

    def test_bridge_topology_generation_is_order_independent(self):
        bridge = BridgeBroker()
        first = local_fixture("two-screens.json")
        bridge.report_snapshot(first)
        initial, _ = bridge.snapshot()
        self.assertEqual(initial["generation"], 7)
        reordered = json.loads(json.dumps(first))
        reordered["outputs"].reverse()
        bridge.report_snapshot(reordered)
        same_topology, _ = bridge.snapshot()
        self.assertEqual(same_topology["generation"], initial["generation"])
        changed = json.loads(json.dumps(first))
        changed["outputs"][1]["modes"].append({"id": "mode-4k-60", "width": 3840, "height": 2160, "refresh_hz": 60})
        bridge.report_snapshot(changed)
        next_topology, _ = bridge.snapshot()
        self.assertGreater(next_topology["generation"], initial["generation"])

    def test_local_display_preview_confirm_uses_active_readback(self):
        temp = tempfile.TemporaryDirectory()
        bridge = BridgeBroker()
        initial = local_fixture("two-screens.json")
        bridge.report_snapshot(initial)
        service = HostService(temp.name, bridge=bridge)
        try:
            response = service.local_display_preview("output:dp-1", 7)
            preview_id = response["preview"]["preview_id"]
            command = wait_for_bridge_command(bridge)
            self.assertIsNotNone(command)
            self.assertEqual(command["kind"], "select_output")
            self.assertEqual(command["payload"], {"output_key": "output:dp-1", "generation": 7})
            service.report_bridge_result(command["command_id"], {"ok": True, "snapshot": local_switched_snapshot("output:dp-1")})
            operation = wait_for_terminal_operation(service, response["operation"]["id"])
            self.assertEqual(operation["state"], "observed_return")
            self.assertEqual(service.local_display_outputs()["preview"]["state"], "observed_return")
            confirmed = service.local_display_confirm(preview_id)
            self.assertEqual(confirmed["operation"]["state"], "succeeded")
            self.assertIsNone(service.store.get("local_display_preview"))
        finally:
            service.stop()
            temp.cleanup()

    def test_local_display_preview_revert_restores_baseline(self):
        temp = tempfile.TemporaryDirectory()
        bridge = BridgeBroker()
        initial = local_fixture("two-screens.json")
        bridge.report_snapshot(initial)
        service = HostService(temp.name, bridge=bridge)
        try:
            response = service.local_display_preview("output:dp-1", 7)
            preview_id = response["preview"]["preview_id"]
            command = wait_for_bridge_command(bridge)
            self.assertIsNotNone(command)
            service.report_bridge_result(command["command_id"], {"ok": True, "snapshot": local_switched_snapshot("output:dp-1")})
            self.assertEqual(wait_for_terminal_operation(service, response["operation"]["id"])["state"], "observed_return")
            revert = service.local_display_revert(preview_id)
            restore_command = wait_for_bridge_command(bridge)
            self.assertIsNotNone(restore_command)
            self.assertEqual(restore_command["kind"], "select_output")
            self.assertEqual(restore_command["payload"], {"output_key": "output:hdmi-a-1", "generation": 7})
            service.report_bridge_result(restore_command["command_id"], {"ok": True, "snapshot": initial})
            restored = wait_for_terminal_operation(service, revert["operation"]["id"])
            self.assertEqual(restored["state"], "succeeded")
            original = service.journal.get(response["operation"]["id"])
            self.assertEqual(original["state"], "failed")
            self.assertEqual(original["restore_state"], "succeeded")
            self.assertIsNone(service.store.get("local_display_preview"))
        finally:
            service.stop()
            temp.cleanup()

    def test_local_display_reload_reconciles_readback_without_replaying(self):
        temp = tempfile.TemporaryDirectory()
        bridge = BridgeBroker()
        bridge.report_snapshot(local_fixture("two-screens.json"))
        first = HostService(temp.name, bridge=bridge)
        try:
            with mock.patch.object(first, "_spawn"):
                response = first.local_display_preview("output:dp-1", 7)
            bridge.report_snapshot(local_switched_snapshot("output:dp-1"))
        finally:
            first.stop()
        second = HostService(temp.name, bridge=bridge)
        try:
            status = second.start(start_server=False)
            preview = status["local_display"]["preview"]
            self.assertEqual(preview["state"], "observed_return")
            self.assertIsNotNone(preview["deadline"])
            self.assertIsNone(bridge.next_command(), "reload reconciliation must not replay the selector")
        finally:
            second.stop()
            temp.cleanup()

    def test_local_display_failed_apply_attempts_baseline_recovery(self):
        temp = tempfile.TemporaryDirectory()
        bridge = BridgeBroker()
        initial = local_fixture("two-screens.json")
        bridge.report_snapshot(initial)
        service = HostService(temp.name, bridge=bridge)
        try:
            response = service.local_display_preview("output:dp-1", 7)
            command = wait_for_bridge_command(bridge)
            self.assertIsNotNone(command)
            service.report_bridge_result(command["command_id"], {"ok": False, "reason": "verified adapter rejected selection"})
            restore_command = wait_for_bridge_command(bridge)
            self.assertIsNotNone(restore_command)
            self.assertEqual(restore_command["payload"], {"output_key": "output:hdmi-a-1", "generation": 7})
            service.report_bridge_result(restore_command["command_id"], {"ok": True, "snapshot": initial})
            operation = wait_for_terminal_operation(service, response["operation"]["id"])
            self.assertEqual(operation["state"], "failed")
            self.assertEqual(operation["restore_state"], "succeeded")
            self.assertTrue(operation["restored"])
            self.assertIsNone(service.store.get("local_display_preview"))
        finally:
            service.stop()
            temp.cleanup()

    def test_local_display_unknown_and_disconnected_targets_are_rejected(self):
        for fixture_name, expected_code, target_key, generation in (
            ("unknown-active.json", "active_output_unknown", "output:two", 8),
            ("ambiguous-active.json", "active_output_ambiguous", "output:two", 9),
            ("disconnected-preferred.json", "stale_display_target", "output:preferred", 10),
        ):
            temp = tempfile.TemporaryDirectory()
            bridge = BridgeBroker()
            bridge.report_snapshot(local_fixture(fixture_name))
            service = HostService(temp.name, bridge=bridge)
            try:
                with self.assertRaises(Exception) as error:
                    service.local_display_preview(target_key, generation)
                self.assertEqual(error.exception.code, expected_code, fixture_name)
            finally:
                service.stop()
                temp.cleanup()

    def test_local_display_stale_inventory_is_labelled_and_cannot_mutate(self):
        now = [0.0]
        bridge = BridgeBroker(clock=lambda: now[0])
        bridge.report_snapshot(local_fixture("two-screens.json"))
        now[0] = 1.0
        unavailable = {"ready": False, "reason": "DisplayManager temporarily unavailable", "outputs": []}
        bridge.report_snapshot(unavailable)
        temp = tempfile.TemporaryDirectory()
        drm_root = Path(temp.name) / "drm"
        drm_root.mkdir()
        service = HostService(temp.name, bridge=bridge, drm_root=drm_root)
        try:
            previous = service.local_display_outputs()
            self.assertFalse(previous["available"])
            self.assertTrue(previous["previous_reading"])
            self.assertTrue(previous["stale"])
            self.assertIn("Previous display reading", previous["reason"])
            self.assertFalse(previous["selection"]["can_switch_live"])
            now[0] = 10.0
            stale = service.local_display_outputs()
            self.assertFalse(stale["available"])
            self.assertTrue(stale["previous_reading"])
            self.assertEqual(len(stale["outputs"]), 2)
            with self.assertRaises(Exception) as error:
                service.local_display_preview("output:dp-1", 7)
            self.assertEqual(error.exception.code, "stale_display_inventory")
        finally:
            service.stop()
            temp.cleanup()

    def test_coordinator_gates_local_display_controls_to_server_or_both(self):
        with tempfile.TemporaryDirectory() as directory:
            coordinator = DeviceCoordinator(directory)
            try:
                with self.assertRaises(CoordinatorError) as error:
                    coordinator.local_display_outputs()
                self.assertEqual(error.exception.code, "server_role_required")
                with self.assertRaises(CoordinatorError) as sunshine_error:
                    coordinator.local_sunshine_restart()
                self.assertEqual(sunshine_error.exception.code, "server_role_required")
                with self.assertRaises(CoordinatorError) as monitor_error:
                    coordinator.local_preferred_monitor("drm:card0:DP-1")
                self.assertEqual(monitor_error.exception.code, "server_role_required")
            finally:
                coordinator.stop()

    def test_private_state_rejects_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "other.json"
            target.write_text(json.dumps({"unexpected": True}))
            path = root / "state.json"
            path.symlink_to(target)
            with self.assertRaises(StateError):
                StateStore(root, lambda: {})

    def test_sunshine_provider_is_passive_and_recovers_only_from_confirmed_stop(self):
        class FakeSunshine:
            provider_name = "fake-sunshine"
            contract_version = "test-1"

            def __init__(self):
                self.running = False
                self.status_calls = 0
                self.ensure_calls = 0

            def get_status(self):
                self.status_calls += 1
                return {"running": self.running}

            def ensure_running(self):
                self.ensure_calls += 1
                self.running = True
                return {"accepted": True}

        temp, service = self.make_service()
        provider = FakeSunshine()
        try:
            service.set_sunshine_provider(provider)
            service.update_settings({"monitor_sunshine": True})
            credential = self.pair(service)
            headers = {"Authorization": "Bearer " + credential["token"]}
            status, _, value = service.handle_http("GET", "/v1/status", headers)
            self.assertEqual(status, 200)
            self.assertEqual(value["sunshine"]["state"], "stopped")
            response = service.handle_http("POST", "/v1/sunshine/restart", headers, {"request_id": "sunshine-restart"})
            operation_id = response[2]["operation"]["id"]
            deadline = time.time() + 2
            while time.time() < deadline:
                operation = service.journal.get(operation_id, credential["client_id"])
                if operation and operation["state"] in {"succeeded", "failed", "unknown"}:
                    break
                time.sleep(0.01)
            self.assertEqual(service.journal.get(operation_id, credential["client_id"])["state"], "succeeded")
            self.assertEqual(provider.ensure_calls, 1)
        finally:
            service.stop()
            temp.cleanup()

    def test_sunshine_auto_recovery_is_one_shot_after_a_running_to_stopped_transition(self):
        class FailingSunshine:
            provider_name = "failing-sunshine"
            contract_version = "test-1"

            def __init__(self):
                self.running = True
                self.ensure_calls = 0

            def get_status(self):
                return {"running": self.running}

            def ensure_running(self):
                self.ensure_calls += 1
                raise ProviderError("owner plugin refused to start Sunshine")

        temp, service = self.make_service()
        provider = FailingSunshine()
        try:
            service.set_sunshine_provider(provider)
            service.monitor.SAMPLE_INTERVAL = 60
            service.update_settings({"monitor_sunshine": True, "auto_recover_sunshine": True})
            service.monitor.refresh_now()
            provider.running = False
            service.monitor.refresh_now()
            deadline = time.time() + 2
            while time.time() < deadline and provider.ensure_calls < 1:
                time.sleep(0.01)
            self.assertEqual(provider.ensure_calls, 1)
            # Repeated stopped samples do not create a restart loop after the
            # bounded owner request has failed.
            service.monitor.refresh_now()
            service.monitor.refresh_now()
            time.sleep(0.05)
            self.assertEqual(provider.ensure_calls, 1)
            operations = [
                value for value in service.store.get("operations", {}).values()
                if value.get("kind") == "sunshine.restart" and value.get("client_id") == "decky-local"
            ]
            self.assertEqual(len(operations), 1)
            self.assertEqual(operations[0]["state"], "failed")
        finally:
            service.stop()
            temp.cleanup()

    def test_local_sunshine_restart_is_owner_gated_and_reconciles(self):
        class FakeSunshine:
            provider_name = "fake-sunshine"
            contract_version = "test-1"

            def __init__(self):
                self.running = False
                self.ensure_calls = 0

            def get_status(self):
                return {"running": self.running}

            def ensure_running(self):
                self.ensure_calls += 1
                self.running = True
                return {"accepted": True, "outcome": "started"}

        temp, service = self.make_service()
        provider = FakeSunshine()
        try:
            with self.assertRaises(Exception) as unavailable:
                service.local_sunshine_restart()
            self.assertEqual(unavailable.exception.code, "provider_unavailable")

            service.set_sunshine_provider(provider)
            service.update_settings({"monitor_sunshine": True})
            response = service.local_sunshine_restart()
            operation_id = response["operation"]["id"]
            operation = wait_for_terminal_operation(service, operation_id)
            self.assertEqual(operation["state"], "succeeded")
            self.assertEqual(operation["outcome"], "running")
            self.assertEqual(provider.ensure_calls, 1)
            self.assertEqual(service.get_local_status()["sunshine"]["state"], "running")
        finally:
            service.stop()
            temp.cleanup()

    def test_private_state_rolls_back_in_memory_mutation_when_write_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(directory, lambda: {"value": 1})
            with mock.patch.object(store, "_write", side_effect=StateError("disk full")):
                with self.assertRaises(StateError):
                    store.mutate(lambda state: state.__setitem__("value", 2))
            self.assertEqual(store.get("value"), 1)

    def test_sunshine_owner_report_installs_and_clears_bridge_provider(self):
        temp, service = self.make_service()
        try:
            ready = service.report_sunshine_owner({"available": True, "owner": "Decky Sunshine"})
            self.assertTrue(ready["ready"])
            self.assertEqual(ready["provider"], "decky-sunshine")
            unavailable = service.report_sunshine_owner({"available": False, "reason": "owner plugin is disabled"})
            self.assertFalse(unavailable["ready"])
            self.assertEqual(unavailable["reason"], "owner plugin is disabled")
        finally:
            service.stop()
            temp.cleanup()

    def test_sunshine_process_observer_is_read_only_fallback(self):
        observer = DeckySunshineProcessObserver()
        with mock.patch("host.backend.provider.subprocess.run", return_value=mock.Mock(returncode=0, stderr="")) as run:
            self.assertEqual(observer.get_status(), {"running": True, "reason": ""})
        run.assert_called_once_with(
            ["pgrep", "-x", "sunshine"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        with self.assertRaises(ProviderError):
            observer.ensure_running()

    def test_cpu_temperature_uses_cpu_hwmon_sensor(self):
        with tempfile.TemporaryDirectory() as directory:
            device = Path(directory) / "hwmon0"
            device.mkdir()
            (device / "name").write_text("k10temp\n")
            (device / "temp1_input").write_text("63500\n")
            (device / "temp1_label").write_text("Tdie\n")
            self.assertEqual(read_cpu_temperature(directory), {"celsius": 63.5, "label": "Tdie"})


class BridgeTests(unittest.TestCase):
    def test_bridge_round_trip_and_timeout(self):
        bridge = BridgeBroker()
        result = {}

        def requester():
            result["value"] = bridge.request("power", {"action": "suspend"}, "op-1", timeout=1)

        thread = threading.Thread(target=requester)
        thread.start()
        deadline = time.time() + 1
        command = None
        while time.time() < deadline and command is None:
            command = bridge.next_command()
            time.sleep(0.005)
        self.assertIsNotNone(command)
        bridge.report_result(command["command_id"], {"ok": True, "outcome": "method_returned"})
        thread.join(1)
        self.assertEqual(result["value"]["ok"], True)

    def test_sunshine_owner_provider_uses_fixed_bridge_commands(self):
        bridge = BridgeBroker()
        provider = BridgeSunshineProvider(bridge)
        result = {}

        def requester():
            result["status"] = provider.get_status()

        thread = threading.Thread(target=requester)
        thread.start()
        deadline = time.time() + 1
        command = None
        while time.time() < deadline and command is None:
            command = bridge.next_command()
            time.sleep(0.005)
        self.assertIsNotNone(command)
        self.assertEqual(command["kind"], "sunshine_status")
        bridge.report_result(command["command_id"], {"ok": True, "running": False})
        thread.join(1)
        self.assertEqual(result["status"], {"running": False, "reason": ""})

        result = {}

        def requester():
            result["restart"] = provider.ensure_running()

        thread = threading.Thread(target=requester)
        thread.start()
        deadline = time.time() + 1
        command = None
        while time.time() < deadline and command is None:
            command = bridge.next_command()
            time.sleep(0.005)
        self.assertIsNotNone(command)
        self.assertEqual(command["kind"], "sunshine_restart")
        bridge.report_result(command["command_id"], {"ok": True, "outcome": "method_returned"})
        thread.join(1)
        self.assertEqual(result["restart"], {"accepted": True, "outcome": "method_returned"})
