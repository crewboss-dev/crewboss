"""Task #155, kept through #479: the app shell is sized to the DYNAMIC viewport so the page can't scroll into dead
space on mobile (a collapsing URL bar makes a percentage-height 100% taller than the visible area). Since #479 the
shell is the ERP's: <body> is a fixed-height flex column that never scrolls (100dvh, 100vh fallback, in input.css),
neither <html> nor <body> carries a percentage height, and <main id="page"> is the ONE scroll container, with
overscroll-contain so a momentum flick at its end can't chain out. It holds at every level: All projects, a project
page and an account page."""
import re
from pathlib import Path

from app import repo, tree

ROOT = Path(__file__).resolve().parent.parent
CSS = (ROOT / "frontend/src/input.css").read_text()


def _rule(selector):
    m = re.search(r"(?m)^" + re.escape(selector) + r"\s*\{([^}]*)\}", CSS)
    assert m, f"no top-level {selector} rule in input.css"
    return m.group(1)


def _assert_shell(html):
    body = html.split("<body", 1)[1].split(">", 1)[0]
    html_tag = html.split("<html", 1)[1].split(">", 1)[0]
    assert "h-full" not in body and "h-full" not in html_tag, (html_tag, body)
    assert "flex flex-col" in body, body
    main = html.split("<main", 1)[1].split(">", 1)[0]
    assert 'id="page"' in main, main
    # the header, drawer and phone bar sit outside the scroller (the ERP's chrome)
    assert html.index('<header class="chrome-fixed') < html.index('<main id="page"')
    assert html.index('id="nav-drawer"') < html.index('<main id="page"')


def test_shell_css_is_the_dynamic_viewport():
    body = _rule("body")
    assert "100dvh" in body and "100vh" in body and "overflow: hidden" in body, body
    page = _rule("#page")
    assert "overflow-y: auto" in page and "overscroll-behavior: contain" in page and "min-height: 0" in page, page


def test_home_shell_sizes_to_dynamic_viewport(client, project):
    _assert_shell(client.get("/").text)


def test_project_shell_sizes_to_dynamic_viewport(client, project):
    _assert_shell(client.get("/test-proj").text)


def test_account_shell_sizes_to_dynamic_viewport(client, project):
    acct = repo.create_project("acme", "Acme", "internal", "owner")
    tree.set_place(acct["id"], "owner", None, kind="account")
    _assert_shell(client.get("/acme").text)


def test_html_still_sets_viewport_fit_cover(client, project):
    # dvh + env(safe-area-inset-*) depend on viewport-fit=cover staying in the viewport meta.
    assert "viewport-fit=cover" in client.get("/").text


def test_task_drawer_starts_off_screen_before_alpine_runs(client):
    """The task sheet (owner 11-18: a full-page window over the current page) is off-screen from the FIRST paint: .task-sheet is translated
    away and hidden in CSS until Alpine adds .is-open, so a page load never flashes it. The binding is object syntax, so Alpine adds and
    removes only that class."""
    repo.create_project("drawer-proj", "Drawer Project", "internal", "system")
    html = client.get("/drawer-proj/board").text
    aside = html.split('<aside class="task-sheet', 1)[1].split(">", 1)[0]
    assert "is-open" not in aside.split('"', 1)[0]
    binding = aside.split(':class="', 1)[1].split('"', 1)[0]
    assert binding.strip().startswith("{") and "'is-open'" in binding, binding
    css = (Path(__file__).resolve().parent.parent / "app/static/css/shell.css").read_text()
    rule = css.split(".task-sheet{", 1)[1].split("}", 1)[0]
    assert "visibility:hidden" in rule and "translateX(100%)" in rule
    assert ".task-sheet.is-open{" in css


def test_the_drawer_is_the_same_on_home_and_inside_a_project(client, project):
    """#531 -> #733: the menu never changes: no project pages in the drawer or the phone bar on any page; they are pills on the record."""
    client.get(f"/{project['slug']}/board")
    html = client.get("/").text
    drawer = html.split('id="nav-drawer"', 1)[1].split("</aside>", 1)[0]
    assert 'data-nav="now"' in drawer and "data-sidebar-project" not in drawer and 'data-nav="board"' not in drawer
    tabbar = html.split('id="tabbar"', 1)[1].split("</nav>", 1)[0]
    assert f'/{project["slug"]}/board' not in tabbar
    page = client.get(f"/{project['slug']}/board").text
    inner = page.split('id="nav-drawer"', 1)[1].split("</aside>", 1)[0]
    assert 'data-nav="board"' not in inner and 'data-pill="board"' in page and "data-record-head" in page


def test_menu_links_are_in_the_profile_menu_only_and_there_is_no_admin_link(client, project, monkeypatch):
    """#577 (owner 10-27): no global Admin link; the instance's menu_links stay: in the profile menu (#733: the menu itself is fixed)."""
    from app import config
    monkeypatch.setattr(config, "MENU_LINKS", [{"label": "Company site", "url": "https://example.com"}])
    for path in ("/", f"/{project['slug']}/board"):
        html = client.get(path).text
        drawer = html.split('id="nav-drawer"', 1)[1].split("</aside>", 1)[0]
        head = html.split("<header", 1)[1].split("</header>", 1)[0]
        menu = head.split('id="user-menu"', 1)[1]
        assert 'href="https://example.com"' in menu and "Company site" in menu
        assert 'href="https://example.com"' not in drawer
        assert ">Admin<" not in html and 'data-surface="admin"' not in html and "admin.example.com" not in html
    assert not hasattr(config, "ADMIN_URL")


def test_task_drawer_top_is_a_phone_drag_zone_and_blocks_pull_to_refresh(client):
    """#601 (owner: "I cannot grab the bar at the top and flick down to close the drawer. It tries to reload the page."), kept for the task
    sheet (11-18): the head row (Back, the task number, the x) is one [data-sheet-grab] zone that is touch-none on a phone, the x stays a
    button, the body still scrolls without chaining to the page, and chrome.js sets html.sheet-open while it is open (what blocks
    pull-to-refresh)."""
    repo.create_project("grab-proj", "Grab Project", "internal", "system")
    html = client.get("/grab-proj/board").text
    shell = html.split("data-drag-sheet data-task-sheet", 1)[1].split("</aside>", 1)[0]
    zone, content = shell.split('class="task-sheet-body"', 1)
    assert 'data-sheet-grab class="task-sheet-head"' in zone
    assert "data-task-sheet-exit" in zone and 'aria-label="Close"' in zone and "<button" in zone   # exit + x in the zone
    assert 'id="drawer-content"' in content
    css = (Path(__file__).resolve().parent.parent / "app/static/css/shell.css").read_text()
    assert ".task-sheet-head{ touch-action:none; }" in css
    body = css.split(".task-sheet-body{", 1)[1].split("}", 1)[0]
    assert "overflow-y:auto" in body and "overscroll-behavior:contain" in body
    js = (Path(__file__).resolve().parent.parent / "app/static/js/chrome.js").read_text()
    assert "function syncSheetOpen" in js and 'Alpine.store("drawer")' in js and 'Alpine.effect' in js
    assert 'root.classList.toggle("sheet-open", !!document.querySelector(".m-sheet.is-open"))' not in js
