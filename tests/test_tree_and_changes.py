"""Task #132 (the project tree: accounts, parents and children, sessions only for parents, environments) and task
#134 (the Change log: Recommended → Decided → Done)."""
import importlib.util
from pathlib import Path

import pytest

from app import changes, config, mcp_server, plan, repo, scope, tree
from app.auth import issue_token, resolve_session_token
from app.db import connect

ROOT = Path(__file__).resolve().parent.parent
OWN = {"Origin": "https://crewboss.example.com", "Sec-Fetch-Site": "same-origin"}


@pytest.fixture
def fam(monkeypatch):
    """dash (root, session) → Acme (account) → acme-app (session) → acme-portal (no session); dash → website (no
    session, under the account); a top-level 'other' project with its own session (unrelated)."""
    monkeypatch.setattr(config, "OVERSEER_PROJECT", "dash-root")
    dash = repo.create_project("dash-root", "Dash", "internal", "owner", short_name="dash", session_roles=["all"])
    acct = repo.create_project("acme", "Acme", "client", "owner", short_name="acme", session_roles=[])
    tree.set_place(acct["id"], "owner", None, kind="account")
    app_ = repo.create_project("acme-app", "Acme App", "client", "owner", short_name="acmeapp", session_roles=["all"])
    tree.set_place(app_["id"], "owner", acct["id"])
    portal = repo.create_project("acme-portal", "Acme Portal", "client", "owner", short_name="acmeportal",
                                 session_roles=[])
    tree.set_place(portal["id"], "owner", app_["id"])
    site = repo.create_project("acme-site", "Acme Site", "client", "owner", short_name="acmesite", session_roles=[])
    tree.set_place(site["id"], "owner", acct["id"])
    other = repo.create_project("other", "Other", "internal", "owner", short_name="other", session_roles=["all"])
    return {"dash": dash, "acct": acct, "app": app_, "portal": portal, "site": site, "other": other}


def _session(project, stream="all"):
    return resolve_session_token(issue_token(stream, f"t-{project['slug']}", project_id=project["id"]))


# --- the tree --------------------------------------------------------------------------------------------------

def test_tree_order_depth_and_rollups(fam):
    t = tree.project_tree()
    slugs = [p["slug"] for p in t]
    assert slugs[0] == "dash-root"
    assert slugs.index("acme") < slugs.index("acme-app") < slugs.index("acme-portal")
    depth = {p["slug"]: p["depth"] for p in t}
    # no project root (change #76): the dashboard project, the account and 'other' are all top level
    assert depth == {"dash-root": 0, "acme": 0, "acme-app": 1, "acme-portal": 2, "acme-site": 1, "other": 0}
    repo.create_task(fam["portal"]["id"], "p task", "", "must", "feature", [], {}, "owner")
    roll = tree.rollups(t, {fam["portal"]["id"]: {"open": 1, "must": 1}})
    assert roll[fam["acct"]["id"]]["open"] == 1 and roll[fam["acct"]["id"]]["must"] == 1 and roll[fam["app"]["id"]]["open"] == 1


def test_place_validation(fam):
    with pytest.raises(repo.BadRequest):
        tree.set_place(fam["dash"]["id"], "owner", None, kind="account")        # the dashboard project is a project, never an account
    with pytest.raises(repo.BadRequest):
        tree.set_place(fam["app"]["id"], "owner", fam["portal"]["id"])          # a cycle
    with pytest.raises(repo.BadRequest):
        tree.set_place(fam["site"]["id"], "owner", fam["app"]["id"], kind="account")  # an account sits at the top of the tree
    with pytest.raises(repo.Forbidden):
        tree.set_place(fam["site"]["id"], "claude:all", fam["app"]["id"])


def test_sessions_only_for_parents(fam):
    assert tree.session_holder(fam["portal"]["id"])["slug"] == "acme-app"
    assert tree.session_holder(fam["site"]["id"]) is None   # the account runs no session and nothing sits above it (change #76)
    assert tree.routed_project_ids(fam["app"]["id"]) == {fam["app"]["id"], fam["portal"]["id"]}
    assert tree.routed_project_ids(fam["dash"]["id"]) == {fam["dash"]["id"]}
    assert tree.session_parent_slug("acme-app") is None
    assert repo.session_for_task(fam["portal"], "feature") == "acmeapp"


