"""Who may read the live runner conversation (#89): people who can see the task's project, nobody else. A designer of another project, a
signed-out visitor and the client views (a task's client_visible flag changes nothing) get no frames; frames are plain text in JSON, so a
task or agent that wrote markup shows it as text."""
import json
import re

import pytest

from app import access, repo
from app.db import get_cursor

from test_multi_user import _client, _user, local, world  # noqa: F401  (fixtures: `local` is autouse there; `world` is the multi-user tree)


@pytest.fixture
def live(world):
    """A running job on D2's task (project d2-proj) with three frames stored."""
    with get_cursor() as cur:
        cur.execute("SELECT id FROM agent_profiles WHERE name = 'builder'")
        prof = cur.fetchone()["id"]
        cur.execute("INSERT INTO device_tokens (user_id, account_id, name, mode, token_hash, pubkey, pubkey_fp, expires_at) "
                    "VALUES (%s, %s, 'pc', 'subscription', 'h', 'k', 'f', now() + interval '1 day') RETURNING id", (world["d2"]["id"], world["p2"]["id"]))
        dev = cur.fetchone()["id"]
        cur.execute("INSERT INTO jobs (task_id, account_id, user_id, profile_id, approved_at, approved_by, state, device_id, fence, lease_expires_at) "
                    "VALUES (%s, %s, %s, %s, now(), %s, 'running', %s, 5, now() + interval '1 hour') RETURNING id",
                    (world["t2"]["id"], world["p2"]["id"], world["d2"]["id"], prof, world["d2"]["id"], dev))
        jid = cur.fetchone()["id"]
        for seq, kind, text, meta in ((1, "status", "working", {"state": "working"}), (2, "assistant_text", "<img src=x onerror=alert(1)> **hi**", {}),
                                      (3, "status", "session", {"session_url": "https://claude.ai/code/session_abc12345"})):
            cur.execute("INSERT INTO job_activity (job_id, seq, fence, rseq, speaker, kind, text, meta) VALUES (%s, %s, 5, %s, 'claude', %s, %s, %s)",
                        (jid, seq, seq, kind, text, json.dumps(meta)))
    return {**world, "job": jid, "url": f"/d2-proj/tasks/{world['t2']['id']}/run/activity"}


def test_a_person_who_can_see_the_project_reads_the_history_by_seq(live):
    c = _client(live["d2"])
    r = c.get(live["url"])
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store" and r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert body["job_id"] == live["job"] and body["live"] and body["last_seq"] == 3 and body["session_url"] == "https://claude.ai/code/session_abc12345"
    assert [f["seq"] for f in body["frames"]] == [1, 2, 3] and "<img" in body["frames"][1]["text"]   # data, not markup: the page escapes it
    assert [f["seq"] for f in c.get(live["url"] + "?after=2").json()["frames"]] == [3]
    assert c.get(live["url"] + "?after=3").json() == {**c.get(live["url"] + "?after=3").json(), "frames": [], "last_seq": 3}
    assert c.get(live["url"] + "?after=junk").status_code == 422
    assert _client(live["owner"]).get(live["url"]).status_code == 200


def test_a_viewer_who_was_given_the_project_can_read_it_and_a_designer_of_another_cannot(live):
    assert _client(live["v"]).get(live["url"]).status_code == 404            # a viewer with no share: the same 404 as a missing project
    access.share(live["p2"]["id"], live["v"]["id"], "viewer", live["owner"]["id"])
    assert _client(live["v"]).get(live["url"]).status_code == 200
    r = _client(live["d1"]).get(live["url"])                                 # another designer
    assert r.status_code in (403, 404) and "onerror" not in r.text and "working" not in r.text
    assert _client(live["d1"]).get(live["url"] + "/stream").status_code in (403, 404)


def test_a_client_visible_task_gives_a_non_member_nothing_more(live):
    with get_cursor() as cur:
        cur.execute("UPDATE tasks SET client_visible = true WHERE id = %s", (live["t2"]["id"],))
    r = _client(live["d1"]).get(live["url"])
    assert r.status_code in (403, 404) and "frames" not in r.text


def test_nobody_signed_out_reads_anything(live):
    from fastapi.testclient import TestClient
    from app.main import app
    from test_multi_user import ORIGIN
    anon = TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 50001))
    for path in ("", "/stream"):
        r = anon.get(live["url"] + path, follow_redirects=False)
        assert r.status_code in (302, 303, 401, 403, 404) and "onerror" not in r.text and "working" not in r.text


