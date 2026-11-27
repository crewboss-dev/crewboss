"""Browser hardening headers (OSS review N2, the safe subset): nosniff, same-origin referrer, no framing. No full CSP (htmx / Alpine
inline handlers), so none is asserted; a route's own header is never replaced."""
import re
from pathlib import Path

from app import repo

ROOT = Path(__file__).resolve().parent.parent


def test_pages_api_and_refusals_carry_the_headers(client):
    repo.create_project("hdr-proj", "Hdr Project", "internal", "owner", short_name="hdr", session_roles=["all"])
    page = client.get("/hdr-proj/board")
    assert page.status_code == 200
    for r in (page, client.get("/healthz"), client.get("/no-such-page-at-all")):
        assert r.headers["x-content-type-options"] == "nosniff", r.url
        assert r.headers["referrer-policy"] == "same-origin" and r.headers["x-frame-options"] == "DENY"
    assert page.headers["content-security-policy"] == "frame-ancestors 'none'"                # HTML only, and only that directive
    assert "content-security-policy" not in client.get("/healthz").headers
    cross = client.post("/hdr-proj/tasks", data={}, headers={"Origin": "https://evil.example"})     # refused by the guard: still headed
    assert cross.status_code == 403 and cross.headers["x-frame-options"] == "DENY"


def test_a_route_that_sets_its_own_csp_keeps_it():
    import asyncio
    from app.security_headers import SecurityHeaders
    sent = []

    async def inner(scope, receive, send):
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/html"), (b"content-security-policy", b"sandbox; default-src 'none'")]})
        await send({"type": "http.response.body", "body": b""})

    async def collect(m):
        sent.append(m)
    asyncio.run(SecurityHeaders(inner)({"type": "http", "headers": []}, None, collect))
    h = dict(sent[0]["headers"])
    assert h[b"content-security-policy"] == b"sandbox; default-src 'none'" and h[b"x-frame-options"] == b"DENY"


def test_nothing_in_the_app_frames_a_page_of_the_app():
    """DENY is only safe while no template or script of ours renders an iframe / object / embed."""
    offenders = []
    for path in list((ROOT / "app" / "templates").rglob("*.html")) + [p for p in (ROOT / "app" / "static").rglob("*.js")
                                                                      if "canvas" not in p.parts]:
        if re.search(r"<\s*(iframe|object|embed)\b", path.read_text(errors="ignore"), re.I):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
