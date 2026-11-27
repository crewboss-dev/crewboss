"""Task #148: the left menu by level (All projects / account / project), the All projects dashboard, ACCOUNT pages as
rollups over the account and every project under it, account-level SHARED rules / AI skills / knowledge and their
inheritance (pages + session grounding), the archive Danger zone, and the project switcher's tree."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app import changes, config, mcp_server, repo, shared, tree
from app.auth import issue_token
from app.routers.pages import nav_items, switcher_rows

ROOT = Path(__file__).resolve().parent.parent
OWN = {"Origin": "https://crewboss.example.com", "Sec-Fetch-Site": "same-origin"}
# A project's own rows in the menu drawer (#479: the ERP's .nav-row; All projects is data-nav="home").
NAV_LABEL = re.compile(r'data-pill="[^"]+"[^>]*>.*?<span data-tab-label>([^<]+)</span>', re.S)    # a record's pills (#733): the left menu is fixed


@pytest.fixture
def fam(monkeypatch):
    """dash (root, session) → Acme (account) → acme-app (session) → acme-portal (no session: a GRANDCHILD of the
    account); Acme → acme-site (no session). A second account Globex → globex-app (session). A top-level 'pa' project
    with its own session (like personal-pa: under dash, under no account)."""
    monkeypatch.setattr(config, "OVERSEER_PROJECT", "dash-root")
    mk = lambda slug, name, short, roles: repo.create_project(slug, name, "internal", "owner", short_name=short,  # noqa: E731
                                                               session_roles=roles)
    dash = mk("dash-root", "Dash", "dash", ["all"])
    acct = mk("acme", "Acme", "acme", [])
    tree.set_place(acct["id"], "owner", None, kind="account")
    app_ = mk("acme-app", "Acme App", "acmeapp", ["all"])
    tree.set_place(app_["id"], "owner", acct["id"])
    portal = mk("acme-portal", "Acme Portal", "acmeportal", [])
    tree.set_place(portal["id"], "owner", app_["id"])
    site = mk("acme-site", "Acme Site", "acmesite", [])
    tree.set_place(site["id"], "owner", acct["id"])
    globex = mk("globex", "Globex", "globex", [])
    tree.set_place(globex["id"], "owner", None, kind="account")
    gapp = mk("globex-app", "Globex App", "globexapp", ["all"])
    tree.set_place(gapp["id"], "owner", globex["id"])
    pa = mk("pa", "Personal Assistant", "pa", ["all"])
    return {"dash": dash, "acct": acct, "app": app_, "portal": portal, "site": site, "globex": globex, "gapp": gapp,
            "pa": pa}


HUMANIZER_BODY = "Rewrite AI-sounding text in a plain human voice.\nNo em dashes. Short sentences."


@pytest.fixture
def humanizer(fam):
    """The owner's worked example: an AI skill authored at the account, inherited by all software under it."""
    return repo.create_knowledge(fam["acct"]["id"], "skill", "Humanizer", HUMANIZER_BODY, [], "", False, "owner",
                                 summary="Makes AI-written text read like a person wrote it.")


def _t(project, title, status="backlog", **kw):
    t = repo.create_task(project["id"], title, "", kw.pop("priority", "should"), "feature", [], {}, "owner",
                         assignee=kw.pop("assignee", "owner"))
    if status != "backlog":
        t = repo.update_task(t["id"], {"status": status}, "owner")
    return t


def _session(project, stream="all"):
    from app.auth import resolve_session_token
    return resolve_session_token(issue_token(stream, f"t-{project['slug']}-{stream}", project_id=project["id"]))


# --- the left menu by level --------------------------------------------------------------------------------------

def test_nav_items_by_level():
    assert nav_items("home") == []
    assert [k for k, *_ in nav_items("account")] == ["summary", "list", "board", "changes", "approvals", "build_skills", "ai_features", "rules",
                                                         "knowledge", "materials", "discussions", "handoffs", "settings"]
    assert [k for k, *_ in nav_items("project")][:4] == ["summary", "list", "board", "changes"]     # #772; Focus and Overview are gone (#773)
    assert "focus" not in [k for k, *_ in nav_items("project")] and "overview" not in [k for k, *_ in nav_items("project")]
    assert dict((k, p) for k, p, *_ in nav_items("project"))["summary"] == "" and dict((k, p) for k, p, *_ in nav_items("account"))["summary"] == ""
    assert "workflows" in [k for k, *_ in nav_items("project")]