# --- a parent's token reaches its children; nothing else ---------------------------------------------------------

def test_parent_token_reaches_its_children_only(client, fam):
    h = {"Authorization": f"Bearer {issue_token('all', 'app', project_id=fam['app']['id'])}"}
    r = client.post("/api/tasks", json={"project": "acme-portal", "title": "child work", "stream": "feature"}, headers=h)
    assert r.status_code == 201, r.text
    assert client.get("/api/tasks?project=acme-portal", headers=h).status_code == 200
    for slug in ("acme", "dash-root", "acme-site", "other"):          # parent, root, sibling branch, unrelated
        assert client.get(f"/api/tasks?project={slug}", headers=h).status_code == 404, slug
    child = {"Authorization": f"Bearer {issue_token('all', 'site', project_id=fam['site']['id'])}"}
    assert client.get("/api/tasks?project=acme-app", headers=child).status_code == 404
    assert {p["slug"] for p in client.get("/api/projects", headers=h).json()} == {"acme-app", "acme-portal"}


def test_dash_overseer_writes_only_its_own_project(fam):
    # task #368: the overseer's reach (what every write checks) is its own project; it never writes into an account,
    # not even a sessionless project under one it used to "work". It still READS everything.
    s = _session(fam["dash"])
    assert scope.is_overseer(s)
    assert scope.reach(s) == {fam["dash"]["id"]}
    for slug in ("acme-app", "acme-site", "acme"):
        with pytest.raises(repo.NotFound):
            scope.project_for(s, slug)
    assert scope.read_project_for(s, "acme-app")["slug"] == "acme-app"  # but it reads everything
    assert scope.read_project_for(s, "acme-site")["slug"] == "acme-site"


def test_child_events_route_to_the_parent_session(fam):
    t = repo.create_task(fam["portal"]["id"], "child", "", "should", "feature", [], {}, "owner")
    repo.comment_task(t["id"], "hello", "owner")
    rows = repo.session_outbox(repo.get_project(fam["app"]["id"]), "all", undelivered_only=True)
    assert any(r["task_id"] == t["id"] for r in rows)
    assert not any(r["task_id"] == t["id"]
                   for r in repo.session_outbox(repo.get_project(fam["dash"]["id"]), "all", undelivered_only=True))


def test_ground_lists_child_projects(fam):
    repo.create_task(fam["portal"]["id"], "portal job", "", "must", "feature", [], {}, "owner")
    tree.set_details(fam["portal"]["id"], "owner", repo_name="acme", repo_path="portal/")
    g = repo.build_ground("all", session_project=repo.get_project(fam["app"]["id"]))
    assert [c["slug"] for c in g["child_projects"]] == ["acme-portal"]
    assert g["child_projects"][0]["open_ai"] == 1
    text = repo.render_ground_text(g)
    assert "YOUR CHILD PROJECTS" in text and "acme @ portal/" in text and "portal job" in text


def test_tree_api_for_the_guard(client, fam):
    over = {"Authorization": f"Bearer {issue_token('all', 'd', project_id=fam['dash']['id'])}"}
    rows = {r["slug"]: r for r in client.get("/api/projects/tree", headers=over).json()}
    assert rows["acme-app"]["session_parent"] is None and rows["acme-portal"]["session_parent"] == "acme-app"
    assert set(rows["acme"]) == {"slug", "parent", "session_parent", "has_session"}
    assert set(rows) == {"dash-root", "acme", "acme-app", "acme-portal", "acme-site", "other"}
    # any other session: its own account only (task #368); no token: refused
    app_h = {"Authorization": f"Bearer {issue_token('all', 'a', project_id=fam['app']['id'])}"}
    assert {r["slug"] for r in client.get("/api/projects/tree", headers=app_h).json()} == {
        "acme", "acme-app", "acme-portal", "acme-site"}
    other_h = {"Authorization": f"Bearer {issue_token('all', 'o', project_id=fam['other']['id'])}"}
    assert {r["slug"] for r in client.get("/api/projects/tree", headers=other_h).json()} == {"other"}
    assert client.get("/api/projects/tree").status_code == 401


# --- environments, the new-project form, the overview page -------------------------------------------------------

