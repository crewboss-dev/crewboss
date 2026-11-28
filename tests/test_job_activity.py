"""The live runner conversation, server side (#89): frames from the device that holds the job at its fence, scrubbed and capped again here, the
history API and the SSE stream for people who can see the task (and nobody else), and a 30-day retention."""
import json

import pytest

from app import job_activity, jobs
from app.db import get_cursor

from test_devices import est  # noqa: F401  (fixtures)
from test_jobs import _queue, world  # noqa: F401  (fixtures)
from test_runner_socket_jobs import _enrol_alice, _rpc
from test_runner_ws import Runner, _expect_close, _next


def F(seq, kind="assistant_text", text="hello", speaker="claude", **meta):
    return {"seq": seq, "speaker": speaker, "kind": kind, "text": text, "meta": meta}


def _rows(job_id):
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT * FROM job_activity WHERE job_id = %s ORDER BY seq", (job_id,))
        return cur.fetchall()


def _claimed(world):
    job = _queue(world)
    claim = jobs.claim(world["dev"]["device_id"])
    return job, claim


# --- the frame itself ----------------------------------------------------------------------------------------------------------------

def test_a_frame_is_cleaned_again_on_the_server():
    f = job_activity.clean_frame(F(3, text="key is sk-" + "a" * 40 + " and password=hunter2 " + "x" * 5000, tool="Bash", path="a/b " * 200, junk="no"))
    assert "hunter2" not in f["text"] and "a" * 40 not in f["text"] and len(f["text"]) <= job_activity.MAX_TEXT
    assert f["meta"] == {"tool": "Bash", "path": ("a/b " * 200)[:300]}                # only the known keys, cut
    assert job_activity.clean_frame(F(1, speaker="human")) is None          # an unknown speaker or kind is not a frame
    assert job_activity.clean_frame(F(1, kind="html")) is None
    assert job_activity.clean_frame({"kind": "status", "text": "x"}) is None   # no seq
    assert job_activity.clean_frame("nope") is None
    ok = job_activity.clean_frame(F(1, kind="status", text="x", session_url="https://claude.ai/code/session_abc12345"))
    assert ok["meta"]["session_url"] == "https://claude.ai/code/session_abc12345"
    assert "session_url" not in job_activity.clean_frame(F(1, kind="status", text="x", session_url="https://evil.example/code/x"))["meta"]
    assert "session_url" not in job_activity.clean_frame(F(1, kind="status", text="x", session_url="javascript:alert(1)"))["meta"]


# --- who may send ----------------------------------------------------------------------------------------------------------------------

def test_the_device_holding_the_job_stores_frames_and_a_resend_is_not_doubled(world):
    job, claim = _claimed(world)
    batch = [F(0, "status", "working"), F(1, text="Reading the code"), F(2, "tool_call", "Read app/x.py", tool="Read", path="app/x.py")]
    assert job_activity.accept(world["dev"]["device_id"], job["id"], claim["fence"], batch) == {"accepted": 3, "dropped": 0}
    assert job_activity.accept(world["dev"]["device_id"], job["id"], claim["fence"], batch + [F(3, "tool_result", "ok")]) == {"accepted": 1, "dropped": 0}
    rows = _rows(job["id"])
    assert [r["seq"] for r in rows] == [1, 2, 3, 4] and rows[2]["meta"] == {"tool": "Read", "path": "app/x.py"} and rows[0]["fence"] == claim["fence"]


def test_a_stale_fence_or_another_device_is_refused(world):
    job, claim = _claimed(world)
    with pytest.raises(jobs.StaleFence):
        job_activity.accept(world["dev"]["device_id"], job["id"], claim["fence"] + 1, [F(0)])
    with pytest.raises(jobs.StaleFence):
        job_activity.accept(world["dev"]["device_id"] + 99, job["id"], claim["fence"], [F(0)])
    jobs.finish(world["dev"]["device_id"], job["id"], claim["fence"], True, "ok")
    with pytest.raises(jobs.StaleFence):                                    # a finished job takes nothing more
        job_activity.accept(world["dev"]["device_id"], job["id"], claim["fence"], [F(0)])
    assert _rows(job["id"]) == []


