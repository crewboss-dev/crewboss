from app import repo
from app.auth import issue_token


def test_owner_write_enqueues_outbox(project):
    task = repo.create_task(project["id"], "Owner made this", "", "should", "bug", [], {}, "owner")
    pending = repo.list_outbox("bugs", undelivered_only=True)
    assert len(pending) == 1
    assert str(task["id"]) in pending[0]["message"] or task["title"] in pending[0]["message"]
    assert pending[0]["delivered_via"] is None


def test_claude_write_does_not_enqueue_outbox(project):
    repo.create_task(project["id"], "Claude made this", "", "should", "bug", [], {}, "claude:bugs")
    assert repo.list_outbox("bugs", undelivered_only=True) == []


def test_owner_update_enqueues_outbox_with_change_summary(project):
    task = repo.create_task(project["id"], "Needs a status change", "", "should", "review", [], {}, "system")
    repo.update_task(task["id"], {"status": "testing"}, "owner")  # old word: -> review
    pending = repo.list_outbox("reviewer", undelivered_only=True)
    assert len(pending) == 1
    assert pending[0]["message"] == f"Owner moved task #{task['id']} to review (should, review)"


def test_claude_update_does_not_enqueue_outbox(project):
    task = repo.create_task(project["id"], "Claude will update", "", "should", "review", [], {}, "system")
    repo.update_task(task["id"], {"status": "testing"}, "claude:reviewer")
    assert repo.list_outbox("reviewer", undelivered_only=True) == []


def test_owner_comment_enqueues_outbox(project):
    task = repo.create_task(project["id"], "Comment target", "", "should", "tweak", [], {}, "system")
    repo.comment_task(task["id"], "please look at this", "owner")
    pending = repo.list_outbox("tweaks", undelivered_only=True)
    assert len(pending) == 1
    assert pending[0]["message"] == f"Owner commented on task #{task['id']} (should, tweak)"


def test_mark_delivered(project):
    task = repo.create_task(project["id"], "Deliver me", "", "should", "bug", [], {}, "owner")
    pending = repo.list_outbox("bugs", undelivered_only=True)
    assert len(pending) == 1
    repo.mark_outbox_delivered(pending[0]["id"], delivered_via="channel")
    assert repo.list_outbox("bugs", undelivered_only=True) == []
    all_rows = repo.list_outbox("bugs", undelivered_only=False)
    assert all_rows[0]["delivered_via"] == "channel"


def test_outbox_dedupe_key_coalesces_repeats(project):
    repo.push_outbox("errors", "health_error", "Xero refresh failing (1st)", project_id=project["id"],
                      dedupe_key="xero-refresh-500")
    repo.push_outbox("errors", "health_error", "Xero refresh failing (2nd)", project_id=project["id"],
                      dedupe_key="xero-refresh-500")
    pending = repo.list_outbox("errors", undelivered_only=True)
    assert len(pending) == 1
    assert "2nd" in pending[0]["message"]


def test_generic_outbox_push_via_api(client, project):
    token = issue_token("errors", "health-pusher")
    resp = client.post("/api/outbox", json={
        "stream": "errors", "kind": "health_error", "message": "Something broke",
        "project": project["slug"], "dedupe_key": "boom-1",
    }, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 201
    pending = client.get("/api/outbox?stream=errors&undelivered=true",
                          headers={"Authorization": f"Bearer {token}"}).json()
    assert len(pending) == 1
    assert pending[0]["message"] == "Something broke"
