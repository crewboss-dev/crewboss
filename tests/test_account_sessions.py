"""Task #253 (owner, 2026-10-12): standing AI sessions attach to ACCOUNTS, not projects.

The estate here mirrors production: dash (root, overseer) → Mallen (account) → mallen-erp (→ aios) and
mallen-website; personal-pa (top level, its own session); plus a second account, Acme (→ acme-app), so account
isolation can be tested. Covers the 0023 data migration (and its rollback), the session plan, event routing to the
nearest session-running ancestor, subtree scope (an account session never reads or acts on another account's work;
dash reads everything) and the retiring erp session's outbox."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

from app import catalog_import, config, db, mcp_server, repo, scope, tree
from app.auth import issue_token, resolve_session_token

ROOT = Path(__file__).resolve().parent.parent
MIGRATION = ROOT / "db/migrations/0023_account_sessions.py"
OWN = {"Origin": "https://crewboss.example.com", "Sec-Fetch-Site": "same-origin"}


def _migration():
    spec = importlib.util.spec_from_file_location("_m0023", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _apply(fn_name):
    with db.connect() as conn:
        getattr(_migration(), fn_name)(conn)
        conn.commit()


def _project(slug, short, roles, parent=None, kind="project"):
    p = repo.create_project(slug, slug.replace("-", " ").title(), "internal", "owner", short_name=short,
                            session_roles=roles)
    if parent is not None:
        tree.set_place(p["id"], "owner", None if kind == "account" or parent["slug"] == config.OVERSEER_PROJECT else parent["id"], kind=kind)   # no project root (change #76)
    return repo.get_project(p["id"])


@pytest.fixture
def pre(client, monkeypatch):
    """The estate BEFORE the migration: erp is mallen-erp's session, the Mallen account runs none. (The client's
    start-up seed may have made a mallen-erp of its own: cleared first, so the estate is exactly this one.)"""
    with db.connect() as conn:
        conn.execute("TRUNCATE projects RESTART IDENTITY CASCADE")
        conn.commit()
    monkeypatch.setattr(config, "OVERSEER_PROJECT", "mallen-dashboard")
    dash = _project("mallen-dashboard", "dash", ["all"])
    mallen = _project("mallen", "mallen", [], dash, kind="account")
    erp = _project("mallen-erp", "erp", ["all"], mallen)
    aios = _project("aios", "aios", [], erp)
    site = _project("mallen-website", "website", [], mallen)
    pa = _project("personal-pa", "my-pa", ["all"], dash)
    acme = _project("acme", "acme", ["all"], dash, kind="account")
    acme_app = _project("acme-app", "acmeapp", [], acme)
    return {"dash": dash, "mallen": mallen, "erp": erp, "aios": aios, "site": site, "pa": pa, "acme": acme,
            "acme_app": acme_app}


@pytest.fixture
def est(pre):
    """The estate AFTER the migration."""
    _apply("migrate")
    return {k: repo.get_project(v["id"]) for k, v in pre.items()}


def _tok(project, stream="all"):
    return issue_token(stream, f"t-{project['short_name']}", project_id=project["id"])


def _h(token):
    return {"Authorization": f"Bearer {token}"}


def _task(project, title="t", stream="feature", assignee="ai"):
    return repo.create_task(project["id"], title, "", "should", stream, [], {}, "owner", assignee=assignee)


# --- the migration -----------------------------------------------------------------------------------------------

def test_migration_moves_the_erp_session_to_the_account(pre):
    _apply("migrate")
    roles = {k: repo.get_project(v["id"])["session_roles"] for k, v in pre.items()}
    assert roles["mallen"] == ["all"] and roles["erp"] == []
    assert roles["dash"] == ["all"] and roles["pa"] == ["all"] and roles["acme"] == ["all"]
    assert any("moved here from mallen-erp" in a["summary"] for a in repo.recent_activity(pre["mallen"]["id"]))
    # idempotent: a second run changes nothing
    _apply("migrate")
    assert repo.get_project(pre["mallen"]["id"])["session_roles"] == ["all"]
    assert repo.get_project(pre["erp"]["id"])["session_roles"] == []


def test_migration_rollback_restores_the_erp_session(pre):
    _apply("migrate")
    db.rollback_python_migration(MIGRATION.name, migrations_dir=str(MIGRATION.parent))
    assert repo.get_project(pre["mallen"]["id"])["session_roles"] == []
    assert repo.get_project(pre["erp"]["id"])["session_roles"] == ["all"]
    assert {e["session_name"] for e in repo.session_plan()} == {"dash", "erp", "my-pa", "acme"}
    _apply("rollback")  # a second rollback is a no-op
    assert repo.get_project(pre["erp"]["id"])["session_roles"] == ["all"]


def test_migration_keeps_per_stream_roles_and_leaves_a_fresh_database_alone(monkeypatch):
    _apply("migrate")  # no projects at all: nothing to do, no error
    dash = _project("mallen-dashboard", "dash", ["all"])
    mallen = _project("mallen", "mallen", [], dash, kind="account")
    erp = _project("mallen-erp", "erp", ["bugs", "features"], mallen)
    _apply("migrate")
    assert repo.get_project(mallen["id"])["session_roles"] == ["features", "bugs"]
    assert repo.get_project(erp["id"])["session_roles"] == []


def test_migration_skips_an_account_that_already_runs_a_session(monkeypatch):
    dash = _project("mallen-dashboard", "dash", ["all"])
    mallen = _project("mallen", "mallen", ["all"], dash, kind="account")
    erp = _project("mallen-erp", "erp", ["all"], mallen)
    _apply("migrate")
    assert repo.get_project(erp["id"])["session_roles"] == ["all"]
    # it moved nothing, so its rollback touches nothing (the owner's account session stays, erp keeps its own)
    _apply("rollback")
    assert repo.get_project(mallen["id"])["session_roles"] == ["all"]
    assert repo.get_project(erp["id"])["session_roles"] == ["all"]


def test_rollback_only_reverses_its_own_move(pre):
    _apply("migrate")
    _apply("rollback")
    assert repo.get_project(pre["erp"]["id"])["session_roles"] == ["all"]
    # later the OWNER moves the session to the account by hand: a stray second rollback must not undo that
    with repo.get_cursor() as cur:
        cur.execute("UPDATE projects SET session_roles = '{all}' WHERE id = %s", (pre["mallen"]["id"],))
        cur.execute("UPDATE projects SET session_roles = '{}' WHERE id = %s", (pre["erp"]["id"],))
    _apply("rollback")
    assert repo.get_project(pre["mallen"]["id"])["session_roles"] == ["all"]
    assert repo.get_project(pre["erp"]["id"])["session_roles"] == []


def test_rollback_without_a_source_session_only_turns_the_account_off(monkeypatch):
    dash = _project("mallen-dashboard", "dash", ["all"])
    mallen = _project("mallen", "mallen", [], dash, kind="account")
    erp = _project("mallen-erp", "erp", [], mallen)
    _apply("migrate")                                    # erp had none: the account just gets the default
    assert repo.get_project(mallen["id"])["session_roles"] == ["all"]
    _apply("rollback")
    assert repo.get_project(mallen["id"])["session_roles"] == []
    assert repo.get_project(erp["id"])["session_roles"] == []    # never given a session it didn't have


def test_rollback_leaves_a_session_the_owner_gave_erp_since(pre):
    _apply("migrate")
    with repo.get_cursor() as cur:
        cur.execute("UPDATE projects SET session_roles = '{bugs}' WHERE id = %s", (pre["erp"]["id"],))
    _apply("rollback")
    assert repo.get_project(pre["erp"]["id"])["session_roles"] == ["bugs"]
    assert repo.get_project(pre["mallen"]["id"])["session_roles"] == ["all"]


def test_rollback_cli_refuses_a_migration_without_rollback():
    with pytest.raises(ValueError):
        db.rollback_python_migration("0019_project_tree_seed.py")
    with pytest.raises(ValueError):
        db.rollback_python_migration("0001_init.sql")


# --- the plan ----------------------------------------------------------------------------------------------------

def test_plan_has_one_session_per_account_named_by_its_short_name(client, est):
    # the plan spans every account: the overseer's only (task #368)
    assert client.get("/api/sessions/plan", headers=_h(_tok(est["mallen"]))).status_code == 404
    plan = {e["session_name"]: e for e in client.get("/api/sessions/plan", headers=_h(_tok(est["dash"]))).json()}
    assert set(plan) == {"dash", "mallen", "my-pa", "acme"}
    assert plan["mallen"] == {"project": "mallen", "short_name": "mallen", "role": "all", "stream": "all",
                              "session_name": "mallen", "kind": "account"}
    assert plan["my-pa"]["kind"] == "project"


def test_session_list_shows_the_account_session(est):
    names = {s["session_name"]: s for s in repo.list_sessions()}
    assert names["mallen"]["label"] == "mallen (all streams)" and "erp" not in names


def test_a_new_account_gets_its_own_session_by_default(client, est):
    r = client.post("/projects", data={"slug": "beta", "name": "Beta", "short_name": "beta", "kind": "account",
                                       "parent": ""}, headers=OWN, follow_redirects=False)
    assert r.status_code == 303, r.text
    assert repo.get_project_by_slug("beta")["session_roles"] == ["all"]
    r = client.post("/projects", data={"slug": "beta-app", "name": "Beta App", "short_name": "betaapp",
                                       "parent": "beta"}, headers=OWN, follow_redirects=False)
    assert repo.get_project_by_slug("beta-app")["session_roles"] == []  # a project: its account's session works it


# --- routing -----------------------------------------------------------------------------------------------------

def test_events_route_to_the_nearest_session_ancestor(est):
    t_aios, t_site, t_pa, t_acme = _task(est["aios"]), _task(est["site"]), _task(est["pa"]), _task(est["acme_app"])
    for t in (t_aios, t_site, t_pa, t_acme):
        repo.comment_task(t["id"], "hi", "owner")
    got = {k: {o["task_id"] for o in repo.session_outbox(est[k], "all") if o["kind"] == "task_comment"}
           for k in ("mallen", "dash", "pa", "acme")}
    assert got == {"mallen": {t_aios["id"], t_site["id"]}, "dash": set(), "pa": {t_pa["id"]},
                   "acme": {t_acme["id"]}}
    assert repo.session_for_task(est["aios"], "feature") == "mallen"
    assert repo.session_for_task(est["site"], "bug") == "mallen"
    assert repo.session_for_task(est["pa"], "bug") == "my-pa"
    assert tree.session_holder(est["aios"]["id"])["slug"] == "mallen"


def test_dash_keeps_plan_stale_for_mallen_changes(est):
    _task(est["site"])
    stale = [o for o in repo.session_outbox(est["dash"], "all") if o["kind"] == "plan_stale"]
    assert len(stale) == 1
    assert all(o["kind"] != "plan_stale" for o in repo.session_outbox(est["mallen"], "all"))


def test_the_account_sessions_own_edits_nudge_dash(est, monkeypatch):
    monkeypatch.setattr(config, "PLAN_STALE_COOLDOWN_MINUTES", 0)
    with repo.get_cursor() as cur:  # an 'all' actor in Mallen is the mallen session now, not dash
        assert repo.mark_plan_stale(cur, est["site"]["id"], None, "updated", "claude:all") is True
        assert repo.mark_plan_stale(cur, est["dash"]["id"], None, "updated", "claude:all") is False


def test_project_less_rows_follow_the_legacy_project_to_the_account_session(est, monkeypatch):
    monkeypatch.setattr(config, "LEGACY_TOKEN_PROJECT", "mallen-erp")
    repo.push_outbox("errors", "health_error", "something broke")
    assert [o["kind"] for o in repo.session_outbox(est["mallen"], "all")] == ["health_error"]
    assert repo.session_outbox(est["erp"], "all") == []          # erp left the plan
    assert repo.session_outbox(est["dash"], "all") == []


def test_the_retiring_erp_session_gets_only_its_stop_notice(client, est):
    """Cut-over: erp's token stays bound to mallen-erp. It can still finish its work, but events go to mallen."""
    erp_tok, mallen_tok = _tok(est["erp"]), _tok(est["mallen"])
    t = _task(est["erp"])
    repo.comment_task(t["id"], "hi", "owner")
    assert client.get("/api/outbox?undelivered=1", headers=_h(erp_tok)).json() == []
    assert {o["kind"] for o in client.get("/api/outbox?undelivered=1", headers=_h(mallen_tok)).json()} == {
        "task_assigned", "task_comment"}
    # sync tells erp to wrap up: ONLY erp hears it, never the mallen session that now works its project
    r = client.post("/api/sessions/stopping", json={"phase": "notice"}, headers=_h(erp_tok))
    assert r.status_code == 200 and r.json()["reason"] == "sessions_off" and r.json()["told"] is True
    assert [o["kind"] for o in client.get("/api/outbox?undelivered=1", headers=_h(erp_tok)).json()] == ["session_stop"]
    assert "session_stop" not in {o["kind"] for o in client.get("/api/outbox?undelivered=1", headers=_h(mallen_tok)).json()}
    stop_id = repo.list_outbox("features", project_id=est["erp"]["id"])[-1]["id"]
    assert client.post(f"/api/outbox/{stop_id}/delivered?via=channel", headers=_h(mallen_tok)).status_code == 404
    assert client.post(f"/api/outbox/{stop_id}/delivered?via=channel", headers=_h(erp_tok)).status_code == 200
    # its in-flight work: it may still update its ERP-branch task
    assert client.patch(f"/api/tasks/{t['id']}", json={"status": "doing"}, headers=_h(erp_tok)).status_code == 200
    # the mallen session can't stop itself through this route: it is in the plan
    assert client.post("/api/sessions/stopping", json={"phase": "notice"}, headers=_h(mallen_tok)).status_code == 409


