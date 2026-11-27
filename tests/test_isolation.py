"""Project isolation (owner rule 2026-09-30): sessions within a project may work together; sessions in DIFFERENT
projects never read or touch each other's data. A token from project A gets, for anything of project B, the
SAME 404 a missing id gets (existence never leaks) — on every REST route and every MCP tool — and B is left
exactly as it was. Plus the 'all' session (one session for every stream) and outbox routing."""
import json
from pathlib import Path

import pytest

from app import mcp_server, repo
from app.auth import issue_token

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = json.loads((ROOT / "fixtures/catalog-fixture.json").read_text())
WF = FIXTURE["workflows"][0]["id"]


@pytest.fixture
def ab():
    a = repo.create_project("proj-a", "Project A", "internal", "owner", short_name="pa",
                            session_roles=list(repo.SESSION_STREAMS))
    b = repo.create_project("proj-b", "Project B", "client", "owner", short_name="pb", session_roles=["all"])
    repo.import_catalog("proj-b", FIXTURE)
    bt = repo.create_task(b["id"], "B secret task", "b body", "must", "bug", [], {}, "owner", subtasks=["b step"])
    repo.comment_task(bt["id"], "b comment", "owner")
    bk = repo.create_knowledge(b["id"], "decision", "B decision", "b", [], "", False, "owner", summary="b s")
    at = repo.create_task(a["id"], "A task", "", "must", "bug", [], {}, "owner")
    b_row = repo.list_outbox("bugs", project_id=b["id"])[0]
    return {"a": a, "b": b, "bt": bt, "bsub": repo.list_subtasks(bt["id"])[0], "bk": bk, "at": at, "b_row": b_row}


def _b_snapshot(ab):
    t = repo.get_task(ab["bt"]["id"])
    k = repo.get_knowledge(ab["bk"]["id"])
    return (t["title"], t["body"], t["status"], t["priority"], t["assignee"], t["project_id"], t["triage_state"],
            json.dumps(t["links"], sort_keys=True), len(t["comments"]), [(s["text"], s["done"]) for s in t["subtasks"]],
            k["title"], k["summary"], k["archived_at"], len(repo.list_tasks(ab["b"]["id"])),
            len(repo.list_knowledge(ab["b"]["id"])), repo.get_project(ab["b"]["id"])["archived_at"],
            [(o["id"], o["delivered_at"]) for o in repo.list_outbox("bugs", project_id=ab["b"]["id"])])


# --- REST ---------------------------------------------------------------------------------------------------------

def test_every_rest_route_refuses_project_b_to_a_token_of_project_a(client, ab):
    tok = issue_token("bugs", "pa-bugs", project_id=ab["a"]["id"])
    h = {"Authorization": f"Bearer {tok}"}
    b, bt, bk, bsub, row = ab["b"], ab["bt"], ab["bk"], ab["bsub"], ab["b_row"]
    before = _b_snapshot(ab)
    cases = [
        ("get", f"/api/projects/{b['id']}", None),
        ("post", f"/api/projects/{b['id']}/archive", None),
        ("post", f"/api/projects/{b['id']}/unarchive", None),
        ("get", "/api/tasks?project=proj-b", None),
        ("get", "/api/my-tasks?project=proj-b", None),
        ("get", f"/api/tasks/{bt['id']}", None),
        ("post", "/api/tasks", {"project": "proj-b", "title": "x", "stream": "bug"}),
        ("patch", f"/api/tasks/{bt['id']}", {"title": "hijack"}),
        ("post", f"/api/tasks/{bt['id']}/assign", {"assignee": "owner"}),
        ("post", f"/api/tasks/{bt['id']}/subtasks", {"items": ["x"]}),
        ("patch", f"/api/subtasks/{bsub['id']}", {"done": True}),
        ("post", f"/api/tasks/{bt['id']}/subtask-proposals", {"changes": [{"op": "add", "text": "x"}], "rationale": "r"}),
        ("post", f"/api/tasks/{bt['id']}/comments", {"body": "x"}),
        ("post", f"/api/tasks/{bt['id']}/link-workflow", {"workflow_id": WF}),
        ("post", f"/api/tasks/{bt['id']}/suggest-triage", {"rationale": "r", "priority": "could"}),
        ("get", "/api/knowledge?project=proj-b", None),
        ("get", "/api/knowledge/graph?project=proj-b", None),
        ("get", f"/api/knowledge/{bk['id']}", None),
        ("post", "/api/knowledge", {"project": "proj-b", "title": "x", "summary": "s"}),
        ("patch", f"/api/knowledge/{bk['id']}", {"title": "hijack"}),
        ("post", f"/api/knowledge/{bk['id']}/archive", {"reason": "x"}),
        ("get", "/api/activity?project=proj-b", None),
        ("get", "/api/objectives?project=proj-b", None),
        ("get", "/api/workflows?project=proj-b", None),
        ("get", f"/api/workflows/{WF}?project=proj-b", None),
        ("post", f"/api/outbox/{row['id']}/delivered", None),
        ("post", f"/api/outbox/{row['id']}/undelivered", None),
        ("post", "/api/outbox", {"stream": "bugs", "message": "hi", "project": "proj-b"}),
        ("post", "/api/outbox", {"stream": "bugs", "message": "hi", "task_id": bt["id"]}),
        ("post", "/api/sessions/heartbeat", {"state": "online", "project": "proj-b"}),
        ("get", "/api/ground?project=proj-b", None),
        ("get", "/api/plan", None),  # the global plan spans every project: the overseer's (and the owner's) only
    ]
    for method, path, body in cases:
        kw = {"headers": h}
        if body is not None:
            kw["json"] = body
        r = getattr(client, method)(path, **kw)
        assert r.status_code == 404, (method, path, r.status_code, r.text)
        assert "proj-b" not in r.text.replace("'proj-b'", "") or "not found" in r.text
    assert _b_snapshot(ab) == before


