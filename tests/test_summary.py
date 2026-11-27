"""#828 piece 3: GET /api/summary, the read-only feed for an external digest. A 'digest' role token may call it and nothing
else; it returns counts plus at most 10 titled items per list and never a task body or comment."""
from datetime import datetime, timezone

import pytest

from app import config, erp_health, repo, summary
from app.auth import issue_token
from app.db import get_cursor

BODY_SECRET = "SECRET-BODY-TEXT-do-not-leak"
COMMENT_SECRET = "SECRET-COMMENT-TEXT"


def _h(tok):
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture
def digest():
    return issue_token("digest", "morning-digest")


@pytest.fixture(autouse=True)
def _health_project(project, monkeypatch):
    monkeypatch.setattr(config, "HEALTH_PROJECT", project["slug"])


def _review_task(project, title, assignee="owner", status="review", body=BODY_SECRET):
    t = repo.create_task(project["id"], title, body, "should", "feature", [], {}, "system", assignee=assignee)
    with get_cursor() as cur:
        cur.execute("UPDATE tasks SET status = %s WHERE id = %s", (status, t["id"]))
    return t


def _problem(eid, name, kind, resolved=False):
    with get_cursor() as cur:
        cur.execute("INSERT INTO health_problems (source, eventid, host, name, severity, started_at, resolved_at, kind) "
                    "VALUES ('erp', %s, 'app', %s, 4, now(), %s, %s)",
                    (eid, name, datetime.now(timezone.utc) if resolved else None, kind))


def test_the_token_gate(client, project, digest):
    assert client.get("/api/summary").status_code == 401
    assert client.get("/api/summary", headers=_h("nope")).status_code == 401
    for kind in ("scanner", "zabbix", "bugs", "all"):
        assert client.get("/api/summary", headers=_h(issue_token(kind, f"t-{kind}"))).status_code == 403
    assert client.get("/api/summary", headers=_h(digest)).status_code == 200


def test_a_digest_token_may_only_call_the_summary(client, project, digest):
    for method, path in (("get", "/api/tasks"), ("get", "/api/health"), ("get", "/api/plan"), ("post", "/api/health/report"),
                         ("post", "/api/catalog/import"), ("post", "/api/health/zabbix/poke")):
        r = getattr(client, method)(path, headers=_h(digest))
        assert r.status_code in (401, 403, 404, 405, 422), (path, r.status_code)
        assert r.status_code != 200 and r.status_code != 202, path
    assert client.post("/api/summary", headers=_h(digest)).status_code == 405   # read-only


def test_shape_when_empty(client, digest):
    r = client.get("/api/summary", headers=_h(digest)).json()
    assert set(r) == {"generated_at", "code_faults", "releases_waiting", "owner_reviews"}
    for k in ("code_faults", "releases_waiting", "owner_reviews"):
        assert r[k] == {"count": 0, "items": []}
    assert datetime.fromisoformat(r["generated_at"]).tzinfo is not None


def test_code_faults_list_open_code_faults_only(client, project, digest):
    _problem(1, "Null deref in billing", "code_fault")
    _problem(2, "Disk at 91%", "ops")
    _problem(3, "Old and fixed", "code_fault", resolved=True)
    _problem(4, "Tracked before kinds existed", None)
    r = client.get("/api/summary", headers=_h(digest)).json()["code_faults"]
    assert r["count"] == 1
    item = r["items"][0]
    assert set(item) == {"title", "project", "since", "path"}
    assert item["title"] == "Null deref in billing" and item["project"] == project["slug"]
    assert item["path"] == f"/{project['slug']}/health" and item["since"]


def test_owner_reviews_are_the_owners_review_tasks_with_titles_only(client, project, digest):
    a = _review_task(project, "Test the new login")
    _review_task(project, "AI's own review", assignee="ai")
    _review_task(project, "Still doing", status="doing")
    r = client.get("/api/summary", headers=_h(digest)).json()["owner_reviews"]
    assert r["count"] == 1
    assert r["items"] == [{"id": a["id"], "title": "Test the new login", "project": project["slug"],
                           "path": f"/{project['slug']}/tasks/{a['id']}"}]


def test_item_lists_are_capped_at_ten_but_counts_are_true(client, project, digest):
    for i in range(13):
        _review_task(project, f"Review {i}")
        _problem(100 + i, f"Fault {i}", "code_fault")
    r = client.get("/api/summary", headers=_h(digest)).json()
    assert r["owner_reviews"]["count"] == 13 and len(r["owner_reviews"]["items"]) == summary.ITEM_CAP == 10
    assert r["code_faults"]["count"] == 13 and len(r["code_faults"]["items"]) == 10


def test_no_task_bodies_comments_or_secrets_in_the_output(client, project, digest):
    t = _review_task(project, "Check the thing")
    repo.comment_task(t["id"], COMMENT_SECRET, "owner")
    raw = client.get("/api/summary", headers=_h(digest)).text
    assert BODY_SECRET not in raw and COMMENT_SECRET not in raw and digest not in raw
    assert "body" not in raw and "comment" not in raw.lower().replace("owner_reviews", "")


