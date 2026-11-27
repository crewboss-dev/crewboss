"""#772 with real people (#713): the Summary and the List only ever show what a person may see, and saved filters are the person's own.
Sign-in is on (the multi-user fixtures); a designer sees only what they own or were shared."""
import json
import re

from app import repo, tree
from tests.test_multi_user import H, _client, local, world  # noqa: F401  (fixtures)


def _tiles(html):
    seg = html.split("data-by-status", 1)[1][:2500]
    return {l: int(n) for n, l in re.findall(r'tabular[^>]*>(\d+)</div>\s*<div class="text-sm text-charcoal/60 mt-0.5">([^<]+)</div>', seg)}


def test_summary_counts_only_what_the_person_may_see(world):
    child = repo.create_project("hidden-kid", "Hidden Kid", "internal", "owner", short_name="hkid", session_roles=[], owner_user_id=world["owner"]["id"])
    tree.set_place(child["id"], "owner", world["p1"]["id"])               # under d1's project, but made by the owner: d1 was not shared into it
    repo.create_task(child["id"], "KID SECRET", "", "must", "bug", [], {}, "owner")
    d1, owner = _client(world["d1"]), _client(world["owner"])
    mine = d1.get("/d1-proj")
    assert mine.status_code == 200 and "KID SECRET" not in mine.text
    assert _tiles(mine.text)["Backlog"] == 1                                # D1 task only
    assert _tiles(owner.get("/d1-proj").text)["Backlog"] == 2               # the owner's Summary includes the child
    assert d1.get("/own-proj").status_code == 404 and d1.get("/d2-proj").status_code == 404 and d1.get("/hidden-kid").status_code == 404


def test_summary_never_lists_another_persons_reviews_or_activity(world):
    repo.update_task(world["t2"]["id"], {"status": "review"}, "owner")
    d1 = _client(world["d1"])
    html = d1.get("/d1-proj").text
    assert "D2 SECRET" not in html and "OWNER SECRET" not in html
    assert "D2 SECRET" not in d1.get("/d1-proj").text


def test_list_is_scoped_by_url_and_id(world):
    d1, d2 = _client(world["d1"]), _client(world["d2"])
    assert "D1 task" in d1.get("/d1-proj/list").text and "D2 SECRET" not in d1.get("/d1-proj/list").text
    assert d1.get("/d2-proj/list").status_code == 404 and d1.get("/d2-proj/tasks/list").status_code == 404
    assert d1.get("/own-proj/list?open=%d" % world["t_own"]["id"]).status_code == 404
    # a task of another project can't be opened through MY list by id
    assert "OWNER SECRET" not in d1.get(f"/d1-proj/list?open={world['t_own']['id']}").text
    assert d2.post(f"/d1-proj/tasks/{world['t1']['id']}/status", data={"status": "done"}, headers=H).status_code == 404


# --- saved filters --------------------------------------------------------------------------------------------------------

def _save(c, page, name, data, project="", default=False):
    return c.post("/all/filters", data={"page": page, "name": name, "data": json.dumps(data), "project": project, "is_default": "1" if default else ""}, headers=H)


def _names(c, page, project=""):
    r = c.get("/all/filters", params={"page": page, "project": project})
    assert r.status_code == 200
    return [f["name"] for f in r.json()["filters"]]


def test_saved_filters_are_never_visible_to_another_person(world):
    d1, d2, owner = _client(world["d1"]), _client(world["d2"]), _client(world["owner"])
    r = _save(d1, "list", "My urgent", {"priority": "must", "assignee": "owner"}, "d1-proj")
    assert r.status_code == 201
    fid = r.json()["filter"]["id"]
    assert _names(d1, "list", "d1-proj") == ["My urgent"]
    assert _names(owner, "list", "d1-proj") == []                           # the owner sees none of d1's: they are d1's own
    assert d2.get("/all/filters", params={"page": "list", "project": "d1-proj"}).status_code == 404   # d2 can't even ask about d1's project
    assert _names(d2, "list", "d2-proj") == []                              # and their own project's is separate
    # another person's id: rename, default and delete are 404s, the filter survives
    for path, data in ((f"/all/filters/{fid}/rename", {"name": "stolen"}), (f"/all/filters/{fid}/default", {"on": "1"}), (f"/all/filters/{fid}/delete", {})):
        assert d2.post(path, data=data, headers=H).status_code == 404
    assert _names(d1, "list", "d1-proj") == ["My urgent"]


