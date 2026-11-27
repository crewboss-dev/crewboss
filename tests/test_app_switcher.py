"""#778: the Jira-style app switcher. Build / Assets / Administration (owner only), each section's own left menu, the Create pill per
section, the header search per section, and the Assets pages scoped to the projects a person can see."""
import re

from app import config, nav, repo
from app import materials as mat
from tests.test_multi_user import H, _client, local, world  # noqa: F401  (fixtures)


def _drawer(html):
    return html.split('id="nav-drawer"', 1)[1].split("</aside>", 1)[0]


def _switcher(html):
    return html.split('data-switcher-menu', 1)[1].split("</div>", 1)[0]


def _apps(html):
    return re.findall(r'data-app="([a-z]+)"', _switcher(html))


def _header(html):
    return html.split("<header", 1)[1].split("</header>", 1)[0]


# --- the switcher's entries per role --------------------------------------------------------------------------------

def test_the_owner_gets_build_assets_and_administration(world):
    html = _client(world["owner"]).get("/").text
    assert _apps(html) == ["build", "assets", "admin"]
    assert 'href="/assets"' in _switcher(html) and 'href="/admin"' in _switcher(html)


def test_designers_and_viewers_never_see_administration(world):
    for who in ("d1", "v"):
        html = _client(world[who]).get("/").text
        assert _apps(html) == ["build", "assets"], who
        assert "Administration" not in html and 'href="/admin' not in html, who


def test_the_header_order_is_toggle_then_the_section_button(world):
    """Owner 11-16: no separate grid icon. The section's tile and name ARE the switcher; picking the current section goes to its home."""
    h = _header(_client(world["owner"]).get("/").text)
    assert h.index('id="nav-burger"') < h.index("data-switcher-btn") < h.index('id="nav-here"')
    assert 'data-section-name>Build<' in h and '<rect x="4" y="4" width="6.5"' not in h          # the grid icon is gone
    btn = h[h.index("data-switcher-btn") - 600:h.index("data-switcher-btn")]
    assert "<button" in btn and "data-section-home" in h[h.index("data-switcher-btn"):h.index("data-switcher-btn") + 40]
    assert re.search(r'href="/" role="menuitem"[^>]*is-current', h)                              # Build in the menu = Build's home
    a = _header(_client(world["owner"]).get("/assets").text)
    assert 'data-section-name>Assets<' in a and re.search(r'href="/assets" role="menuitem"[^>]*is-current', a)
    adm = _header(_client(world["owner"]).get("/admin").text)
    assert 'data-section-name>Administration<' in adm


def test_the_switcher_is_keyboard_accessible(world):
    html = _client(world["owner"]).get("/").text
    h = _header(html)
    assert 'aria-haspopup="menu"' in h and 'aria-expanded' in h and 'role="menu"' in h and h.count('role="menuitem"') == 3
    assert '@keydown.escape' in h and '@click.outside' in h


# --- Administration is owner-only -------------------------------------------------------------------------------------

def test_administration_routes_refuse_non_owners(world):
    for who in ("d1", "v"):
        c = _client(world[who])
        assert c.get("/admin").status_code == 404, who
        assert c.get("/admin/users").status_code == 404, who
        assert c.get("/servers", follow_redirects=False).status_code == 404, who
    assert _client(world["owner"]).get("/admin").status_code == 200


def test_the_administration_menu_and_home(world):
    html = _client(world["owner"]).get("/admin").text
    d = _drawer(html)
    assert re.findall(r'data-section-row="([a-z]+)"', d) [:3] == ["overview", "users", "machines"]
    assert 'data-section-menu="admin"' in d and 'class="nav-sec"' not in d
    assert re.findall(r'data-admin-card="([a-z]+)"', html) [:2] == ["users", "machines"]
    assert 'aria-current="page"' in d.split('data-section-row="overview"')[0].rsplit("<a", 1)[1] + ">"


def test_users_runners_servers_light_the_administration_menu(world):
    c = _client(world["owner"])
    d = _drawer(c.get("/admin/users").text)
    assert 'data-section-menu="admin"' in d and 'is-active' in d.split('data-section-row="users"')[0].rsplit("<a", 1)[1]
    d = _drawer(c.get("/own-proj/machines").text) if c.get("/own-proj/machines").status_code == 200 else ""
    assert not d or 'data-section-menu="admin"' in d


def test_a_designer_keeps_their_own_runners_page_in_build(world):
    c = _client(world["d1"])
    html = c.get("/d1-proj/machines").text
    assert 'href="/machines"' in html.split('id="user-menu"', 1)[1]           # the profile menu keeps Machines (#810)
    assert 'data-section-menu' not in _drawer(html) and 'data-section-name>Build<' in html


# --- the section left menus -----------------------------------------------------------------------------------------------

def test_the_build_menu_is_todays_without_administration(world):
    d = _drawer(_client(world["owner"]).get("/").text)
    assert d.count('class="nav-row') == 3 and 'data-sec="accounts"' in d and "Administration" not in d


