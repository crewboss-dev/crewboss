"""Runners phase P3 (task #627): WS /runner/ws. Host allowlist, no Origin, device token only, the mutual signed challenge
(repeated), revoke closing the socket with 4401 within a second, and ids-only nudges. A small in-test runner speaks the
protocol with a real ed25519 key (docs/runner-protocol.md)."""
import base64
import json
import secrets
import threading
import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from app import config, devices, jobs, runner_identity, runner_ws
from app.auth import issue_token
from app.db import get_cursor

from test_devices import _project, _secret, _user, est  # noqa: F401  (fixtures)
from test_jobs import _queue, _task, world  # noqa: F401  (fixtures)
from test_web_auth import auth_on  # noqa: F401

HOST = "dash.example.test:8443"
REFUSED = (WebSocketDisconnect, WebSocketDenialResponse)   # closed before the upgrade (403 or close 1008)


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _enrol(est, account="acme", mode="subscription", user=None, name="pc"):
    user = user or _user()
    issued = devices.issue_code(user, est[account], mode)
    key = Ed25519PrivateKey.generate()
    pub = b64(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))
    dev = devices.enrol(_secret(issued["code"]), pub, name, "linux", ip="10.0.0.1")
    return dev, key, user


def _headers(token, host=HOST, **extra):
    return {"host": host, "authorization": f"Bearer {token}", **extra}


def _sign(key, device_id, client_nonce, server_nonce, host=HOST, label=runner_ws.DEVICE_LABEL):
    return b64(key.sign(runner_ws.transcript(label, client_nonce, server_nonce, device_id, host)))


class Runner:
    """Just enough of a runner: connects, checks the server's proof against the pinned key, answers challenges."""

    def __init__(self, client, dev, key, host=HOST, legacy=False):
        self.client, self.dev, self.key, self.host, self.legacy = client, dev, key, host, legacy
        self.client_nonce = secrets.token_bytes(32)
        self.ws = None

    def connect(self):
        self.cm = self.client.websocket_connect("/runner/ws", headers=_headers(self.dev["device_token"], self.host))
        self.ws = self.cm.__enter__()
        return self

    def close(self):
        try:
            self.cm.__exit__(None, None, None)
        except Exception:  # noqa: BLE001
            pass

    def hello(self):
        self.ws.send_text(json.dumps({"type": "hello", "client_nonce": b64(self.client_nonce),
                                      **({} if self.legacy else {"labels": "crewboss"})}))
        return json.loads(self.ws.receive_text())

    def check_server(self, ch):
        assert ch["type"] == "challenge" and ch["fp8"] == runner_identity.fp8()
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(ch["server_public_key"]))
        pub.verify(base64.b64decode(ch["server_proof"]), runner_ws.transcript(
            runner_ws.LEGACY_SERVER_LABEL if self.legacy else runner_ws.SERVER_LABEL, self.client_nonce,
            base64.b64decode(ch["server_nonce"]), self.dev["device_id"], self.host))

    def answer(self, ch, key=None, device_id=None):
        sig = _sign(key or self.key, device_id or self.dev["device_id"], self.client_nonce,
                    base64.b64decode(ch["server_nonce"]), self.host,
                    label=runner_ws.LEGACY_DEVICE_LABEL if self.legacy else runner_ws.DEVICE_LABEL)
        self.ws.send_text(json.dumps({"type": "proof", "signature": sig}))

    def handshake(self):
        self.connect()
        ch = self.hello()
        self.check_server(ch)
        self.answer(ch)
        ready = json.loads(self.ws.receive_text())
        assert ready["type"] == "ready"
        return ready


@pytest.fixture
def ws_world(est, client, monkeypatch):
    dev, key, user = _enrol(est)
    return {"est": est, "client": client, "dev": dev, "key": key, "user": user}


def _next(runner):
    return json.loads(runner.ws.receive_text())


def _expect_close(runner, code):
    with pytest.raises(WebSocketDisconnect) as ei:
        while True:
            runner.ws.receive_text()
    assert ei.value.code == code
    return ei.value