def test_a_missing_id_and_another_projects_id_look_the_same(client, ab):
    h = {"Authorization": f"Bearer {issue_token('bugs', 'x', project_id=ab['a']['id'])}"}
    theirs = client.get(f"/api/tasks/{ab['bt']['id']}", headers=h).json()["detail"]
    missing = client.get("/api/tasks/999999", headers=h).json()["detail"]
    assert theirs.replace(str(ab["bt"]["id"]), "N") == missing.replace("999999", "N")
    theirs = client.get("/api/tasks?project=proj-b", headers=h).json()["detail"]
    missing = client.get("/api/tasks?project=nope", headers=h).json()["detail"]
    assert theirs.replace("proj-b", "X") == missing.replace("nope", "X")


def test_rest_lists_with_a_token_are_its_own_project_only(client, ab):
    h = {"Authorization": f"Bearer {issue_token('bugs', 'x', project_id=ab['a']['id'])}"}
    assert [p["slug"] for p in client.get("/api/projects", headers=h).json()] == ["proj-a"]
    assert [t["title"] for t in client.get("/api/tasks", headers=h).json()] == ["A task"]
    assert client.get("/api/knowledge", headers=h).json() == []
    assert all(o["project_id"] == ab["a"]["id"] for o in client.get("/api/outbox", headers=h).json())
    assert {s["project"] for s in client.get("/api/sessions", headers=h).json()} == {"proj-a"}
    text = client.get("/api/ground", headers=h).text
    assert "A task" in text and "B secret" not in text


def test_catalog_import_features_token_is_own_project_only_scanner_unchanged(client, ab):
    feat = {"Authorization": f"Bearer {issue_token('features', 'f', project_id=ab['a']['id'])}"}
    r = client.post("/api/catalog/import?project=proj-b", json=FIXTURE, headers=feat)
    assert r.status_code == 404
    assert client.post("/api/catalog/import", json=FIXTURE, headers=feat).status_code == 201  # → proj-a
    assert repo.latest_scan(ab["a"]["id"]) is not None
    scanner = {"Authorization": f"Bearer {issue_token('scanner', 'rescan')}"}
    assert client.post("/api/catalog/import?project=proj-b", json=FIXTURE, headers=scanner).status_code == 201
    assert client.get("/api/tasks", headers=scanner).status_code == 403  # still catalog-only


def test_moving_a_task_out_is_never_a_session(client, ab):
    h = {"Authorization": f"Bearer {issue_token('bugs', 'x', project_id=ab['a']['id'])}"}
    r = client.patch(f"/api/tasks/{ab['at']['id']}", json={"project": "proj-b"}, headers=h)
    assert r.status_code == 403
    assert repo.get_task(ab["at"]["id"])["project_id"] == ab["a"]["id"]


# --- MCP ----------------------------------------------------------------------------------------------------------

