"""P2-9: MCP write auth is decided PER TOOL CALL from that call's Authorization header — never from
whatever header came with `initialize`."""
import json

import pytest

from app import mcp_server, repo
from app.auth import issue_token
from app.db import get_cursor

# --- unit: the tool functions, with the per-call header stubbed ----------------------------------------


def test_require_write_session_without_token_raises(mcp_caller):
    mcp_caller(None)
    with pytest.raises(PermissionError):
        mcp_server._require_write_session()


def test_require_write_session_with_valid_token(mcp_caller, project):
    mcp_caller(issue_token("bugs", "test-session"))
    assert mcp_server._require_write_session()["stream"] == "bugs"


def test_require_write_session_outside_any_request_is_denied():
    # No HTTP request at all (not stubbed): nothing to authenticate with.
    with pytest.raises(PermissionError):
        mcp_server._require_write_session()


def test_add_task_tool_requires_session(project, mcp_caller):
    mcp_caller(None)
    with pytest.raises(PermissionError):
        mcp_server.add_task(title="No auth", stream="bug", project=project["slug"])


def test_add_task_tool_works_with_session(project, mcp_caller):
    mcp_caller(issue_token("features", "test-features"))
    task = mcp_server.add_task(title="Authed task", stream="feature", project=project["slug"])
    assert task["title"] == "Authed task"
    assert task["created_by"] == "claude:features"


# --- end to end over streamable HTTP ---------------------------------------------------------------------

BASE = {"Host": "127.0.0.1", "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _rpc(client, body, token=None, session_id=None):
    headers = dict(BASE)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if session_id:
        headers["mcp-session-id"] = session_id
    resp = client.post("/mcp/", content=json.dumps(body), headers=headers)
    assert resp.status_code in (200, 202), (resp.status_code, resp.text)
    return resp


def _result(resp):
    if resp.headers.get("content-type", "").startswith("application/json"):
        return resp.json()
    data = [line[5:].strip() for line in resp.text.splitlines() if line.startswith("data:")]
    return json.loads(data[-1])


def _open_session(client, token):
    resp = _rpc(client, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "pytest", "version": "0"}}}, token=token)
    sid = resp.headers["mcp-session-id"]
    _rpc(client, {"jsonrpc": "2.0", "method": "notifications/initialized"}, token=token, session_id=sid)
    return sid


def _add_task(client, sid, project, title, token=None):
    resp = _rpc(client, {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
        "name": "add_task", "arguments": {"title": title, "stream": "bug", "project": project["slug"]}}},
        token=token, session_id=sid)
    return _result(resp)["result"]


def _titles(project):
    return {t["title"]: t["created_by"] for t in repo.list_tasks(project["id"])}


def test_per_call_header_wins_over_initialize(client, project):
    bugs, tweaks = issue_token("bugs", "a"), issue_token("tweaks", "b")
    sid = _open_session(client, bugs)
    res = _add_task(client, sid, project, "as tweaks", token=tweaks)
    assert not res.get("isError"), res
    assert _titles(project)["as tweaks"] == "claude:tweaks"


def test_missing_header_on_a_call_is_denied(client, project):
    sid = _open_session(client, issue_token("bugs", "a"))
    res = _add_task(client, sid, project, "no header", token=None)
    assert res.get("isError") and "Authorization" in json.dumps(res)
    assert "no header" not in _titles(project)


def test_revoked_token_is_denied_immediately(client, project):
    token = issue_token("bugs", "soon revoked")
    sid = _open_session(client, token)
    assert not _add_task(client, sid, project, "before revoke", token=token).get("isError")
    with get_cursor() as cur:  # revoke = delete the token row
        cur.execute("DELETE FROM session_tokens WHERE name = 'soon revoked'")
    res = _add_task(client, sid, project, "after revoke", token=token)
    assert res.get("isError")
    assert "after revoke" not in _titles(project)