def test_saved_filters_follow_the_project_and_the_page_type(world):
    d1 = _client(world["d1"])
    _save(d1, "list", "A", {"q": "x"}, "d1-proj")
    _save(d1, "board", "B", {"priority": "could"}, "d1-proj")
    _save(d1, "recent", "R", {"kind": "comments"})
    assert _names(d1, "list", "d1-proj") == ["A"] and _names(d1, "board", "d1-proj") == ["B"] and _names(d1, "recent") == ["R"]
    # a project they can't see: no saving, no listing (the same 404 as a missing project)
    assert _save(d1, "list", "x", {}, "d2-proj").status_code == 404
    assert d1.get("/all/filters", params={"page": "list", "project": "d2-proj"}).status_code == 404
    assert _save(d1, "list", "no project", {}).status_code == 400


def test_apply_saved_filter_keeps_only_known_keys_and_applies_through_the_page(world):
    d1 = _client(world["d1"])
    r = _save(d1, "list", "Mine", {"assignee": "ai", "evil": "1", "cols": "reporter,progress", "sort": "title"}, "d1-proj")
    data = r.json()["filter"]["data"]
    assert data == {"assignee": "ai", "cols": "reporter,progress", "sort": "title"}
    from urllib.parse import urlencode
    page = d1.get("/d1-proj/list?" + urlencode(data))
    assert page.status_code == 200 and "D1 task" in page.text
    assert "tl-hide-reporter" not in re.search(r'<table class="([^"]*)" data-tl-table', page.text).group(1)
    assert _client(world["d2"]).get("/all/filters", params={"page": "list", "project": "d1-proj"}).status_code == 404


def test_saving_the_same_name_replaces_and_rename_delete_work(world):
    d1 = _client(world["d1"])
    _save(d1, "board", "Urgent", {"priority": "must"}, "d1-proj")
    r = _save(d1, "board", "urgent", {"priority": "should"}, "d1-proj")          # same name (case-insensitive): replaced, not duplicated
    assert _names(d1, "board", "d1-proj") == ["urgent"] and r.json()["filter"]["data"] == {"priority": "should"}
    fid = r.json()["filter"]["id"]
    assert d1.post(f"/all/filters/{fid}/rename", data={"name": "Renamed"}, headers=H).json()["filter"]["name"] == "Renamed"
    assert d1.post(f"/all/filters/{fid}/delete", headers=H).status_code == 200
    assert _names(d1, "board", "d1-proj") == []
    assert d1.post(f"/all/filters/{fid}/delete", headers=H).status_code == 404