# --- refused before the upgrade --------------------------------------------------------------------------------------

def test_wrong_host_refused(ws_world):
    for host in ("evil.example.test", "evil.example.test:8443", ""):
        with pytest.raises(REFUSED):
            with ws_world["client"].websocket_connect("/runner/ws", headers=_headers(ws_world["dev"]["device_token"], host)):
                pass
    # the configured host and an operator-added one are fine; case and port don't matter
    assert runner_ws.host_allowed("DASH.example.test:1234")
    assert not runner_ws.host_allowed("dash.example.test.evil.test")


def test_extra_hosts_from_config(monkeypatch):
    monkeypatch.setattr(config, "RUNNER_ALLOWED_HOSTS", "box.lan, other.lan")
    assert runner_ws.host_allowed("box.lan:8000") and runner_ws.host_allowed("other.lan")


@pytest.mark.parametrize("origin", ["https://dash.example.test:8443", "null", "https://evil.example.test", ""])
def test_any_origin_refused(ws_world, origin):
    with pytest.raises(REFUSED):
        with ws_world["client"].websocket_connect(
                "/runner/ws", headers=_headers(ws_world["dev"]["device_token"], origin=origin)):
            pass


def test_job_session_and_missing_tokens_refused(world, client):
    task_dev = world["dev"]
    queued = _queue(world)
    claim = jobs.claim(task_dev["device_id"])
    assert claim["job_token"].startswith("cbj_")
    session = issue_token("features", "s", project_id=world["acme_app"]["id"])
    for token in (claim["job_token"], session, "mdr_notarealtoken", ""):
        with pytest.raises(REFUSED):
            with client.websocket_connect("/runner/ws", headers=_headers(token)):
                pass
    with pytest.raises(REFUSED):   # no Authorization header at all
        with client.websocket_connect("/runner/ws", headers={"host": HOST}):
            pass
    with pytest.raises(REFUSED):   # and never from the URL
        with client.websocket_connect(f"/runner/ws?token={task_dev['device_token']}", headers={"host": HOST}):
            pass
    jobs.done(queued["id"], claim["fence"], True)


# --- the handshake ---------------------------------------------------------------------------------------------------

def test_a_runner_that_predates_the_rename_still_connects_under_the_old_labels(ws_world):
    r = Runner(ws_world["client"], ws_world["dev"], ws_world["key"], legacy=True)   # no "labels" in its hello
    try:
        assert r.handshake()["type"] == "ready"
    finally:
        r.close()


def test_handshake_is_mutual_and_marks_the_device_seen(ws_world):
    r = Runner(ws_world["client"], ws_world["dev"], ws_world["key"])
    ready = r.handshake()
    try:
        assert ready["challenge_seconds"] == config.RUNNER_WS_CHALLENGE_SECONDS and ready["v"] == 1
        assert runner_ws.connected(ws_world["dev"]["device_id"])
        with get_cursor(commit=False) as cur:
            cur.execute("SELECT last_seen FROM device_tokens WHERE id = %s", (ws_world["dev"]["device_id"],))
            assert cur.fetchone()["last_seen"] is not None
    finally:
        r.close()


def test_bad_device_signature_refused(ws_world):
    r = Runner(ws_world["client"], ws_world["dev"], ws_world["key"]).connect()
    try:
        ch = r.hello()
        r.check_server(ch)
        r.answer(ch, key=Ed25519PrivateKey.generate())   # signed by a key the server doesn't have
        _expect_close(r, 4401)
        assert not runner_ws.connected(ws_world["dev"]["device_id"])
    finally:
        r.close()


def test_garbage_signature_and_wrong_label_refused(ws_world):
    r = Runner(ws_world["client"], ws_world["dev"], ws_world["key"]).connect()
    try:
        ch = r.hello()
        r.ws.send_text(json.dumps({"type": "proof", "signature": "!!not base64!!"}))
        _expect_close(r, 4401)
    finally:
        r.close()
    r = Runner(ws_world["client"], ws_world["dev"], ws_world["key"]).connect()
    try:   # the SERVER's label signed by the device's own key: the two directions can't be reflected into each other
        ch = r.hello()
        sig = _sign(ws_world["key"], ws_world["dev"]["device_id"], r.client_nonce, base64.b64decode(ch["server_nonce"]),
                    label=runner_ws.SERVER_LABEL)
        r.ws.send_text(json.dumps({"type": "proof", "signature": sig}))
        _expect_close(r, 4401)
    finally:
        r.close()