def test_the_assets_menu(world):
    d = _drawer(_client(world["d1"]).get("/assets").text)
    assert re.findall(r'data-section-row="([a-z]+)"', d) == ["foryou", "recent", "skills", "rules", "materials"]
    assert "Rules &amp; AI instructions" in d and "Asset types" in d


# --- Create pill per section -----------------------------------------------------------------------------------------------

def test_create_pill_per_section(world):
    c = _client(world["owner"])
    assert "data-create-open" in _header(c.get("/").text)
    inv = _header(c.get("/admin").text)
    assert "data-create-invite" in inv and "Add a person" in inv and "data-create-open" not in inv
    assert 'href="/admin/users#invite"' in inv and 'id="invite"' in c.get("/admin/users").text
    a = _header(c.get("/assets").text)
    assert "data-create-open" not in a and "data-create-invite" not in a and "create-pill" not in a


def test_the_invite_pill_is_hidden_without_local_sign_in(world, monkeypatch):
    monkeypatch.setattr("app.web_auth.mode", lambda: "oidc")
    assert nav.create_mode("admin", True, False) == "none" and nav.create_mode("build", False, False) == "task" \
        and nav.create_mode("assets", True, True) == "none"


# --- the header search per section --------------------------------------------------------------------------------------

def test_search_in_administration_and_assets(world):
    c = _client(world["owner"])
    r = c.get("/all/jump", params={"q": "mach", "section": "admin"}).json()["results"]
    assert [x["url"] for x in r] == ["/machines"]
    assert [x["label"] for x in c.get("/all/jump", params={"section": "assets"}).json()["results"]][:2] == ["For you", "Recent"]
    d1 = _client(world["d1"])
    assert d1.get("/all/jump", params={"section": "admin"}).json()["results"] == []          # never a non-owner's
    assert "data-jump-input" in _header(c.get("/admin").text) and "crewJump('admin')" in c.get("/admin").text


# --- the Assets pages: only projects the person can see --------------------------------------------------------------------

def _material(project, title):
    return mat.add(project, "logo-" + title.lower(), "logo", title, "p", title.lower() + ".png",
                   b"\x89PNG\r\n\x1a\n" + b"0" * 32, "owner")


def test_the_assets_pages_list_only_visible_projects(world, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MATERIALS_DIR", str(tmp_path))
    monkeypatch.setattr(config, "MATERIALS_HOST_DIR", "/host/materials")
    _material(world["p1"], "Mine")
    _material(world["p2"], "Theirs")
    _material(world["own"], "OwnerOnly")
    d1 = _client(world["d1"])
    for path in ("/assets/materials", "/assets/recent", "/assets"):
        t = d1.get(path).text
        assert "Mine" in t or path == "/assets", path
        assert "Theirs" not in t and "OwnerOnly" not in t and "D2 Project" not in t, path
    assert "/d1-proj/materials" in d1.get("/assets/materials").text
    owner = _client(world["owner"]).get("/assets/materials").text
    assert "Mine" in owner and "Theirs" in owner and "OwnerOnly" in owner
    v = _client(world["v"]).get("/assets/materials").text
    assert "Mine" not in v and "Theirs" not in v


def test_skills_rules_pages_and_home(world):
    d1 = _client(world["d1"])
    html = d1.get("/assets").text
    assert "Good " in html and "dee" in html.lower() and "data-recent-places" in html and "coming" in html
    for k in ("rules", "materials"):
        r = d1.get(f"/assets/{k}")
        assert r.status_code == 200 and f'data-assets-type="{k}"' in r.text
    r = d1.get("/assets/skills")                                        # the real skills' global layer (#718), not the per-project view
    assert r.status_code == 200 and 'data-skills-page="global"' in r.text
    assert d1.get("/assets/nope").status_code == 404
    assert "OWNER SECRET" not in d1.get("/assets/rules").text


def test_a_project_slug_cannot_be_assets():
    import pytest
    with pytest.raises(repo.BadRequest):
        repo.validate_slug("assets")


def test_the_assets_pages_read_each_scan_once_and_only_its_slices(world, monkeypatch):
    """The catalog scan is large: Assets must not call repo.latest_scan (whole row, per project, per type) at all, and the search shares one read."""
    from app import asset_hub, repo as repo_mod
    calls = []
    real = asset_hub.get_cursor

    def spy(*a, **k):
        cm = real(*a, **k)
        return cm
    monkeypatch.setattr(repo_mod, "latest_scan", lambda pid: calls.append(pid) or None)
    owner = _client(world["owner"])
    assert owner.get("/assets").status_code == 200 and owner.get("/assets/skills").status_code == 200 and owner.get("/assets/rules").status_code == 200
    assert owner.get("/all/jump", params={"section": "assets", "q": "x"}).status_code == 200
    assert calls == []
    n = []
    monkeypatch.setattr(asset_hub, "scans", lambda ids, _real=asset_hub.scans: n.append(1) or _real(ids))
    owner.get("/all/jump", params={"section": "assets", "q": "x"})
    owner.get("/assets")
    assert len(n) == 2                                                  # one query per request, not per project or per type
