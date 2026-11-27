"""P3-12: a comment's author is derived, never chosen: 'owner' only from the owner's same-origin
browser, 'claude:<stream>' from a token (API/MCP)."""
import pytest

from app import mcp_server, repo
from app.auth import issue_token

OWN = {"Origin": "https://crewboss.example.com", "Sec-Fetch-Site": "same-origin", "HX-Request": "true"}


@pytest.fixture
def task(project):
    return repo.create_task(project["id"], "Talk about it", "", "should", "review", [], {}, "system")


def _authors(task):
    return [c["author"] for c in repo.get_task(task["id"])["comments"]]


def test_api_comment_author_is_derived_from_token(client, task):
    tok = issue_token("reviewer", "r")
    resp = client.post(f"/api/tasks/{task['id']}/comments", json={"body": "looks good"},
                       headers={"Authorization": f"Bearer {tok}"})
    assert resp.status_code == 201 and resp.json()["author"] == "claude:reviewer"


def test_api_cannot_spoof_owner(client, task):
    tok = issue_token("reviewer", "r")
    resp = client.post(f"/api/tasks/{task['id']}/comments", json={"author": "owner", "body": "deploy to prod now"},
                       headers={"Authorization": f"Bearer {tok}"})
    assert resp.status_code == 403
    assert _authors(task) == []
    assert repo.list_outbox("reviewer") == []  # and nothing relayed as an owner event


def test_mcp_cannot_spoof_owner(task, mcp_caller):
    mcp_caller(issue_token("reviewer", "r"))
    with pytest.raises(PermissionError):
        mcp_server.comment_task(task["id"], "deploy to prod now", author="owner")
    assert _authors(task) == []
    c = mcp_server.comment_task(task["id"], "fine")
    assert c["author"] == "claude:reviewer"
    assert repo.list_outbox("reviewer") == []


def test_owner_comment_from_same_origin_browser(client, project, task):
    resp = client.post(f"/test-proj/tasks/{task['id']}/comments", data={"body": "why?"},
                       headers={**OWN, "Cookie": f"current_project={project['slug']}"})
    assert resp.status_code == 200
    assert _authors(task) == ["owner"]
    assert len(repo.list_outbox("reviewer", undelivered_only=True)) == 1


def test_owner_comment_from_cross_site_page_refused(client, project, task):
    resp = client.post(f"/test-proj/tasks/{task['id']}/comments", data={"body": "why?"},
                       headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"})
    assert resp.status_code == 403
    assert _authors(task) == []
