"""Runners P4 (task #627), server side: claim, heartbeat and finish are messages on the VERIFIED socket, and REST refuses a
lone device token (review comment 2280, docs/runner-protocol.md)."""
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app import config, devices, jobs
from app.db import get_cursor

from test_devices import _secret, est  # noqa: F401  (fixtures)
from test_jobs import _queue, world  # noqa: F401  (fixtures)
from test_runner_ws import Runner, _expect_close, _next, b64
from test_web_auth import auth_on  # noqa: F401


def _rpc(runner, **msg):
    runner.ws.send_text(json.dumps(msg))
    while True:
        reply = _next(runner)
        if reply["type"] != "job_available":   # a nudge can arrive between a request and its reply
            return reply


def _enrol_alice(world, name):
    key = Ed25519PrivateKey.generate()
    pub = b64(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))
    issued = devices.issue_code(world["alice"], world["acme"], "subscription")
    return devices.enrol(_secret(issued["code"]), pub, name, "linux", ip="10.0.0.9"), key


def _state(job_id):
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT state FROM jobs WHERE id = %s", (job_id,))
        return cur.fetchone()["state"]


def test_claim_heartbeat_finish_over_the_socket(world, client):
    dev, key = _enrol_alice(world, "sock-pc")
    r = Runner(client, dev, key)
    r.handshake()
    try:
        assert _rpc(r, type="claim", id=1) == {"type": "claim_result", "id": 1, "job": None, "ok": True}
        job = _queue(world)
        assert _next(r)["type"] == "job_available"
        reply = _rpc(r, type="claim", id="a")
        claim = reply["job"]
        assert reply["ok"] and reply["id"] == "a" and claim["job_id"] == job["id"]
        assert claim["job_token"].startswith("cbj_") and claim["branch"] == f"dash/j{job['id']}-e{claim['fence']}"
        assert "SECRET TASK TEXT" not in json.dumps(reply)   # ids only, as before
        hb = _rpc(r, type="heartbeat", id=2, job_id=claim["job_id"], fence=claim["fence"])
        assert hb["ok"] and hb["lease_seconds"] == config.JOB_LEASE_SECONDS
        done = _rpc(r, type="finish", id=3, job_id=claim["job_id"], fence=claim["fence"], ok=True, summary="pushed")
        assert done["ok"] and done["state"] == "done"
        assert _rpc(r, type="claim")["job"] is None
    finally:
        r.close()


def test_rest_claim_and_heartbeat_refuse_a_lone_device_token(world, client):
    job = _queue(world)
    h = {"authorization": f"Bearer {world['dev']['device_token']}"}
    assert client.post("/runner/jobs/claim", headers=h).status_code == 403
    assert client.post(f"/runner/jobs/{job['id']}/heartbeat", json={"fence": 1}, headers=h).status_code == 403
    assert _state(job["id"]) == "queued"   # and it took nothing


def test_claim_before_the_first_proof_is_refused(world, client):
    """A connection holding only the token, which has not proven the device key, can't claim."""
    dev, key = _enrol_alice(world, "thief")
    job = _queue(world)
    r = Runner(client, dev, key).connect()
    try:
        r.hello()   # challenge sent, never answered
        r.ws.send_text(json.dumps({"type": "claim", "id": 1}))
        _expect_close(r, 4400)
    finally:
        r.close()
    assert _state(job["id"]) == "queued"


def test_heartbeat_with_a_stale_fence_is_409_and_changes_nothing(world, client):
    dev, key = _enrol_alice(world, "sleepy-pc")
    r = Runner(client, dev, key)
    r.handshake()
    try:
        job = _queue(world)
        _next(r)
        old = _rpc(r, type="claim")["job"]
        with get_cursor() as cur:   # the PC slept; its lease lapsed and the job went back to the queue on a new fence
            jobs.end_for_device(cur, dev["device_id"], "test")
        hb = _rpc(r, type="heartbeat", job_id=old["job_id"], fence=old["fence"])
        assert hb["ok"] is False and hb["status"] == 409
        fin = _rpc(r, type="finish", job_id=old["job_id"], fence=old["fence"], ok=True)
        assert fin["ok"] is False and fin["status"] == 409
        assert _state(job["id"]) == "queued"
        assert _rpc(r, type="heartbeat", job_id="x", fence=1)["status"] == 400
    finally:
        r.close()