def test_a_requeued_job_with_a_new_fence_starts_its_own_numbers_in_the_same_thread(world):
    job, claim = _claimed(world)
    job_activity.accept(world["dev"]["device_id"], job["id"], claim["fence"], [F(0), F(1)])
    with get_cursor() as cur:
        cur.execute("UPDATE jobs SET state = 'running', fence = %s WHERE id = %s", (claim["fence"] + 7, job["id"]))
    job_activity.accept(world["dev"]["device_id"], job["id"], claim["fence"] + 7, [F(0, text="second attempt")])
    assert [(r["seq"], r["fence"]) for r in _rows(job["id"])] == [(1, claim["fence"]), (2, claim["fence"]), (3, claim["fence"] + 7)]


def test_over_the_socket_a_batch_over_4k_is_taken_and_a_stale_fence_is_409(world, client):
    dev, key = _enrol_alice(world, "live-pc")
    r = Runner(client, dev, key)
    r.handshake()
    try:
        job = _queue(world)
        _next(r)
        claim = _rpc(r, type="claim", id=1)["job"]
        frames = [F(i, text="ab cd " * 200) for i in range(4)]                  # ~5 KB on the wire: over MAX_FRAME, under MAX_BIG_FRAME
        msg = {"type": "job_activity", "id": "a", "job_id": claim["job_id"], "fence": claim["fence"], "frames": frames}
        assert 4096 < len(json.dumps(msg, separators=(",", ":"))) <= 7168
        reply = _rpc(r, **msg)
        assert reply["type"] == "job_activity_result" and reply["ok"] and reply["accepted"] == 4
        stale = _rpc(r, **{**msg, "id": "b", "fence": claim["fence"] + 1})
        assert stale["ok"] is False and stale["status"] == 409
        bad = _rpc(r, type="job_activity", id="c", job_id="x", fence=1, frames=[])
        assert bad["status"] == 400
        assert len(_rows(job["id"])) == 4
    finally:
        r.close()


def test_a_big_frame_of_another_type_is_still_refused(world, client):
    dev, key = _enrol_alice(world, "big-pc")
    r = Runner(client, dev, key)
    r.handshake()
    try:
        r.ws.send_text(json.dumps({"type": "claim", "id": 1, "pad": "x" * 5000}))
        _expect_close(r, 4400)
    finally:
        r.close()


# --- caps ----------------------------------------------------------------------------------------------------------------------------

def test_a_job_keeps_at_most_its_rows_and_says_so_once(world, monkeypatch):
    monkeypatch.setattr(job_activity, "MAX_ROWS", 5)
    job, claim = _claimed(world)
    out = job_activity.accept(world["dev"]["device_id"], job["id"], claim["fence"], [F(i) for i in range(10)])
    assert out == {"accepted": 4, "dropped": 6}                             # 4 rows + the one "truncated" note = the cap
    job_activity.accept(world["dev"]["device_id"], job["id"], claim["fence"], [F(i) for i in range(10, 14)])
    rows = _rows(job["id"])
    assert len(rows) == 5 and rows[-1]["meta"] == {"truncated": True} and "truncated" in rows[-1]["text"]


def test_a_job_keeps_at_most_its_bytes(world, monkeypatch):
    monkeypatch.setattr(job_activity, "MAX_BYTES", 3000)
    job, claim = _claimed(world)
    job_activity.accept(world["dev"]["device_id"], job["id"], claim["fence"], [F(i, text="ab cd " * 170) for i in range(6)])
    rows = _rows(job["id"])
    assert sum(len(r["text"]) for r in rows if not r["meta"]) <= 3000 and rows[-1]["meta"] == {"truncated": True}


