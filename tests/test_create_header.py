"""#778: the header's + Create dialog. Always visible on every page (a pill on desktop, a round + on a phone), with Jira's shape (project picker
with recents and search, type picker, title, description, priority, "Create another"). It posts to the existing create route, so the access checks
are that route's; the picker lists only projects the person may CHANGE. Sign-in is on (the multi-user fixtures)."""
import html as _html
import json
import re

from app import access, repo, tree
from tests.test_multi_user import H, _client, _user, local, world  # noqa: F401  (fixtures)

JSON_H = {**H, "Accept": "application/json"}


def _projects(page_html):
    raw = re.search(r'data-create-dialog data-current="[^"]*" data-projects="([^"]*)"', page_html).group(1)
    return [p["slug"] for p in json.loads(_html.unescape(raw))]


def _current(page_html):
    return re.search(r'data-create-dialog data-current="([^"]*)"', page_html).group(1)


# --- the pill: everywhere, always -----------------------------------------------------------------------------------------

def test_create_is_in_the_header_of_every_page_even_without_a_project(world):
    c = _client(world["d1"])
    for path in ("/", "/dashboard", "/recent", "/d1-proj", "/d1-proj/board", "/d1-proj/list"):
        r = c.get(path)
        assert r.status_code == 200, path
        assert 'class="create-pill hidden md:inline-flex"' in r.text and 'class="create-round md:hidden"' in r.text, path
        assert "data-create-dialog" in r.text and "js/create.js" in r.text, path
        assert "+ Request" not in r.text and "quick-act" not in r.text, path           # the old one-tap Request is replaced
    assert 'data-app-switcher' in c.get("/").text                                      # the app switcher (#778) took the slot


def test_the_picker_starts_empty_on_pages_with_no_project_and_on_the_project_you_are_in(world):
    c = _client(world["d1"])
    for path in ("/", "/dashboard", "/recent"):
        assert _current(c.get(path).text) == "", path
    assert _current(c.get("/d1-proj/board").text) == "d1-proj"
    assert _current(c.get("/d1-proj").text) == "d1-proj"


# --- the picker is scoped to what the person may change -------------------------------------------------------------------

def test_picker_lists_only_projects_the_person_may_change(world):
    d1, d2, owner = _client(world["d1"]), _client(world["d2"]), _client(world["owner"])
    assert _projects(d1.get("/").text) == ["d1-proj"]
    assert _projects(d2.get("/").text) == ["d2-proj"]
    assert set(_projects(owner.get("/").text)) >= {"own-proj", "d1-proj", "d2-proj"}
    for secret in ("own-proj", "d2-proj", "Own Project", "D2 Project"):
        assert secret not in d1.get("/").text.split("data-create-dialog", 1)[1].split("</form>", 1)[0]


def test_a_viewer_and_a_shared_member_see_the_right_projects(world):
    from app.db import get_cursor
    vic = _client(world["v"])
    assert _projects(vic.get("/").text) == []                                            # a viewer writes nothing: nothing to offer
    with get_cursor() as cur:                                                            # d1 shared into d2's project as a MEMBER, vic as a VIEWER
        cur.execute("INSERT INTO project_members (project_id, user_id, role) VALUES (%s, %s, 'member'), (%s, %s, 'viewer')",
                    (world["p2"]["id"], world["d1"]["id"], world["p2"]["id"], world["v"]["id"]))
    assert _projects(_client(world["d1"]).get("/").text) == ["d1-proj", "d2-proj"]
    assert _projects(_client(world["v"]).get("/").text) == []                            # visible to the viewer, but not changeable


def test_archived_projects_and_accounts_are_not_offered(world):
    acct = repo.create_project("acct-x", "Acct X", "internal", "owner", short_name="ax", session_roles=[], owner_user_id=world["owner"]["id"])
    tree.set_place(acct["id"], "owner", None, kind="account")
    repo.set_project_archived(world["p2"]["id"], True, "owner")
    slugs = _projects(_client(world["owner"]).get("/").text)
    assert "acct-x" not in slugs and "d2-proj" not in slugs and "d1-proj" in slugs


