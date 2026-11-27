"""#773: the record page TAB BAR (Jira-style). A shared per-project layout (starting layout Summary, List, Board; accounts Summary, List, Board, Approvals), edited by
the project's owner or an admin only: Set as default, Rename, Move left/right, Remove, and the "+" catalogue. Settings is pinned. Every page stays reachable by URL.
Focus and Overview are gone from records (#773): their URLs redirect to Summary."""
import re

from app import access, repo, tabs
from tests.test_multi_user import H, _client, local, world  # noqa: F401  (fixtures)


def _bar(html):
    nav = html.split('data-record-pills', 1)[1].split("</nav>", 1)[0]
    return re.findall(r'data-pill="([a-z_]+)"', nav)


def _cat(html):
    return re.findall(r'data-cat="([a-z_]+)"', html)


def _edit(c, slug, action, key="", value=""):
    return c.post(f"/{slug}/tabs/{action}", data={"key": key, "value": value}, headers=H)


def _share(world, who, role):
    access.share(world["p1"]["id"], world[who]["id"], role, world["owner"]["id"])


def test_the_starting_layout_is_summary_list_board_and_the_rest_is_in_the_catalogue(world):
    html = _client(world["d1"]).get("/d1-proj").text
    assert _bar(html) == ["summary", "list", "board"]
    assert "focus" not in _cat(html) and "overview" not in _cat(html)
    assert {"changes", "workflows", "knowledge", "build_skills", "ai_features", "rules", "materials"} <= set(_cat(html))
    assert "settings" not in _cat(html)


def test_an_account_starts_with_summary_list_board_approvals(world):
    assert tabs.start_layout("account") == ["summary", "list", "board", "approvals"]
    assert tabs.start_layout("project") == ["summary", "list", "board"]


def test_add_remove_move_rename_and_default(world):
    c = _client(world["d1"])
    assert _edit(c, "d1-proj", "add", "changes").status_code == 200
    assert _bar(c.get("/d1-proj").text) == ["summary", "list", "board", "changes"]
    assert _edit(c, "d1-proj", "left", "changes").status_code == 200
    assert _bar(c.get("/d1-proj").text) == ["summary", "list", "changes", "board"]
    assert _edit(c, "d1-proj", "right", "summary").status_code == 200
    assert _bar(c.get("/d1-proj").text) == ["list", "summary", "changes", "board"]
    assert _edit(c, "d1-proj", "rename", "board", "Tasks wall").json()["labels"] == {"board": "Tasks wall"}
    html = c.get("/d1-proj").text
    assert "Tasks wall" in html and 'data-orig="Board"' in html                          # the catalogue/menu keep the original name
    assert _edit(c, "d1-proj", "remove", "changes").status_code == 200
    assert "changes" not in _bar(c.get("/d1-proj").text) and "changes" in _cat(c.get("/d1-proj").text)
    assert _edit(c, "d1-proj", "rename", "board", "").json()["labels"] == {}              # empty = the original name again


