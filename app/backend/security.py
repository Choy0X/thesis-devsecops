"""Access control for the local API.

The backend listens on 127.0.0.1, but a web page open in the user's normal
browser can still send requests to localhost (and, with DNS rebinding, read the
answers). Three checks keep the API to the app's own window:

* **Host header** must be ``127.0.0.1`` / ``localhost`` (any port): a rebinding
  attack arrives with the attacker's host name.
* **Session cookie**: serving the UI (``/``) sets an HttpOnly, SameSite=Strict
  cookie holding a random token. The browser sends it on the app's own fetches,
  WebSockets and download links, never on requests started by another site.
  Every ``/api`` and ``/ws`` request must carry it.
* **Origin** of a WebSocket handshake, when present, must be the app itself.

The token is kept in ``app/backend/.api_token`` (gitignored) so a backend
restart does not lock out an open window.
"""
from __future__ import annotations
import json
import pathlib
import secrets

from app.backend import errors
from http.cookies import SimpleCookie
from urllib.parse import urlsplit

COOKIE = "aivf_session"
TOKEN_PATH = pathlib.Path(__file__).resolve().parent / ".api_token"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
OPEN_PATHS = {"/api/health"}             # harmless: booleans and stage names


def _load_token() -> str:
    try:
        tok = TOKEN_PATH.read_text().strip()
        if len(tok) >= 32:
            return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(32)
    try:
        TOKEN_PATH.write_text(tok)
    except OSError:
        pass
    return tok


TOKEN = _load_token()


def cookie_header() -> str:
    return f"{COOKIE}={TOKEN}; Path=/; HttpOnly; SameSite=Strict"


def _host_ok(host: str | None) -> bool:
    if not host:
        return False
    name = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
    return name.lower() in LOCAL_HOSTS


def _cookie_ok(raw: str | None) -> bool:
    if not raw:
        return False
    c = SimpleCookie()
    try:
        c.load(raw)
    except Exception:  # ignored: a malformed cookie header counts as no cookie, so access is refused
        return False
    m = c.get(COOKIE)
    return bool(m) and secrets.compare_digest(m.value, TOKEN)


def _origin_ok(origin: str | None) -> bool:
    if not origin:
        return True                      # non-browser clients send none
    try:
        return _host_ok(urlsplit(origin).netloc)
    except ValueError:
        return False


class LocalOnly:
    """Pure ASGI middleware (also covers WebSockets, unlike BaseHTTPMiddleware)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
        path = scope.get("path", "")
        guarded = path.startswith("/api/") or path.startswith("/ws/")
        ok = _host_ok(headers.get("host"))
        if ok and guarded and path not in OPEN_PATHS:
            ok = _cookie_ok(headers.get("cookie")) and _origin_ok(headers.get("origin"))
        if ok:
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            await receive()                                   # websocket.connect
            await send({"type": "websocket.close", "code": 4403})
            return
        body = json.dumps(errors.body("forbidden: open the app from its own window (reload the page)", 403,
                                      hint="Close this tab and open the app from its own window.")).encode("utf-8")
        await send({"type": "http.response.start", "status": 403,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
