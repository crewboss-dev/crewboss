"""Task #368 (owner rule): accounts are FULLY PARTITIONED. A project sees itself and its descendants, a parent sees
everything below it up to its account, and an account NEVER sees another account's data. Only the global Dashboard
level — the owner's pages (no login, network-gated by design) and the overseer session (dash) — sees across accounts.

The estate mirrors production: dash (root, overseer) → Mallen (account, its `mallen` session) → northwind-erp (→ aios)
and mallen-website; personal-pa (top level, its own session); a sessionless top-level project `loose`; and a second
account, Acme (its `acme` session) → acme-app. Covers: no token on /api and MCP-over-HTTP is refused; cross-account
reads/writes of tasks, knowledge, materials, workflows, activity, objectives, changes; the account-scoped tree;
System Health + the business pulse; the sessions plan (+ its in-container CLI); materials never inherited from the
root; the owner drawer's same-account move; POST /api/projects; the overseer's own reach; knowledge links; the global
Focus under a project URL; the account-scoped page chrome."""
import json
from pathlib import Path

import pytest

from app import changes, config, db, mcp_server, pulse, repo, scope, tree
from app.auth import issue_token, resolve_session_token
from app.db import get_cursor

OWN = {"Origin": "https://crewboss.example.com", "Sec-Fetch-Site": "same-origin"}
FIXTURE = json.loads((Path(__file__).resolve().parent.parent / "fixtures/catalog-fixture.json").read_text())
WF = FIXTURE["workflows"][0]["id"]


def _project(slug, short, roles, parent=None, kind="project"):
    p = repo.create_project(slug, slug.replace("-", " ").title(), "internal", "owner", short_name=short,
                            session_roles=roles)
    if parent is not None:
        tree.set_place(p["id"], "owner", None if kind == "account" or parent["slug"] == config.OVERSEER_PROJECT else parent["id"], kind=kind)   # no project root (change #76)
    return repo.get_project(p["id"])


@pytest.fixture
def est(client, monkeypatch):
    with db.connect() as conn:
        conn.execute("TRUNCATE projects RESTART IDENTITY CASCADE")
        conn.commit()
    monkeypatch.setattr(config, "OVERSEER_PROJECT", "mallen-dashboard")
    monkeypatch.setattr(config, "HEALTH_PROJECT", "northwind-erp")
    dash = _project("mallen-dashboard", "dash", ["all"])
    mallen = _project("mallen", "mallen", ["all"], dash, kind="account")
    erp = _project("northwind-erp", "erp", [], mallen)
    aios = _project("aios", "aios", [], erp)
    site = _project("mallen-website", "website", [], mallen)
    pa = _project("personal-pa", "my-pa", ["all"], dash)
    loose = _project("loose", "loose", [], dash)
    acme = _project("acme", "acme", ["all"], dash, kind="account")
    acme_app = _project("acme-app", "acmeapp", [], acme)
    return {"dash": dash, "mallen": mallen, "erp": erp, "aios": aios, "site": site, "pa": pa, "loose": loose,
            "acme": acme, "acme_app": acme_app}


def _tok(project, stream="all"):
    return issue_token(stream, f"t-{project['short_name']}-{stream}", project_id=project["id"])


def _h(token):
    return {"Authorization": f"Bearer {token}"}


def _task(project, title="t", stream="feature", links=None):
    return repo.create_task(project["id"], title, "", "should", stream, [], links or {}, "owner")


def _know(project, title, kind="note"):
    return repo.create_knowledge(project["id"], kind, title, "body", [], "", False, "owner", summary="s")


def _material(project, slot="logo.primary", title=None):
    with get_cursor() as cur:
        cur.execute("INSERT INTO materials (project_id, slot, kind, title, purpose, filename, rel_path, content_type, "
                    "bytes, sha256, created_by) VALUES (%s, %s, 'logo', %s, 'p', 'l.svg', %s, 'image/svg+xml', 1, "
                    "'x', 'owner') RETURNING id",
                    (project["id"], slot, title or f"{project['slug']} logo", f"{project['slug']}/{slot}/v1-l.svg"))
        return cur.fetchone()["id"]