def _mcp_cases(ab):
    b, bt, bk, bsub = ab["b"], ab["bt"], ab["bk"], ab["bsub"]
    return {
        "ground": dict(project="proj-b"),
        "list_tasks": dict(project="proj-b"),
        "get_task": dict(task_id=bt["id"]),
        "my_tasks": dict(project="proj-b"),
        "add_task": dict(title="x", stream="bug", project="proj-b"),
        "assign_task": dict(task_id=bt["id"], assignee="owner"),
        "add_subtasks": dict(task_id=bt["id"], items=["x"]),
        "update_subtask": dict(subtask_id=bsub["id"], done=True),
        "propose_subtask_changes": dict(task_id=bt["id"], changes=[{"op": "add", "text": "x"}], rationale="r"),
        "update_task": dict(task_id=bt["id"], title="hijack"),
        "comment_task": dict(task_id=bt["id"], body="x"),
        "add_knowledge": dict(title="x", summary="s", project="proj-b"),
        "update_knowledge": dict(knowledge_id=bk["id"], title="hijack"),
        "archive_knowledge": dict(knowledge_id=bk["id"], reason="x"),
        "search_knowledge": dict(query="B", project="proj-b"),
        "get_knowledge": dict(knowledge_id=bk["id"]),
        "recent_activity": dict(project="proj-b"),
        "list_workflows": dict(project="proj-b"),
        "get_workflow": dict(workflow_id=WF, project="proj-b"),
        "link_task_workflow": dict(task_id=bt["id"], workflow_id=WF),
        "suggest_triage": dict(task_id=bt["id"], rationale="r", priority="could"),
        "list_objectives": dict(project="proj-b"),
        "list_milestones": dict(project="proj-b"),
        "add_milestone": dict(project="proj-b", name="x", due_on="2026-12-11"),
        "update_milestone": dict(milestone_id=_b_milestone(b)["id"], name="hijack"),
        "ask_owner": dict(task_id=bt["id"], question="q?", context="enough context to answer it cold"),
        "record_answer": dict(question_id=repo.ask_owner(bt["id"], "B's question?", "context for project B only",
                                                         None, "claude:bugs")["id"], answer="hijack"),
        "recommend_change": dict(title="x", recommendation="y", project="proj-b"),
        "mark_change_done": dict(change_id=_b_change(b)["id"]),
        "record_decision": dict(change_id=_b_change(b)["id"], decision="approved", owner_words="hijack"),
        "list_changes": dict(project="proj-b"),
        "list_materials": dict(project="proj-b"),
        "get_material": dict(material_id=_b_material(b)),
        "add_material": dict(slot="x", kind="other", title="t", purpose="p", source_path="_inbox/x.txt",
                             project="proj-b"),
        # Handoffs and shared docs (#908): another partition's project, handoff and doc are NotFound. Real cross-account rows are
        # covered in tests/test_handoffs.py; the two list tools return only the caller's own partition's rows (never an error).
        "send_handoff": dict(to_project="proj-b", subject="x", body="y"),
        "get_handoff": dict(id=987654),
        "reply_handoff": dict(id=987654, body="x"),
        "close_handoff": dict(id=987654),
        "put_doc": dict(slug="doc-b", title="t", body="b", base_version=1),
        "get_doc": dict(slug="doc-b"),
        "doc_diff": dict(slug="doc-b", a=1, b=2),
    }


def _b_material(b):
    """A material row on project B (task #295). No file is needed: get_material never reads it."""
    from app.db import get_cursor
    with get_cursor() as cur:
        cur.execute("INSERT INTO materials (project_id, slot, kind, title, purpose, filename, rel_path, content_type, "
                    "bytes, sha256, created_by) VALUES (%s, 'logo.primary', 'logo', 'B logo', 'b', 'b.svg', "
                    "'proj-b/logo.primary/v1-b.svg', 'image/svg+xml', 1, 'x', 'owner') "
                    "ON CONFLICT (rel_path) DO UPDATE SET title = EXCLUDED.title RETURNING id", (b["id"],))
        return cur.fetchone()["id"]


def _b_milestone(b):
    from app import planning
    return planning.create_milestone(b["id"], "B launch", "2026-12-11", "", "owner")


def _b_change(b):
    from app import changes
    c = changes.recommend(b["id"], "B change", "b", "claude:all")
    return changes.decide(c["id"], "approved", "owner")


GLOBAL_TOOLS = {"get_pulse", "list_handoffs", "list_docs"}  # not project data / lists of the caller's own partition only (#908)
# The global plan (task #129) is the overseer's alone: any other token gets a PermissionError, not a 404
# (see tests/test_global_plan.py for the overseer's cross-project reads and refused writes).
OVERSEER_TOOLS = {"get_global_view", "set_global_plan", "patch_global_plan"}


def test_every_mcp_tool_refuses_project_b_to_a_token_of_project_a(ab, mcp_caller):
    import asyncio

    tools = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    cases = _mcp_cases(ab)
    assert tools - GLOBAL_TOOLS - OVERSEER_TOOLS == set(cases), \
        f"a new MCP tool needs an isolation case: {tools - GLOBAL_TOOLS - OVERSEER_TOOLS - set(cases)}"
    before = _b_snapshot(ab)
    for role in ("bugs", "all"):
        mcp_caller(issue_token(role, f"pa-{role}", project_id=ab["a"]["id"]))
        for name, kwargs in cases.items():
            with pytest.raises(repo.NotFound):
                getattr(mcp_server, name)(**kwargs)
    assert _b_snapshot(ab) == before


def test_mcp_reads_with_a_token_default_to_its_own_project(ab, mcp_caller):
    mcp_caller(issue_token("bugs", "x", project_id=ab["a"]["id"]))
    assert [t["title"] for t in mcp_server.list_tasks()] == ["A task"]
    assert mcp_server.search_knowledge("B") == []
    assert mcp_server.list_workflows() == []
    t = mcp_server.add_task(title="Mine", stream="bug")
    assert t["project_id"] == ab["a"]["id"]


# --- the 'all' session and outbox routing ---------------------------------------------------------------------------

def test_all_token_sees_every_stream_of_its_project(ab, mcp_caller):
    b = ab["b"]
    repo.create_task(b["id"], "B feature", "", "should", "feature", [], {}, "owner")
    repo.create_task(b["id"], "B tweak", "", "could", "tweak", [], {}, "owner", assignee="owner")
    mcp_caller(issue_token("all", "pb", project_id=b["id"]))
    mine = {t["title"] for t in mcp_server.my_tasks()}
    assert mine == {"B secret task", "B feature"}
    g = mcp_server.ground()
    assert {t["title"] for t in g["assigned_to_ai"]} == {"B secret task", "B feature"}
    assert {t["title"] for t in g["awaiting_owner"]} == {"B tweak"}
    assert repo.render_ground_text(g).startswith("GROUND — all on proj-b")
    # an 'all' session may hand any stream's task of its project to the AI
    tw = [t for t in repo.list_tasks(b["id"]) if t["title"] == "B tweak"][0]
    assert mcp_server.assign_task(tw["id"], "ai")["assignee"] == "ai"


