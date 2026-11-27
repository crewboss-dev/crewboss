"""#771: Runners, Servers and Users moved out of the profile menu into an ADMINISTRATION group at the foot of the left menu, for the owner only.
The group is hidden from everyone else and the routes refuse them too. Sign-in is on (the multi-user fixtures)."""
import re

from tests.test_multi_user import H, _client, local, world  # noqa: F401  (fixtures)


def _drawer(html):
    return html.split('id="nav-drawer"', 1)[1].split("</aside>", 1)[0]


def _admin(html):
    d = _drawer(html)
    return d.split('id="nav-admin"', 1)[1].split("</aside>", 1)[0] if 'id="nav-admin"' in d else ""


def _profile(html):
    return html.split("<header", 1)[1].split('id="user-menu"', 1)[1].split("</header>", 1)[0]


def test_the_group_moved_to_the_administration_section(world):
    """#778: the owner's Runners, Servers and Users are the Administration SECTION (the app switcher), not a group at the foot of Build's menu."""
    html = _client(world["owner"]).get("/").text
    assert _admin(html) == "" and 'data-sec="admin"' not in html
    d = _drawer(_client(world["owner"]).get("/admin").text)
    assert re.findall(r'data-section-row="([a-z]+)"', d)[:3] == ["overview", "users", "machines"]
    assert 'href="/admin/users"' in d and 'href="/machines"' in d and 'href="/runners"' not in d and 'href="/servers"' not in d


def test_the_group_is_absent_for_designers_and_viewers(world):
    for who in ("d1", "v"):
        html = _client(world[who]).get("/").text
        assert 'id="nav-admin"' not in html and "Administration" not in html, who
        assert 'href="/servers"' not in html and 'href="/admin/users"' not in html, who


def test_the_profile_menu_dropped_runners_servers_and_users_for_the_owner(world):
    menu = _profile(_client(world["owner"]).get("/").text)
    assert 'href="/runners"' not in menu and 'href="/servers"' not in menu and 'href="/admin/users"' not in menu
    assert "Sign out" in menu and "Account security" in menu


def test_a_designer_keeps_their_own_runner_in_the_profile_menu(world):
    menu = _profile(_client(world["d1"]).get("/").text)
    assert 'href="/machines"' in menu and 'href="/servers"' not in menu and 'href="/admin/users"' not in menu


def test_the_routes_refuse_a_non_admin(world):
    d1 = _client(world["d1"])
    assert d1.get("/admin/users").status_code == 404                          # the Users page: owner only (access.owner_only)
    assert d1.get("/servers", follow_redirects=False).status_code == 404      # the Servers entry: owner only (access.require_owner)
    assert d1.get("/d1-proj/servers").status_code == 404                      # and the old page (owner only; the Machines page shows a designer only their own)
    assert d1.post("/admin/users/create", data={"username": "zed", "role": "designer"}, headers=H).status_code == 404
    v = _client(world["v"])
    assert v.get("/admin/users").status_code == 404 and v.get("/servers", follow_redirects=False).status_code == 404
    owner = _client(world["owner"])
    assert owner.get("/admin/users").status_code == 200


def test_runners_stays_open_to_a_person_for_their_own_devices_only(world):
    d1 = _client(world["d1"])
    r = d1.get("/runners", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/d1-proj/machines?role=jobs"      # their own project is their runner home
    assert d1.get("/d1-proj/machines").status_code == 200
    assert d1.get("/d2-proj/machines").status_code == 404                              # never someone else's