def test_replayed_signature_refused(ws_world):
    first = Runner(ws_world["client"], ws_world["dev"], ws_world["key"])
    first.connect()
    ch = first.hello()
    sig = _sign(ws_world["key"], ws_world["dev"]["device_id"], first.client_nonce, base64.b64decode(ch["server_nonce"]))
    first.ws.send_text(json.dumps({"type": "proof", "signature": sig}))
    assert _next(first)["type"] == "ready"
    first.close()
    # A second connection, even with the SAME client nonce: the server's nonce is fresh, so the recorded proof is useless.
    second = Runner(ws_world["client"], ws_world["dev"], ws_world["key"])
    second.client_nonce = first.client_nonce
    second.connect()
    try:
        second.hello()
        second.ws.send_text(json.dumps({"type": "proof", "signature": sig}))
        _expect_close(second, 4401)
    finally:
        second.close()


def test_relayed_signature_refused(ws_world, monkeypatch):
    est = ws_world["est"]
    dev_b, key_b, _ = _enrol(est, user=_user("bbbbbbbb-0000-4000-8000-000000000002", "Bob"), name="bob-pc")
    a = Runner(ws_world["client"], ws_world["dev"], ws_world["key"])
    b = Runner(ws_world["client"], dev_b, key_b)
    a.connect()
    b.connect()
    try:
        # B's connection is relayed a proof that A's device made for A's own connection (A signs the nonce it was shown).
        ch_a, ch_b = a.hello(), b.hello()
        b.client_nonce = a.client_nonce
        sig = _sign(ws_world["key"], ws_world["dev"]["device_id"], a.client_nonce, base64.b64decode(ch_b["server_nonce"]))
        b.ws.send_text(json.dumps({"type": "proof", "signature": sig}))
        _expect_close(b, 4401)
        # And a proof for another HOST (a runner tricked into dialling a relay signs the relay's name) fails too.
        c = Runner(ws_world["client"], ws_world["dev"], ws_world["key"])
        c.connect()
        try:
            ch_c = c.hello()
            bad = _sign(ws_world["key"], ws_world["dev"]["device_id"], c.client_nonce,
                        base64.b64decode(ch_c["server_nonce"]), host="relay.evil.test")
            c.ws.send_text(json.dumps({"type": "proof", "signature": bad}))
            _expect_close(c, 4401)
        finally:
            c.close()
    finally:
        a.close()
        b.close()


def test_no_hello_and_malformed_frames_close(ws_world, monkeypatch):
    monkeypatch.setattr(runner_ws, "HELLO_TIMEOUT", 0.3)
    r = Runner(ws_world["client"], ws_world["dev"], ws_world["key"]).connect()
    try:
        _expect_close(r, 4408)
    finally:
        r.close()
    r = Runner(ws_world["client"], ws_world["dev"], ws_world["key"]).connect()
    try:
        r.ws.send_text("not json")
        _expect_close(r, 4400)
    finally:
        r.close()


# --- the repeated challenge ------------------------------------------------------------------------------------------

def test_rechallenge_repeats_and_a_failure_closes(ws_world, monkeypatch):
    monkeypatch.setattr(config, "RUNNER_WS_CHALLENGE_SECONDS", 0.3)
    r = Runner(ws_world["client"], ws_world["dev"], ws_world["key"])
    r.handshake()
    try:
        for _ in range(2):   # it is asked again, with a fresh nonce each time, and keeps answering
            ch = _next(r)
            r.check_server(ch)
            r.answer(ch)
        ch = _next(r)   # the next one is answered wrongly
        r.answer(ch, key=Ed25519PrivateKey.generate())
        _expect_close(r, 4401)
    finally:
        r.close()


