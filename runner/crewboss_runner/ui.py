"""The local status page and control panel (#974), 127.0.0.1 only.

* Binds 127.0.0.1 (never 0.0.0.0).
* The one-use bootstrap token is in the URL FRAGMENT (`http://127.0.0.1:PORT/#<token>`), so it never reaches a server log, a Referer
  or the wire. The page exchanges it ONCE (`POST /api/exchange`) for a session secret that it keeps in page memory / sessionStorage and
  sends as `Authorization: Bearer` on every request (also GETs of /api/state, /api/log, /api/health). No cookies, ever: a cookie is
  ambient and is shared by every port of 127.0.0.1, so another local user's server could ride it. A bearer header is never sent by a browser
  on its own, which is also why no CSRF token is needed.
* Every state-changing action is `POST /api/action/<fixed name>` with the bearer and a JSON content type. No action takes a path,
  command or URL from the request.
* A request whose Host isn't 127.0.0.1:PORT / localhost:PORT (DNS rebinding) or whose Origin isn't the page's own (or whose
  Sec-Fetch-Site says cross-site) gets 403. Actions are rate-limited.
* Everything is served from here (no CDN). It shows state only: never a token, key or the PC's Claude login."""
from __future__ import annotations

import hmac
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import panel
from .panel_page import ASSETS

CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


class StatusServer:
    def __init__(self, get_status=None, port: int = 0, on_rearm=None, on_exchange=None, actions=None, limiter=None):
        """get_status() feeds the legacy /api/status (default: the panel's own state). on_rearm(url): when set, a used bootstrap key is
        replaced by a fresh one right after the exchange and on_rearm is called with the new link (the headless runner writes it to a
        user-only file for `crewboss-runner status`). on_exchange(): called after a successful exchange (the tray deletes its launcher
        file). Each key is still one-use."""
        self.get_status = get_status or panel.local_state
        self.on_rearm, self.on_exchange = on_rearm, on_exchange
        self.bootstrap = secrets.token_urlsafe(24)
        self._bootstrap_used = False
        self.session = secrets.token_urlsafe(32)
        self.actions = actions or panel.Actions()
        self.limiter = limiter or panel.RateLimit()
        self._lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "crewboss-runner"
            sys_version = ""

            def log_message(self, *a):   # no access log: it would only repeat URLs
                pass

            def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", CSP)
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code: int, obj: dict) -> None:
                self._send(code, json.dumps(obj).encode())

            def _guard(self) -> bool:
                """403 on a wrong Host / Origin / cross-site fetch."""
                own = {f"127.0.0.1:{outer.port}", f"localhost:{outer.port}"}
                host = (self.headers.get("Host") or "").lower()
                origin = self.headers.get("Origin")
                site = self.headers.get("Sec-Fetch-Site")
                if host not in own or (origin is not None and origin.lower() not in {f"http://{h}" for h in own}) \
                        or site not in (None, "same-origin", "none"):
                    self._json(403, {"detail": "forbidden"})
                    return False
                return True

            def _session_ok(self) -> bool:
                auth = self.headers.get("Authorization") or ""
                return auth.startswith("Bearer ") and hmac.compare_digest(auth[7:], outer.session)

            def do_GET(self):
                if not self._guard():
                    return
                path = self.path.split("?", 1)[0]
                if path in ASSETS:
                    body, ctype = ASSETS[path]
                    return self._send(200, body.encode(), ctype)
                if path.startswith("/api/"):
                    if not self._session_ok():
                        return self._json(401, {"detail": "unauthorised"})
                    if path == "/api/status":
                        return self._json(200, outer.get_status())
                    if path == "/api/state":
                        return self._json(200, panel.local_state(outer.actions.health.data["failing"] if outer.actions.health.data else 0))
                    if path == "/api/health":
                        return self._json(200, outer.actions.health.get())
                    if path == "/api/log":
                        from . import appctl
                        return self._json(200, {"lines": appctl.log_lines(100)})
                self._json(404, {"detail": "not found"})

            def do_POST(self):
                if not self._guard():
                    return
                path = self.path.split("?", 1)[0]
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    n = 0
                raw = self.rfile.read(max(0, min(n, 2048)))
                if path != "/api/exchange":   # who is asking comes before what they sent
                    if not path.startswith("/api/action/"):
                        return self._json(404, {"detail": "not found"})
                    if not self._session_ok():
                        return self._json(401, {"detail": "unauthorised"})
                    if "json" not in (self.headers.get("Content-Type") or ""):   # a plain cross-site form post cannot send this
                        return self._json(403, {"detail": "forbidden"})
                    if not outer.limiter.allow():
                        return self._json(429, {"detail": "slow down"})
                try:
                    body = json.loads(raw or b"{}")
                except ValueError:
                    return self._json(400, {"detail": "bad request"})
                if path == "/api/exchange":
                    return self._exchange(body)
                code, obj = outer.actions.run(path[len("/api/action/"):], body)
                self._json(code, obj)

            def _exchange(self, body) -> None:
                token = body.get("token", "") if isinstance(body, dict) else ""
                with outer._lock:
                    ok = isinstance(token, str) and not outer._bootstrap_used and hmac.compare_digest(token, outer.bootstrap)
                    if ok:
                        outer._bootstrap_used = True
                        if outer.on_rearm:
                            outer.bootstrap = secrets.token_urlsafe(24)
                            outer._bootstrap_used = False
                if not ok:
                    return self._json(403, {"detail": "that key was already used or is wrong"})
                self._json(200, {"session": outer.session})
                if outer.on_rearm:
                    outer.on_rearm(outer.url)
                if outer.on_exchange:
                    outer.on_exchange()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/#{self.bootstrap}"

    def new_link(self) -> str:
        """A fresh one-use link (the old key stops working): what the tray opens."""
        with self._lock:
            self.bootstrap = secrets.token_urlsafe(24)
            self._bootstrap_used = False
        return self.url

    def start(self) -> "StatusServer":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