def test_a_task_of_another_project_is_not_reachable_through_this_one(live):
    r = _client(live["d2"]).get(f"/d2-proj/tasks/{live['t1']['id']}/run/activity")
    assert r.status_code == 404
    r = _client(live["owner"]).get(f"/own-proj/tasks/{live['t2']['id']}/run/activity")
    assert r.status_code == 404


def test_a_task_with_no_job_has_an_empty_thread(live):
    r = _client(live["d1"]).get(f"/d1-proj/tasks/{live['t1']['id']}/run/activity")
    assert r.status_code == 200 and r.json()["frames"] == [] and r.json()["job_id"] is None


def test_the_stream_sends_new_frames_then_ends_with_the_job(live, monkeypatch):
    from app.routers import job_activity_pages as pages
    monkeypatch.setattr(pages, "STREAM_POLL_SECONDS", 0.05)
    monkeypatch.setattr(pages, "STREAM_AFTER_END_SECONDS", 0.2)
    with get_cursor() as cur:
        cur.execute("UPDATE jobs SET state = 'done', finished_at = now() WHERE id = %s", (live["job"],))
    with _client(live["d2"]).stream("GET", live["url"] + "/stream?after=1") as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream") and r.headers["cache-control"] == "no-store"
        text = "".join(r.iter_text())
    events = re.findall(r"event: (\w+)\ndata: (.*)", text)
    assert [e for e, _ in events] == ["frames", "end"]
    data = json.loads(events[0][1])
    assert [f["seq"] for f in data["frames"]] == [2, 3] and data["last_seq"] == 3 and events[1][1] == "done"


def test_a_viewer_reads_messages_and_calls_but_never_tool_output(live):
    with get_cursor() as cur:
        cur.execute("INSERT INTO job_activity (job_id, seq, fence, rseq, speaker, kind, text, meta) VALUES (%s, 4, 5, 4, 'claude', 'tool_call', 'Read app/x.py', %s)", (live["job"], json.dumps({"tool": "Read"})))
        cur.execute("INSERT INTO job_activity (job_id, seq, fence, rseq, speaker, kind, text, meta) VALUES (%s, 5, 5, 5, 'claude', 'tool_result', 'FILE CONTENTS HERE', %s)", (live["job"], json.dumps({"tool": "Read", "status": "ok"})))
    access.share(live["p2"]["id"], live["v"]["id"], "viewer", live["owner"]["id"])
    seen = _client(live["v"]).get(live["url"])
    body = seen.json()
    assert "FILE CONTENTS HERE" not in seen.text and [f["text"] for f in body["frames"] if f["kind"] == "tool_call"] == ["Read app/x.py"]
    hidden = [f for f in body["frames"] if f["kind"] == "tool_result"][0]
    assert hidden["text"] == "" and hidden["meta"]["restricted"] is True
    for who in (live["d2"], live["owner"]):                                  # the project's designer (edit) and the owner see the output
        assert "FILE CONTENTS HERE" in _client(who).get(live["url"]).text
    with _client(live["v"]).stream("GET", live["url"] + "/stream?after=3") as r:     # and the stream says the same
        with get_cursor() as cur:
            cur.execute("UPDATE jobs SET state = 'done', finished_at = now() WHERE id = %s", (live["job"],))
        text = "".join(r.iter_text())
    assert "FILE CONTENTS HERE" not in text and "restricted" in text


def test_a_person_may_hold_only_a_few_live_views_open(live, monkeypatch):
    from app.routers import job_activity_pages as pages
    monkeypatch.setattr(pages, "MAX_STREAMS_PER_USER", 2)
    key = live["d2"]["id"]
    monkeypatch.setitem(pages._open, key, 2)
    r = _client(live["d2"]).get(live["url"] + "/stream")
    assert r.status_code == 429 and "too many" in r.text
    pages._open.pop(key, None)


def test_the_stream_ends_when_the_person_loses_access(live, monkeypatch):
    from app.routers import job_activity_pages as pages
    monkeypatch.setattr(pages, "STREAM_POLL_SECONDS", 0.05)
    monkeypatch.setattr(pages, "ACCESS_RECHECK_SECONDS", 0.1)
    monkeypatch.setattr(pages, "_recheck", lambda request, project_id: (False, False))
    with _client(live["d2"]).stream("GET", live["url"] + "/stream?after=3") as r:
        text = "".join(r.iter_text())
    assert "event: end\ndata: access" in text
    assert pages._open.get(live["d2"]["id"], 0) == 0                          # the slot is given back
