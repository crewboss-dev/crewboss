"""Session presence per (project, role) and the session plan: <short>-<role> names, 'all' = one session."""
from datetime import datetime, timedelta, timezone

import pytest

from app import repo
from app.auth import issue_token


@pytest.fixture
def erp():
    """The seed may already have made example-erp (the app's lifespan runs it when the client starts)."""
    try:
        return repo.get_project_by_slug("example-erp")
    except repo.NotFound:
        return repo.create_project("example-erp", "Example ERP", "internal", "system", short_name="erp",
                                   session_roles=list(repo.SESSION_STREAMS))


@pytest.fixture
def dash():
    return repo.create_project("example-dashboard", "Example Dashboard", "internal", "system", short_name="dash",
                               session_roles=["all"])


def _by_name():
    return {s["session_name"]: s for s in repo.list_sessions()}


def test_plan_names_sessions_short_role(erp, dash):
    plan = repo.session_plan()
    assert {(p["project"], p["role"], p["session_name"]) for p in plan} == {
        ("example-erp", "features", "erp-features"), ("example-erp", "reviewer", "erp-review"),
        ("example-erp", "bugs", "erp-bugs"), ("example-erp", "tweaks", "erp-tweaks"),
        ("example-erp", "errors", "erp-errors"), ("example-dashboard", "all", "dash")}
    assert all(set(p) == {"project", "short_name", "role", "stream", "session_name", "kind"} for p in plan)
    assert {p["kind"] for p in plan} == {"project"}


def test_plan_skips_archived_and_sessionless_projects(erp):
    quiet = repo.create_project("quiet", "Quiet", "internal", "owner", short_name="qt", session_roles=[])
    gone = repo.create_project("gone", "Gone", "internal", "owner", short_name="gn")
    repo.set_project_archived(gone["id"], True, "owner")
    names = {p["session_name"] for p in repo.session_plan()}
    assert "qt" not in names and "gn" not in names and "erp-bugs" in names
    assert quiet["session_roles"] == []


def test_no_heartbeat_is_offline(erp):
    assert _by_name()["erp-errors"]["status"] == "offline"


def test_fresh_heartbeat_is_online_per_project(erp, dash):
    repo.heartbeat_session("bugs", "online", project_id=erp["id"])
    repo.heartbeat_session("all", "online", project_id=dash["id"])
    s = _by_name()
    assert s["erp-bugs"]["status"] == "online" and s["dash"]["status"] == "online"
    assert s["erp-features"]["status"] == "offline"
    assert s["dash"]["label"] == "dash (all streams)"


def test_starting_state_fresh_vs_waiting():
    fresh = {"state": "starting", "last_seen": datetime.now(timezone.utc)}
    assert repo.session_status(fresh) == "starting"
    stale = {"state": "starting", "last_seen": datetime.now(timezone.utc) - timedelta(minutes=2)}
    assert repo.session_status(stale) == "waiting"


def test_stale_online_heartbeat_is_offline():
    old = {"state": "online", "last_seen": datetime.now(timezone.utc) - timedelta(minutes=5)}
    assert repo.session_status(old) == "offline"


def test_heartbeat_via_api_is_the_tokens_project_and_stream(client, erp, dash):
    token = issue_token("reviewer", "erp-review", project_id=erp["id"])
    resp = client.post("/api/sessions/heartbeat", json={"state": "online", "project": "example-erp"},
                       headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200 and resp.json()["stream"] == "reviewer" and resp.json()["project_id"] == erp["id"]
    assert _by_name()["erp-review"]["status"] == "online"
    # the channel names its project; naming another is a 404 and nothing is recorded
    resp = client.post("/api/sessions/heartbeat", json={"state": "online", "project": "example-dashboard"},
                       headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 404 and _by_name()["dash"]["status"] == "offline"


def test_api_plan_has_no_secrets(client, erp, dash, monkeypatch, capsys):
    from app import config, sessions_plan
    monkeypatch.setattr(config, "OVERSEER_PROJECT", "example-dashboard")  # the plan is the overseer's (task #368)
    issue_token("bugs", "erp-bugs", project_id=erp["id"])
    h = {"Authorization": f"Bearer {issue_token('all', 'dash', project_id=dash['id'])}"}
    body = client.get("/api/sessions/plan", headers=h).text
    assert "mdd_" not in body and "cb_" not in body and "token" not in body.lower()
    assert {p["session_name"] for p in client.get("/api/sessions/plan", headers=h).json()} >= {"erp-bugs", "dash"}
    # the launcher's in-container read prints the same JSON, no secrets either
    assert sessions_plan.main([]) == 0
    out = capsys.readouterr().out
    import json
    assert json.loads(out) == json.loads(body) and "mdd_" not in out and "cb_" not in out


def test_sessions_list_with_a_token_is_its_own_project(client, erp, dash):
    tok = issue_token("all", "dash", project_id=dash["id"])
    listed = client.get("/api/sessions", headers={"Authorization": f"Bearer {tok}"}).json()
    assert [s["session_name"] for s in listed] == ["dash"]


def test_migration_derives_unique_valid_short_names():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "m0011", Path(__file__).resolve().parent.parent / "db/migrations/0011_project_sessions.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    taken: set = set()
    out = []
    for slug in ("mallen-portal", "acme-retail-sydney-west", "a", "tasks", "portal"):
        short = m._derive(slug, taken, {"tasks", "api"})
        taken.add(short)
        out.append(short)
    assert out == ["portal", "acme-retail", "a0", "tasks2", "portal2"]
    assert all(m.SHORT_RE.match(s) for s in out)


def test_short_name_validation(project):
    for bad in ("a", "Acme", "acme_", "-ac", "ac-", "a" * 13, "tasks", "all"):
        with pytest.raises(repo.BadRequest):
            repo.create_project(f"p-{len(bad)}-x", "X", "internal", "owner", short_name=bad)
    repo.create_project("ok-one", "Ok", "internal", "owner", short_name="ok")
    with pytest.raises(repo.BadRequest, match="already exists"):
        repo.create_project("ok-two", "Ok 2", "internal", "owner", short_name="ok")
    with pytest.raises(repo.BadRequest):
        repo.create_project("ok-three", "Ok 3", "internal", "owner", short_name="ok3", session_roles=["all", "bugs"])


def test_new_project_form_asks_short_name_and_sessions_and_shows_the_command(client, project):
    html = client.get("/test-proj/board").text
    # sessions only for parents (task #132): a new project defaults to none (its parent's session works it)
    assert 'name="short_name"' in html and 'value="none" checked' in html and 'name="sessions" value="all"' in html
    r = client.post("/projects", data={"slug": "acme-retail", "name": "Acme Retail", "short_name": "acme",
                                       "sessions": "per-stream"},
                    headers={"Origin": "https://crewboss.example.com", "Sec-Fetch-Site": "same-origin"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "automatically within a minute" in r.headers["set-cookie"] and "example-sessions.sh" not in r.headers["set-cookie"]
    p = repo.get_project_by_slug("acme-retail")
    assert p["short_name"] == "acme" and p["session_roles"] == list(repo.SESSION_STREAMS)
    r = client.post("/projects", data={"slug": "no-short", "name": "No"}, follow_redirects=False,
                    headers={"Origin": "https://crewboss.example.com", "Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 422