# --- the partition itself -----------------------------------------------------------------------------------------

def test_partition_roots(est):
    idx = tree.index()
    root = lambda k: tree.account_root_id(est[k]["id"], idx)  # noqa: E731
    assert {root(k) for k in ("mallen", "erp", "aios", "site")} == {est["mallen"]["id"]}
    assert {root(k) for k in ("acme", "acme_app")} == {est["acme"]["id"]}
    assert root("pa") == est["pa"]["id"] and root("loose") == est["loose"]["id"]
    assert root("dash") == est["dash"]["id"]  # the global root is a partition of one, above every account
    assert tree.account_member_ids(est["dash"]["id"], idx) == {est["dash"]["id"]}
    assert not tree.same_account(est["erp"]["id"], est["acme_app"]["id"], idx)


def test_overseer_reach_is_its_own_project_only(client, est, mcp_caller):
    d = resolve_session_token(_tok(est["dash"]))
    assert scope.reach(d) == {est["dash"]["id"]}  # never the sessionless `loose`, never any account
    loose_t = _task(est["loose"], "loose task")
    hd = _h(_tok(est["dash"]))
    assert client.get(f"/api/tasks/{loose_t['id']}", headers=hd).status_code == 200            # reads: yes
    assert client.patch(f"/api/tasks/{loose_t['id']}", json={"status": "doing"},
                        headers=hd).status_code == 404                                          # writes: no
    assert repo.get_task(loose_t["id"])["status"] == "backlog"
    mcp_caller(_tok(est["dash"]))
    g = mcp_server.ground()  # its briefing is its reach: no work it couldn't touch
    assert "loose task" not in json.dumps(g, default=str) and not g.get("multi_project")


# --- (a) no token on /api and on MCP over HTTP is refused -------------------------------------------------------

def test_no_token_on_every_api_read_is_a_401(client, est):
    t, k = _task(est["erp"]), _know(est["erp"], "K")
    for path in ("/api/projects", "/api/projects/tree", f"/api/projects/{est['erp']['id']}", "/api/tasks",
                 f"/api/tasks/{t['id']}", "/api/knowledge", f"/api/knowledge/{k['id']}", "/api/activity",
                 "/api/objectives", "/api/pulse", "/api/workflows", "/api/workflows/x", "/api/sessions",
                 "/api/sessions/plan", "/api/health", "/api/plan", "/api/my-tasks", "/api/outbox", "/api/ground"):
        r = client.get(path)
        assert r.status_code == 401, (path, r.status_code, r.text)
        assert "Authorization: Bearer" in r.json()["detail"], path


def test_a_bad_token_is_a_401_and_a_role_token_a_403(client, est):
    assert client.get("/api/tasks", headers=_h("mdd_nope")).status_code == 401
    assert client.get("/api/tasks", headers=_h(issue_token("scanner", "s"))).status_code == 403


READ_TOOLS = {
    "list_tasks": {}, "get_task": {"task_id": 1}, "search_knowledge": {"query": "x"}, "get_knowledge": {"knowledge_id": 1},
    "list_materials": {}, "get_material": {"material_id": 1}, "recent_activity": {}, "list_workflows": {},
    "get_workflow": {"workflow_id": "x"}, "list_objectives": {}, "get_pulse": {}, "list_milestones": {},
}


@pytest.mark.parametrize("name", sorted(READ_TOOLS))
def test_every_mcp_read_tool_needs_a_token_over_http(est, mcp_caller, name):
    mcp_caller(None)  # an HTTP call with no Authorization header
    with pytest.raises(PermissionError, match="Authorization: Bearer <session token>"):
        getattr(mcp_server, name)(**READ_TOOLS[name])


@pytest.mark.parametrize("name", sorted(READ_TOOLS))
def test_every_mcp_read_tool_refuses_a_call_outside_any_request(est, name):
    # No HTTP request at all (nothing stubbed): there is no owner-level MCP path.
    with pytest.raises(PermissionError):
        getattr(mcp_server, name)(**READ_TOOLS[name])


