"""P1-5 (delivery + heartbeat actually land; one deliverer per row) and P2-7 (a token is bound to its
own stream for ground, outbox reads and claims)."""
import pytest

from app import mcp_server, repo
from app.auth import issue_token


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def bugs_row(project):
    repo.create_task(project["id"], "Deliver me", "", "should", "bug", [], {}, "owner")
    return repo.list_outbox("bugs", undelivered_only=True)[0]


def test_delivered_with_no_body_and_via_query(client, bugs_row):
    tok = issue_token("bugs", "channel")
    resp = client.post(f"/api/outbox/{bugs_row['id']}/delivered?via=channel", headers=_auth(tok))
    assert resp.status_code == 200, resp.text
    assert resp.json()["delivered_via"] == "channel"
    assert repo.list_outbox("bugs", undelivered_only=True) == []


def test_delivered_no_body_no_query_defaults_to_hook(client, bugs_row):
    tok = issue_token("bugs", "hook")
    resp = client.post(f"/api/outbox/{bugs_row['id']}/delivered", headers=_auth(tok))
    assert resp.status_code == 200
    assert resp.json()["delivered_via"] == "hook"


def test_delivered_body_still_accepted_and_query_wins(client, bugs_row):
    tok = issue_token("bugs", "x")
    resp = client.post(f"/api/outbox/{bugs_row['id']}/delivered?via=mcp", json={"delivered_via": "hook"},
                       headers=_auth(tok))
    assert resp.json()["delivered_via"] == "mcp"


def test_bad_via_is_400(client, bugs_row):
    tok = issue_token("bugs", "x")
    assert client.post(f"/api/outbox/{bugs_row['id']}/delivered?via=carrier-pigeon",
                       headers=_auth(tok)).status_code == 400


def test_second_claim_loses_with_409(client, bugs_row):
    tok = issue_token("bugs", "x")
    first = client.post(f"/api/outbox/{bugs_row['id']}/delivered?via=channel", headers=_auth(tok))
    second = client.post(f"/api/outbox/{bugs_row['id']}/delivered?via=hook", headers=_auth(tok))
    assert (first.status_code, second.status_code) == (200, 409)
    assert repo.list_outbox("bugs")[0]["delivered_via"] == "channel"
    assert repo.list_outbox("bugs")[0]["attempts"] == 1


def test_unclaim_after_failed_relay_reoffers_the_row(client, bugs_row):
    tok = issue_token("bugs", "x")
    client.post(f"/api/outbox/{bugs_row['id']}/delivered?via=channel", headers=_auth(tok))
    resp = client.post(f"/api/outbox/{bugs_row['id']}/undelivered?via=channel", headers=_auth(tok))
    assert resp.status_code == 200
    assert [r["id"] for r in repo.list_outbox("bugs", undelivered_only=True)] == [bugs_row["id"]]
    # only the claimant's own via can undo a claim
    client.post(f"/api/outbox/{bugs_row['id']}/delivered?via=hook", headers=_auth(tok))
    assert client.post(f"/api/outbox/{bugs_row['id']}/undelivered?via=channel",
                       headers=_auth(tok)).status_code == 404


def test_unclaim_superseded_by_newer_dedupe_copy_is_409(project):
    repo.push_outbox("errors", "health_error", "first", project_id=project["id"], dedupe_key="k")
    first = repo.list_outbox("errors", undelivered_only=True)[0]
    repo.mark_outbox_delivered(first["id"], "channel", stream="errors")
    repo.push_outbox("errors", "health_error", "second", project_id=project["id"], dedupe_key="k")
    with pytest.raises(repo.Conflict):
        repo.unclaim_outbox(first["id"], "channel", "errors")


def test_outbox_read_is_bound_to_the_tokens_stream(client, bugs_row):
    tweaks = issue_token("tweaks", "t")
    assert client.get("/api/outbox?stream=bugs&undelivered=1", headers=_auth(tweaks)).status_code == 403
    assert client.get("/api/outbox?undelivered=1", headers=_auth(tweaks)).json() == []
    bugs = issue_token("bugs", "b")
    assert [r["id"] for r in client.get("/api/outbox?undelivered=1", headers=_auth(bugs)).json()] == [bugs_row["id"]]