def test_release_requeues_with_a_new_fence_counts_attempts_and_fails_after_the_cap(world, client):
    dev, key = _enrol_alice(world, "broken-pc")
    r = Runner(client, dev, key)
    r.handshake()
    try:
        job = _queue(world)
        _next(r)
        first = _rpc(r, type="claim")["job"]
        assert first["max_seconds"] == config.JOB_MAX_SECONDS   # the wall-clock ceiling rides in the claim
        rel = _rpc(r, type="release", job_id=first["job_id"], fence=first["fence"], reason="no git credentials")
        assert rel["ok"] and rel["state"] == "queued" and _state(job["id"]) == "queued"
        with get_cursor(commit=False) as cur:
            cur.execute("SELECT fence, attempts, token_hash FROM jobs WHERE id = %s", (job["id"],))
            row = cur.fetchone()
        assert row["fence"] > first["fence"] and row["attempts"] == 1 and row["token_hash"] is None
        stale = _rpc(r, type="release", job_id=first["job_id"], fence=first["fence"])   # the old fence: 409, nothing changes
        assert stale["ok"] is False and stale["status"] == 409
        last = None
        for _ in range(config.JOB_MAX_ATTEMPTS - 1):   # attempts are counted: it is failed for a human at the cap
            claim = _rpc(r, type="claim")["job"]
            last = _rpc(r, type="release", job_id=claim["job_id"], fence=claim["fence"])
        assert last["state"] == "failed" and _state(job["id"]) == "failed"
    finally:
        r.close()


def test_finish_and_release_check_the_device(world, client):
    dev, key = _enrol_alice(world, "pc-a")
    job = _queue(world)
    claim = jobs.claim(dev["device_id"])
    other, other_key = _enrol_alice(world, "pc-b")
    r = Runner(client, other, other_key)
    r.handshake()
    try:
        for t in ("finish", "release", "heartbeat"):   # another device's job at the right fence
            assert _rpc(r, type=t, job_id=claim["job_id"], fence=claim["fence"], ok=True)["status"] == 409
        assert _state(job["id"]) == "running"
    finally:
        r.close()


def test_release_reason_becomes_a_task_comment_and_one_push(world, client, monkeypatch):
    """A runner that gives a job back with a reason (Claude Code signed out, the session did not start): the reason is a
    comment on the task and ONE push to the owner, never a silent requeue."""
    import threading
    from app import push
    sent, got = [], threading.Event()
    monkeypatch.setattr(push, "deliver", lambda payload: (sent.append(payload), got.set(), {"sent": 1})[2])
    dev, key = _enrol_alice(world, "sign-in-pc")
    r = Runner(client, dev, key)
    r.handshake()
    try:
        job = _queue(world)
        _next(r)
        claim = _rpc(r, type="claim")["job"]
        reason = "Claude Code needs you to sign in on sign-in-pc\x07\n" + "x" * 500   # control chars and length are cleaned
        rel = _rpc(r, type="release", job_id=claim["job_id"], fence=claim["fence"], reason=reason)
        assert rel["ok"] and rel["state"] == "queued"
        assert got.wait(5) and len(sent) == 1
        assert sent[0]["title"] == "Runner needs attention" and sent[0]["body"].startswith("Task #")
        assert "Claude Code needs you to sign in on sign-in-pc" in sent[0]["body"] and "\x07" not in sent[0]["body"]
        assert len(sent[0]["body"]) < 260 and sent[0]["url"].endswith("/machines?role=jobs")
        with get_cursor(commit=False) as cur:
            cur.execute("SELECT author, body FROM task_comments WHERE task_id = %s ORDER BY id DESC LIMIT 1", (job["task_id"],))
            c = cur.fetchone()
        assert c["author"] == "runner:sign-in-pc" and "sign in on sign-in-pc" in c["body"] and "\x07" not in c["body"]
        # a release with no reason adds no comment and sends nothing
        before = len(sent)
        claim = _rpc(r, type="claim")["job"]
        _rpc(r, type="release", job_id=claim["job_id"], fence=claim["fence"])
        assert len(sent) == before
    finally:
        r.close()


def test_attention_frame_keeps_only_known_kinds(world, client):
    from app import runner_ws
    dev, key = _enrol_alice(world, "attn-pc")
    r = Runner(client, dev, key)
    r.handshake()
    try:
        r.ws.send_text(json.dumps({"type": "attention", "kinds": ["not_signed_in", "<script>alert(1)</script>", 5, "not_signed_in"]}))
        assert _rpc(r, type="claim")["ok"]   # the frame is not answered and does not close the socket
        assert runner_ws.attention(dev["device_id"]) == [runner_ws.ATTENTION_TEXT["not_signed_in"]]
        r.ws.send_text(json.dumps({"type": "attention", "kinds": []}))
        assert _rpc(r, type="claim")["ok"] and runner_ws.attention(dev["device_id"]) == []
    finally:
        r.close()
    assert runner_ws.attention(dev["device_id"]) == []   # offline: nothing