def test_all_projects_has_the_erp_drawer_and_phone_bar(client, fam):
    """#479: the ERP's drawer is on every page. On All projects it holds All projects (current) and the pages of the
    project you were last in; the phone bar is there too. The switcher still goes everywhere."""
    html = client.get("/").text
    assert 'id="nav-drawer"' in html and "data-tabbar" in html
    assert 'class="nav-row is-active" aria-current="page" data-nav="now"' in html
    assert 'data-sec="accounts"' in html and 'data-record="acme-app"' in html and "data-jump-input" in html   # #771: sections + the jump box
    assert "data-brand" in html  # the logo heads the drawer, as in the ERP


def test_account_menu_is_its_own_set(client, fam):
    html = client.get("/acme").text
    assert NAV_LABEL.findall(html) == ["Summary", "List", "Board", "Approvals", "Settings"]      # the starting layout (#773); the rest are in the "+" catalogue
    assert "data-tabbar" in html and 'data-record="acme"' in html
    # every account page keeps the account menu
    for path in ("/acme/board", "/acme/changes", "/acme/ai-features", "/acme/rules", "/acme/knowledge", "/acme/tasks/new"):
        assert len(NAV_LABEL.findall(client.get(path).text)) in (5, 6), path     # the bar (+ the page you are on when it is not on it); the left menu is the same everywhere


def test_project_menu_unchanged_and_no_all_projects_item(client, fam):
    html = client.get("/acme-portal").text
    labels = NAV_LABEL.findall(html)
    assert labels == ["Summary", "List", "Board"]       # the starting layout (#773); Settings only on a top-level record
    assert "All projects" not in labels





# --- account rollups ---------------------------------------------------------------------------------------------

@pytest.fixture
def work(fam):
    t = {
        "app": _t(fam["app"], "APP doing", "doing"),
        "portal": _t(fam["portal"], "PORTAL review", "review"),
        "site": _t(fam["site"], "SITE ready", "ready", priority="must"),
        "site_bl": _t(fam["site"], "SITE backlog"),
        "gapp": _t(fam["gapp"], "GLOBEX doing", "doing"),
        "dash": _t(fam["dash"], "DASH ready", "ready"),
    }
    t["q"] = repo.ask_owner(t["portal"]["id"], "PORTAL question?", "enough context to answer it", None, "claude:all")
    t["gq"] = repo.ask_owner(t["gapp"]["id"], "GLOBEX question?", "enough context to answer it", None, "claude:all")
    t["ch"] = changes.recommend(fam["site"]["id"], "SITE change", "why", "claude:all")
    t["gch"] = changes.recommend(fam["gapp"]["id"], "GLOBEX change", "why", "claude:all")
    return t


def test_account_focus_rolls_up_its_subtree_only(client, fam, work):
    ids = sorted(tree.descendant_ids(fam["acct"]["id"]))
    f = repo.focus(project_ids=ids)
    assert [t["title"] for t in f["now"]] == ["APP doing"]
    assert [t["title"] for t in f["review"]] == ["PORTAL review"]
    assert [t["title"] for t in f["up_next"]] == ["SITE ready"]
    assert [q["question"] for q in f["questions"]] == ["PORTAL question?"]
    assert [c["title"] for c in f["changes"]] == ["SITE change"]
    html = client.get("/acme").text
    main = html.split("<main", 1)[1]
    # an account's landing is its Summary (#773): what waits on you across its subtree, never another account's
    for s in ("PORTAL review", "PORTAL question?", "SITE change"):
        assert s in main, s
    for s in ("GLOBEX", "DASH ready"):
        assert s not in main, s
    # the account's own head: its projects as cards (the grandchild too), with the branch's counts
    assert 'data-project-card="acme-app"' in main and 'data-project-card="acme-portal"' in main
    assert 'data-project-card="globex-app"' not in main
    assert "data-account-shared" in main and 'data-landing="summary"' in main


def test_account_board_lists_every_subtree_task_with_chips(client, fam, work):
    html = client.get("/acme/board").text
    main = html.split("<main", 1)[1]
    for s in ("APP doing", "PORTAL review", "SITE ready", "SITE backlog"):
        assert s in main, s
    assert "GLOBEX" not in main and "DASH ready" not in main
    pt = work["portal"]
    # the card opens, posts and drags to ITS project, and the board refetches at the account
    assert f'hx-get="/acme-portal/tasks/{pt["id"]}/panel"' in main
    assert f'data-task-base="/acme-portal"' in main
    assert f'hx-post="/acme-portal/tasks/{pt["id"]}/status?layout=board&amp;rollup=acme"' in main
    assert 'hx-get="/acme/tasks/board?layout=board&amp;rollup=acme"' in main
    # quick-add asks which project (accounts hold no tasks); only the account's projects are offered
    assert "data-quick-target" in main and 'value="acme-portal"' in main and 'value="globex-app"' not in main
    # list view too
    lst = client.get("/acme/board?view=list").text
    assert "PORTAL review" in lst and "GLOBEX" not in lst and 'data-project-chip="acme-site"' in lst