def test_a_default_filter_opens_a_bare_page_and_one_default_per_page(world):
    d1 = _client(world["d1"])
    _save(d1, "list", "One", {"assignee": "owner"}, "d1-proj", default=True)
    two = _save(d1, "list", "Two", {"priority": "must"}, "d1-proj").json()["filter"]
    d1.post(f"/all/filters/{two['id']}/default", data={"on": "1"}, headers=H)
    flags = {f["name"]: f["is_default"] for f in d1.get("/all/filters", params={"page": "list", "project": "d1-proj"}).json()["filters"]}
    assert flags == {"One": False, "Two": True}
    r = d1.get("/d1-proj/list", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/d1-proj/list?priority=must"
    assert d1.get("/d1-proj/list?q=x", follow_redirects=False).status_code == 200      # anything in the query means the person chose
    assert _client(world["d2"]).get("/d2-proj/list", follow_redirects=False).status_code == 200   # someone else's default never applies to me
    d1.post(f"/all/filters/{two['id']}/default", data={"on": "0"}, headers=H)
    assert d1.get("/d1-proj/list", follow_redirects=False).status_code == 200


def test_recent_saved_default_applies_and_scopes(world):
    d1 = _client(world["d1"])
    _save(d1, "recent", "Comments", {"kind": "comments", "project": "d1-proj"}, default=True)
    r = d1.get("/recent", follow_redirects=False)
    assert r.status_code == 302 and "kind=comments" in r.headers["location"]
    assert _client(world["owner"]).get("/recent", follow_redirects=False).status_code == 200
    assert "D2 SECRET" not in d1.get(r.headers["location"]).text


def test_the_saved_filter_ui_is_on_list_board_and_recent(world):
    d1 = _client(world["d1"])
    for path, page in (("/d1-proj/list", "list"), ("/d1-proj/board", "board"), ("/recent", "recent")):
        html = d1.get(path).text
        assert f'data-saved-filters data-page="{page}"' in html and "data-sf-save" in html and "js/saved-filters.js" in html, path


def test_filters_table_is_per_user_in_the_schema():
    from app.db import get_cursor
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'saved_filters'")
        cols = {r["column_name"] for r in cur.fetchall()}
    assert {"user_id", "page", "project_id", "name", "data", "is_default"} <= cols


# --- review fixes -------------------------------------------------------------------------------------------------------

def test_a_default_does_not_trap_the_clear_links(world):
    d1 = _client(world["d1"])
    _save(d1, "board", "Must", {"priority": "must"}, "d1-proj", default=True)
    _save(d1, "list", "Mine", {"assignee": "ai"}, "d1-proj", default=True)
    _save(d1, "recent", "Comments", {"kind": "comments"}, default=True)
    for path in ("/d1-proj/board", "/d1-proj/list", "/recent"):
        arrive = d1.get(path, follow_redirects=False)
        assert arrive.status_code == 302, path                                              # a bare ARRIVAL opens the default
        same_page = {"Referer": "https://crewboss.example.com" + path + "?x=1"}
        assert d1.get(path, headers=same_page, follow_redirects=False).status_code == 200, path    # the page's own "All" / "clear": unfiltered
    assert d1.get("/recent?all=1", follow_redirects=False).status_code == 200              # and the explicit marker, with no Referer at all
    assert 'href="/recent?all=1"' in d1.get("/recent?kind=comments").text                  # Recent's clear link carries it
    assert "all=1" in d1.get("/d1-proj/list?priority=must").text                           # so does the List's "Clear all"


def test_renaming_to_a_taken_name_is_a_400_not_a_500(world):
    d1 = _client(world["d1"])
    _save(d1, "board", "One", {"priority": "must"}, "d1-proj")
    two = _save(d1, "board", "Two", {"priority": "could"}, "d1-proj").json()["filter"]
    r = d1.post(f"/all/filters/{two['id']}/rename", data={"name": "one"}, headers=H)            # case-insensitive clash
    assert r.status_code == 400 and "already" in r.text and "pick another name" in r.text
    assert _names(d1, "board", "d1-proj") == ["One", "Two"]
    assert d1.post(f"/all/filters/{two['id']}/rename", data={"name": "Two"}, headers=H).status_code == 200   # its own name is not a clash


def _viewer_in(world, project_key):
    from app.db import get_cursor
    with get_cursor() as cur:
        cur.execute("INSERT INTO project_members (project_id, user_id, role) VALUES (%s, %s, 'viewer')", (world[project_key]["id"], world["v"]["id"]))
    return _client(world["v"])


def test_a_viewer_sees_no_write_controls_on_the_list_or_in_the_menu(world):
    vic = _viewer_in(world, "p2")
    page = vic.get("/d2-proj/list")
    assert page.status_code == 200 and "D2 SECRET" in page.text
    for control in ("data-tl-bulk", 'data-set="status"', "tl-create-form", "data-pick", "data-pick-all"):
        assert control not in page.text, control
    assert "data-readonly" in page.text                                                    # the status is shown, not editable
    assert "nav-sec-add" not in page.text.split('id="nav-drawer"', 1)[1].split("</aside>", 1)[0]    # no "+" for someone who can't create
    owner = _client(world["owner"]).get("/d2-proj/list").text
    for control in ("data-tl-bulk", 'data-set="status"', "tl-create-form", "data-pick-all"):
        assert control in owner, control
    d2 = _client(world["d2"]).get("/d2-proj/list").text                                    # an editing designer has them, and the "+"
    assert 'data-set="status"' in d2 and "nav-sec-add" in d2.split('id="nav-drawer"', 1)[1].split("</aside>", 1)[0]
