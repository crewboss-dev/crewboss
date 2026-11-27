"""#778: favourite projects. A star per PERSON (server-side, follows them across devices), only on projects they can see, shown as a Favourites section above
Accounts in the left menu, first in the Create dialog and the jump results."""
import re

from app import favourites, repo
from tests.test_multi_user import H, _client, local, world  # noqa: F401  (fixtures)


def _drawer(html):
    return html.split('id="nav-drawer"', 1)[1].split("</aside>", 1)[0]


def _fav_section(html):
    m = re.search(r'<div class="nav-sec[^"]*" data-sec="favourites"[^>]*>(.*?)(?=<div class="nav-sec[ "]|</nav>)', _drawer(html), re.S)
    return m.group(1) if m else ""


def _star(c, slug):
    return c.post("/all/favourites", data={"slug": slug}, headers=H)


def test_toggle_stars_and_unstars(world):
    c = _client(world["d1"])
    r = _star(c, "d1-proj")
    assert r.status_code == 200 and r.json()["starred"] is True and r.json()["slug"] == "d1-proj"
    assert _star(c, "d1-proj").json()["starred"] is False
    assert favourites.rows(__import__("app.access", fromlist=["x"]).principal_for_user_id(world["d1"]["id"])) == []


def test_the_menu_section_lists_my_stars_above_accounts_and_highlights_the_current(world):
    c = _client(world["d1"])
    _star(c, "d1-proj")
    html = c.get("/d1-proj/board").text
    d = _drawer(html)
    assert d.index('data-sec="favourites"') < d.index('data-sec="accounts"')
    sec = _fav_section(html)
    assert re.findall(r'data-fav-row="([^"]+)"', sec) == ["d1-proj"]
    assert re.search(r'class="nav-rec is-active"[^>]*data-record="d1-proj"', sec)
    assert 'aria-pressed="true"' in sec and "is-empty" not in d.split('data-sec="favourites"')[0][-60:]


def test_the_empty_state_has_a_hint_and_the_section_folds(world):
    html = _client(world["d1"]).get("/").text
    sec = _fav_section(html)
    assert 'data-sec="favourites"' in html and "is-empty" in html and "Star a project to keep it here" in sec
    assert re.findall(r'data-fav-row="', sec) == [] and 'data-sec-toggle="favourites"' in sec
    assert "fav-seen" in open("app/static/js/chrome.js").read()          # the hint is dropped for good once something is starred


def test_stars_are_per_person(world):
    _star(_client(world["d1"]), "d1-proj")
    owner = _client(world["owner"])
    assert re.findall(r'data-fav-row="([^"]+)"', _fav_section(owner.get("/").text)) == []
    _star(owner, "own-proj")
    assert re.findall(r'data-fav-row="([^"]+)"', _fav_section(_client(world["d1"]).get("/").text)) == ["d1-proj"]
    assert re.findall(r'data-fav-row="([^"]+)"', _fav_section(owner.get("/").text)) == ["own-proj"]


def test_you_cannot_star_a_project_you_cannot_see(world):
    d1 = _client(world["d1"])
    assert _star(d1, "d2-proj").status_code == 404 and _star(d1, "own-proj").status_code == 404 and _star(d1, "no-such").status_code == 404
    assert "d2-proj" not in _fav_section(d1.get("/").text)


def test_a_project_that_becomes_invisible_drops_out(world):
    owner = _client(world["owner"])
    _star(owner, "d2-proj")
    assert "d2-proj" in _fav_section(owner.get("/").text)
    repo.set_project_archived(world["p2"]["id"], True, "owner")                 # archived: hidden from the list
    assert "d2-proj" not in _fav_section(owner.get("/").text)
    # a row for a project the person can no longer see is never returned, whatever the table holds
    from app import access
    from app.db import get_cursor
    with get_cursor() as cur:
        cur.execute("INSERT INTO project_favourites (user_id, project_id) VALUES (%s, %s)", (world["d1"]["id"], world["p2"]["id"]))
    assert "d2-proj" not in _fav_section(_client(world["d1"]).get("/").text)
    assert favourites.visible_ids(access.principal_for_user_id(world["d1"]["id"])) == set()


def test_the_star_is_on_the_project_header_and_the_menu_rows(world):
    c = _client(world["d1"])
    html = c.get("/d1-proj").text
    head = html.split("data-record-head", 1)[1].split("</nav>", 1)[0]
    assert 'class="nav-fav head-fav' in head and 'data-fav="d1-proj"' in head and 'aria-pressed="false"' in head
    assert 'class="nav-recwrap' in _drawer(html) and 'data-fav="d1-proj"' in _drawer(html)
    _star(c, "d1-proj")
    assert 'aria-pressed="true"' in c.get("/d1-proj").text.split("data-record-head", 1)[1].split("</nav>", 1)[0]


def test_favourites_come_first_in_the_create_dialog_and_the_jump_results(world):
    owner = _client(world["owner"])
    _star(owner, "d2-proj")
    html = owner.get("/").text
    data = re.search(r'data-projects="([^"]*)"', html).group(1)
    assert "&#34;fav&#34;: true" in data
    assert "Favourite projects" in html and 'data-create-option="favourite"' in html
    res = owner.get("/all/jump", params={"q": "proj"}).json()["results"]
    assert res[0]["label"] == "D2 Project" and res[0]["fav"] is True
    assert owner.get("/all/jump", params={"q": ""}).json()["results"][0]["label"] == "D2 Project"


def test_a_renamed_hidden_project_echoes_only_the_slug_that_was_posted(world):
    from app import tree
    tree.rename_project(world["p2"]["id"], "owner", "Secret Rename", "secret-renamed")
    r = _star(_client(world["d1"]), "d2-proj")
    assert r.status_code == 404 and "d2-proj" in r.text and "secret-renamed" not in r.text and "Secret Rename" not in r.text
    assert _star(_client(world["d1"]), "secret-renamed").status_code == 404