def test_status_change_from_the_account_board_writes_to_the_tasks_project(client, fam, work):
    st = work["site"]
    r = client.post(f"/acme-site/tasks/{st['id']}/status?layout=board&rollup=acme", data={"status": "doing"},
                    headers={**OWN, "HX-Request": "true"})
    assert r.status_code == 200
    assert repo.get_task(st["id"])["status"] == "doing" and repo.get_task(st["id"])["project_id"] == fam["site"]["id"]
    # the answer is the ACCOUNT's board again (other projects' cards included)
    assert "PORTAL review" in r.text and 'hx-get="/acme/tasks/board?layout=board&amp;rollup=acme"' in r.text
    # a rollup naming an account the task isn't under is ignored: the project's own board comes back
    r = client.post(f"/acme-site/tasks/{st['id']}/status?layout=board&rollup=globex", data={"status": "ready"},
                    headers={**OWN, "HX-Request": "true"})
    assert r.status_code == 200 and "PORTAL review" not in r.text and "rollup=globex" not in r.text
    # and from the account's Focus
    r = client.post(f"/acme-portal/tasks/{work['portal']['id']}/status?layout=focus&rollup=acme",
                    data={"status": "done"}, headers={**OWN, "HX-Request": "true"})
    assert r.status_code == 200 and 'data-layout="focus"' in r.text and "APP doing" in r.text


@pytest.fixture
def colliding(fam):
    """Two projects' Should cards with the ranks they really get (each project starts at 1024, steps of 1024):
    S1 = A1 = 1024, S2 = A2 = 2048. The account's merged column is [S1, A1, S2, A2] (ties by created_at)."""
    s1, a1, s2, a2 = (_t(fam[p], title, priority="should")
                      for p, title in (("site", "S1"), ("app", "A1"), ("site", "S2"), ("app", "A2")))
    assert [repo.get_task(t["id"])["rank"] for t in (s1, a1, s2, a2)] == [1024, 1024, 2048, 2048]
    return {"S1": s1, "A1": a1, "S2": s2, "A2": a2}


def _acme_ids(fam):
    return sorted(tree.descendant_ids(fam["acct"]["id"]))


def _merged(fam, priority="should"):
    """The account board's column order (list_tasks' order over the account's projects)."""
    return [t["title"] for t in repo.list_tasks(None, project_ids=_acme_ids(fam), priority=priority)]


def _own(project, priority="should"):
    return [t["title"] for t in repo.list_tasks(project["id"], priority=priority)]


def test_cross_project_rerank_keeps_the_dropped_order(fam, colliding):
    assert _merged(fam) == ["S1", "A1", "S2", "A2"]
    # drop A2 directly under S1 (another project's card): the ranks collide, yet it lands where it was dropped
    row = repo.reorder_task(colliding["A2"]["id"], "should", colliding["S1"]["id"], "owner", project_ids=_acme_ids(fam))
    assert row["project_id"] == fam["app"]["id"]
    assert _merged(fam) == ["S1", "A2", "A1", "S2"] and _own(fam["app"]) == ["A2", "A1"]   # Up next agrees
    # only the dropped task's own project was re-spaced: the site's ranks never moved
    assert [repo.get_task(colliding[k]["id"])["rank"] for k in ("S1", "S2")] == [1024, 2048]


def test_cross_project_rerank_own_card_under_own_card_past_a_tie(fam, colliding):
    # drop S2 directly under S1: the next merged card is A1, at S1's own rank
    repo.reorder_task(colliding["S2"]["id"], "should", colliding["S1"]["id"], "owner", project_ids=_acme_ids(fam))
    assert _merged(fam) == ["S1", "S2", "A1", "A2"] and _own(fam["site"]) == ["S1", "S2"]
    assert [repo.get_task(colliding[k]["id"])["rank"] for k in ("A1", "A2")] == [1024, 2048]
    # with a gap it is the plain midpoint of the merged neighbours, touching nothing else
    s1_rank = repo.get_task(colliding["S1"]["id"])["rank"]
    row = repo.reorder_task(colliding["A2"]["id"], "should", colliding["S2"]["id"], "owner",
                            project_ids=_acme_ids(fam))
    assert row["rank"] == (repo.get_task(colliding["S2"]["id"])["rank"] + 1024) / 2
    assert _merged(fam) == ["S1", "S2", "A2", "A1"] and _own(fam["app"]) == ["A2", "A1"]
    assert repo.get_task(colliding["S1"]["id"])["rank"] == s1_rank