def test_outbox_routes_to_the_streams_session_else_the_all_session(client, ab):
    a, b = ab["a"], ab["b"]
    # project A has one session per stream; project B has one 'all' session
    a_bugs = {"Authorization": f"Bearer {issue_token('bugs', 'pa-bugs', project_id=a['id'])}"}
    a_all = {"Authorization": f"Bearer {issue_token('all', 'pa', project_id=a['id'])}"}
    b_all = {"Authorization": f"Bearer {issue_token('all', 'pb', project_id=b['id'])}"}
    b_bugs = {"Authorization": f"Bearer {issue_token('bugs', 'pb-bugs', project_id=b['id'])}"}
    repo.create_task(b["id"], "B tweak", "", "should", "tweak", [], {}, "owner")
    ids = lambda h: {o["id"] for o in client.get("/api/outbox?undelivered=1", headers=h).json()}  # noqa: E731
    b_rows = {o["id"] for o in repo.list_outbox("bugs", project_id=b["id"]) + repo.list_outbox("tweaks", project_id=b["id"])}
    a_rows = {o["id"] for o in repo.list_outbox("bugs", project_id=a["id"])}
    assert ids(b_all) == b_rows                 # B has no dedicated sessions: 'all' gets every stream
    assert ids(a_bugs) == a_rows                # A's bugs session gets A's bug events
    assert ids(a_all) == set()                  # A has dedicated sessions for every stream
    assert not (ids(a_bugs) & b_rows) and not (ids(b_all) & a_rows)
    # claiming: only the routed session can
    rid = next(iter(a_rows))
    assert client.post(f"/api/outbox/{rid}/delivered?via=channel", headers=b_all).status_code == 404
    assert client.post(f"/api/outbox/{rid}/delivered?via=channel", headers=a_all).status_code == 404
    assert client.post(f"/api/outbox/{rid}/delivered?via=channel", headers=a_bugs).status_code == 200
    # a B bug token (not in B's plan, but valid) still only ever sees B
    assert ids(b_bugs) <= b_rows


def test_owner_ui_still_sees_every_project(client, ab):
    assert client.get("/proj-b/board").status_code == 200 and client.get("/proj-a/board").status_code == 200
    html = client.get("/").text
    assert "Project A" in html and "Project B" in html



def test_overseer_tools_refuse_every_ordinary_session(ab, mcp_caller):
    for role in ("bugs", "all"):
        mcp_caller(issue_token(role, f"pa-{role}", project_id=ab["a"]["id"]))
        with pytest.raises(PermissionError):
            mcp_server.get_global_view()
        with pytest.raises(PermissionError):
            mcp_server.set_global_plan(items=[])
        with pytest.raises(PermissionError):
            mcp_server.patch_global_plan(ops=[])


# --- the one new read path of task #148: an account's SHARED items, inherited ----------------------------------------

def test_inherited_account_items_never_cross_accounts(client, mcp_caller):
    """A session READS (never writes) the shared rules / AI skills / knowledge of the accounts above the projects it
    works; another account's are the same 404 a missing id gets, on MCP get_knowledge and REST GET /api/knowledge/<id>.
    (tests/test_levels_and_accounts.py covers the grandchild, dash's reach and an unrelated top-level project.)"""
    from app import tree
    acct_a = repo.create_project("acct-a", "Account A", "client", "owner", short_name="aa", session_roles=[])
    acct_b = repo.create_project("acct-b", "Account B", "client", "owner", short_name="ab", session_roles=[])
    for acct in (acct_a, acct_b):
        tree.set_place(acct["id"], "owner", None, kind="account")
    pa = repo.create_project("proj-a", "Project A", "client", "owner", short_name="pa", session_roles=["all"])
    pb = repo.create_project("proj-b", "Project B", "client", "owner", short_name="pb", session_roles=["all"])
    tree.set_place(pa["id"], "owner", acct_a["id"])
    tree.set_place(pb["id"], "owner", acct_b["id"])
    b_skill = repo.create_knowledge(acct_b["id"], "skill", "B skill", "b instructions", [], "", False, "owner",
                                    summary="B's shared skill")
    a_tok, b_tok = (issue_token("all", f"{p['slug']}-t", project_id=p["id"]) for p in (pa, pb))
    mcp_caller(a_tok)
    with pytest.raises(repo.NotFound):
        mcp_server.get_knowledge(b_skill["id"])
    assert "B skill" not in repo.render_ground_text(mcp_server.ground())
    theirs = client.get(f"/api/knowledge/{b_skill['id']}", headers={"Authorization": f"Bearer {a_tok}"})
    missing = client.get("/api/knowledge/999999", headers={"Authorization": f"Bearer {a_tok}"})
    assert theirs.status_code == missing.status_code == 404
    assert theirs.json()["detail"].replace(str(b_skill["id"]), "N") == missing.json()["detail"].replace("999999", "N")
    # B's own project inherits it: readable, in its briefing, never writable
    mcp_caller(b_tok)
    assert mcp_server.get_knowledge(b_skill["id"])["title"] == "B skill"
    assert "B skill" in repo.render_ground_text(mcp_server.ground())
    with pytest.raises(repo.NotFound):
        mcp_server.update_knowledge(b_skill["id"], title="hijack")
    assert repo.get_knowledge(b_skill["id"])["title"] == "B skill"


