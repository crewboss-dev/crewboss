"""#733 (change #80), #771: the menu never changes. One fixed left menu (Now, Dashboard, Recent, then the Projects and Accounts sections) on
EVERY page and the same phone bar (Now, Dashboard, CA, Recent, Menu); only the highlight moves. Everything else is a pill on a record or a
row of the profile menu."""
import re

import pytest

from app import nav
from tests.test_levels_and_accounts import fam  # noqa: F401  (fixture)

ROW = re.compile(r'<a href="([^"]*)" class="nav-row ?(is-active)?"[^>]*data-nav="([^"]+)"[^>]*>.*?<span>([^<]+)</span>', re.S)
TAB = re.compile(r'<a href="([^"]*)" class="tab ?(is-on)?"[^>]*data-key="([^"]+)"[^>]*>.*?<span class="tab-label">([^<]+)</span>', re.S)

FIXED = [("/", "now", "Now"), ("/dashboard", "dashboard", "Dashboard"), ("/recent", "recent", "Recent")]


def _drawer(html):
    return html.split('id="nav-drawer"', 1)[1].split("</aside>", 1)[0]


def _tabbar(html):
    return html.split('id="tabbar"', 1)[1].split("</nav>", 1)[0]


def _rows(html):
    return [(h, k, l) for h, a, k, l in ROW.findall(_drawer(html))]


def _active(html):
    return [k for h, a, k, l in ROW.findall(_drawer(html)) if a]


def _tabs(html):
    return [(h, k, l) for h, a, k, l in TAB.findall(_tabbar(html))]


# (path, the fixed row that is highlighted: "" = none; a record's pages light the record's own row in its section, see test_menu_sections.py)
def _pages(fam):
    p, a = fam["app"]["slug"], fam["acct"]["slug"]
    return [("/", "now"), ("/dashboard", "dashboard"), ("/recent", "recent"),
            (f"/{a}", ""), (f"/{a}/board", ""), (f"/{a}/knowledge", ""),
            # (an account's Servers and Runners pages are the owner's Administration section now, #778: tests/test_app_switcher.py)
            (f"/{p}", ""), (f"/{p}/board", ""), (f"/{p}/changes", ""), (f"/{p}/workflows", ""),
            (f"/{p}/settings", ""), (f"/{p}/knowledge", ""), (f"/{p}/skills", ""), (f"/{p}/rules", ""),
            (f"/{fam['portal']['slug']}/board", "")]


@pytest.fixture(autouse=True)
def _a_person_is_signed_in(monkeypatch):
    """The Runners page needs a signed-in person's row; with sign-in off there is none, so give it one."""
    monkeypatch.setattr("app.routers.runners_pages._user", lambda request: {"id": 1})


def test_the_menu_is_identical_on_every_page(client, fam):
    seen = {}
    for path, _ in _pages(fam):
        r = client.get(path)
        assert r.status_code == 200, path
        seen[path] = r.text
    want = [(h, k, l) for h, k, l in FIXED]
    for path, html in seen.items():
        assert _rows(html) == want, path
        assert [k for h, k, l in _tabs(html)] == ["now", "dashboard", "recent"], path
        assert "data-sidebar-project" not in _drawer(html), path


def test_only_the_highlight_moves(client, fam):
    for path, area in _pages(fam):
        html = client.get(path).text
        assert _active(html) == ([area] if area else []), path
        on_bar = [k for h, a, k, l in TAB.findall(_tabbar(html)) if a]
        assert on_bar == ([area] if area else []), path


def test_the_phone_bar_has_the_assistant_in_the_middle_and_no_more_tab(client, fam):
    html = client.get("/").text
    bar = _tabbar(html)
    assert "tab-more" not in bar and "More" not in bar and 'id="more-sheet"' not in html
    slots = re.findall(r'class="tab tab-ca-slot|class="tab-ca-slot|<a href="[^"]*" class="tab', bar)
    assert len(slots) == 4 and "tab-ca-slot" in slots[2]                       # Now, Dashboard, [CA], Recent, then the Menu button
    assert 'id="ca-btn"' in bar
    assert 'data-nav-toggle' in bar.split("tab-ca-slot", 1)[1].split('id="ca-btn"')[0]   # Menu opens the drawer
    assert nav.bar() == ["now", "dashboard", "ca", "recent", "menu"]


def test_nothing_but_the_fixed_rows_and_two_sections_is_in_the_menu(client, fam):
    drawer = _drawer(client.get("/").text)
    assert drawer.count('class="nav-row') == 3 and drawer.count('data-sec="') == 2          # Favourites and Accounts (#778)
    for gone in ("Focus", "Board", "Changes", "Servers", "Runners", "Integrations", "All projects"):
        assert f"<span>{gone}</span>" not in drawer


def test_the_profile_menu_no_longer_holds_runners_and_servers(client, fam):
    html = client.get("/").text
    menu = html.split('id="user-menu"', 1)[1].split("</header>", 1)[0]
    assert 'href="/runners"' not in menu and 'href="/servers"' not in menu and "Theme" in menu          # #771/#778: they are the Administration section
    assert 'id="nav-admin"' not in html and 'data-section="admin"' not in _drawer(html)     # #778: the group moved to the Administration section
    assert 'data-app="admin"' in html and 'href="/admin"' in html                           # reached from the app switcher
    assert 'id="me-pop"' in html and 'id="me-btn"' in html                       # the phone: the avatar top left opens the same menu


def test_profile_entry_points_land_on_the_account_pages(client, fam):
    r = client.get("/servers", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].endswith("/machines?role=deploys") and "/acme" in r.headers["location"]
    r = client.get("/runners", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].endswith("/machines?role=jobs")
    r = client.get("/machines", follow_redirects=False)                          # #810: the one Machines page
    assert r.status_code == 302 and r.headers["location"].endswith("/machines") and "/acme" in r.headers["location"]


@pytest.mark.parametrize("slug", ["dashboard", "accounts", "projects", "servers", "runners"])
def test_the_menus_paths_cannot_be_project_slugs(slug):
    from app import repo
    with pytest.raises(repo.BadRequest):
        repo.validate_slug(slug)