def test_cross_project_rerank_top_bottom_and_a_tie_between_other_projects(fam, colliding):
    ids = _acme_ids(fam)
    repo.reorder_task(colliding["A2"]["id"], "should", None, "owner", project_ids=ids)                    # top
    assert _merged(fam) == ["A2", "S1", "A1", "S2"]
    repo.reorder_task(colliding["A2"]["id"], "should", colliding["S2"]["id"], "owner", project_ids=ids)   # bottom
    assert _merged(fam) == ["S1", "A1", "S2", "A2"]
    # a third project's card ties with both at 1024: dropped between S1 and A1 (two OTHER projects' cards at one
    # rank), the whole band is re-spaced with every project's own order kept
    p1 = _t(fam["portal"], "P1", priority="should")
    assert repo.get_task(p1["id"])["rank"] == 1024 and _merged(fam) == ["S1", "A1", "P1", "S2", "A2"]
    repo.reorder_task(p1["id"], "should", colliding["S1"]["id"], "owner", project_ids=ids)
    assert _merged(fam) == ["S1", "P1", "A1", "S2", "A2"]
    assert _own(fam["site"]) == ["S1", "S2"] and _own(fam["app"]) == ["A1", "A2"]


def test_account_board_drag_writes_the_merged_place(client, fam, colliding):
    a2, s1 = colliding["A2"], colliding["S1"]
    r = client.post(f"/acme-app/tasks/{a2['id']}/priority?layout=board&rollup=acme",
                    data={"priority": "should", "after_id": str(s1["id"])}, headers={**OWN, "HX-Request": "true"})
    assert r.status_code == 200 and _merged(fam) == ["S1", "A2", "A1", "S2"]
    # a status move dropped on the account board is placed from the merged column too
    r = client.post(f"/acme-site/tasks/{colliding['S2']['id']}/status?layout=board&rollup=acme",
                    data={"status": "ready", "after_id": str(s1["id"])}, headers={**OWN, "HX-Request": "true"})
    assert r.status_code == 200 and _merged(fam) == ["S1", "S2", "A2", "A1"]


def test_project_board_drag_is_unchanged(client, fam, colliding):
    # the project's own board (no rollup): the plain midpoint among its own cards
    s3 = _t(fam["site"], "S3", priority="should")
    r = client.post(f"/acme-site/tasks/{s3['id']}/priority?layout=board",
                    data={"priority": "should", "after_id": str(colliding["S1"]["id"])},
                    headers={**OWN, "HX-Request": "true"})
    assert r.status_code == 200 and repo.get_task(s3["id"])["rank"] == 1536
    assert _own(fam["site"]) == ["S1", "S3", "S2"]
    assert [repo.get_task(colliding[k]["id"])["rank"] for k in ("A1", "A2")] == [1024, 2048]


def test_account_changes_page_groups_the_subtree(client, fam, work):
    changes.decide(work["ch"]["id"], "approved", "owner")
    other = changes.recommend(fam["portal"]["id"], "PORTAL change", "why", "claude:all")
    html = client.get("/acme/changes").text
    main = html.split("<main", 1)[1]
    assert "SITE change" in main and "PORTAL change" in main and "GLOBEX change" not in main
    waiting = main[main.index("data-changes-waiting"):main.index("data-changes-decided")]
    assert "PORTAL change" in waiting and "SITE change" not in waiting
    assert 'data-project-chip="acme-site"' in main
    # the card decides on the change's own project
    assert f'action="/acme-portal/changes/{other["id"]}/decide"' in main