def test_stop_notices_of_two_projects_never_coalesce(client, est):
    a, b = _project("aa", "aa", ["all"]), _project("bb", "bb", ["all"])
    ta, tb = _tok(a), _tok(b)
    for p in (a, b):
        repo.set_project_archived(p["id"], True, "owner")
    for t in (ta, tb):
        assert client.post("/api/sessions/stopping", json={"phase": "notice"}, headers=_h(t)).status_code == 200
    assert len(client.get("/api/outbox?undelivered=1", headers=_h(ta)).json()) == 1
    assert len(client.get("/api/outbox?undelivered=1", headers=_h(tb)).json()) == 1


# --- scope: subtree reach, account isolation, dash reads all -----------------------------------------------------

def test_account_session_reach_is_its_subtree(est):
    s = resolve_session_token(_tok(est["mallen"]))
    assert scope.reach(s) == {est[k]["id"] for k in ("mallen", "erp", "aios", "site")}
    assert [p["slug"] for p in scope.reach_projects(s)] == ["mallen", "mallen-erp", "aios", "mallen-website"]
    d = resolve_session_token(_tok(est["dash"]))
    assert scope.reach(d) == {est["dash"]["id"]}  # dash no longer works any Mallen project


def test_list_reads_default_to_the_whole_account(client, est):
    tasks = {k: _task(est[k], title=k) for k in ("erp", "aios", "site", "pa", "acme_app")}
    h = _h(_tok(est["mallen"]))
    listed = {t["id"]: t for t in client.get("/api/tasks", headers=h).json()}
    assert set(listed) == {tasks[k]["id"] for k in ("erp", "aios", "site")}
    assert listed[tasks["aios"]["id"]]["project_slug"] == "aios"
    mine = client.get("/api/my-tasks", headers=h).json()
    assert {(t["id"], t["project"]) for t in mine} == {
        (tasks["erp"]["id"], "mallen-erp"), (tasks["aios"]["id"], "aios"), (tasks["site"]["id"], "mallen-website")}
    # naming one project narrows it; naming the account rolls it up
    assert [t["id"] for t in client.get("/api/tasks?project=aios", headers=h).json()] == [tasks["aios"]["id"]]
    assert len(client.get("/api/tasks?project=mallen", headers=h).json()) == 3
    assert [t["project"] for t in client.get("/api/my-tasks?project=mallen-website", headers=h).json()] == [
        "mallen-website"]
    acts = client.get("/api/activity", headers=h).json()
    assert acts and {a["project_name"] for a in acts} <= {est[k]["name"] for k in ("mallen", "erp", "aios", "site")}