def test_set_as_default_redirects_the_bare_project_url(world):
    c = _client(world["d1"])
    assert c.get("/d1-proj", follow_redirects=False).status_code == 200
    assert _edit(c, "d1-proj", "default", "board").json()["default"] == "board"
    r = c.get("/d1-proj", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/d1-proj/board"
    assert c.get("/d1-proj/list", follow_redirects=False).status_code == 200              # other pages are unaffected
    _edit(c, "d1-proj", "remove", "board")                                                 # removing the default tab clears it
    assert c.get("/d1-proj", follow_redirects=False).status_code == 200


def test_settings_cannot_be_removed_and_is_not_in_the_catalogue(world):
    owner = _client(world["owner"])
    html = owner.get("/own-proj").text
    assert _bar(html)[-1] == "settings"
    nav = html.split('data-record-pills', 1)[1].split("</nav>", 1)[0]
    assert not re.search(r'data-tab-more="settings"', nav)                                 # pinned: no "..." menu
    assert _edit(owner, "own-proj", "remove", "settings").status_code == 400
    assert _edit(owner, "own-proj", "add", "settings").status_code == 400
    assert _edit(owner, "own-proj", "add", "nope").status_code == 400


def test_a_page_not_on_the_bar_still_opens_and_shows_as_the_current_tab(world):
    c = _client(world["d1"])
    r = c.get("/d1-proj/changes")
    assert r.status_code == 200 and 'aria-current="page" data-pill="changes"' in r.text
    nav = r.text.split('data-record-pills', 1)[1].split("</nav>", 1)[0]
    assert not re.search(r'data-tab-more="changes"', nav)                                  # a visitor tab, not a bar tab


def test_only_the_owner_or_an_admin_edits_the_bar(world):
    _share(world, "d2", "member")
    member = _client(world["d2"])
    html = member.get("/d1-proj").text
    assert _bar(html) == ["summary", "list", "board"]
    assert "data-tab-more" not in html and "data-tab-add" not in html and "data-tab-cat" not in html       # they see the bar, nothing to edit it
    assert _edit(member, "d1-proj", "add", "changes").status_code == 403
    viewer_share = access.share(world["p1"]["id"], world["v"]["id"], "viewer", world["owner"]["id"])
    v = _client(world["v"])
    assert "data-tab-add" not in v.get("/d1-proj").text
    assert _edit(v, "d1-proj", "add", "changes").status_code in (403, 404)
    assert _bar(_client(world["d1"]).get("/d1-proj").text) == ["summary", "list", "board"]   # nothing changed
    _share(world, "d2", "admin") if False else access.share(world["p1"]["id"], world["d2"]["id"], "admin", world["owner"]["id"])
    assert _edit(member, "d1-proj", "add", "changes").status_code == 200                   # an admin member may
    assert "data-tab-add" in _client(world["d2"]).get("/d1-proj").text


def test_a_project_you_cannot_see_is_a_404_for_tab_edits(world):
    assert _edit(_client(world["d1"]), "d2-proj", "add", "changes").status_code == 404


def test_the_layout_is_shared_by_everyone_who_sees_the_project(world):
    access.share(world["p1"]["id"], world["d2"]["id"], "member", world["owner"]["id"])
    _edit(_client(world["d1"]), "d1-proj", "add", "knowledge")
    assert "knowledge" in _bar(_client(world["d2"]).get("/d1-proj").text)
    assert "knowledge" in _bar(_client(world["owner"]).get("/d1-proj").text)


def test_renaming_is_checked(world):
    c = _client(world["d1"])
    assert _edit(c, "d1-proj", "rename", "board", "x" * 25).status_code == 400
    assert _edit(c, "d1-proj", "rename", "list", "  Task   table ").json()["labels"]["list"] == "Task table"
    assert _edit(c, "d1-proj", "bogus", "board").status_code in (400, 404)


def test_the_catalogue_describes_and_previews_every_view(world):
    html = _client(world["d1"]).get("/d1-proj").text
    cat = html.split("data-tab-cat", 1)[1]
    for k in ("changes", "workflows", "build_skills", "ai_features", "materials"):
        pane = re.search(r'data-cat-pane="%s"[^>]*>(.*?)</div>\s*</div>' % k, cat, re.S).group(1)
        assert "<svg" in pane and "Add to navigation" in pane and "<p>" in pane, k
    assert "https://" not in cat and "<img" not in cat                                       # inline SVG, no external images


def test_focus_and_overview_redirect_to_summary(world):
    c = _client(world["d1"])
    for old in ("focus", "overview"):
        r = c.get(f"/d1-proj/{old}", follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "/d1-proj", old


def test_summary_carries_what_overview_had(world):
    html = _client(world["d1"]).get("/d1-proj").text
    assert "data-overview-tiles" in html and "data-objectives" in html and "data-changes" in html and "Tasks" not in "" and "data-waiting" in html


def test_an_account_has_a_summary_landing(world):
    owner = _client(world["owner"])
    from app import tree
    acct = repo.create_project("tabs-acct", "Tabs Account", "client", "owner", short_name="tabacct", session_roles=[])
    tree.set_place(acct["id"], "owner", None, kind="account")
    html = owner.get("/tabs-acct").text
    assert 'data-landing="summary"' in html and 'data-account-projects' in html
    assert _bar(html) == ["summary", "list", "board", "approvals", "settings"]


# --- an editor who cannot see some pages never wipes what they cannot see (review fix) -----------------------------------------

def test_a_non_owner_admins_edit_keeps_the_owners_hidden_tabs_labels_and_default(world):
    owner, d1 = _client(world["owner"]), _client(world["d1"])
    assert _edit(owner, "d1-proj", "add", "integrations").status_code == 200           # owner-only page
    assert _edit(owner, "d1-proj", "rename", "integrations", "Links").status_code == 200
    assert _edit(owner, "d1-proj", "default", "integrations").status_code == 200
    assert _bar(d1.get("/d1-proj").text) == ["summary", "list", "board"]                 # d1 cannot see it
    assert _edit(d1, "d1-proj", "left", "board").status_code == 200
    r = tabs.row(world["p1"]["id"])
    assert r["tabs"] == ["summary", "board", "list", "integrations"] and r["labels"] == {"integrations": "Links"} and r["default_tab"] == "integrations"
    assert _edit(d1, "d1-proj", "rename", "board", "Wall").status_code == 200
    assert _edit(d1, "d1-proj", "remove", "list").status_code == 200
    r = tabs.row(world["p1"]["id"])
    assert r["labels"] == {"integrations": "Links", "board": "Wall"} and r["default_tab"] == "integrations" and "integrations" in r["tabs"]
    assert _bar(_client(world["owner"]).get("/d1-proj").text)[:3] == ["summary", "board", "integrations"]


def test_the_same_for_an_account_and_its_owner_only_approvals(world):
    from app import tree
    acct = repo.create_project("keep-acct", "Keep Account", "client", "owner", short_name="keepacct", session_roles=[])
    tree.set_place(acct["id"], "owner", None, kind="account")
    level_keys = ["summary", "list", "board", "changes", "approvals", "build_skills"]
    visible = [k for k in level_keys if k != "approvals"]              # what a non-owner admin sees
    lay = tabs.edit(acct, "account", visible, "left", "board", "", None, level_keys)
    assert lay["tabs"] == ["summary", "board", "list", "approvals"]    # approvals still on the bar, still last
    tabs.edit(acct, "account", level_keys, "default", "approvals", "", None, level_keys)
    lay = tabs.edit(acct, "account", visible, "add", "changes", "", None, level_keys)
    assert lay["default"] == "approvals" and lay["tabs"][-2:] == ["approvals", "changes"]


def test_a_renamed_hidden_project_never_leaks_its_new_slug_through_tab_edits(world):
    from app import tree
    tree.rename_project(world["p2"]["id"], "owner", "Secret Rename", "secret-renamed")
    d1 = _client(world["d1"])
    r = _edit(d1, "d2-proj", "add", "changes")
    assert r.status_code == 404 and "secret-renamed" not in r.text and "Secret Rename" not in r.text