def test_every_read_tool_is_listed_here():
    import asyncio
    tools = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    writes = {"ground", "my_tasks", "add_task", "assign_task", "add_subtasks", "update_subtask",
              "propose_subtask_changes", "update_task", "comment_task", "ask_owner", "record_answer", "add_knowledge",
              "update_knowledge", "archive_knowledge", "add_material", "link_task_workflow", "suggest_triage",
              "recommend_change", "mark_change_done", "record_decision", "list_changes", "get_global_view", "set_global_plan", "patch_global_plan",
              # handoffs and shared docs (#908): the overseer is refused outright; covered by tests/test_handoffs.py
              "send_handoff", "list_handoffs", "get_handoff", "reply_handoff", "close_handoff", "put_doc", "get_doc", "list_docs", "doc_diff",
              "add_milestone", "update_milestone"}
    assert tools - writes == set(READ_TOOLS), tools - writes - set(READ_TOOLS)


BASE = {"Host": "127.0.0.1", "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _rpc(client, body, token=None, session_id=None):
    headers = dict(BASE)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if session_id:
        headers["mcp-session-id"] = session_id
    resp = client.post("/mcp/", content=json.dumps(body), headers=headers)
    assert resp.status_code in (200, 202), (resp.status_code, resp.text)
    if resp.headers.get("content-type", "").startswith("application/json"):
        return resp, (resp.json() if resp.text else None)
    data = [line[5:].strip() for line in resp.text.splitlines() if line.startswith("data:")]
    return resp, (json.loads(data[-1]) if data else None)


def test_mcp_read_over_real_http_without_a_token_is_refused(client, est):
    _task(est["acme_app"], "Acme secret")
    tok = _tok(est["mallen"])
    resp, _ = _rpc(client, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "pytest", "version": "0"}}},
        token=tok)
    sid = resp.headers["mcp-session-id"]
    _rpc(client, {"jsonrpc": "2.0", "method": "notifications/initialized"}, token=tok, session_id=sid)
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "list_tasks", "arguments": {}}}
    _, out = _rpc(client, call, token=None, session_id=sid)  # the session began WITH a token; this call has none
    result = out["result"]
    assert result.get("isError") is True
    assert "Authorization: Bearer" in json.dumps(result) and "Acme secret" not in json.dumps(result)


# --- cross-account reads and writes, per surface ------------------------------------------------------------------