def test_claim_is_bound_to_the_tokens_stream(client, bugs_row):
    tweaks = issue_token("tweaks", "t")
    resp = client.post(f"/api/outbox/{bugs_row['id']}/delivered?via=hook", headers=_auth(tweaks))
    assert resp.status_code == 404
    assert len(repo.list_outbox("bugs", undelivered_only=True)) == 1


def test_heartbeat_with_no_body_lands_online(client, project):
    tok = issue_token("features", "channel")
    resp = client.post("/api/sessions/heartbeat", headers=_auth(tok))
    assert resp.status_code == 200, resp.text
    row = resp.json()
    assert row["state"] == "online" and row["stream"] == "features" and row["project_id"] == project["id"]


def test_heartbeat_optional_state(client, project):
    tok = issue_token("features", "launcher")
    assert client.post("/api/sessions/heartbeat", json={"state": "starting"},
                       headers=_auth(tok)).json()["state"] == "starting"
    assert client.post("/api/sessions/heartbeat?state=online", headers=_auth(tok)).json()["state"] == "online"
    assert client.post("/api/sessions/heartbeat", json={"state": "<script>"},
                       headers=_auth(tok)).status_code == 400


# --- ground ------------------------------------------------------------------------------------

def test_rest_ground_requires_a_token(client, project):
    assert client.get(f"/api/ground?project={project['slug']}").status_code == 401


def test_rest_ground_derives_stream_from_token(client, project):
    repo.create_task(project["id"], "Bugs work", "", "should", "bug", [], {}, "system")
    repo.create_task(project["id"], "Tweaks work", "", "should", "tweak", [], {}, "system")
    tok = issue_token("bugs", "hook")
    resp = client.get(f"/api/ground?project={project['slug']}", headers=_auth(tok))
    assert resp.status_code == 200
    assert "Bugs work" in resp.text and "Tweaks work" not in resp.text
    assert client.get(f"/api/ground?stream=tweaks&project={project['slug']}",
                      headers=_auth(tok)).status_code == 403


def test_mcp_ground_without_token_is_denied_and_marks_nothing(project, mcp_caller):
    repo.create_task(project["id"], "Tell reviewer", "", "should", "review", [], {}, "owner")
    mcp_caller(None)
    with pytest.raises(PermissionError):
        mcp_server.ground(project=project["slug"])
    with pytest.raises(PermissionError):
        mcp_server.ground(project=project["slug"], stream="reviewer")
    assert len(repo.list_outbox("reviewer", undelivered_only=True)) == 1


def test_mcp_ground_claims_only_its_own_stream(project, mcp_caller):
    repo.create_task(project["id"], "Tell reviewer", "", "should", "review", [], {}, "owner")
    repo.create_task(project["id"], "Tell bugs", "", "should", "bug", [], {}, "owner")
    mcp_caller(issue_token("bugs", "s"))
    with pytest.raises(PermissionError):
        mcp_server.ground(project=project["slug"], stream="reviewer")
    g = mcp_server.ground(project=project["slug"])
    assert g["stream"] == "bugs" and len(g["undelivered_outbox"]) == 1
    assert repo.list_outbox("bugs", undelivered_only=True) == []
    assert len(repo.list_outbox("reviewer", undelivered_only=True)) == 1


def test_ground_mark_delivered_reports_only_rows_it_won(project):
    repo.create_task(project["id"], "a", "", "should", "bug", [], {}, "owner")
    repo.create_task(project["id"], "b", "", "should", "bug", [], {}, "owner")
    first = repo.list_outbox("bugs", undelivered_only=True)[0]
    repo.mark_outbox_delivered(first["id"], "channel", stream="bugs")
    g = repo.build_ground("bugs", project["slug"], mark_delivered=True)
    assert first["id"] not in [o["id"] for o in g["undelivered_outbox"]]
    assert len(g["undelivered_outbox"]) == 1
