"""A small local web server for the dashboard (standard library only).

Security, because one endpoint places orders:
- binds to 127.0.0.1 only;
- rejects requests whose Host header is not this server (blocks DNS-rebinding attacks);
- order requests need a per-session token that is only embedded in the page itself, sent in a
  custom header. Other websites cannot read the page (same-origin policy) and cannot send a
  custom header cross-origin without a CORS preflight, which this server never approves;
- a strict Content-Security-Policy, and the order is re-validated server-side before sending.
"""

from __future__ import annotations

import hmac
import json
import logging
import secrets
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import parse_qs, urlsplit

from tradebot.ui.monitor import Monitor

log = logging.getLogger(__name__)

TOKEN_HEADER = "X-Tradebot-Token"
MAX_BODY = 4096
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
        "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, monitor: Monitor, port: int = 8765, host: str = "127.0.0.1") -> None:
        self.monitor = monitor
        self.token = secrets.token_urlsafe(32)
        super().__init__((host, port), _Handler)
        self.port = self.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def allowed_hosts(self) -> set[str]:
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}

    def page(self) -> bytes:
        html = resources.files("tradebot.ui").joinpath("static/index.html").read_text(encoding="utf-8")
        return html.replace("__TRADEBOT_TOKEN__", self.token).encode("utf-8")


class _Handler(BaseHTTPRequestHandler):
    server: DashboardServer
    server_version = "tradebot"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:
        log.debug("%s %s", self.address_string(), format % args)

    def do_GET(self) -> None:
        if not self._host_ok():
            return
        url = urlsplit(self.path)
        if url.path in ("/", "/index.html"):
            self._send(HTTPStatus.OK, self.server.page(), "text/html; charset=utf-8")
        elif url.path == "/api/state":
            self._json(HTTPStatus.OK, self.server.monitor.snapshot())
        elif url.path == "/api/chart":
            symbol = parse_qs(url.query).get("symbol", [""])[0]
            chart = self.server.monitor.chart(symbol)
            if chart is None:
                self._json(HTTPStatus.NOT_FOUND, {"error": f"no chart for {symbol!r} yet"})
            else:
                self._json(HTTPStatus.OK, chart)
        else:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:
        # Read the (small) body before any refusal: answering with unread request bytes makes the
        # socket close with a reset, and the browser reports a network error instead of the reason.
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= MAX_BODY:
            self.close_connection = True
            return self._json(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"ok": False, "message": "bad request size"}
            )
        raw = self.rfile.read(length)
        if not self._host_ok():
            return
        if urlsplit(self.path).path != "/api/order":
            return self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        origin = self.headers.get("Origin")
        if origin and origin.removeprefix("http://") not in self.server.allowed_hosts():
            return self._json(HTTPStatus.FORBIDDEN, {"ok": False, "message": "cross-origin request refused"})
        if not hmac.compare_digest(self.headers.get(TOKEN_HEADER, ""), self.server.token):
            return self._json(
                HTTPStatus.FORBIDDEN, {"ok": False, "message": "missing or wrong session token"}
            )
        try:
            body = json.loads(raw or b"{}")
            symbol, side = str(body["symbol"]), int(body["side"])
        except (ValueError, KeyError, TypeError) as exc:
            return self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "message": f"bad request: {exc}"})
        result = self.server.monitor.submit(symbol, side)
        self._json(HTTPStatus.OK if result.get("ok") else HTTPStatus.CONFLICT, result)

    def _host_ok(self) -> bool:
        if (self.headers.get("Host") or "").lower() in self.server.allowed_hosts():
            return True
        self._json(HTTPStatus.FORBIDDEN, {"error": "unexpected Host header"})
        return False

    def _json(self, status: HTTPStatus, payload: Any) -> None:
        body = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode("utf-8")
        self._send(status, body, "application/json")

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)