def test_an_account_session_never_reads_or_writes_another_accounts_data(client, est, mcp_caller):
    mt, at = _task(est["aios"], "Mallen task"), _task(est["acme_app"], "Acme task")
    mk, ak = _know(est["erp"], "Mallen fact"), _know(est["acme_app"], "Acme fact")
    _know(est["acme_app"], "Acme goal", kind="objective")
    am = _material(est["acme"])
    repo.import_catalog("acme-app", FIXTURE)
    ac = changes.decide(changes.recommend(est["acme_app"]["id"], "Acme change", "r", "claude:all")["id"],
                        "approved", "owner")
    hm = _h(_tok(est["mallen"]))
    # REST: every Acme id or slug is a 404 to the Mallen session
    for method, path, body in (
            ("get", f"/api/tasks/{at['id']}", None), ("patch", f"/api/tasks/{at['id']}", {"status": "doing"}),
            ("get", "/api/tasks?project=acme-app", None), ("get", "/api/tasks?project=acme", None),
            ("get", f"/api/knowledge/{ak['id']}", None), ("get", "/api/knowledge?project=acme-app", None),
            ("patch", f"/api/knowledge/{ak['id']}", {"title": "x"}),
            ("get", "/api/workflows?project=acme-app", None), ("get", f"/api/workflows/{WF}?project=acme-app", None),
            ("get", "/api/activity?project=acme-app", None), ("get", "/api/objectives?project=acme-app", None),
            ("get", f"/api/projects/{est['acme']['id']}", None)):
        r = getattr(client, method)(path, headers=hm, **({"json": body} if body else {}))
        assert r.status_code == 404, (method, path, r.status_code)
    # the Mallen session's own lists hold nothing of Acme's
    assert "Acme" not in json.dumps(client.get("/api/tasks", headers=hm).json())
    assert "Acme" not in json.dumps(client.get("/api/knowledge", headers=hm).json())
    assert "Acme" not in json.dumps(client.get("/api/objectives", headers=hm).json())
    assert client.get("/api/workflows", headers=hm).json() == []
    assert client.get(f"/api/workflows/{WF}", headers=hm).status_code == 404
    assert "Acme" not in json.dumps(client.get("/api/activity", headers=hm).json())
    # MCP: the same, plus materials and changes
    mcp_caller(_tok(est["mallen"]))
    for fn, kw in ((mcp_server.get_task, {"task_id": at["id"]}), (mcp_server.get_knowledge, {"knowledge_id": ak["id"]}),
                   (mcp_server.get_material, {"material_id": am}), (mcp_server.list_materials, {"project": "acme"}),
                   (mcp_server.list_changes, {"project": "acme-app"}),
                   (mcp_server.mark_change_done, {"change_id": ac["id"]}),
                   (mcp_server.get_workflow, {"workflow_id": WF, "project": "acme-app"}),
                   (mcp_server.recent_activity, {"project": "acme-app"}),
                   (mcp_server.list_objectives, {"project": "acme-app"}),
                   (mcp_server.update_task, {"task_id": at["id"], "status": "doing"}),
                   (mcp_server.comment_task, {"task_id": at["id"], "body": "x"})):
        with pytest.raises(repo.NotFound):
            fn(**kw)
    assert {c["title"] for c in mcp_server.list_changes()} == set()
    assert mcp_server.search_knowledge("Acme") == []
    assert repo.get_task(at["id"])["status"] == "backlog" and not repo.get_task(at["id"])["comments"]
    # and the reverse direction
    mcp_caller(_tok(est["acme"]))
    with pytest.raises(repo.NotFound):
        mcp_server.get_task(mt["id"])
    with pytest.raises(repo.NotFound):
        mcp_server.get_knowledge(mk["id"])
    assert {t["title"] for t in mcp_server.list_tasks()} == {"Acme task"}


# --- (b) the tree, System Health, the pulse, the sessions plan --------------------------------------------------

def test_the_tree_is_the_callers_own_account_only(client, est):
    slugs = lambda h: {r["slug"] for r in client.get("/api/projects/tree", headers=h).json()}  # noqa: E731
    assert slugs(_h(_tok(est["acme"]))) == {"acme", "acme-app"}
    assert slugs(_h(_tok(est["mallen"]))) == {"mallen", "northwind-erp", "aios", "mallen-website"}
    assert slugs(_h(_tok(est["pa"]))) == {"personal-pa"}
    assert slugs(_h(_tok(est["dash"]))) == {est[k]["slug"] for k in est}  # the overseer: the whole tree
    # the tree has no project root (change #76): an account is top level, so no session sits above it
    rows = {r["slug"]: r for r in client.get("/api/projects/tree", headers=_h(_tok(est["mallen"]))).json()}
    assert rows["mallen"]["session_parent"] is None


@pytest.mark.parametrize("path", ["/api/health", "/api/pulse"])
def test_estate_feeds_are_the_overseers_and_the_estate_accounts_only(client, est, path):
    assert client.get(path, headers=_h(_tok(est["acme"]))).status_code == 404
    assert client.get(path, headers=_h(_tok(est["pa"]))).status_code == 404
    assert client.get(path, headers=_h(_tok(est["mallen"]))).status_code == 200
    assert client.get(path, headers=_h(_tok(est["dash"]))).status_code == 200