def test_account_pages_open_a_subtree_task_in_place(client, fam, work):
    html = client.get(f"/acme/board?open={work['portal']['id']}").text
    assert f'"/acme-portal/tasks/{work["portal"]["id"]}/panel"' in html
    r = client.get(f"/acme/board?open={work['gapp']['id']}", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("/globex-app/board")


# --- accounts hold no tasks ----------------------------------------------------------------------------------------

def test_accounts_hold_no_tasks_at_the_repo_level(fam):
    with pytest.raises(repo.BadRequest, match="account"):
        repo.create_task(fam["acct"]["id"], "x", "", "should", "feature", [], {}, "owner")
    t = _t(fam["site"], "movable")
    with pytest.raises(repo.BadRequest, match="account"):
        repo.update_task(t["id"], {"project": "acme"}, "owner")
    assert repo.get_task(t["id"])["project_id"] == fam["site"]["id"]
    # a project with open work can't become an account
    _t(fam["pa"], "pa work")
    with pytest.raises(repo.BadRequest, match="open task"):
        tree.set_place(fam["pa"]["id"], "owner", None, kind="account")


def test_new_task_on_an_account_must_pick_one_of_its_projects(client, fam):
    page = client.get("/acme/tasks/new").text
    picker = page[page.index("data-task-target"):page.index("</select>", page.index("data-task-target"))]
    assert 'value="acme-portal"' in picker and 'value="acme"' not in picker and 'value="globex-app"' not in picker
    r = client.post("/acme/tasks/new", data={"title": "No target", "stream": "feature"}, headers=OWN,
                    follow_redirects=False)
    assert r.status_code == 303 and "pick" in r.headers.get("set-cookie", "")
    r = client.post("/acme/tasks/new", data={"title": "Wrong target", "stream": "feature", "target": "globex-app"},
                    headers=OWN, follow_redirects=False)
    assert r.status_code == 303
    r = client.post("/acme/tasks/new", data={"title": "Portal work", "stream": "feature", "target": "acme-portal"},
                    headers=OWN, follow_redirects=False)
    made = [t for t in repo.list_tasks(fam["portal"]["id"]) if t["title"] == "Portal work"]
    assert len(made) == 1 and r.headers["location"] == f"/acme/board?open={made[0]['id']}"
    titles = {t["title"] for p in fam.values() for t in repo.list_tasks(p["id"])}
    assert "No target" not in titles and "Wrong target" not in titles
    # board quick-add on the account
    r = client.post("/acme/tasks?layout=board&rollup=acme", data={"title": "Quick one", "stream": "feature",
                                                                  "target": "acme-site"},
                    headers={**OWN, "HX-Request": "true"})
    assert r.status_code == 200 and "Quick one" in r.text
    assert [t["title"] for t in repo.list_tasks(fam["site"]["id"])] == ["Quick one"]
    r = client.post("/acme/tasks", data={"title": "Nowhere", "stream": "feature"}, headers={**OWN, "HX-Request": "true"})
    assert r.status_code == 400


def test_drawer_never_offers_an_account_as_a_move_target(client, fam):
    t = _t(fam["site"], "move me")
    html = client.get(f"/acme-site/tasks/{t['id']}/panel").text
    assert 'value="acme-portal"' in html and 'value="acme"' not in html and 'value="globex"' not in html


# --- shared items at the account, inherited below ------------------------------------------------------------------

def test_shared_kinds_live_on_accounts_only(fam, humanizer):
    with pytest.raises(repo.BadRequest, match="account"):
        repo.create_knowledge(fam["app"]["id"], "skill", "X", "", [], "", False, "owner", summary="s")
    with pytest.raises(repo.BadRequest, match="account"):
        repo.create_knowledge(fam["portal"]["id"], "rule", "X", "", [], "", False, "owner", summary="s")
    with pytest.raises(repo.BadRequest):
        repo.update_knowledge(humanizer["id"], {"kind": "note"}, "owner")
    note = repo.create_knowledge(fam["acct"]["id"], "note", "Acme note", "", [], "", False, "owner", summary="s")
    with pytest.raises(repo.BadRequest):
        repo.update_knowledge(note["id"], {"kind": "rule"}, "owner")


def test_humanizer_is_inherited_by_the_grandchild_and_nowhere_else(client, fam, humanizer):
    acct_page = client.get("/acme/ai-features").text
    assert "Humanizer" in acct_page and "data-add-shared" in acct_page and "data-applies-to" in acct_page
    grand = client.get("/acme-portal/ai-features").text
    assert 'data-inherited-from="acme"' in grand and "Humanizer" in grand and "No em dashes" in grand
    assert 'href="/acme/ai-features?open=' in grand
    for slug in ("globex-app", "pa", "dash-root"):
        html = client.get(f"/{slug}/ai-features").text
        assert "Humanizer" not in html and "data-inherited-from" not in html, slug
    fresh = {k: repo.get_project(fam[k]["id"]) for k in ("portal", "gapp", "pa")}
    assert [i["account"]["slug"] for i in shared.inherited(fresh["portal"])] == ["acme"]
    assert shared.inherited(fresh["gapp"])[0]["skills"] == [] and shared.inherited(fresh["pa"]) == []


def test_shared_rules_and_knowledge_inherit_too(client, fam):
    repo.create_knowledge(fam["acct"]["id"], "rule", "Australian English", "Everywhere.", [], "", False, "owner",
                          summary="Spell it the Australian way.")
    repo.create_knowledge(fam["acct"]["id"], "fact", "Acme fact", "", [], "", False, "owner", summary="Shared fact.")
    repo.create_knowledge(fam["acct"]["id"], "objective", "Acme goal", "", [], "", False, "owner", summary="Own goal.")
    rules = client.get("/acme-site/rules").text
    assert "Inherited from" in rules and "Australian English" in rules and "Acme fact" not in rules
    kn = client.get("/acme-portal/knowledge").text
    assert "Acme fact" in kn and "Australian English" not in kn and "Acme goal" not in kn
    acct_kn = client.get("/acme/knowledge").text
    assert "data-account-shared-note" in acct_kn and "Acme fact" in acct_kn and "Australian English" not in acct_kn
    assert "Australian English" in client.get("/acme/rules").text
    assert "Australian English" not in client.get("/globex-app/rules").text


def test_account_editor_authors_a_shared_skill(client, fam):
    page = client.get("/acme/knowledge/new?kind=skill").text
    assert 'name="kind" value="skill"' in page and "data-shared-kind" in page
    r = client.post("/acme/knowledge", data={"kind": "skill", "title": "Humanizer", "summary": "Plain voice.",
                                             "body": "Do it."}, headers=OWN, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/acme/ai-features?open=")
    assert [e["title"] for e in repo.list_knowledge(fam["acct"]["id"], kind="skill")] == ["Humanizer"]
    # a project can't author one (the kind isn't offered and repo refuses it)
    client.post("/acme-app/knowledge", data={"kind": "skill", "title": "Sneaky", "summary": "s"}, headers=OWN)
    assert repo.list_knowledge(fam["app"]["id"], kind="skill") == []


def _account_runs_a_session(fam):
    """The tree has no project root (change #76): an account with no session of its own has nobody above it to ask."""
    from app.db import get_cursor
    with get_cursor() as cur:
        cur.execute("UPDATE projects SET session_roles = '{all}' WHERE id = %s", (fam["acct"]["id"],))


def test_asking_the_ai_about_a_shared_item_goes_to_the_accounts_session_holder(fam, humanizer):
    with pytest.raises(repo.BadRequest, match="no session works"):
        repo.request_knowledge_change(humanizer["id"], "change", "shorter", "owner")
    _account_runs_a_session(fam)
    t = repo.request_knowledge_change(humanizer["id"], "change", "shorter", "owner")
    assert t["project_id"] == fam["site"]["id"] and t["links"]["knowledge"] == [humanizer["id"]]


def test_asking_the_ai_about_a_shared_skill_marks_its_card(client, fam, humanizer):
    _account_runs_a_session(fam)
    kid = humanizer["id"]
    r = client.post(f"/acme/knowledge/{kid}/ask", data={"mode": "change", "text": "shorter"}, headers=OWN,
                    follow_redirects=False)
    assert r.status_code == 303
    first = repo.list_tasks(fam["site"]["id"])
    assert len(first) == 1 and first[0]["links"] == {"knowledge": [kid]}
    t1 = first[0]["id"]
    assert f"AI asked &middot; #{t1}<" in client.get("/acme/ai-features").text
    # a second ask files a second request, as it does for a project's own entry; the card names both
    client.post(f"/acme/knowledge/{kid}/ask", data={"mode": "change", "text": "plainer"}, headers=OWN)
    t2 = max(t["id"] for t in repo.list_tasks(fam["site"]["id"]))
    assert t2 != t1 and f"AI asked &middot; #{t1}, #{t2}<" in client.get("/acme/ai-features").text
    # a done request drops off
    repo.update_task(t1, {"status": "done"}, "owner")
    assert f"AI asked &middot; #{t2}<" in client.get("/acme/ai-features").text


def test_knowledge_requests_never_name_another_projects_work(fam, humanizer):
    _account_runs_a_session(fam)
    kid = humanizer["id"]
    asked = repo.request_knowledge_change(kid, "change", "shorter", "owner")
    # a task in an unrelated project that links the account's skill is not looked at
    repo.create_task(fam["pa"]["id"], "pa link", "", "should", "feature", [], {"knowledge": [kid]}, "owner")
    assert repo.open_knowledge_requests(fam["acct"]["id"]) == {kid: [asked["id"]]}
    # a project's page sees its own tasks for its own entries, exactly as before
    own = repo.create_knowledge(fam["app"]["id"], "fact", "App fact", "", [], "", False, "owner", summary="s")
    t = repo.request_knowledge_change(own["id"], "change", "x", "owner")
    assert t["project_id"] == fam["app"]["id"]
    assert repo.open_knowledge_requests(fam["app"]["id"]) == {own["id"]: [t["id"]]}
    assert repo.open_knowledge_requests(fam["dash"]["id"]) == {}


# --- grounding: shared items reach exactly the sessions working a project under the account -----------------------

def test_grandchild_session_ground_carries_the_humanizer(fam, humanizer):
    # acme-app's session works acme-portal (the account's grandchild)
    g = repo.build_ground("all", session_project=repo.get_project(fam["app"]["id"]))
    [sh] = g["shared"]
    assert sh["account"] == "acme" and sh["yours"] is False
    assert [s["title"] for s in sh["skills"]] == ["Humanizer"] and "No em dashes" in sh["skills"][0]["body"]
    text = repo.render_ground_text(g)
    assert "SHARED BY ACCOUNT Acme (acme)" in text and "AI skill [k" in text and "No em dashes" in text
    assert "inherited, read-only" in text


def test_ground_never_leaks_another_accounts_items(fam, humanizer):
    for p in ("gapp", "pa"):
        g = repo.build_ground("all", session_project=repo.get_project(fam[p]["id"]))
        assert g["shared"] == [] and "Humanizer" not in repo.render_ground_text(g), p
    # task #368: dash's briefing is its own project only — it never works inside an account any more
    g = repo.build_ground("all", session_project=repo.get_project(fam["dash"]["id"]))
    assert g["shared"] == [] and "Humanizer" not in repo.render_ground_text(g)
    # the account's own session works the account: the items are its to edit
    g = repo.build_ground("all", session_project=repo.get_project(fam["acct"]["id"]))
    assert [(s["account"], s["yours"]) for s in g["shared"]] == [("acme", True)]


def test_ground_shared_items_escape_angle_brackets(fam):
    repo.create_knowledge(fam["acct"]["id"], "rule", "<b>Loud</b>", "<script>x</script>", [], "", False, "owner",
                          summary="<i>s</i>")
    text = repo.render_ground_text(repo.build_ground("all", session_project=repo.get_project(fam["app"]["id"])))
    assert "<script>" not in text and "<b>" not in text


def test_account_decisions_and_duties_are_in_the_shared_section_not_doubled(fam):
    repo.create_knowledge(fam["acct"]["id"], "responsibility", "Acme duty", "", [], "", False, "owner", summary="d")
    g = repo.build_ground("all", session_project=repo.get_project(fam["acct"]["id"]))  # the account's own session
    assert [r["title"] for r in g["responsibilities"]] == []
    assert [k["title"] for k in g["shared"][0]["knowledge"]] == ["Acme duty"]


# --- isolation of the new read path (inherited get_knowledge) -------------------------------------------------------

def test_inherited_items_are_readable_never_writable(client, fam, humanizer, mcp_caller):
    kid = humanizer["id"]
    app_tok = issue_token("all", "app-x", project_id=fam["app"]["id"])
    mcp_caller(app_tok)
    assert mcp_server.get_knowledge(kid)["title"] == "Humanizer"
    with pytest.raises(repo.NotFound):
        mcp_server.update_knowledge(kid, title="hijack")
    with pytest.raises(repo.NotFound):
        mcp_server.archive_knowledge(kid, reason="x")
    with pytest.raises(repo.NotFound):
        mcp_server.add_knowledge(title="x", summary="s", kind="skill", project="acme")
    h = {"Authorization": f"Bearer {app_tok}"}
    assert client.get(f"/api/knowledge/{kid}", headers=h).status_code == 200
    assert client.patch(f"/api/knowledge/{kid}", json={"title": "hijack"}, headers=h).status_code == 404
    assert client.post(f"/api/knowledge/{kid}/archive", json={"reason": "x"}, headers=h).status_code == 404
    assert repo.get_knowledge(kid)["title"] == "Humanizer" and repo.get_knowledge(kid)["archived_at"] is None


@pytest.mark.parametrize("who", ["gapp", "pa"])
def test_other_accounts_and_unrelated_projects_get_a_404(client, fam, humanizer, mcp_caller, who):
    tok = issue_token("all", f"{who}-x", project_id=fam[who]["id"])
    mcp_caller(tok)
    with pytest.raises(repo.NotFound):
        mcp_server.get_knowledge(humanizer["id"])
    theirs = client.get(f"/api/knowledge/{humanizer['id']}", headers={"Authorization": f"Bearer {tok}"})
    missing = client.get("/api/knowledge/999999", headers={"Authorization": f"Bearer {tok}"})
    assert theirs.status_code == missing.status_code == 404
    assert theirs.json()["detail"].replace(str(humanizer["id"]), "N") == missing.json()["detail"].replace("999999", "N")


def test_only_live_shared_items_inherit(fam, mcp_caller):
    goal = repo.create_knowledge(fam["acct"]["id"], "objective", "Acme goal", "", [], "", False, "owner", summary="g")
    old = repo.create_knowledge(fam["acct"]["id"], "skill", "Old skill", "", [], "", False, "owner", summary="o")
    repo.archive_knowledge(old["id"], "retired", "owner")
    mcp_caller(issue_token("all", "app-y", project_id=fam["app"]["id"]))
    for kid in (goal["id"], old["id"]):
        with pytest.raises(repo.NotFound):
            mcp_server.get_knowledge(kid)


def test_a_session_whose_reach_includes_the_account_may_write_there(fam, humanizer, mcp_caller):
    # task #368: a session ABOVE the account (the root's) never writes into it — reach stops at the account boundary
    mcp_caller(issue_token("all", "dash-x", project_id=fam["dash"]["id"]))
    with pytest.raises(repo.NotFound):
        mcp_server.update_knowledge(humanizer["id"], summary="Plainer.")
    with pytest.raises(repo.NotFound):
        mcp_server.add_knowledge(title="Tone", summary="Warm, direct.", kind="rule", project="acme")
    # the account's own session does
    mcp_caller(issue_token("all", "acme-x", project_id=fam["acct"]["id"]))
    assert mcp_server.update_knowledge(humanizer["id"], summary="Plainer.")["summary"] == "Plainer."
    made = mcp_server.add_knowledge(title="Tone", summary="Warm, direct.", kind="rule", project="acme")
    assert made["project_id"] == fam["acct"]["id"]
    with pytest.raises(repo.BadRequest):  # but not a shared kind on a project
        mcp_server.add_knowledge(title="Tone", summary="s", kind="rule", project="acme-site")


# --- All projects --------------------------------------------------------------------------------------------------

def test_now_no_longer_carries_the_project_tree_the_projects_list_does(client, fam, work):
    """#733: the tree cards moved off Now. Now is what needs you (the plan, waiting); the Projects and Accounts lists are the lists."""
    main = client.get("/").text.split("<main", 1)[1]
    assert "data-project-tree" not in main and 'id="global-plan"' in main
    assert client.get("/projects", follow_redirects=False).status_code == 302      # #771: the lists are the menu's sections now
    o = {r["project"]["slug"]: r for r in repo.global_overview()["projects"]}
    assert o["acme"]["branch"]["review"] == 1 and o["acme"]["branch"]["questions"] == 1
    assert o["dash-root"]["branch"]["questions"] == 0  # the dashboard project is a leaf now, not the root (change #76)


def test_global_overview_projects_carry_guides(fam):
    # task #157: the home rows reuse the switcher's guide computation, so every project carries guides/last
    o = {r["project"]["slug"]: r["project"] for r in repo.global_overview()["projects"]}
    assert o["dash-root"]["guides"] == [] and o["acme"]["guides"] == []
    assert o["acme-app"]["guides"] == []                          # acme is top level now: nothing to run past
    assert o["acme-portal"]["guides"] == [True] and o["acme-portal"]["last"]   # past acme-app (acme-site still to come)


# --- the Danger zone ----------------------------------------------------------------------------------------------

def test_archive_needs_the_typed_name_and_respects_children(client, fam):
    assert "data-danger-zone" in client.get("/acme-site/settings").text
    r = client.post(f"/projects/{fam['site']['id']}/archive", data={"confirm": "acme site"}, headers=OWN,
                    follow_redirects=False)
    assert r.status_code == 303 and repo.get_project(fam["site"]["id"])["archived_at"] is None
    r = client.post(f"/projects/{fam['site']['id']}/archive", data={"confirm": "Acme Site"}, headers=OWN,
                    follow_redirects=False)
    assert r.status_code == 303 and repo.get_project(fam["site"]["id"])["archived_at"] is not None
    # an account with active projects can't go
    client.post(f"/projects/{fam['acct']['id']}/archive", data={"confirm": "Acme"}, headers=OWN)
    assert repo.get_project(fam["acct"]["id"])["archived_at"] is None
    # cross-site is refused by the origin guard
    r = client.post(f"/projects/{fam['portal']['id']}/archive", data={"confirm": "Acme Portal"},
                    headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403 and repo.get_project(fam["portal"]["id"])["archived_at"] is None
    r = client.post(f"/projects/{fam['site']['id']}/unarchive", data={"confirm": "Acme Site"}, headers=OWN,
                    follow_redirects=False)
    assert repo.get_project(fam["site"]["id"])["archived_at"] is None





# --- the switcher's tree --------------------------------------------------------------------------------------------

def test_switcher_rows_guides_and_last(fam):
    rows = {r["slug"]: r for r in switcher_rows(tree.project_tree(include_archived=True))}
    assert rows["dash-root"]["guides"] == [] and rows["acme"]["guides"] == []
    # top level (no project root, change #76): dash-root, acme, globex (accounts first), then pa
    assert not rows["acme"]["last"] and not rows["globex"]["last"]
    # under acme: acme-app then acme-site (last)
    assert rows["acme-site"]["last"] and not rows["acme-app"]["last"]
    assert rows["acme-app"]["guides"] == []
    # acme-portal: past acme-app (acme-site still to come)
    assert rows["acme-portal"]["guides"] == [True] and rows["acme-portal"]["last"]








# --- list + card: the card's pills load INSIDE the card (#770) -----------------------------------------------------