# --- ROUTE COVERAGE (task #713): a new route that nothing scopes fails this test ------------------------------------------
# Every route of the app must be, by its declared dependencies, one of:
#   slug   behind pages.project_from_path (a /{slug}/... page route: 404 for a project you can't see, no write without the
#          right to change it, owner-only / creator-only sections by access.slug_policy)
#   owner  behind access.owner_only (a 404 to anyone but the owner)
#   token  behind a Bearer-token dependency (REST: reach of the token's project, filtered to its person; runner device token)
# ...or be listed here, with the reason it is safe. Adding a route means picking one; forgetting fails the suite.

def _route_table():
    """{(METHOD, path): {scoping kinds}} for every HTTP route, WebSocket route ('WS') and mounted app ('MOUNT') of the app."""
    import importlib
    import pkgutil

    from fastapi import APIRouter
    from fastapi.routing import APIRoute

    import app.routers as pkg
    from app import access, auth
    from app.main import app
    from app.routers import pages

    marks = {pages.project_from_path: "slug", access.owner_only: "owner", auth.require_session: "token",
             auth.require_satellite: "token", auth.require_reporter: "token", auth.require_catalog_importer: "token",
             auth.require_zabbix_poke: "token", auth.require_digest: "token", auth.require_device: "token"}

    def calls(dep):
        out = set()
        for sub in dep.dependencies:
            out.add(sub.call)
            out |= calls(sub)
        return out

    routes = list(app.router.routes)
    for m in pkgutil.iter_modules(pkg.__path__):
        mod = importlib.import_module("app.routers." + m.name)
        routes += [r for v in vars(mod).values() if isinstance(v, APIRouter) for r in v.routes]
    table = {}
    for r in routes:
        kind = type(r).__name__
        if isinstance(r, APIRoute):
            kinds = {marks[c] for c in calls(r.dependant) if c in marks}
            for method in r.methods - ({"HEAD"} if "GET" in r.methods else set()):
                table.setdefault((method, r.path), set()).update(kinds)
        elif kind in ("APIWebSocketRoute", "WebSocketRoute"):
            table.setdefault(("WS", r.path), set())
        elif kind == "Mount":
            table.setdefault(("MOUNT", r.path), set())
    return table


def A(methods, path, reason):
    return [((m, path), reason) for m in methods.split()]


