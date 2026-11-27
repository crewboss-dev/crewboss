"""Cache-busting: every static <link>/<script> carries ?v=<the file's current content hash>, and /static
answers versioned URLs with an immutable year-long cache, unversioned ones with no-cache. (The owner got a
stale app.css after a rebuild: new .popover markup, old CSS without the rule, see-through menus.)"""
import json
import re
from pathlib import Path

import pytest

from app import assets, repo

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = json.loads((ROOT / "fixtures/catalog-fixture.json").read_text())
STATIC_REF = re.compile(r'(?:href|src)="(/static/[^"]*)"')


@pytest.fixture
def seeded(project):
    repo.import_catalog(project["slug"], FIXTURE)
    return project


PAGES = ["/", "/test-proj", "/test-proj/tasks", "/test-proj/tasks/new", "/test-proj/workflows",
         "/test-proj/workflows/sales.lead.qualify-from-email-reply", "/test-proj/ai-features", "/test-proj/build-skills", "/test-proj/rules",
         "/test-proj/knowledge", "/test-proj/objectives", "/test-proj/activity", "/test-proj/health"]


@pytest.mark.parametrize("path", PAGES)
def test_every_static_reference_is_versioned_with_the_current_hash(client, seeded, path):
    html = client.get(path).text
    refs = STATIC_REF.findall(html)
    assert refs, path
    for ref in refs:
        m = re.fullmatch(r"/static/([^?]+)\?v=([0-9a-f]{12}|0)", ref.replace("&amp;", "&"))
        assert m, f"{path}: unversioned static reference {ref}"
        rel, v = m.groups()
        assert v == assets.file_hash(rel), f"{path}: {rel} carries v={v}, file hash is {assets.file_hash(rel)}"
        if (assets.STATIC_DIR / rel).exists():  # built files (app.css, the canvas bundle) exist in the image only
            assert v != assets.MISSING


def test_no_template_links_static_without_asset():
    for f in (ROOT / "app/templates").rglob("*.html"):
        for ref in STATIC_REF.findall(f.read_text()):
            pytest.fail(f"{f.relative_to(ROOT)} links {ref} directly; use {{{{ asset('…') }}}}")


def test_hash_changes_with_the_content(tmp_path, monkeypatch):
    monkeypatch.setattr(assets, "STATIC_DIR", tmp_path)
    (tmp_path / "a.css").write_text("x")
    one = assets.file_hash("a.css")
    (tmp_path / "a.css").write_text("y")
    assert assets.file_hash("a.css") != one and len(one) == 12


def test_versioned_static_is_immutable_unversioned_revalidates(client):
    v = assets.version("js/app.js")
    r = client.get(f"/static/js/app.js?v={v}")
    assert r.status_code == 200 and r.headers["cache-control"] == "public, max-age=31536000, immutable"
    r = client.get("/static/js/app.js")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"


def test_floating_menus_use_popover_never_card():
    """Anything that floats over content (a shown/hidden absolutely- or fixed-positioned surface) is an opaque
    .popover — .card is 40% charcoal in dark mode and lets the page read through. Scrims (inset-0) exempt."""
    from html.parser import HTMLParser

    bad = []

    class P(HTMLParser):
        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            cls = (a.get("class") or "").split()
            floating = {"absolute", "fixed"} & set(cls)
            shown = any(k in a for k in ("x-show", "x-cloak"))
            if floating and shown and "inset-0" not in cls and "popover" not in cls:
                bad.append((self.fname, tag, a.get("class")))
            if "popover" not in cls and "card" in cls and floating:
                bad.append((self.fname, tag, a.get("class")))

    for f in (ROOT / "app/templates").rglob("*.html"):
        p = P()
        p.fname = f.name
        p.feed(f.read_text())
    assert not bad, bad
