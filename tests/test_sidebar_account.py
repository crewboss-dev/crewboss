"""The menu drawer's account block (who is signed in, and how) and the instance's menu_links (no hard-coded link)."""
import logging

import pytest

from app import config, web_auth
from tests.test_local_auth import _tc, local, lc, owner, signed  # noqa: F401  (fixtures)


def _drawer(html):
    return html.split('id="nav-drawer"', 1)[1].split("</aside>", 1)[0]


def _header(html):
    return html.split("<header", 1)[1].split("</header>", 1)[0]


def test_local_session_shows_username_and_local_sign_in(signed):
    html = signed["client"].get("/").text
    block = _header(html).split('id="user-wrap"', 1)[1]
    assert "owner &middot; Local sign-in" in block
    assert "NetBird" not in html and "no app login" not in html
    assert "/account/security" in block and 'action="/auth/logout"' in block and "data-sign-out" in block
    pop = html.split('id="me-pop"', 1)[1]
    assert 'id="me-panel"' in pop and 'id="me-btn"' in html      # the phone's avatar popover takes the drawer's #user-menu rows when opened


def test_sign_in_off_is_neutral_and_has_no_account_links(client, monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH", "off")
    html = client.get("/").text
    block = _header(html).split('id="user-wrap"', 1)[1]
    assert "Sign-in off" in block
    assert "NetBird" not in html and "/account/security" not in block and "/auth/logout" not in block


def test_signin_label():
    assert web_auth.signin_label(None) == "Sign-in off"
    assert web_auth.signin_label({"user_id": 3}) == "Local sign-in"
    assert web_auth.signin_label({"user_id": None, "oid": "x"}) == "Microsoft sign-in"


# --- menu_links ---------------------------------------------------------------------------------------------------------

def test_menu_links_valid():
    raw = [{"label": " Company site ", "url": "https://example.com"}, {"label": "Wiki", "url": "http://wiki.local:8080/x"}]
    assert config.parse_menu_links(raw) == [{"label": "Company site", "url": "https://example.com"},
                                            {"label": "Wiki", "url": "http://wiki.local:8080/x"}]


@pytest.mark.parametrize("bad", [
    {"label": "x", "url": "javascript:alert(1)"}, {"label": "x", "url": "ftp://example.com"}, {"label": "x", "url": "example.com"},
    {"label": "x", "url": "https://"}, {"label": "", "url": "https://example.com"}, {"label": "  ", "url": "https://example.com"},
    {"label": "L" * 41, "url": "https://example.com"}, {"label": 5, "url": "https://example.com"}, {"label": "x"},
    {"url": "https://example.com"}, "https://example.com", None, 7, ["a"]])
def test_menu_links_invalid_entries_are_skipped_with_a_warning(bad, caplog):
    good = {"label": "Ok", "url": "https://example.com"}
    with caplog.at_level(logging.WARNING, logger="app.config"):
        assert config.parse_menu_links([bad, good]) == [good]
    assert "menu_links[0] skipped" in caplog.text


@pytest.mark.parametrize("raw", [None, [], "https://example.com", {"label": "x", "url": "https://example.com"}, 5])
def test_menu_links_empty_or_wrong_shape_is_no_links_and_never_raises(raw):
    assert config.parse_menu_links(raw) == []


def test_no_menu_links_renders_no_link_section(client, monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH", "off")
    monkeypatch.setattr(config, "MENU_LINKS", [])
    monkeypatch.setattr(config, "ERP_URL", "https://erp.example.com/")     # ERP_URL no longer feeds the menu
    html = client.get("/").text
    assert "data-menu-links" not in html and "erp.example.com" not in html and "Mallen" not in _drawer(html)
    assert "Company site" not in html


def test_menu_links_render_in_the_profile_menu(client, monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH", "off")
    monkeypatch.setattr(config, "MENU_LINKS", [{"label": "Company site", "url": "https://example.com"}])
    html = client.get("/").text
    assert html.count('href="https://example.com"') == 1 and "Company site" in _header(html).split('id="user-menu"', 1)[1]   # not a menu row: the profile menu


def test_header_order_avatar_far_right_ca_beside_create_none_in_sidebar(client, monkeypatch):
    """#786: search, + Create, CA, (gap), session dots, avatar at the far right; the sidebar foot is gone."""
    monkeypatch.setattr(config, "WEB_AUTH", "off")
    html = client.get("/").text
    head = _header(html)
    drawer = _drawer(html)
    assert 'id="user-btn"' not in drawer and 'id="user-wrap"' not in drawer and 'id="user-menu"' not in drawer
    assert 'id="user-btn"' in head and 'id="user-menu"' in head and "Sign-in off" in head
    pos = {k: head.index(k) for k in ("data-jump-input", "data-create-open", 'class="ca-hdr-btn"', 'id="user-btn"')}
    assert pos["data-jump-input"] < pos["data-create-open"] < pos['class="ca-hdr-btn"'] < pos['id="user-btn"']
    between = head[head.index("data-create-open"):pos['class="ca-hdr-btn"']]
    assert between.count("<button") == 2 and "create-round" in between          # only Create (desktop pill + phone round) sits between Create and CA
    assert 'data-ca-open' in head.split('class="ca-hdr-btn"', 1)[1].split(">", 1)[0] and 'aria-controls="ca-drawer"' in head
    assert head.index('class="ca-hdr-btn"') < head.index('class="ml-auto')          # CA is in the left group, before the flexible gap
    assert head.rstrip().endswith("</div>") and head.index('id="user-wrap"') > head.index('class="ml-auto')


def test_header_has_no_back_forward_buttons(client, monkeypatch):
    """#786 (owner 11-12): the header's back/forward buttons are gone; the left side is the sidebar toggle, the app switcher, the section name."""
    monkeypatch.setattr(config, "WEB_AUTH", "off")
    html = client.get("/").text
    head = _header(html)
    assert "data-history" not in html and 'id="pwa-nav"' not in head
    assert head.index('id="nav-burger"') < head.index("data-app-switcher") < head.index("data-section-name")