# (method, path) -> why it needs no project/owner/token dependency. METHOD-SENSITIVE: a new POST on an allowlisted GET path fails.
_ALLOW = [
    # public by design: what a browser or monitor fetches before anyone is signed in
    *A("GET", "/trust", "open page: the LAN fingerprint and click-through help (#976); reveals only what a browser certificate viewer shows"),
    *A("GET", "/healthz", "liveness"), *A("GET", "/favicon.ico", "icon"), *A("GET", "/manifest.webmanifest", "icon set"),
    *A("GET", "/manifest.json", "icon set (root convention, #750)"), *A("GET", "/apple-touch-icon.png", "icon (root convention)"),
    *A("GET", "/apple-touch-icon-precomposed.png", "icon (root convention)"),
    *A("GET", "/sw.js", "service worker file"), *A("MOUNT", "/static", "static files"),
    *A("GET", "/auth/login", "sign-in"), *A("GET", "/auth/callback", "sign-in (OIDC)"), *A("POST", "/auth/logout", "sign-out"),
    *A("GET", "/auth/signed-out", "sign-in"), 
    *A("POST", "/auth/login/identify", "sign-in (identifier step, #809)"), *A("GET", "/auth/login/id", "sign-in (identifier step)"),
    *A("POST", "/auth/login/id/password", "sign-in (password + authenticator code; a temporary password opens the restricted session)"),
    *A("POST", "/auth/login/id/recovery", "sign-in (recovery code: restricted session)"),
    *A("POST", "/auth/login/id/passkey/options", "sign-in"), *A("POST", "/auth/login/id/passkey/verify", "sign-in"),
    # a signed-in person acting on their OWN sign-in factors, keyed by their session's user id
    *A("GET", "/auth/enrol", "own enrolment"), *A("POST", "/auth/enrol/passkey/options", "own enrolment"),
    *A("POST", "/auth/enrol/passkey/verify", "own enrolment"), *A("POST", "/auth/enrol/codes", "own enrolment (restricted session)"),
    *A("POST", "/auth/enrol/complete", "own enrolment (restricted session)"),
    *A("POST", "/auth/enrol/password", "own enrolment (restricted session, stage 1)"), *A("POST", "/auth/enrol/totp", "own enrolment (restricted session, stage 2)"),
    *A("POST", "/account/security/reauth", "own account (a fresh authenticator code)"), *A("GET", "/account/security/stepup-info", "own account"),
    *A("POST", "/account/security/password", "own account (after a fresh factor)"),
    *A("POST", "/account/security/authenticator/start", "own account (after a fresh factor)"),
    *A("POST", "/account/security/authenticator/confirm", "own account (after a fresh factor)"), *A("GET", "/account/security", "own account"),
    *A("POST", "/account/security/passkey/options", "own account"), *A("POST", "/account/security/passkey/verify", "own account"),
    *A("POST", "/account/security/passkeys/{cred_id}/remove", "own account (matched to the session's user)"),
    *A("POST", "/account/security/recovery/regenerate", "own account"),
    *A("POST", "/account/security/signout-everywhere", "own account"),
    *A("POST", "/account/security/email", "own account (sign-in email, after a fresh factor)"),
    *A("POST", "/account/security/sessions/{session_id}/end", "own account (matched to the session's user)"),
    # runner program: its own enrolment-code / device-signature auth (app/devices.py), never a browser session
    *A("GET", "/runner/identity", "public key"), *A("POST", "/runner/enrol", "one-time code"),
    *A("POST", "/runner/jobs/claim", "device token checked in the handler"),
    *A("POST", "/runner/jobs/{job_id:int}/heartbeat", "device token"), *A("WS", "/runner/ws", "device token over the socket"),
    # token-authenticated mounts: checked per call in the tool (session token + scope.reach / job token)
    *A("MOUNT GET POST DELETE", "/mcp", "mounted MCP: per-call session token"),
    *A("MOUNT GET POST DELETE", "/runner/mcp", "mounted job MCP: per-call job token"),
    *A("MOUNT GET POST DELETE", "/runner/ca-mcp", "mounted CA MCP (#702): per-call CA turn token (cbt_), one turn of one user; tools reach = ca.visible_project_ids -> access.visible_ids"),
    # the CA drawer (#702): a signed-in person's OWN conversations, found by (id, user_id) so another user's id is a 404 (tests/test_ca.py);
    # project reach of every CA read/write = ca.visible_project_ids -> access.visible_ids
    *A("GET", "/ca/state", "CA: signed_in_user_row; own rows"), *A("POST", "/ca/conversations", "CA: signed_in_user_row; own rows"),
    *A("GET", "/ca/conversations/{conversation_id}", "CA: own conversation by (id, user_id), 404 for another user; a conversation linked to a TASK is readable iff access.can_view on the task's project (404 else)"),
    *A("POST", "/ca/conversations/{conversation_id}/messages", "CA: own conversation; a task's conversation is postable iff access.can_edit on the task's project (403 view-only, 404 none)"),
    *A("POST", "/ca/conversations/{conversation_id}/share", "CA #830: own conversation (id, user_id) linked to a task; needs access.can_edit on the task's project"),
    *A("POST", "/ca/conversations/{conversation_id}/stop", "CA: own conversation; a task's: access.can_edit"),
    *A("GET", "/ca/tasks/{task_id}/discussion", "CA #830: access.can_view on the task's project, else 404; can_post = access.can_edit"),
    *A("POST", "/ca/conversations/{conversation_id}/archive", "CA: (id, user_id)"),
    *A("POST", "/ca/cards/{card_id}", "CA: card of the user's own conversation"),
    *A("PATCH", "/ca/conversations/{conversation_id}", "CA #727: (id, user_id); the linked item is checked against access.visible_ids, 404 if unseen"),
    *A("POST", "/ca/conversations/{conversation_id}/uploads", "CA #727: (id, user_id); type sniffed, size capped"),
    *A("GET", "/ca/uploads/{attachment_id}", "CA #727: (id, user_id), 404 for another user"),
    *A("DELETE", "/ca/uploads/{attachment_id}", "CA #727: (id, user_id), unsent only"),
    *A("GET", "/ca/items/{kind}/{ident:path}/discussions", "CA #727: the item must be visible to the person (ca.resolve_entity -> access.visible_ids), else 404; others' chats are soft links only"),
    *A("GET", "/ca/speech/url", "CA #727: signed-in person; 404 unless a speech provider is configured; rate-limited"),
    *A("GET", "/ca/history", "CA #727: own conversations only"), *A("GET", "/ca/history.json", "CA #727: own conversations only"),
    *A("GET", "/ca/notices", "CA: own notices"), *A("POST", "/ca/notices/read", "CA: own notices"), *A("GET", "/ca/stream", "CA: own SSE stream"),
    # scoped INSIDE the handler by access.* (each has a behavioural test in tests/test_multi_user.py)
    *A("GET", "/", "home: repo.global_overview is the person's reach"), *A("GET", "/all/plan", "plan: empty for a non-owner"),
    *A("POST", "/all/plan/{task_id}/pin", "handler: access.require_owner"),
    *A("POST", "/all/plan/{task_id}/today", "handler: access.require_owner"),
    *A("POST", "/all/plan/move", "handler: access.require_owner"), *A("GET", "/all/focus", "repo.focus: ambient reach"),
    *A("GET", "/all/tasks/{ref}", "repo.locate_task: ambient reach"), *A("GET", "/all/tasks/{ref}/peek", "repo.locate_task: ambient reach"), *A("GET", "/all/tasks/{ref}/page", "repo.locate_task: ambient reach (#816)"),
    *A("GET", "/all/events", "SSE: cursor over visible_list(person)"),
    # the fixed menu's pages (#733)
    *A("GET", "/dashboard", "app/snapshot.py: every figure through access.visible_ids; the production-problem tile is the owner's alone (None for anyone else)"),
    *A("GET", "/projects", "redirect; ?archived=1 lists tree.project_tree over access.filtered_index(person); ?open= needs access.can_view"),
    *A("GET", "/accounts", "redirect; ?archived=1 lists tree.project_tree over access.filtered_index(person); ?open= needs access.can_view"),
    *A("GET", "/recent", "app/recent.py: activity joined to the person's visible_list; a project filter they can't see is ignored"),
    *A("GET", "/all/filters", "app/saved_filters.py: every row by the principal's user_id; a project filter needs access.require_view"),
    *A("POST", "/all/filters", "app/saved_filters.py: by user_id; project via access.require_view"),
    *A("POST", "/all/filters/{filter_id}/rename", "app/saved_filters.py: _mine() = the principal's own row, else 404"),
    *A("POST", "/all/filters/{filter_id}/default", "app/saved_filters.py: _mine() = the principal's own row, else 404"),
    *A("POST", "/all/filters/{filter_id}/delete", "app/saved_filters.py: _mine() = the principal's own row, else 404"),
    *A("GET", "/all/jump", "app/search.py: access.filtered_index for records, tasks limited to visible_list(person)"),
    *A("POST", "/all/favourites", "app/favourites.py: by the principal's user_id; access.require_view(project), 404 if unseen"),
    *A("GET", "/all/tour", "app/tour.py: the principal's own tour_progress row by user_id; steps only name projects in access.filtered_index(person)"),
    *A("POST", "/all/tour", "app/tour.py: writes only the principal's own tour_progress row by user_id; action and step id whitelisted, signed_in_person"),
    *A("GET", "/assets", "Assets home (#778): app/asset_hub.py reads only projects in access.filtered_index(person); no project in the URL"),
    *A("GET", "/assets/recent", "Assets (#778): materials joined to access.visible_list(person)"),
    *A("GET", "/assets/{kind}", "Assets (#778): app/asset_hub.collect over access.filtered_index(person); unknown kind 404"),
    # Skills (#718): the GLOBAL layer. Every handler asks app/asset_skills.py, which asks app/asset_access.py: global skills are read by any signed-in
    # person and written only by the instance owner; a skill owned by a project or account is a 404 here (the project's own route serves it).
    *A("GET", "/assets/skills", "Skills (#718): global layer, any signed-in person reads"),
    *A("GET POST", "/assets/skills/new", "Skills (#718): installing globally = the owner (asset_access.can_install_global); a project target needs access.require_edit"),
    *A("GET POST", "/assets/skills/import", "Skills (#718): same install rights; step-up (access.require_fresh) on the POST; host allow-list + zip checks in app/skill_import.py"),
    *A("GET", "/assets/skills/{uid}", "Skills (#718): asset_skills.get = asset_access.require_view; a non-global skill is a 404 on this route"),
    *A("POST", "/assets/skills/{uid}/edit", "Skills (#718): asset_skills.edit = asset_access.require_edit (global: the owner)"),
    *A("POST", "/assets/skills/{uid}/licence", "Skills (#718): asset_access.can_confirm_licence (global: the owner); step-up on the POST"),
    *A("POST", "/assets/skills/{uid}/review", "Skills (#718): asset_access.can_confirm_licence (global: the owner); step-up on the POST"),
    *A("POST", "/assets/skills/{uid}/archive", "Skills (#718): asset_access.require_edit"),
    *A("POST", "/assets/skills/{uid}/copy", "Skills (#718): view the skill, then the install target's access.require_edit"),
    *A("GET", "/projects/{slug}/hub","handler: access.require_view(person, project), 404 if unseen"),
    *A("GET", "/accounts/{slug}/hub", "handler: access.require_view(person, project), 404 if unseen"),
    *A("GET", "/machines", "redirect into an account the person may see (access.filtered_index); the page it lands on is ProjectDep"),
    *A("GET", "/runners", "redirect into an account the person may see (access.filtered_index); the page it lands on is ProjectDep"),
    *A("GET", "/servers", "handler: access.require_owner, then a redirect into /<account>/machines?role=deploys (ProjectDep)"),
    *A("POST", "/projects", "handler: access.check_new_project"),
    *A("POST", "/projects/{project_id}/archive", "handler: require_view + creator"),
    *A("POST", "/projects/{project_id}/unarchive", "handler: require_view + creator"),
    *A("GET", "/api/servers/{server_id}/manifest", "require_manifest_server: its own per-server read token only (401 none, 404 another server); tests/test_where_it_lives.py"),
    *A("GET", "/media/knowledge/{image_id:int}.{ext}", "handler: can_view(image.project_id)"),
    *A("GET", "/media/materials/{material_id:int}/{filename}", "handler: can_view(material.project_id)"),
]
ROUTE_ALLOWLIST = dict(_ALLOW)
# legacy redirects: any method; they only 301/308 to /<current project>/<same path>, which is itself scoped (and the cookie's project
# is filtered to the person's), so they can show nothing.
LEGACY = ("/tasks", "/subtasks", "/workflows", "/skills", "/rules", "/knowledge", "/objectives", "/activity", "/health")


