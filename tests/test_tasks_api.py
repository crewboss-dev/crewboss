from app import repo
from app.auth import issue_token


def _token(stream="bugs"):
    return issue_token(stream, f"test-{stream}")


def test_create_task_via_api_requires_bearer(client, project):
    resp = client.post("/api/tasks", json={
        "project": project["slug"], "title": "No token", "stream": "bug",
    })
    assert resp.status_code == 401


def test_create_and_fetch_task_via_api(client, project):
    token = _token("bugs")
    resp = client.post("/api/tasks", json={
        "project": project["slug"], "title": "Fix the thing", "stream": "bug", "priority": "must",
    }, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["title"] == "Fix the thing"
    assert body["status"] == "backlog"
    assert body["owner_stream"] == "bugs"

    got = client.get(f"/api/tasks/{body['id']}", headers={"Authorization": f"Bearer {token}"})
    assert got.status_code == 200
    assert got.json()["title"] == "Fix the thing"


def test_patch_task_status(client, project):
    token = _token("features")
    created = client.post("/api/tasks", json={
        "project": project["slug"], "title": "Ship it", "stream": "feature",
    }, headers={"Authorization": f"Bearer {token}"}).json()

    patched = client.patch(f"/api/tasks/{created['id']}", json={"status": "doing"},
                            headers={"Authorization": f"Bearer {token}"})
    assert patched.status_code == 200
    assert patched.json()["status"] == "doing"


def test_comment_task_via_api(client, project):
    token = _token("reviewer")
    created = client.post("/api/tasks", json={
        "project": project["slug"], "title": "Review this", "stream": "review",
    }, headers={"Authorization": f"Bearer {token}"}).json()

    resp = client.post(f"/api/tasks/{created['id']}/comments", json={"author": "claude:reviewer", "body": "looks good"},
                        headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 201

    full = client.get(f"/api/tasks/{created['id']}", headers={"Authorization": f"Bearer {token}"}).json()
    assert len(full["comments"]) == 1
    assert full["comments"][0]["body"] == "looks good"


def test_list_tasks_filters_by_stream(client, project):
    token = _token("tweaks")
    client.post("/api/tasks", json={"project": project["slug"], "title": "A tweak", "stream": "tweak"},
                headers={"Authorization": f"Bearer {token}"})
    client.post("/api/tasks", json={"project": project["slug"], "title": "A bug", "stream": "bug"},
                headers={"Authorization": f"Bearer {_token('bugs')}"})

    resp = client.get(f"/api/tasks?project={project['slug']}&stream=tweak", headers={"Authorization": f"Bearer {token}"})
    titles = [t["title"] for t in resp.json()]
    assert titles == ["A tweak"]


def test_error_stream_task_defaults_priority_must_when_needs_owner(client, project):
    token = issue_token("errors", "test-errors")
    resp = client.post("/api/tasks", json={
        "project": project["slug"], "title": "Xero refresh is failing", "stream": "error",
        "needs_owner": True,
    }, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 201
    assert resp.json()["priority"] == "must"
    assert resp.json()["needs_owner"] is True


def test_invalid_token_rejected(client, project):
    resp = client.post("/api/tasks", json={"project": project["slug"], "title": "x", "stream": "bug"},
                        headers={"Authorization": "Bearer not-a-real-token"})
    assert resp.status_code == 401


def test_owner_stream_reads_both_the_old_prefixed_and_the_short_form(client, project):
    from app.db import get_cursor
    token = _token("bugs")
    h = {"Authorization": f"Bearer {token}"}
    old = client.post("/api/tasks", json={"project": project["slug"], "title": "Old row", "stream": "bug"}, headers=h).json()
    new = client.post("/api/tasks", json={"project": project["slug"], "title": "New row", "stream": "bug"}, headers=h).json()
    with get_cursor() as cur:   # an old row, written when owner_stream held '<prefix>-bugs'
        cur.execute("UPDATE tasks SET owner_stream = 'example-bugs' WHERE id = %s", (old["id"],))
    assert new["owner_stream"] == "bugs"                       # new writes are the short form only
    assert client.get(f"/api/tasks/{old['id']}", headers=h).json()["owner_stream"] == "bugs"
    listed = {t["id"]: t["owner_stream"] for t in repo.list_tasks(project["id"])}
    assert listed[old["id"]] == listed[new["id"]] == "bugs"
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT owner_stream FROM tasks WHERE id = %s", (new["id"],))
        assert cur.fetchone()["owner_stream"] == "bugs"
    assert [repo.normalize_owner_stream(v) for v in ("example-bugs", "bugs", "x-y-features", None, "odd-name")] == \
        ["bugs", "bugs", "features", None, "odd-name"]