def test_account_sessions_never_touch_each_others_work(client, est):
    mallen_t, acme_t = _task(est["aios"]), _task(est["acme_app"])
    hm, ha = _h(_tok(est["mallen"])), _h(_tok(est["acme"]))
    for h, theirs, their_slug in ((hm, acme_t, "acme-app"), (ha, mallen_t, "aios")):
        assert client.get(f"/api/tasks/{theirs['id']}", headers=h).status_code == 404
        assert client.patch(f"/api/tasks/{theirs['id']}", json={"status": "doing"}, headers=h).status_code == 404
        assert client.post(f"/api/tasks/{theirs['id']}/comments", json={"body": "x"}, headers=h).status_code == 404
        assert client.get(f"/api/tasks?project={their_slug}", headers=h).status_code == 404
        assert client.post("/api/tasks", json={"title": "x", "stream": "feature", "project": their_slug},
                           headers=h).status_code == 404
    assert client.get("/api/tasks?project=personal-pa", headers=hm).status_code == 404
    assert {p["slug"] for p in client.get("/api/projects", headers=hm).json()} == {
        "mallen", "mallen-erp", "aios", "mallen-website"}
    # each acts freely inside its own account
    assert client.patch(f"/api/tasks/{mallen_t['id']}", json={"status": "doing"}, headers=hm).status_code == 200
    assert client.patch(f"/api/tasks/{acme_t['id']}", json={"status": "doing"}, headers=ha).status_code == 200