# --- retention -----------------------------------------------------------------------------------------------------------------------

def test_thirty_days_is_kept_and_older_goes(world):
    job, claim = _claimed(world)
    job_activity.accept(world["dev"]["device_id"], job["id"], claim["fence"], [F(0), F(1), F(2)])
    with get_cursor() as cur:
        cur.execute("UPDATE job_activity SET at = now() - interval '31 days' WHERE seq = 1")
        cur.execute("UPDATE job_activity SET at = now() - interval '29 days' WHERE seq = 2")
    assert job_activity.purge(force=True) == 1
    assert [r["seq"] for r in _rows(job["id"])] == [2, 3]
    assert job_activity.purge() == 0                                        # throttled: once an hour


def test_the_job_sweeper_runs_the_purge(world, monkeypatch):
    seen = []
    monkeypatch.setattr(job_activity, "purge", lambda force=False: seen.append(1) or 0)
    jobs.Sweeper().run()
    assert seen == [1]


# --- review follow-ups: the second look at secrets, the session link, the viewer, the wake ----------------------------------------------------

def test_the_server_scrubs_secret_assignments_again():
    for line in ("ADMIN_PIN=1234", "PGPASSWORD=hunter2 psql", "wifi_psk='abc def'", "export API_KEY=\"x y\""):
        out = job_activity.clean_frame(F(1, text=line))["text"]
        assert "[removed]" in out and not any(v in out for v in ("1234", "hunter2", "abc def", "x y")), out
    assert job_activity.clean_frame(F(1, text="shipping=fast"))["text"] == "shipping=fast"


def test_a_realistic_session_link_survives_and_a_lookalike_does_not():
    good = "https://claude.ai/code/session_01Ax3qtxi34QrcUFCpPkumxE"                       # a 24-character id: redact would call it an opaque secret
    assert job_activity.clean_frame(F(1, "status", "", speaker="runner", session_url=good))["meta"]["session_url"] == good
    for bad in ("https://claude.ai/code/session_x", "https://claude.ai/code/session_abc12345/extra", "https://claude.ai.evil.test/code/session_abc12345", "https://claude.ai/code/other"):
        assert "session_url" not in job_activity.clean_frame(F(1, "status", "", speaker="runner", session_url=bad))["meta"]


def test_the_session_link_is_one_query_not_a_scan(world):
    job, claim = _claimed(world)
    good = "https://claude.ai/code/session_01Ax3qtxi34QrcUFCpPkumxE"
    job_activity.accept(world["dev"]["device_id"], job["id"], claim["fence"], [F(0, "status", "", speaker="runner", session_url=good)] + [F(i) for i in range(1, 30)])
    assert job_activity.session_url(job["id"]) == good and job_activity.session_url(job["id"] + 99) == ""


def test_a_viewer_gets_no_tool_output():
    frames = [{"seq": 1, "kind": "assistant_text", "text": "hi", "meta": {}}, {"seq": 2, "kind": "tool_call", "text": "Read x.py", "meta": {"tool": "Read"}},
              {"seq": 3, "kind": "tool_result", "text": "SECRET CONTENT", "meta": {"tool": "Read", "status": "ok"}}]
    seen = job_activity.for_viewer(frames, False)
    assert seen[0]["text"] == "hi" and seen[1]["text"] == "Read x.py"
    assert seen[2]["text"] == "" and seen[2]["meta"]["restricted"] is True and "SECRET" not in json.dumps(seen)
    assert job_activity.for_viewer(frames, True) == frames


def test_accepting_frames_wakes_a_waiting_stream(world):
    import asyncio
    job, claim = _claimed(world)

    async def go():
        with job_activity.Watch(job["id"]) as w:
            await asyncio.to_thread(job_activity.accept, world["dev"]["device_id"], job["id"], claim["fence"], [F(0)])
            return await w.wait(5.0)

    assert asyncio.run(go()) is True
    assert job_activity._waiters == {}                                      # the watch cleaned up after itself