def test_pulse_in_mcp_ground_and_kpis_only_for_the_estate_account(client, est, mcp_caller, monkeypatch):
    monkeypatch.setattr(pulse, "get_pulse", lambda: {"paid": 1.0})
    mcp_caller(_tok(est["acme"]))
    with pytest.raises(PermissionError):
        mcp_server.get_pulse()
    assert mcp_server.ground()["pulse"] is None
    assert client.get("/api/ground", headers=_h(_tok(est["acme"]))).text.count("connects in phase 3") == 1
    mcp_caller(_tok(est["mallen"]))
    assert mcp_server.get_pulse() == {"paid": 1.0}
    assert mcp_server.ground()["pulse"] == {"paid": 1.0}
    assert repo.dashboard_kpis(est["erp"]["id"])["pulse"] == {"paid": 1.0}
    assert repo.dashboard_kpis(est["acme_app"]["id"])["pulse"] is None
    assert repo.dashboard_kpis(est["pa"]["id"])["pulse"] is None


def test_sessions_plan_is_the_overseers_only_and_the_cli_prints_the_same(client, est, capsys):
    assert client.get("/api/sessions/plan", headers=_h(_tok(est["mallen"]))).status_code == 404
    over = client.get("/api/sessions/plan", headers=_h(_tok(est["dash"])))
    assert over.status_code == 200
    from app import sessions_plan
    assert sessions_plan.main([]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == over.json() == json.loads(json.dumps(repo.session_plan()))
    assert {e["session_name"] for e in printed} == {"dash", "mallen", "my-pa", "acme"}


# --- (c) materials stop at the account ----------------------------------------------------------------------------

def test_materials_never_inherit_from_the_root_or_across_accounts(client, est, mcp_caller):
    from app import materials
    root_m = _material(est["dash"], "logo.root", "Root logo")
    mallen_m = _material(est["mallen"], "logo.primary", "Mallen logo")
    acme_m = _material(est["acme"], "logo.primary", "Acme logo")
    eff = {r["title"] for r in materials.effective(est["aios"])}
    assert eff == {"Mallen logo"}                                  # its account's, never the root's
    assert {r["title"] for r in materials.effective(est["acme_app"])} == {"Acme logo"}
    assert materials.effective(est["pa"]) == []                    # a top-level project inherits nothing
    assert materials.overridden(est["mallen"]) == []
    mcp_caller(_tok(est["mallen"]))
    assert {r["title"] for r in mcp_server.list_materials(project="aios")} == {"Mallen logo"}
    assert mcp_server.get_material(mallen_m)["title"] == "Mallen logo"
    for mid in (root_m, acme_m):
        with pytest.raises(repo.NotFound):
            mcp_server.get_material(mid)
    html = client.get("/aios/materials").text
    assert "Mallen logo" in html and "Root logo" not in html and "Acme logo" not in html


# --- (d) the overseer's writes stay limited to requests, comments and suggestions -----------------------------------

def test_overseer_communicates_across_accounts_but_never_edits(client, est, mcp_caller):
    at = _task(est["acme_app"], "Acme task")
    mcp_caller(_tok(est["dash"]))
    assert mcp_server.get_task(at["id"])["title"] == "Acme task"
    mcp_server.comment_task(task_id=at["id"], body="please look")              # communicate: yes
    req = mcp_server.add_task(title="Do this", stream="feature", project="acme-app")
    assert req["project_id"] == est["acme_app"]["id"]                          # a request filed in Acme
    for fn, kw in ((mcp_server.update_task, {"task_id": at["id"], "status": "done"}),
                   (mcp_server.assign_task, {"task_id": at["id"], "assignee": "owner"}),
                   (mcp_server.add_subtasks, {"task_id": at["id"], "items": ["x"]}),
                   (mcp_server.link_task_workflow, {"task_id": at["id"], "workflow_id": "w"}),
                   (mcp_server.add_knowledge, {"title": "x", "summary": "s", "project": "acme-app"}),
                   (mcp_server.recommend_change, {"title": "x", "recommendation": "y", "project": "acme-app"})):
        with pytest.raises(repo.NotFound):
            fn(**kw)
    assert repo.get_task(at["id"])["status"] == "backlog"


# --- (e) the owner drawer moves a task only within its account ---------------------------------------------------

def test_drawer_offers_and_accepts_only_same_account_moves(client, est):
    t = _task(est["erp"], "Move me")
    html = client.get(f"/northwind-erp/tasks/{t['id']}/panel").text
    for ok in ('value="northwind-erp"', 'value="aios"', 'value="mallen-website"'):
        assert ok in html
    for no in ('value="acme-app"', 'value="personal-pa"', 'value="loose"', 'value="mallen-dashboard"'):
        assert no not in html
    r = client.post(f"/northwind-erp/tasks/{t['id']}", data={"move_to": "acme-app"}, headers={**OWN, "HX-Request": "true"})
    assert r.status_code == 400 and "another account" in r.text
    assert repo.get_task(t["id"])["project_id"] == est["erp"]["id"]
    r = client.post(f"/northwind-erp/tasks/{t['id']}", data={"move_to": "aios"}, headers={**OWN, "HX-Request": "true"})
    assert r.status_code == 200 and repo.get_task(t["id"])["project_id"] == est["aios"]["id"]
    with pytest.raises(repo.BadRequest):
        repo.update_task(t["id"], {"project": "mallen-dashboard"}, "owner")  # nor up to the root


# --- (f) POST /api/projects -----------------------------------------------------------------------------------------

def test_only_the_overseer_creates_projects_over_the_api(client, est):
    body = {"slug": "sneaky", "name": "Sneaky", "short_name": "sneaky"}
    for k in ("mallen", "acme", "pa"):
        assert client.post("/api/projects", json=body, headers=_h(_tok(est[k]))).status_code == 404
    with pytest.raises(repo.NotFound):
        repo.get_project_by_slug("sneaky")
    assert client.post("/api/projects", json=body, headers=_h(_tok(est["dash"]))).status_code == 201


# --- (g) knowledge links, the global Focus, the page chrome -------------------------------------------------------

def test_task_links_resolve_and_validate_only_within_the_account(client, est, mcp_caller):
    mk, ak = _know(est["erp"], "Mallen fact"), _know(est["acme_app"], "Acme fact")
    t = _task(est["erp"], "Linked", links={"knowledge": [mk["id"], ak["id"]]})  # an old / owner-made link
    html = client.get(f"/northwind-erp/tasks/{t['id']}/panel").text
    assert "Mallen fact" in html and "Acme fact" not in html
    mcp_caller(_tok(est["mallen"]))
    with pytest.raises(repo.NotFound):
        mcp_server.add_task(title="x", stream="feature", project="aios", links={"knowledge": [ak["id"]]})
    with pytest.raises(repo.NotFound):
        mcp_server.update_task(task_id=t["id"], links={"knowledge": [ak["id"]]})
    assert mcp_server.add_task(title="ok", stream="feature", project="aios", links={"knowledge": [mk["id"]]})
    hm = _h(_tok(est["mallen"]))
    r = client.post("/api/tasks", json={"title": "x", "stream": "feature", "project": "aios",
                                        "links": {"knowledge": [ak["id"]]}}, headers=hm)
    assert r.status_code == 404
    assert client.patch(f"/api/tasks/{t['id']}", json={"links": {"knowledge": [ak["id"]]}},
                        headers=hm).status_code == 404


def test_global_focus_never_renders_under_a_project_url(client, est):
    _task(est["acme_app"], "Acme secret")
    r = client.get("/northwind-erp/tasks/board?layout=home", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/all/focus"


def _switcher_and_dots(html_text):
    """(slugs in the topbar's project switcher, session-dot names) of a rendered page."""
    import html as _html
    import re
    raw = _html.unescape(html_text)
    dots = set(re.findall(r"([a-z0-9-]+) \(all streams\): ", raw))
    return None, dots


def test_pages_inside_an_account_show_only_that_accounts_tree_and_sessions(client, est):
    slugs, dots = _switcher_and_dots(client.get("/acme").text)
    assert dots == {"acme"}
    slugs, dots = _switcher_and_dots(client.get("/aios/board").text)
    assert dots == {"mallen"}
    # the global pages (All projects, the dashboard root's own) keep everything
    for url in ("/", "/mallen-dashboard"):
        slugs, dots = _switcher_and_dots(client.get(url).text)
        assert dots == {"dash", "mallen", "my-pa", "acme"}
