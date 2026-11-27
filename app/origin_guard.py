"""Same-origin guard for the owner's browser writes.

The HTML pages take no login (NetBird gates the network), so every page POST is implicitly "the
owner". Without a check, any other site the owner has open — including a sibling *.example.com
dev site — could make the browser POST a task or comment here, and that write would be relayed
to a shell-capable Claude session as an owner update. This middleware rejects such writes.

Scope: every non-GET/HEAD request whose path is NOT under /api or /mcp (those authenticate with a
Bearer token, which a cross-site browser request cannot carry). Rejected with 403 when:
  - Sec-Fetch-Site is present and is not `same-origin` or `none` (user-typed / bookmark), or
  - Origin is present and its host[:port] is not one of config.ORIGIN_HOSTS (Origin "null" fails).
A request with neither header (curl, a very old browser) passes: it is not a browser CSRF.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from starlette.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app import config

SAFE_METHODS = frozenset({"GET", "HEAD"})
# /runner (task #627) authenticates by enrolment code or device signature, not a browser session: a runner is not a
# browser, and no cookie rides on it, so there is no ambient credential for a cross-site request to abuse.
EXEMPT_PREFIXES = ("/api", "/mcp", "/runner")
_DEFAULT_PORTS = {"http": "80", "https": "443"}


def _exempt(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in EXEMPT_PREFIXES)


def origin_host(origin: str) -> str | None:
    """'https://dash.example.com' -> 'dash.example.com'; default ports dropped; None if unparseable."""
    try:
        parts = urlsplit(origin.strip())
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return None
    if not parts.scheme or not host:
        return None
    if port is None or str(port) == _DEFAULT_PORTS.get(parts.scheme.lower()):
        return host
    return f"{host}:{port}"


def rejection_reason(method: str, path: str, headers: dict[str, str],
                     allowed_hosts: tuple[str, ...] | None = None) -> str | None:
    """None if the request may proceed, else a short reason for the 403."""
    if method.upper() in SAFE_METHODS or _exempt(path):
        return None
    allowed = allowed_hosts if allowed_hosts is not None else config.ORIGIN_HOSTS
    site = headers.get("sec-fetch-site")
    if site is not None and site.strip().lower() not in ("same-origin", "none"):
        return f"cross-site request refused (Sec-Fetch-Site: {site})"
    origin = headers.get("origin")
    if origin is not None and origin_host(origin) not in allowed:
        return "cross-origin request refused (Origin not a dashboard host)"
    return None


class SameOriginGuard:
    """Pure ASGI middleware (no BaseHTTPMiddleware) so it never buffers or reorders the body."""

    def __init__(self, app: ASGIApp, allowed_hosts: tuple[str, ...] | None = None) -> None:
        self.app = app
        self.allowed_hosts = allowed_hosts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
            reason = rejection_reason(scope["method"], scope["path"], headers, self.allowed_hosts)
            if reason:
                await PlainTextResponse(reason, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)