def test_new_project_with_dev_environment(client, fam):
    r = client.post("/projects", data={"slug": "acme-new", "name": "Acme New", "short_name": "acmenew",
                                       "type": "client", "sessions": "none", "parent": "acme-app",
                                       "repo": "acme-new", "dev_env": "1", "prod_server": "app01.example.com",
                                       "dev_server": "dev01.example.com"}, headers=OWN, follow_redirects=False)
    assert r.status_code == 303, r.text
    p = repo.get_project_by_slug("acme-new")
    assert p["parent_id"] == fam["app"]["id"] and p["repo"] == "acme-new" and p["dev_prod_sync"]
    envs = {e["env"]: e["server"] for e in tree.list_environments(p["id"])}
    assert envs == {"dev": "dev01.example.com", "prod": "app01.example.com"}
    assert p["session_roles"] == []


def test_new_project_prod_only_and_bad_server(client, fam):
    client.post("/projects", data={"slug": "acme-live", "name": "Acme Live", "short_name": "acmelive",
                                   "parent": "acme", "prod_server": "web-1.example.com"}, headers=OWN)
    p = repo.get_project_by_slug("acme-live")
    assert [e["env"] for e in tree.list_environments(p["id"])] == ["prod"] and not p["dev_prod_sync"]
    client.post("/projects", data={"slug": "acme-bad", "name": "Bad", "short_name": "acmebad",
                                   "prod_server": "evil-box"}, headers=OWN)
    with pytest.raises(repo.NotFound):
        repo.get_project_by_slug("acme-bad")      # refused before anything was created