def test_every_route_is_scoped_owner_only_token_or_explicitly_allowlisted():
    table = _route_table()
    assert len(table) > 150, "route discovery found too little: the test would pass for the wrong reason"
    assert ("WS", "/runner/ws") in table and ("MOUNT", "/mcp") in table, "WebSocket and mounted apps are part of the table"
    unscoped = set()
    for (method, path), kinds in table.items():
        if kinds or (method, path) in ROUTE_ALLOWLIST or any(path == p or path.startswith(p + "/") for p in LEGACY):
            continue
        unscoped.add(f"{method} {path}")
    assert not unscoped, ("routes with no project/owner/token scoping and no allowlist entry: " + ", ".join(sorted(unscoped))
                          + ". Put them behind ProjectDep / access.owner_only / require_session, or add a reasoned (method, path) entry.")


def test_the_allowlist_is_method_sensitive_and_has_no_stale_entries():
    table = _route_table()
    stale = sorted(f"{m} {p}" for (m, p) in ROUTE_ALLOWLIST if (m, p) not in table)
    assert not stale, f"allowlisted (method, path) pairs that no longer exist: {stale}"
    # a method nobody listed on an allowlisted path would fail the test above: prove the check bites
    assert ("POST", "/healthz") not in ROUTE_ALLOWLIST and ("DELETE", "/auth/login/id") not in ROUTE_ALLOWLIST