# --- creating: the existing route, its checks --------------------------------------------------------------------------------

def test_create_from_a_page_with_no_project_makes_the_task_where_the_picker_chose(world):
    c = _client(world["d1"])
    assert _current(c.get("/recent").text) == ""                                         # on Recent: no project yet, so the dialog picks one
    r = c.post("/d1-proj/tasks/new", data={"title": "From the header", "body": "details", "stream": "bug", "priority": "must", "assignee": "ai"},
               headers=JSON_H)
    assert r.status_code == 201
    d = r.json()
    assert d["project"] == "d1-proj" and d["url"] == f"/d1-proj/board?open={d['id']}"
    t = repo.get_task(d["id"])
    assert t["title"] == "From the header" and t["body"] == "details" and t["stream"] == "bug" and t["priority"] == "must"
    assert t["project_id"] == world["p1"]["id"]


def test_create_defaults_to_a_feature_and_needs_a_title(world):
    c = _client(world["d1"])
    d = c.post("/d1-proj/tasks/new", data={"title": "Plain"}, headers=JSON_H).json()
    assert repo.get_task(d["id"])["stream"] == "feature"
    r = c.post("/d1-proj/tasks/new", data={"title": "   "}, headers=JSON_H)
    assert r.status_code == 400 and "title" in r.text


def test_create_without_json_still_redirects_to_the_board(world):
    r = _client(world["d1"]).post("/d1-proj/tasks/new", data={"title": "Old way"}, headers=H, follow_redirects=False)
    assert r.status_code == 303 and "/d1-proj/board?open=" in r.headers["location"]


def test_create_refuses_a_project_the_person_cannot_see_or_change(world):
    d1 = _client(world["d1"])
    before = len(repo.list_tasks(world["p2"]["id"]))
    assert d1.post("/d2-proj/tasks/new", data={"title": "Sneaky"}, headers=JSON_H).status_code == 404       # not theirs: the same 404 as a missing one
    assert d1.post("/own-proj/tasks/new", data={"title": "Sneaky"}, headers=JSON_H).status_code == 404
    assert len(repo.list_tasks(world["p2"]["id"])) == before
    from app.db import get_cursor
    with get_cursor() as cur:
        cur.execute("INSERT INTO project_members (project_id, user_id, role) VALUES (%s, %s, 'viewer')", (world["p2"]["id"], world["v"]["id"]))
    r = _client(world["v"]).post("/d2-proj/tasks/new", data={"title": "Viewer write"}, headers=JSON_H)
    assert r.status_code == 403 and len(repo.list_tasks(world["p2"]["id"])) == before


def test_the_dialog_markup_has_the_jira_shape(world):
    html = _client(world["d1"]).get("/").text.split("data-create-dialog", 1)[1].split("</form>", 1)[0]
    for hook in ("data-create-project", "data-create-find", "Recent projects", "All projects", "data-create-type", "data-create-title", "data-create-body",
                 "data-create-priority", "data-create-another", "data-create-submit", "Create another"):
        assert hook in html, hook
    assert [m for m in re.findall(r'<option value="([a-z]+)">(?:Feature|Bug|Tweak|Review|Error)</option>', html)] == ["feature", "bug", "tweak", "review", "error"]
    js = open("app/static/js/create.js").read()
    assert "/tasks/new" in js and "application/json" in js and "crewboss-create-recent" in js and "try {" in js


def test_the_picker_groups_projects_under_their_account_heading(world):
    from app import tree
    acct = repo.create_project("grp-acct", "Group Account", "client", "owner", short_name="grpacct", session_roles=[])
    tree.set_place(acct["id"], "owner", None, kind="account")
    inner = repo.create_project("grp-inner", "Inner Project", "internal", "owner", short_name="grpinner", session_roles=[])
    tree.set_place(inner["id"], "owner", acct["id"])
    html = _client(world["owner"]).get("/").text
    data = re.search(r'data-projects="([^"]*)"', html).group(1)
    assert "&#34;group&#34;: &#34;Group Account&#34;" in data and "data-create-group" in html       # a heading per account (#778), no sub-project indent
