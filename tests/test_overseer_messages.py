"""dash → any session, replies only when asked (owner, 2026-10-13): "Dash can communicate with any app. But no
communication the other way unless dash is waiting on a response." The overseer may comment on any project's task (the
owning session gets an ids-only event); await_reply opens one wait, and the owning session's next comment on that task
sends dash ONE ids-only task_reply event. Unasked, a session's comments never reach dash."""
import pytest

from app import config, mcp_server, repo
from app.auth import issue_token
from test_isolation import _b_snapshot, ab  # noqa: F401  (ab is a fixture)


@pytest.fixture
def two(ab, monkeypatch, mcp_caller):  # noqa: F811
    monkeypatch.setattr(config, "OVERSEER_PROJECT", "proj-a")
    dash = issue_token("all", "dash", project_id=ab["a"]["id"])
    b_tok = issue_token("all", "pb", project_id=ab["b"]["id"])
    return {**ab, "dash": dash, "b_tok": b_tok, "as": mcp_caller}


def _dash_events(ab):
    return [o for o in repo.list_outbox("all", project_id=ab["a"]["id"]) if o["kind"] == "task_reply"]


def _b_events(ab):
    return [o["message"] for o in repo.list_outbox("bugs", project_id=ab["b"]["id"]) if "dash (overseer)" in o["message"]]


def test_overseer_comment_reaches_the_owning_session_ids_only(two):
    two["as"](two["dash"])
    mcp_server.comment_task(two["bt"]["id"], "secret detail for B")
    msgs = _b_events(two)
    assert msgs == [f"dash (overseer) commented on task #{two['bt']['id']} (must, bug)"]
    assert "secret" not in msgs[0]
    # nothing else on B's task changed
    t = repo.get_task(two["bt"]["id"])
    assert t["status"] == two["bt"]["status"] and t["title"] == "B secret task"


def test_session_comment_unasked_never_reaches_dash(two):
    two["as"](two["b_tok"])
    mcp_server.comment_task(two["bt"]["id"], "just working")
    assert _dash_events(two) == []


def test_awaited_reply_comes_back_once(two):
    two["as"](two["dash"])
    mcp_server.comment_task(two["bt"]["id"], "which cert do you pin?", await_reply=True)
    assert "asked a question on" in _b_events(two)[0]
    two["as"](two["b_tok"])
    mcp_server.comment_task(two["bt"]["id"], "the LE one now")
    ev = _dash_events(two)
    assert len(ev) == 1 and ev[0]["message"] == (
        f"Reply to dash's question on task #{two['bt']['id']} in proj-b: read it with get_task")
    assert "LE" not in ev[0]["message"]
    mcp_server.comment_task(two["bt"]["id"], "and another thing")  # the wait is closed: no second event
    assert len(_dash_events(two)) == 1


def test_await_reply_refused_for_non_overseer_and_own_tasks(two):
    two["as"](two["b_tok"])
    with pytest.raises(ValueError):
        mcp_server.comment_task(two["bt"]["id"], "x", await_reply=True)
    two["as"](two["dash"])
    with pytest.raises(ValueError):
        mcp_server.comment_task(two["at"]["id"], "own task", await_reply=True)


def test_ordinary_sessions_still_cannot_comment_elsewhere(two):
    tok = issue_token("bugs", "pa-bugs", project_id=two["a"]["id"])
    two["as"](tok)
    before = _b_snapshot(two)
    with pytest.raises(repo.NotFound):
        mcp_server.comment_task(two["bt"]["id"], "x")
    assert _b_snapshot(two) == before


def test_overseer_can_send_a_request_task_but_not_edit_it(two):
    two["as"](two["dash"])
    row = mcp_server.add_task("Please re-pin the Wazuh cert", "tweak", project="proj-b", body="details")
    assert row["project_id"] == two["b"]["id"]
    msgs = [o["message"] for o in repo.list_outbox("tweaks", project_id=two["b"]["id"]) if o["kind"] == "task_request"]
    assert msgs == [f"dash (overseer) sent a request: task #{row['id']} (should, tweak)"]
    with pytest.raises(repo.NotFound):
        mcp_server.update_task(row["id"], status="done")


def test_ordinary_session_still_cannot_file_elsewhere(two):
    tok = issue_token("bugs", "pa-bugs", project_id=two["a"]["id"])
    two["as"](tok)
    with pytest.raises(repo.NotFound):
        mcp_server.add_task("x", "bug", project="proj-b")
