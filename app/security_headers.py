"""Browser hardening headers on every response (OSS review N2, the safe subset).

  X-Content-Type-Options: nosniff        a browser never guesses a type (an uploaded file can't turn into a page)
  Referrer-Policy: same-origin           a link out of the app carries no address of it
  X-Frame-Options: DENY                  no other site frames a page (click-jacking); and, on HTML only,
  Content-Security-Policy: frame-ancestors 'none'   the modern form of the same rule

NOT a full Content-Security-Policy: the pages use htmx and Alpine with inline handlers, so `script-src` would need 'unsafe-inline'
or a rework of every template. That is a later, separate change. No page of the app frames another one (nothing renders an iframe),
so DENY is safe. A header a route set itself (the upload routes' sandboxing CSP, their nosniff) is never replaced."""
from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

DEFAULTS = (("x-content-type-options", "nosniff"), ("referrer-policy", "same-origin"), ("x-frame-options", "DENY"))
FRAME_CSP = "frame-ancestors 'none'"


class SecurityHeaders:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in DEFAULTS:
                    if name not in headers:
                        headers[name] = value
                if "content-security-policy" not in headers and headers.get("content-type", "").lower().startswith("text/html"):
                    headers["content-security-policy"] = FRAME_CSP
            await send(message)

        await self.app(scope, receive, send_with_headers)