def test_dash_reads_every_account_but_writes_only_its_own(client, est):
    mallen_t, acme_t, own = _task(est["aios"]), _task(est["acme_app"]), _task(est["dash"])
    hd = _h(_tok(est["dash"]))
    for t in (mallen_t, acme_t):
        assert client.get(f"/api/tasks/{t['id']}", headers=hd).status_code == 200
        assert client.patch(f"/api/tasks/{t['id']}", json={"status": "doing"}, headers=hd).status_code == 404
        assert client.post(f"/api/tasks/{t['id']}/comments", json={"body": "x"}, headers=hd).status_code == 404
    assert len(client.get("/api/tasks?project=mallen", headers=hd).json()) == 1      # the account, rolled up
    assert [t["id"] for t in client.get("/api/tasks", headers=hd).json()] == [own["id"]]
    assert client.patch(f"/api/tasks/{own['id']}", json={"status": "doing"}, headers=hd).status_code == 200


def test_mcp_tools_cover_the_subtree(est, mcp_caller):
    tasks = {k: _task(est[k], title=k) for k in ("erp", "site", "acme_app")}
    mcp_caller(_tok(est["mallen"]))
    assert {t["project"] for t in mcp_server.my_tasks()} == {"mallen-erp", "mallen-website"}
    assert {t["id"] for t in mcp_server.list_tasks()} == {tasks["erp"]["id"], tasks["site"]["id"]}
    with pytest.raises(repo.NotFound):
        mcp_server.get_task(tasks["acme_app"]["id"])
    with pytest.raises(repo.NotFound):
        mcp_server.update_task(tasks["acme_app"]["id"], status="doing")
    # accounts hold no tasks: the account session names the project, and the refusal lists its projects
    with pytest.raises(repo.BadRequest) as exc:
        mcp_server.add_task("new", "feature")
    assert "mallen-erp" in str(exc.value) and "mallen-website" in str(exc.value) and "acme" not in str(exc.value)
    made = mcp_server.add_task("new", "feature", project="aios")
    assert made["project_id"] == est["aios"]["id"]
    g = mcp_server.ground()
    assert {c["slug"] for c in g["child_projects"]} == {"mallen-erp", "aios", "mallen-website"}
    rec = mcp_server.recommend_change("Do a thing", "because", project="mallen-website")
    assert {c["id"] for c in mcp_server.list_changes()} == {rec["id"]}
    # dash: the overseer still plans across everything
    mcp_caller(_tok(est["dash"]))
    assert mcp_server.get_task(tasks["acme_app"]["id"])["id"] == tasks["acme_app"]["id"]
    assert {t["project"] for t in mcp_server.get_global_view()["tasks"]} >= {"mallen-erp", "acme-app"}


