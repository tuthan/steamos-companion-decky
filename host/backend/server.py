"""Bounded HTTPS server for the private LAN API."""

from __future__ import annotations

import base64
import http
import json
import socket
import socketserver
import ssl
import sys
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


def _restore_system_http_package_path() -> None:
    """Expose stdlib ``http.server`` inside Decky's frozen Python runtime.

    Decky bundles the parent ``http`` package but not every child module.  It
    also appends the host Python paths after startup; those paths cannot be
    used for child imports while the frozen parent's ``__path__`` remains
    authoritative.  Add only an existing stdlib package directory discovered
    through Decky's own trusted ``sys.path`` entries.
    """
    package_path = getattr(http, "__path__", None)
    if package_path is None:
        return
    for entry in sys.path:
        if not entry:
            continue
        candidate = Path(entry) / "http" / "server.py"
        if candidate.is_file():
            directory = str(candidate.parent)
            if directory not in package_path:
                package_path.append(directory)
            return


_restore_system_http_package_path()

from http.server import BaseHTTPRequestHandler, HTTPServer

from .protocol import MAX_JSON_BYTES, ProtocolError, parse_json_body
from .service import ApiError, HostService


CONNECTION_LIMIT = 32
TLS_HANDSHAKE_TIMEOUT = 5.0
REQUEST_TIMEOUT = 10.0


def _close_socket(connection: socket.socket) -> None:
    """Interrupt a blocking read before closing a timed-out connection."""
    try:
        connection.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        connection.close()
    except OSError:
        pass


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False
    request_queue_size = CONNECTION_LIMIT

    def __init__(self, server_address, request_handler, ssl_context: ssl.SSLContext):
        self._ssl_context = ssl_context
        self._connection_slots = threading.BoundedSemaphore(CONNECTION_LIMIT)
        super().__init__(server_address, request_handler)

    def process_request(self, request, client_address):
        """Dispatch accepted sockets to bounded workers before TLS handshake.

        Wrapping the listening socket, or handshaking here, performs work in
        the accept loop and allows one incomplete client to block every other
        client. The raw socket is handed to ``process_request_thread`` first.
        """
        if not self._connection_slots.acquire(blocking=False):
            try:
                self.shutdown_request(request)
            except OSError:
                pass
            return
        try:
            request.settimeout(TLS_HANDSHAKE_TIMEOUT)
            super().process_request(request, client_address)
        except Exception:
            self._connection_slots.release()
            try:
                self.shutdown_request(request)
            except OSError:
                pass

    def process_request_thread(self, request, client_address):
        wrapped = None
        def abort_handshake() -> None:
            # ``wrap_socket`` may detach the raw socket object, so interrupt
            # both references when the handshake deadline fires.
            _close_socket(request)
            if wrapped is not None:
                _close_socket(wrapped)

        handshake_timer = threading.Timer(TLS_HANDSHAKE_TIMEOUT, abort_handshake)
        handshake_timer.daemon = True
        handshake_timer.start()
        try:
            wrapped = self._ssl_context.wrap_socket(
                request,
                server_side=True,
                do_handshake_on_connect=False,
            )
            wrapped.do_handshake()
            wrapped.settimeout(REQUEST_TIMEOUT)
            super().process_request_thread(wrapped, client_address)
        except Exception:
            try:
                self.shutdown_request(wrapped if wrapped is not None else request)
            except OSError:
                pass
        finally:
            self._connection_slots.release()
            handshake_timer.cancel()


class _ThreadingHTTPServerV6(_ThreadingHTTPServer):
    address_family = socket.AF_INET6