def test_rechallenge_timeout_closes(ws_world, monkeypatch):
    monkeypatch.setattr(config, "RUNNER_WS_CHALLENGE_SECONDS", 0.2)
    monkeypatch.setattr(config, "RUNNER_WS_PROOF_TIMEOUT_SECONDS", 0.4)
    r = Runner(ws_world["client"], ws_world["dev"], ws_world["key"])
    r.handshake()
    try:
        assert _next(r)["type"] == "challenge"   # never answered
        _expect_close(r, 4408)
        assert not runner_ws.connected(ws_world["dev"]["device_id"])
    finally:
        r.close()


# --- revoke ----------------------------------------------------------------------------------------------------------

def test_revoke_closes_the_socket_with_4401_within_a_second(ws_world, monkeypatch):
    monkeypatch.setattr(config, "RUNNER_WS_RECHECK_SECONDS", 60)   # so it is the signal, not the backstop
    r = Runner(ws_world["client"], ws_world["dev"], ws_world["key"])
    r.handshake()
    try:
        started = time.monotonic()
        devices.revoke(ws_world["est"]["acme"]["id"], ws_world["dev"]["device_id"], "owner")
        err = _expect_close(r, 4401)
        assert time.monotonic() - started < 1.0, "closed too slowly"
        assert not runner_ws.connected(ws_world["dev"]["device_id"])
        assert err.code == 4401
    finally:
        r.close()
    with pytest.raises(REFUSED):   # and the revoked token can't reconnect
        with ws_world["client"].websocket_connect("/runner/ws", headers=_headers(ws_world["dev"]["device_token"])):
            pass


def test_revoke_releases_leases_and_closes_the_socket(world, client, monkeypatch):
    """The two halves together: the running job is ended by end_for_device AND the socket goes."""
    monkeypatch.setattr(config, "RUNNER_WS_RECHECK_SECONDS", 60)
    key = Ed25519PrivateKey.generate()
    pub = b64(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))
    issued = devices.issue_code(world["alice"], world["acme"], "subscription")
    dev = devices.enrol(_secret(issued["code"]), pub, "ws-pc", "linux", ip="10.0.0.9")
    queued = _queue(world)
    claim = jobs.claim(dev["device_id"])
    assert claim and claim["job_id"] == queued["id"]
    r = Runner(client, dev, key)
    r.handshake()
    try:
        devices.revoke(world["acme"]["id"], dev["device_id"], "owner")
        _expect_close(r, 4401)
    finally:
        r.close()
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT state, token_hash FROM jobs WHERE id = %s", (queued["id"],))
        row = cur.fetchone()
    assert row["state"] in ("queued", "failed") and row["token_hash"] is None


@pytest.mark.parametrize("how", ["user disabled", "token expired"])
def test_backstop_recheck_closes_without_a_signal(ws_world, monkeypatch, how):
    monkeypatch.setattr(config, "RUNNER_WS_RECHECK_SECONDS", 0.2)
    r = Runner(ws_world["client"], ws_world["dev"], ws_world["key"])
    r.handshake()
    try:
        with get_cursor() as cur:
            if how == "user disabled":
                cur.execute("UPDATE users SET disabled_at = now() WHERE id = %s", (ws_world["user"]["id"],))
            else:
                cur.execute("UPDATE device_tokens SET expires_at = now() - interval '1 second' WHERE id = %s",
                            (ws_world["dev"]["device_id"],))
        _expect_close(r, 4401)
    finally:
        r.close()


# --- nudges ----------------------------------------------------------------------------------------------------------