def test_every_slug_route_has_a_policy_and_owner_sections_are_listed():
    from app.routers import pages
    slug_routes = [path for (_m, path), kinds in _route_table().items() if "slug" in kinds]
    assert len(slug_routes) > 60
    for path in slug_routes:
        assert pages.slug_policy(path) in ("owner", "manage", "project"), path
    owner_firsts = {p.split("/")[2] for p in slug_routes if pages.slug_policy(p) == "owner"}
    assert {"servers", "approvals", "health", "integrations", "move", "external-agent"} <= owner_firsts
    for must_be_owner in ("/{slug}/servers", "/{slug}/approvals/{request_id:int}/approve", "/{slug}/health",
                          "/{slug}/move", "/{slug}/external-agent/token"):
        assert pages.slug_policy(must_be_owner) == "owner", must_be_owner
    for creator in ("/{slug}/rename", "/{slug}/details", "/{slug}/environments/{env}", "/{slug}/connections/add",
                    "/{slug}/connections/note", "/{slug}/connections/remove"):
        assert pages.slug_policy(creator) == "manage", creator


def test_connection_routes_are_slug_scoped_manage_posts():
    """#784 (security rows 5, 7, 9): the connection writes sit behind ProjectDep with the manage policy, so a route can't be added unscoped."""
    table = _route_table()
    for action in ("add", "note", "remove"):
        assert "slug" in table[("POST", f"/{{slug}}/connections/{action}")]


def test_connections_never_cross_accounts_and_a_missing_id_looks_like_another_projects():
    from app import connections, repo, tree
    from app.db import get_cursor
    import psycopg
    import pytest
    a1, a2 = (repo.create_project(s, s, "internal", "owner", short_name=s, session_roles=[]) for s in ("iso-acct-1", "iso-acct-2"))
    for a in (a1, a2):
        tree.set_place(a["id"], "owner", None, kind="account")
    p1 = repo.create_project("iso-p1", "iso-p1", "internal", "owner", short_name="isop1", session_roles=[])
    p2 = repo.create_project("iso-p2", "iso-p2", "internal", "owner", short_name="isop2", session_roles=[])
    tree.set_place(p1["id"], "owner", a1["id"])
    tree.set_place(p2["id"], "owner", a2["id"])
    from app import access
    from app.repo import BadRequest, NotFound
    with pytest.raises(BadRequest, match="same account"):
        connections.add(access.OWNER_PRINCIPAL, p1, "depends_on", "iso-p2")
    with pytest.raises(psycopg.errors.CheckViolation):
        with get_cursor() as cur:
            cur.execute("INSERT INTO project_connections (project_id, other_id, kind) VALUES (%s, %s, 'depends_on')", (p1["id"], p2["id"]))
    with pytest.raises(NotFound) as missing:
        connections.set_note(access.OWNER_PRINCIPAL, p1, 123456, "x")
    with pytest.raises(NotFound) as bad:
        connections.set_note(access.OWNER_PRINCIPAL, p1, "abc", "x")
    assert str(missing.value) == str(bad.value)


def test_satellite_and_client_code_never_read_ca_conversations():
    """#830: a client's mini dashboard and the satellite routes never touch the assistant's conversations."""
    import inspect
    from app import repo
    from app.routers import api
    src = inspect.getsource(repo.satellite_snapshot) + inspect.getsource(repo.create_client_request) + inspect.getsource(api.api_satellite_snapshot) + inspect.getsource(api.api_satellite_request)
    assert "ca_conversations" not in src and "ca_messages" not in src and "app.ca" not in src