def test_releases_waiting_lists_requested_deploys(client, project, digest):
    with get_cursor() as cur:
        cur.execute("INSERT INTO commissioned_servers (account_id, name, address, login_user, created_by) "
                    "VALUES (%s, 'box1', '10.0.0.1', 'deploy', 'test') RETURNING id", (project["id"],))
        sid = cur.fetchone()["id"]
        cur.execute("INSERT INTO deploy_targets (project_id, server_id, name, repo_path, env) "
                    "VALUES (%s, %s, 'prod', '/srv/app', 'prod') RETURNING id", (project["id"], sid))
        tid = cur.fetchone()["id"]
        cur.execute("INSERT INTO server_jobs (account_id, project_id, target_id, target_name, sha, subject, state, env) "
                    "VALUES (%s, %s, %s, 'prod', %s, %s, 'requested', 'prod')",
                    (project["id"], project["id"], tid, "a" * 40, "SECRET-SUBJECT"))
    raw = client.get("/api/summary", headers=_h(digest))
    r = raw.json()["releases_waiting"]
    assert r["count"] == 1 and r["items"][0]["project"] == project["slug"] and "aaaaaaa" in r["items"][0]["title"]
    assert r["items"][0]["path"] == f"/{project['slug']}/approvals"
    assert "SECRET-SUBJECT" not in raw.text


def test_issuing_a_digest_token_is_project_less():
    from app.auth import resolve_token
    row = resolve_token(issue_token("digest", "d"))
    assert row["stream"] == "digest" and row["project"] is None
    assert "digest" in config.ROLE_KINDS and "digest" in config.TOKEN_KINDS and "digest" not in config.SESSION_KINDS


def test_code_fault_problems_are_what_erp_ingest_tracked(client, project, digest):
    ev = {"id": 77, "severity": "error", "source": "app", "title": "Boom", "kind": "code_fault",
          "first_seen": datetime.now(timezone.utc).isoformat()}
    erp_health.ingest({"v": 1, "generated_at": datetime.now(timezone.utc).isoformat(), "events": []}, datetime.now(timezone.utc))
    erp_health.ingest({"v": 1, "generated_at": datetime.now(timezone.utc).isoformat(), "events": [ev]}, datetime.now(timezone.utc))
    assert client.get("/api/summary", headers=_h(digest)).json()["code_faults"]["count"] == 1


def _designer_project(slug="d-private"):
    with get_cursor() as cur:
        cur.execute("INSERT INTO users (oid, display_name, username, password_hash, role, is_owner) "
                    "VALUES ('local:dee', 'Dee', 'dee', 'x', 'designer', false) RETURNING id")
        uid = cur.fetchone()["id"]
    p = repo.create_project(slug, "Private", "internal", "system")
    with get_cursor() as cur:
        cur.execute("UPDATE projects SET owner_user_id = %s WHERE id = %s", (uid, p["id"]))
    return repo.get_project(p["id"]), uid


def _request_deploy(project, sha):
    with get_cursor() as cur:
        cur.execute("INSERT INTO commissioned_servers (account_id, name, address, login_user, created_by) "
                    "VALUES (%s, %s, '10.0.0.1', 'deploy', 'test') RETURNING id", (project["id"], f"box-{project['slug']}"))
        sid = cur.fetchone()["id"]
        cur.execute("INSERT INTO deploy_targets (project_id, server_id, name, repo_path, env) "
                    "VALUES (%s, %s, 'prod', '/srv/app', 'prod') RETURNING id", (project["id"], sid))
        tid = cur.fetchone()["id"]
        cur.execute("INSERT INTO server_jobs (account_id, project_id, target_id, target_name, sha, state, env) "
                    "VALUES (%s, %s, %s, 'prod', %s, 'requested', 'prod')", (project["id"], project["id"], tid, sha))


def test_a_designers_private_project_never_appears_in_the_owners_digest(client, project, digest):
    private, _uid = _designer_project()
    _review_task(private, "Dee's private review")
    _request_deploy(private, "c" * 40)
    _review_task(project, "Owner's own review")
    _request_deploy(project, "d" * 40)
    raw = client.get("/api/summary", headers=_h(digest))
    r = raw.json()
    assert [i["title"] for i in r["owner_reviews"]["items"]] == ["Owner's own review"] and r["owner_reviews"]["count"] == 1
    assert [i["project"] for i in r["releases_waiting"]["items"]] == [project["slug"]] and r["releases_waiting"]["count"] == 1
    assert "d-private" not in raw.text and "Dee" not in raw.text


def test_an_owner_users_project_counts_and_an_archived_one_does_not(client, project, digest):
    with get_cursor() as cur:
        cur.execute("INSERT INTO users (oid, display_name, username, password_hash, role, is_owner) "
                    "VALUES ('local:boss', 'Boss', 'boss', 'x', 'owner', true) RETURNING id")
        boss = cur.fetchone()["id"]
    mine = repo.create_project("boss-proj", "Boss proj", "internal", "system")
    gone = repo.create_project("boss-old", "Old", "internal", "system")
    with get_cursor() as cur:
        cur.execute("UPDATE projects SET owner_user_id = %s WHERE id = ANY(%s)", (boss, [mine["id"], gone["id"]]))
        cur.execute("UPDATE projects SET archived_at = now() WHERE id = %s", (gone["id"],))
    _review_task(mine, "Boss review")
    _review_task(gone, "Archived review")
    _request_deploy(gone, "e" * 40)
    r = client.get("/api/summary", headers=_h(digest)).json()
    assert [i["title"] for i in r["owner_reviews"]["items"]] == ["Boss review"]
    assert r["releases_waiting"]["count"] == 0