def test_overview_shows_breadcrumb_repo_and_environments(client, fam):
    tree.set_details(fam["portal"]["id"], "owner", repo_name="acme", repo_path="portal/")
    tree.set_environment(fam["portal"]["id"], "prod", "owner", server="app01.example.com", url="https://portal.example")
    html = client.get("/acme-portal/settings").text
    assert "data-breadcrumb" in html and "Acme App" in html and "Acme</a>" in html
    assert "acme @ portal/" in html and "https://portal.example" in html and "its parent's session" in html
    r = client.post("/acme-portal/environments/dev", data={"server": "dev01.example.com"}, headers=OWN, follow_redirects=False)
    assert r.status_code == 303
    assert {e["env"] for e in tree.list_environments(fam["portal"]["id"])} == {"dev", "prod"}
    r = client.post("/acme-portal/environments/dev", data={"server": "dev01.example.com"},
                    headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


def test_seed_migration_is_a_no_op(monkeypatch):
    # 0019 once seeded one installation's project tree; the public release keeps the file as a no-op (applied-by-filename).
    repo.create_project("acme-dashboard", "Acme Dashboard", "internal", "system", short_name="dash", session_roles=["all"])
    before = [(p["slug"], p["parent_name"]) for p in tree.project_tree()]
    spec = importlib.util.spec_from_file_location("m0019", ROOT / "db/migrations/0019_project_tree_seed.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for _ in range(2):
        with connect() as conn:
            mod.migrate(conn)
            conn.commit()
    assert [(p["slug"], p["parent_name"]) for p in tree.project_tree()] == before


# --- audit fixes (task #132 review) ------------------------------------------------------------------------------

def test_archive_is_owner_only_and_never_orphans_a_branch(client, fam):
    # 1a. no session can archive or unarchive, through REST (the routes are gone) or repo
    h = {"Authorization": f"Bearer {issue_token('all', 'app', project_id=fam['app']['id'])}"}
    for path in (f"/api/projects/{fam['portal']['id']}/archive", f"/api/projects/{fam['app']['id']}/unarchive"):
        assert client.post(path, headers=h).status_code in (404, 405)
    with pytest.raises(repo.Forbidden):
        repo.set_project_archived(fam["portal"]["id"], True, "claude:all")
    # 1b. a project with active children can't be archived; its leaves can
    with pytest.raises(repo.Conflict, match="Acme Portal"):
        repo.set_project_archived(fam["app"]["id"], True, "owner")
    with pytest.raises(repo.Conflict):
        repo.set_project_archived(fam["acct"]["id"], True, "owner")
    repo.set_project_archived(fam["portal"]["id"], True, "owner")
    repo.set_project_archived(fam["app"]["id"], True, "owner")
    # ... and a child can't come back under an archived parent
    with pytest.raises(repo.Conflict, match="Acme App"):
        repo.set_project_archived(fam["portal"]["id"], False, "owner")
    # the owner's page shows the refusal as a flash, nothing changes
    r = client.post(f"/projects/{fam['acct']['id']}/archive", data={"confirm": "Acme"}, headers=OWN, follow_redirects=False)
    assert r.status_code in (302, 303) and repo.get_project(fam["acct"]["id"])["archived_at"] is None


def test_holder_lookup_stops_at_an_archived_project(fam):
    # pre-existing data could still hold an active child under an archived parent: nobody works it, consistently
    with connect() as conn:
        conn.execute("UPDATE projects SET archived_at = now() WHERE id = %s", (fam["app"]["id"],))
        conn.commit()
    assert tree.session_holder(fam["portal"]["id"]) is None
    assert fam["portal"]["id"] not in tree.routed_project_ids(fam["dash"]["id"])
    assert repo.session_for_task(repo.get_project(fam["portal"]["id"]), "feature") is None


def test_new_project_form_is_all_or_nothing(client, fam):
    # 2. a bad repo is refused BEFORE anything is created: no half-made project for the sessions timer to start
    client.post("/projects", data={"slug": "half-made", "name": "Half", "short_name": "half", "sessions": "all",
                                   "parent": "acme-app", "repo": "bad repo!"}, headers=OWN)
    with pytest.raises(repo.NotFound):
        repo.get_project_by_slug("half-made")
    client.post("/projects", data={"slug": "half-two", "name": "Half 2", "short_name": "half2",
                                   "parent": "no-such-parent"}, headers=OWN)
    with pytest.raises(repo.NotFound):
        repo.get_project_by_slug("half-two")
    # a duplicate short name rolls the whole thing back too
    client.post("/projects", data={"slug": "dup-short", "name": "Dup", "short_name": "acmeapp"}, headers=OWN)
    with pytest.raises(repo.NotFound):
        repo.get_project_by_slug("dup-short")
    # the default is no session: its parent's works it
    client.post("/projects", data={"slug": "kid", "name": "Kid", "short_name": "kid", "parent": "acme-app"},
                headers=OWN)
    assert repo.get_project_by_slug("kid")["session_roles"] == []


def test_my_tasks_honours_the_child_project(mcp_caller, client, fam):
    repo.create_task(fam["app"]["id"], "app work", "", "must", "feature", [], {}, "owner", assignee="ai")
    repo.create_task(fam["portal"]["id"], "portal work", "", "must", "feature", [], {}, "owner", assignee="ai")
    tok = issue_token("all", "app", project_id=fam["app"]["id"])
    mcp_caller(tok)
    assert [t["title"] for t in mcp_server.my_tasks(project="acme-portal")] == ["portal work"]
    # no project = the session's whole reach (task #253), its own project first
    assert [(t["title"], t["project"]) for t in mcp_server.my_tasks()] == [("app work", "acme-app"),
                                                                           ("portal work", "acme-portal")]
    h = {"Authorization": f"Bearer {tok}"}
    assert [t["title"] for t in client.get("/api/my-tasks?project=acme-portal", headers=h).json()] == ["portal work"]
    assert client.get("/api/my-tasks?project=other", headers=h).status_code == 404


def test_dash_edits_in_its_own_reach_dont_nudge_the_plan(fam):
    # 4. a task dash writes in its own project queues no plan_stale; another project's work does
    def stale():
        return [o for o in repo.list_outbox("all", undelivered_only=True) if o["kind"] == "plan_stale"]
    repo.create_task(fam["dash"]["id"], "dash's own", "", "must", "feature", [], {}, "claude:all")
    assert stale() == []
    repo.create_task(fam["app"]["id"], "app owner's", "", "must", "feature", [], {}, "owner")
    assert len(stale()) == 1


def test_api_created_projects_are_top_level(client, fam):
    # 5. the tree has no project root (change #76): a project made over the API is top level
    tok = issue_token("all", "dash", project_id=fam["dash"]["id"])
    r = client.post("/api/projects", json={"slug": "api-made", "name": "API made", "short_name": "apimade",
                                           "session_roles": []}, headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 201, r.text
    assert repo.get_project_by_slug("api-made")["parent_id"] is None


# --- #92 drift: the Knowledge page has no per-page search box ---------------------------------------------------

def test_knowledge_page_has_no_search_box_but_q_still_works(client, project):
    repo.create_knowledge(project["id"], "decision", "Findable thing", "", [], "", False, "owner", summary="s")
    html = client.get(f"/{project['slug']}/knowledge").text
    main = html.split("<main", 1)[1]
    assert 'type="search"' not in main and 'role="search"' not in main
    html = client.get(f"/{project['slug']}/knowledge?q=Findable").text
    assert "Findable thing" in html and "clear search" in html


# --- the Change log ----------------------------------------------------------------------------------------------

def test_change_lifecycle_and_rules(project):
    c = changes.recommend(project["id"], "Rename Tools to Apps", "**why**: clarity", "claude:all",
                          options=["Rename", "Keep"], links={"tasks": [1, "2", {"bad": 1}], "evil": [1]})
    assert c["stage"] == "recommended" and c["links"] == {"tasks": [1, 2]}
    with pytest.raises(repo.Conflict):
        changes.mark_done(c["id"], "claude:all")                    # not decided yet
    with pytest.raises(repo.Forbidden):
        changes.decide(c["id"], "approved", "claude:all")           # only the owner decides
    with pytest.raises(repo.BadRequest):
        changes.decide(c["id"], "changed", "owner")                 # 'changed' needs a note
    d = changes.decide(c["id"], "approved", "owner")
    assert d["stage"] == "decided" and d["decided_by"] == "owner"
    ev = [o for o in repo.list_outbox("features", undelivered_only=True) if o["kind"] == "change_decided"]
    assert ev and ev[0]["message"] == f"Owner decided change #{c['id']} (approved); read it with list_changes"
    with pytest.raises(repo.Conflict):
        changes.decide(c["id"], "rejected", "owner")                # decided once
    done = changes.mark_done(c["id"], "claude:all", links={"commits": ["abc123"]}, verified=True)
    assert done["stage"] == "done" and done["verified"] and done["links"]["commits"] == ["abc123"]
    r = changes.recommend(project["id"], "Bad idea", "", "claude:all")
    changes.decide(r["id"], "rejected", "owner")
    with pytest.raises(repo.Conflict):
        changes.mark_done(r["id"], "claude:all")                    # rejected can't be done
    g = changes.grouped(project["id"])
    assert {x["id"] for x in g["done"]} == {c["id"], r["id"]} and g["waiting"] == [] and g["decided"] == []


def test_changes_page_focus_and_decide(client, project):
    c = changes.recommend(project["id"], "Add a Change log", "Record changes first", "claude:features")
    assert "Waiting for your decision" in client.get(f"/{project['slug']}/tasks/board?layout=focus").text      # Focus
    assert "Waiting for your decision" in client.get("/").text                        # the global home's Focus
    html = client.get(f"/{project['slug']}/changes").text
    assert f'data-change="{c["id"]}"' in html and "Add a Change log" in html
    r = client.post(f"/{project['slug']}/changes/{c['id']}/decide", data={"decision": "approved"}, headers=OWN,
                    follow_redirects=False)
    assert r.status_code == 303 and changes.get(c["id"])["decision"] == "approved"
    other = repo.create_project("other-p", "Other", "internal", "owner", short_name="otherp")
    c2 = changes.recommend(other["id"], "x", "", "claude:all")
    r = client.post(f"/{project['slug']}/changes/{c2['id']}/decide", data={"decision": "approved"}, headers=OWN)
    assert changes.get(c2["id"])["stage"] == "recommended"                            # another project's id: refused


def test_change_mcp_tools(mcp_caller, project):
    mcp_caller(issue_token("all", "a", project_id=project["id"]))
    c = mcp_server.recommend_change("Move the menu", "Simpler", options=["Do it", "Leave it"])
    assert c["project_id"] == project["id"] and c["recommended_by"] == "claude:all"
    with pytest.raises(ValueError):
        mcp_server.mark_change_done(c["id"])                       # Conflict → ValueError until decided
    changes.decide(c["id"], "approved", "owner")
    tk = repo.create_task(project["id"], "linked", "", None, "feature", [], {}, "owner")
    assert mcp_server.mark_change_done(c["id"], links={"tasks": [tk["id"]]})["stage"] == "done"
    assert [x["id"] for x in mcp_server.list_changes(stage="done")] == [c["id"]]


def test_global_view_lists_waiting_changes(project, monkeypatch):
    c = changes.recommend(project["id"], "Pending one", "", "claude:all")
    v = plan.global_view()
    assert [x["id"] for x in v["changes_waiting"]] == [c["id"]]