class _Handler(BaseHTTPRequestHandler):
    server_version = "SteamOSCompanion/1"
    protocol_version = "HTTP/1.0"

    def __init__(self, request, client_address, server):
        # Socket timeouts only bound an idle read. A separate timer closes the
        # connection even when a client keeps sending one byte at a time, and
        # remains armed through the handler's final response flush.
        self._request_deadline_timer = threading.Timer(REQUEST_TIMEOUT, self._abort_request)
        self._request_deadline_timer.daemon = True
        self._request_deadline_timer.start()
        try:
            super().__init__(request, client_address, server)
        finally:
            self._request_deadline_timer.cancel()

    def _abort_request(self) -> None:
        self.close_connection = True
        connection = getattr(self, "connection", None)
        if connection is not None:
            _close_socket(connection)

    @property
    def service(self) -> HostService:
        return self.server.service  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._write(405, {"error": "method_not_allowed", "message": "method is not supported"})

    def _dispatch(self, method: str) -> None:
        try:
            parsed = urlsplit(self.path)
            if parsed.query or parsed.fragment:
                raise ApiError("query strings are not accepted", 400, "invalid_request")
            body = self._read_body() if method == "POST" else {}
            status, headers, result = self.service.handle_http(
                method,
                parsed.path,
                dict(self.headers.items()),
                body,
                peer_address=str(self.client_address[0]),
                channel_binding=self._channel_binding(),
            )
            self._write(status, result, headers)
        except (ApiError, ProtocolError) as exc:
            extra = {"Retry-After": "2"} if exc.status == 429 else None
            self._write(exc.status, {"error": exc.code, "message": str(exc)[:256]}, extra)
        except Exception:
            # Do not put exception details or request material on the LAN.
            self._write(500, {"error": "internal_error", "message": "the host could not safely complete the request"})

    def _channel_binding(self) -> str | None:
        """Return a non-secret binding for this exact TLS connection."""
        getter = getattr(self.connection, "get_channel_binding", None)
        version_getter = getattr(self.connection, "version", None)
        if not callable(getter) or not callable(version_getter):
            return None
        try:
            value = getter("tls-unique")
            version = version_getter()
        except (ValueError, OSError, ssl.SSLError):
            return None
        # Python exposes RFC 5929 tls-unique but not the TLS 1.3 exporter.
        # Pairing clients therefore negotiate TLS 1.2 explicitly, where this
        # binding is defined and has a 12-byte minimum.
        if version != "TLSv1.2":
            return None
        if not isinstance(value, bytes) or len(value) < 12:
            return None
        return base64.urlsafe_b64encode(value).decode("ascii")

    def _read_body(self) -> dict[str, Any]:
        value = self.headers.get("Content-Length")
        try:
            length = int(value or "0")
        except ValueError as exc:
            raise ApiError("Content-Length is invalid", 400, "invalid_request") from exc
        if length < 0 or length > MAX_JSON_BYTES:
            raise ApiError("request body is too large", 413, "body_too_large")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ApiError("request body was truncated", 400, "invalid_request")
        return parse_json_body(raw)

    def _write(self, status: int, result: dict[str, Any], extra: dict[str, str] | None = None) -> None:
        encoded = json.dumps(result, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode()
        if len(encoded) > MAX_JSON_BYTES:
            status = 500
            encoded = b'{"error":"response_too_large","message":"response exceeded the bounded limit"}'
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        for key, value in (extra or {}).items():
            if key.lower() not in {"content-type", "content-length", "connection", "cache-control"}:
                self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except OSError:
            pass

    def log_message(self, format: str, *args: Any) -> None:
        # Decky owns logging; request paths can contain client-controlled IDs.
        return


class HostHttpServer:
    def __init__(self, service: HostService, address: str, port: int, certificate_path: str, key_path: str):
        self.service = service
        server_class = _ThreadingHTTPServerV6 if ":" in address else _ThreadingHTTPServer
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(certificate_path, key_path)
        self._httpd = server_class((address, port), _Handler, context)
        self._httpd.service = service  # type: ignore[attr-defined]
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        return self._httpd.server_address[:2]

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="steamos-companion-https", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
        self._thread = None
