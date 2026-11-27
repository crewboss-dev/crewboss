"""The real brand assets (vendored from an install's canonical brand folder), the favicon set, and the
installable web-app manifest — its icons at STABLE unversioned URLs an installer (Omarchy) can point at."""
import hashlib
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRAND = ROOT / "app/static/brand"


def test_logo_in_sidebar_links_home(client, project):
    html = client.get("/test-proj/tasks").text
    assert 'data-brand' in html and "/static/brand/crewboss/lockup.svg?v=" in html and "/static/brand/crewboss/lockup-dark.svg?v=" in html and 'alt="Crewboss"' in html
    assert "Crewboss" in html


def test_head_links_icons_and_manifest(client, project):
    html = client.get("/").text
    for needle in ('rel="icon" type="image/svg+xml" href="/static/brand/crewboss/favicon.svg?v=',
                   'rel="icon" href="/favicon.ico"', '/static/brand/crewboss/favicon-180.png?v=', 'sizes="16x16"', 'sizes="32x32"', 'sizes="48x48"',
                   'rel="apple-touch-icon" sizes="180x180"', 'rel="manifest" href="/manifest.webmanifest"',
                   '<meta name="theme-color" content="#F9F3EB" media="(prefers-color-scheme: light)">',
                   '<meta name="theme-color" content="#151412" media="(prefers-color-scheme: dark)">'):
        assert needle in html, needle


def test_manifest_is_valid(client):
    r = client.get("/manifest.webmanifest")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/manifest+json")
    m = r.json()
    assert m["name"] == "Crewboss" and m["short_name"] == "Crewboss"  # #487: the phone's app name
    assert m["start_url"] == "/" and m["display"] == "standalone" and m["scope"] == "/"
    assert m["background_color"] == "#f6f5f0" and m["theme_color"] == "#f6f5f0"
    by_purpose = {}
    for icon in m["icons"]:
        by_purpose.setdefault(icon["purpose"], {})[icon["sizes"]] = icon
    assert set(by_purpose) == {"any", "maskable"}  # maskable: the mark inside the launcher's safe circle (#487)
    for sizes in by_purpose.values():
        assert set(sizes) == {"192x192", "512x512"}
    for size, icon in [(s, i) for sizes in by_purpose.values() for s, i in sizes.items()]:
        px = int(size.split("x")[0])
        res = client.get(icon["src"])  # the stable URL, no ?v
        assert res.status_code == 200 and res.headers["content-type"] == "image/png"
        assert res.content[:8] == b"\x89PNG\r\n\x1a\n"
        w, h = int.from_bytes(res.content[16:20], "big"), int.from_bytes(res.content[20:24], "big")
        assert (w, h) == (px, px), icon["src"]


def test_brand_files_are_the_canonical_ones():
    """Vendored byte-for-byte from the canonical brand folder, never redrawn. Set CREWBOSS_BRAND_CANON to that folder to check."""
    canon = Path(os.environ.get("CREWBOSS_BRAND_CANON") or "/nonexistent-brand-canon")
    if not canon.is_dir():
        return  # no canonical folder on this box (e.g. inside the image)
    for f in BRAND.iterdir():
        src = canon / f.name
        if src.exists():
            assert hashlib.sha256(f.read_bytes()).digest() == hashlib.sha256(src.read_bytes()).digest(), f.name


def test_stable_icon_urls_work_with_and_without_version(client):
    for name in ("crewboss/favicon-192.png", "crewboss/favicon-512.png", "crewboss/favicon-180.png", "crewboss/favicon.svg", "crewboss/lockup.svg"):
        plain = client.get(f"/static/brand/{name}")
        assert plain.status_code == 200 and plain.headers["cache-control"] == "no-cache"
        versioned = client.get(f"/static/brand/{name}?v=abc")
        assert versioned.status_code == 200 and "immutable" in versioned.headers["cache-control"]


def test_crewboss_icon_set_is_complete_and_ico_has_three_sizes(client):
    d = BRAND / "crewboss"
    for n in ("mark", "mark-small", "wordmark", "lockup"):
        assert (d / f"{n}.svg").exists() and (d / f"{n}-dark.svg").exists(), n
    for n in ("mark-tile.svg", "favicon.svg", "maskable-192.png", "maskable-512.png"):
        assert (d / n).exists(), n
    for px in (16, 32, 48, 64, 96, 180, 192, 512):
        b = (d / f"favicon-{px}.png").read_bytes()
        assert (int.from_bytes(b[16:20], "big"), int.from_bytes(b[20:24], "big")) == (px, px), px
    assert "prefers-color-scheme:dark" in (d / "favicon.svg").read_text()
    ico = client.get("/favicon.ico").content
    n = int.from_bytes(ico[4:6], "little")
    assert ico[:4] == b"\x00\x00\x01\x00" and n == 3
    assert [ico[6 + 16 * i] for i in range(n)] == [16, 32, 48]


def test_root_convention_icon_and_manifest_paths(client):
    """#750 (the ERP's ec43ff3d): an installer that isn't signed in finds the icon and name at the well-known root
    paths, as bytes with no redirect."""
    m = client.get("/manifest.json", follow_redirects=False)
    assert m.status_code == 200 and m.headers["content-type"].startswith("application/manifest+json")
    assert m.json() == client.get("/manifest.webmanifest").json()
    for path in ("/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 200 and r.headers["content-type"] == "image/png", path
        assert r.content == (BRAND / "crewboss/favicon-180.png").read_bytes(), path


def test_sign_in_page_carries_the_app_icon_and_name(client):
    """The page an unsigned installer lands on (/ -> /auth/login) names the app and links its icons (#750)."""
    html = client.get("/auth/login").text
    for needle in ('<meta name="application-name" content="Crewboss">', '<meta name="apple-mobile-web-app-title" content="Crewboss">',
                   'rel="apple-touch-icon" sizes="180x180" href="/static/brand/crewboss/favicon-180.png?v=',
                   'sizes="192x192" href="/static/brand/crewboss/favicon-192.png?v=', 'rel="manifest" href="/manifest.webmanifest"'):
        assert needle in html, needle