def test_account_knowledge_requests_go_to_a_project_the_account_session_works(est):
    k = repo.create_knowledge(est["mallen"]["id"], "rule", "A rule", "", [], "", False, "owner", summary="s")
    holder = repo._account_request_holder(est["mallen"])
    # the account's OLDEST project (lowest id) its session works: mallen-erp, as in production (id 1)
    assert holder["slug"] == "mallen-erp"
    assert holder["id"] in scope.reach(resolve_session_token(_tok(est["mallen"])))
    assert k["project_id"] == est["mallen"]["id"]
    # an account with no session of its own and nothing above it (the tree has no project root, change #76): nobody holds it
    bare = _project("bare", "bare", [], est["dash"], kind="account")
    assert repo._account_request_holder(bare) is None


# --- reads default to the whole reach (audit finding 4) ----------------------------------------------------------

FIXTURE = json.loads((ROOT / "fixtures" / "catalog-fixture.json").read_text())


def _scan(project, wf_id):
    c = copy.deepcopy(FIXTURE)
    c["project"], c["commit"] = project["slug"], "b" * 40
    c["workflows"] = [dict(FIXTURE["workflows"][0], id=wf_id, name=wf_id)]
    catalog_import.import_document(c, project["slug"])


def _know(project, title, kind="note"):
    goal = kind == "objective"
    return repo.create_knowledge(project["id"], kind, title, "", [], "", False, "owner",
                                 target_value=1 if goal else None, target_period="monthly" if goal else None,
                                 summary="s", target_unit="count" if goal else None)