def test_nudge_carries_ids_only(world, client):
    key = Ed25519PrivateKey.generate()
    pub = b64(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))
    issued = devices.issue_code(world["alice"], world["acme"], "subscription")
    dev = devices.enrol(_secret(issued["code"]), pub, "ws-pc", "linux", ip="10.0.0.9")
    r = Runner(client, dev, key)
    r.handshake()
    try:
        job = _queue(world, approved=False)   # not approved: a subscription runner can't take it, so no nudge
        job_ok = jobs.approve_job(world["acme"]["id"], job["id"], world["alice"]["id"])   # approved: now claimable
        msg = _next(r)
        assert msg == {"type": "job_available", "job_id": job_ok["id"], "task_id": world["task"]["id"]}
        assert "SECRET TASK TEXT" not in json.dumps(msg) and "Fix the thing" not in json.dumps(msg)
        assert set(msg) == {"type", "job_id", "task_id"}
        claim = jobs.claim(dev["device_id"])   # the REST claim is still the truth
        assert claim["job_id"] == job_ok["id"]
        jobs.done(job_ok["id"], claim["fence"], True)
    finally:
        r.close()


def test_nudge_goes_only_to_the_devices_that_may_claim(world, client):
    """Bob's runner (another user, same account) and beta's runner never hear about Alice's job."""
    est = world
    carol_dev, carol_key, _ = _enrol(est, account="beta", user=_user("cccccccc-0000-4000-8000-000000000003", "Carol"))
    bob_dev, bob_key, _ = _enrol(est, user=_user("bbbbbbbb-0000-4000-8000-000000000002", "Bob"), name="bob-pc")
    alice_dev, alice_key, _ = _enrol(est, user=est["alice"], name="alice-ws")
    runners = [Runner(client, d, k) for d, k in ((carol_dev, carol_key), (bob_dev, bob_key), (alice_dev, alice_key))]
    for r in runners:
        r.handshake()
    try:
        _queue(est)
        assert _next(runners[2])["type"] == "job_available"
        # the others got nothing: their next frame is a ping answer, not a nudge
        for r in runners[:2]:
            r.ws.send_text(json.dumps({"type": "ping"}))
            assert _next(r) == {"type": "pong"}
    finally:
        for r in runners:
            r.close()


def test_connect_replays_what_was_queued_before(world, client):
    key = Ed25519PrivateKey.generate()
    pub = b64(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))
    issued = devices.issue_code(world["alice"], world["acme"], "subscription")
    dev = devices.enrol(_secret(issued["code"]), pub, "late-pc", "linux", ip="10.0.0.9")
    queued = _queue(world)
    r = Runner(client, dev, key)
    r.handshake()
    try:
        assert _next(r) == {"type": "job_available", "job_id": queued["id"], "task_id": world["task"]["id"]}
    finally:
        r.close()


def test_a_newer_socket_replaces_the_older(ws_world):
    first = Runner(ws_world["client"], ws_world["dev"], ws_world["key"])
    first.handshake()
    second = Runner(ws_world["client"], ws_world["dev"], ws_world["key"])
    second.handshake()
    try:
        _expect_close(first, 4409)
        assert runner_ws.connected(ws_world["dev"]["device_id"])
    finally:
        first.close()
        second.close()


def test_unproven_newcomer_does_not_disturb_the_verified_socket(ws_world):
    """A holder of the token alone (it never signs) must not evict the real runner."""
    real = Runner(ws_world["client"], ws_world["dev"], ws_world["key"])
    real.handshake()
    thief = Runner(ws_world["client"], ws_world["dev"], ws_world["key"]).connect()
    try:
        thief.hello()   # gets a challenge, never answers
        assert runner_ws.connected(ws_world["dev"]["device_id"])
        real.ws.send_text(json.dumps({"type": "ping"}))
        assert _next(real) == {"type": "pong"}   # the real socket is still up and was not sent a 4409
        assert runner_ws._conns[ws_world["dev"]["device_id"]].verified
    finally:
        thief.close()
        real.close()


def test_second_pending_replaces_the_first_pending_only(ws_world):
    real = Runner(ws_world["client"], ws_world["dev"], ws_world["key"])
    real.handshake()
    p1 = Runner(ws_world["client"], ws_world["dev"], ws_world["key"]).connect()
    p2 = Runner(ws_world["client"], ws_world["dev"], ws_world["key"]).connect()
    try:
        _expect_close(p1, 4409)
        real.ws.send_text(json.dumps({"type": "ping"}))
        assert _next(real) == {"type": "pong"}
    finally:
        p1.close()
        p2.close()
        real.close()