def test_knowledge_objectives_and_workflows_cover_the_reach(client, est, mcp_caller):
    for k in ("mallen", "erp", "site", "acme_app", "dash"):
        _know(est[k], f"zebra {k}")
        _know(est[k], f"goal {k}", kind="objective")
    _scan(est["erp"], "erp.flow")
    _scan(est["site"], "site.flow")
    _scan(est["acme_app"], "acme.flow")
    mine = {"zebra mallen", "zebra erp", "zebra site"}
    goals = {"goal mallen", "goal erp", "goal site"}
    h = _h(_tok(est["mallen"]))
    assert {k["title"] for k in client.get("/api/knowledge?kind=note", headers=h).json()} == mine
    assert {o["title"] for o in client.get("/api/objectives", headers=h).json()} == goals
    wfs = client.get("/api/workflows", headers=h).json()
    assert {(w["id"], w["project"]) for w in wfs} == {("erp.flow", "mallen-erp"), ("site.flow", "mallen-website")}
    got = client.get("/api/workflows/site.flow", headers=h)
    assert got.status_code == 200 and got.json()["project"] == "mallen-website"
    assert client.get("/api/workflows/acme.flow", headers=h).status_code == 404   # another account's
    # naming a project narrows it; naming the account is its own (shared) entries, never a rollup
    assert [w["id"] for w in client.get("/api/workflows?project=mallen-erp", headers=h).json()] == ["erp.flow"]
    assert {k["title"] for k in client.get("/api/knowledge?kind=note&project=mallen", headers=h).json()} == {
        "zebra mallen"}
    assert client.get("/api/knowledge?project=acme-app", headers=h).status_code == 404
    # the MCP twins
    mcp_caller(_tok(est["mallen"]))
    assert {k["title"] for k in mcp_server.search_knowledge("zebra")} == mine
    assert {o["title"] for o in mcp_server.list_objectives()} == goals
    assert {(w["id"], w["project"]) for w in mcp_server.list_workflows()} == {("erp.flow", "mallen-erp"),
                                                                             ("site.flow", "mallen-website")}
    assert mcp_server.get_workflow("erp.flow")["project"] == "mallen-erp"
    with pytest.raises(repo.NotFound):
        mcp_server.get_workflow("acme.flow")
    assert "project" not in mcp_server.list_workflows(project="mallen-erp")[0]  # one project: shape unchanged


def test_dash_reads_are_unchanged(client, est, mcp_caller):
    for k in ("dash", "erp", "acme_app"):
        _know(est[k], f"zebra {k}")
    _scan(est["acme_app"], "acme.flow")
    h = _h(_tok(est["dash"]))
    # no project: its own (dash's reach is just itself); any active project by name, as before
    assert {k["title"] for k in client.get("/api/knowledge?kind=note", headers=h).json()} == {"zebra dash"}
    assert {k["title"] for k in client.get("/api/knowledge?kind=note&project=acme-app", headers=h).json()} == {
        "zebra acme_app"}
    assert client.get("/api/workflows/acme.flow?project=acme-app", headers=h).status_code == 200
    assert client.get("/api/workflows/acme.flow", headers=h).status_code == 404
    mcp_caller(_tok(est["dash"]))
    assert {k["title"] for k in mcp_server.search_knowledge("zebra", project="mallen-erp")} == {"zebra erp"}
