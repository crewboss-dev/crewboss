"""A Google API key sent from the runner's Machines card (#861): server to runner over the authenticated socket, one way. The key is written
atomically to the runner's private key file; Crewboss keeps only a fingerprint, the last four characters and a time. No Google call is made."""
import asyncio
import hashlib
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runner"))

from crewboss_runner import gemini, link as link_mod  # noqa: E402

from app import devices, gemini_keys, runner_ws  # noqa: E402
from app.db import get_cursor  # noqa: E402
from app.repo import BadRequest, Conflict, NotFound  # noqa: E402

from test_devices import est, signed_in  # noqa: E402,F401  (fixtures)
from test_web_auth import auth_on  # noqa: E402,F401

KEY = "AIza" + "Q" * 35
FP = hashlib.sha256(KEY.encode()).hexdigest()[:8]


# --- the runner side ------------------------------------------------------------------------------------------------------------

def test_the_runner_writes_the_key_atomically_with_mode_0600_and_returns_the_fingerprint(tmp_path):
    f = tmp_path / "cfg" / "gemini_api_key"
    assert gemini.store_key(KEY, f) == FP
    assert f.read_text() == KEY + "\n" and (f.stat().st_mode & 0o777) == 0o600
    assert not list(f.parent.glob("*.tmp"))
    assert gemini.detect(f) == {"ok": True, "reason": ""}
    assert gemini.store_key("B" * 30, f) == hashlib.sha256(b"B" * 30).hexdigest()[:8]     # replacing is a fresh paste
    gemini.remove_key(f)
    assert not f.exists()
    gemini.remove_key(f)                                                                  # nothing to remove: no error


@pytest.mark.parametrize("bad", ["", "short", "has space " + "x" * 30, "x" * 300, None, 5, "a\nb" + "c" * 30])
def test_the_runner_refuses_a_key_that_is_not_key_shaped_without_echoing_it(tmp_path, bad):
    with pytest.raises(gemini.GeminiError) as e:
        gemini.store_key(bad, tmp_path / "k")
    assert str(bad) not in str(e.value) or not str(bad)
    assert not (tmp_path / "k").exists()


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, text):
        self.sent.append(json.loads(text))


def _link():
    lk = link_mod.Link.__new__(link_mod.Link)
    lk.on_gemini_key = lambda: lk.changed.append(1)
    lk.changed = []
    lk._proven = True                 # a socket whose server proof already verified
    return lk


def test_the_link_handles_set_and_clear_and_never_echoes_the_key(tmp_path, monkeypatch, caplog):
    f = tmp_path / "gemini_api_key"
    monkeypatch.setattr(gemini, "key_file", lambda: f)
    lk, ws = _link(), FakeWS()
    with caplog.at_level(logging.DEBUG):
        asyncio.run(lk._handle(ws, json.dumps({"type": "gemini_key", "id": "k1", "action": "set", "key": KEY}), b""))
        asyncio.run(lk._handle(ws, json.dumps({"type": "gemini_key", "id": "k2", "action": "set", "key": "nope"}), b""))
    assert ws.sent[0] == {"type": "gemini_key_result", "id": "k1", "ok": True, "fingerprint": FP}
    assert ws.sent[1]["ok"] is False and "error" in ws.sent[1] and "fingerprint" not in ws.sent[1]
    assert KEY not in json.dumps(ws.sent) and KEY not in caplog.text and f.read_text().strip() == KEY
    assert lk.changed == [1]                                                              # the capability is re-checked after a good write
    asyncio.run(lk._handle(ws, json.dumps({"type": "gemini_key", "id": "k3", "action": "clear"}), b""))
    assert ws.sent[2] == {"type": "gemini_key_result", "id": "k3", "ok": True} and not f.exists()
    asyncio.run(lk._handle(ws, json.dumps({"type": "gemini_key", "id": "k4", "action": "burn"}), b""))
    assert ws.sent[3]["ok"] is False


def test_a_gemini_key_frame_before_the_server_proof_writes_no_file_and_closes(tmp_path, monkeypatch, caplog):
    f = tmp_path / "gemini_api_key"
    monkeypatch.setattr(gemini, "key_file", lambda: f)
    lk = _link()
    lk._proven = False

    class WS(FakeWS):
        closed = None

        async def close(self, code=1000, reason=""):
            self.closed = code

    ws = WS()
    with caplog.at_level(logging.DEBUG), pytest.raises(link_mod.PinFailure):
        asyncio.run(lk._handle(ws, json.dumps({"type": "gemini_key", "id": "k1", "action": "set", "key": KEY}), b""))
    assert not f.exists() and ws.sent == [] and ws.closed == 1008 and lk.changed == []
    assert KEY not in caplog.text and "gemini_key" not in caplog.text          # the log line holds no frame contents


# --- the server side: the socket plumbing -----------------------------------------------------------------------------------------

def test_an_offline_runner_is_an_error_and_nothing_is_queued():
    with pytest.raises(runner_ws.RunnerOffline):
        runner_ws.push_gemini_key(987654, "set", KEY)
    assert not runner_ws._conns.get(987654)


def _fake_conn(device_id, answer):
    """A verified Conn whose inbox is served by a thread that plays the runner: answer(frame) -> result dict."""
    loop = asyncio.new_event_loop()
    conn = runner_ws.Conn(device_id, loop)
    conn.verified = True
    frames = []

    def serve():
        asyncio.set_event_loop(loop)

        async def go():
            item = await conn.inbox.get()
            assert item[0] == "gemini_key"
            frame = {"type": "gemini_key", "id": item[1], "action": item[2], **({"key": item[3]} if item[2] == "set" else {})}
            frames.append(frame)
            res = answer(frame)
            w = conn.key_waiters.get(item[1])
            if res is not None and w:
                w.set_result(res)
        loop.run_until_complete(go())

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    with runner_ws._lock:
        runner_ws._conns[device_id] = conn
    return conn, frames, t


def test_push_sends_one_frame_with_the_key_and_returns_the_runners_answer():
    conn, frames, t = _fake_conn(777001, lambda f: {"ok": True, "fingerprint": FP, "error": ""})
    try:
        assert runner_ws.push_gemini_key(777001, "set", KEY) == {"ok": True, "fingerprint": FP, "error": ""}
        t.join(2)
        assert frames[0]["key"] == KEY and frames[0]["action"] == "set" and conn.key_waiters == {}
    finally:
        runner_ws._conns.pop(777001, None)


def test_a_clear_frame_carries_no_key_and_a_silent_runner_times_out_with_fixed_text():
    conn, frames, t = _fake_conn(777002, lambda f: None)
    try:
        with pytest.raises(runner_ws.KeyPushFailed) as e:
            runner_ws.push_gemini_key(777002, "set", KEY, timeout=0.5)
        assert KEY not in str(e.value) and KEY not in repr(e.value)
        t.join(2)
    finally:
        runner_ws._conns.pop(777002, None)
    conn, frames, t = _fake_conn(777003, lambda f: {"ok": True, "fingerprint": "", "error": ""})
    try:
        runner_ws.push_gemini_key(777003, "clear")
        t.join(2)
        assert "key" not in frames[0] and frames[0]["action"] == "clear"
    finally:
        runner_ws._conns.pop(777003, None)


# --- the server side: what is kept ---------------------------------------------------------------------------------------------

from test_deploys import depl  # noqa: E402,F401  (fixture: acme, one runner 'pc' owned by Alice, a running job)
from test_jobs import world  # noqa: E402,F401
from app import web_auth  # noqa: E402


def _h(signed_in):
    return {"cookie": signed_in["cookie"]}


class FakeRunner:
    """Replaces runner_ws.push_gemini_key: records every (device, action, key) and answers like a runner that wrote the file."""

    def __init__(self, offline=False, wrong_fp=False, silent=(), delay=0.0):
        self.calls, self.offline, self.wrong_fp, self.silent, self.delay = [], offline, wrong_fp, set(silent), delay

    def __call__(self, device_id, action, key="", timeout=None):
        self.calls.append((device_id, action, key))
        if self.delay:
            import time
            time.sleep(self.delay)
        if device_id in self.silent:
            raise runner_ws.KeyPushFailed("the runner did not answer in time")
        if self.offline:
            raise runner_ws.RunnerOffline()
        fp = "0" * 8 if self.wrong_fp else (hashlib.sha256(key.encode()).hexdigest()[:8] if action == "set" else "")
        return {"ok": True, "fingerprint": fp, "error": ""}


@pytest.fixture
def fake(monkeypatch):
    f = FakeRunner()
    monkeypatch.setattr(runner_ws, "push_gemini_key", f)
    return f


def _row(dev_id):
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT gemini_key_fp, gemini_key_last4, gemini_key_set_at, gemini_key_state FROM device_tokens WHERE id = %s", (dev_id,))
        return cur.fetchone()


def _everything_stored(needle):
    """True if `needle` appears in ANY text column of ANY table (the database must never hold the key)."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = 'public' "
                    "AND data_type IN ('text', 'character varying', 'jsonb', 'json')")
        cols = cur.fetchall()
        for c in cols:
            cur.execute(f'SELECT 1 FROM "{c["table_name"]}" WHERE "{c["column_name"]}"::text LIKE %s LIMIT 1', (f"%{needle}%",))
            if cur.fetchone():
                return f'{c["table_name"]}.{c["column_name"]}'
    return None


def test_pasting_a_key_sends_it_to_the_runner_and_keeps_only_the_fingerprint_and_last_four(depl, client, signed_in, fake, caplog):
    dev = depl["dev"]["device_id"]
    with caplog.at_level(logging.DEBUG):
        r = client.post(f"/acme/machines/{dev}/gemini-key", data={"key": KEY}, headers=_h(signed_in), follow_redirects=False)
    assert r.status_code == 303
    assert fake.calls == [(dev, "set", KEY)]
    row = _row(dev)
    assert row["gemini_key_fp"] == FP and row["gemini_key_last4"] == KEY[-4:] and row["gemini_key_set_at"] is not None
    # nowhere it could leak to: the response, its headers and cookies, the page, the logs, the whole database
    assert KEY not in r.text and KEY not in json.dumps(dict(r.headers)) and KEY not in caplog.text and KEY not in r.headers.get("location", "")
    assert _everything_stored(KEY) is None
    page = client.get("/acme/machines?tab=runners", headers=_h(signed_in)).text
    assert KEY not in page and f"Gemini key ending &hellip;{KEY[-4:]}" in page and "Remove key" in page and "type=\"password\"" in page
    assert KEY not in client.get("/acme/machines?tab=runners", headers=_h(signed_in)).headers.get("set-cookie", "")


def test_a_bad_key_is_refused_without_echo_and_a_422_never_carries_it(depl, client, signed_in, fake):
    dev = depl["dev"]["device_id"]
    for body in ({"key": "not a key " + "x" * 20}, {"key": ""}, {}, {"other": KEY}):
        r = client.post(f"/acme/machines/{dev}/gemini-key", data=body, headers=_h(signed_in), follow_redirects=False)
        assert r.status_code == 303 and "x" * 20 not in r.headers.get("set-cookie", "") and KEY not in r.text
    assert fake.calls == [] and _row(dev)["gemini_key_fp"] is None


def test_an_offline_runner_is_a_clear_error_and_nothing_is_queued_or_recorded(depl, client, signed_in, monkeypatch):
    f = FakeRunner(offline=True)
    monkeypatch.setattr(runner_ws, "push_gemini_key", f)
    dev = depl["dev"]["device_id"]
    r = client.post(f"/acme/machines/{dev}/gemini-key", data={"key": KEY}, headers=_h(signed_in), follow_redirects=False)
    assert r.status_code == 303 and "offline" in r.headers["set-cookie"].lower() and "try again" in r.headers["set-cookie"].lower()
    assert KEY not in r.headers["set-cookie"] and _row(dev)["gemini_key_fp"] is None and _everything_stored(KEY) is None
    assert len(f.calls) == 1                       # tried once; never retried or held for later


def test_a_runner_that_stored_something_else_is_a_failure_and_is_cleared(depl, client, signed_in, monkeypatch):
    f = FakeRunner(wrong_fp=True)
    monkeypatch.setattr(runner_ws, "push_gemini_key", f)
    dev = depl["dev"]["device_id"]
    client.post(f"/acme/machines/{dev}/gemini-key", data={"key": KEY}, headers=_h(signed_in), follow_redirects=False)
    assert _row(dev)["gemini_key_fp"] is None and [c[1] for c in f.calls] == ["set", "clear"]


def test_remove_key_clears_the_runner_file_and_the_record(depl, client, signed_in, fake):
    dev = depl["dev"]["device_id"]
    client.post(f"/acme/machines/{dev}/gemini-key", data={"key": KEY}, headers=_h(signed_in), follow_redirects=False)
    r = client.post(f"/acme/machines/{dev}/gemini-key/remove", headers=_h(signed_in), follow_redirects=False)
    assert r.status_code == 303 and fake.calls[-1] == (dev, "clear", "")
    row = _row(dev)
    assert row["gemini_key_fp"] is None and row["gemini_key_last4"] is None and row["gemini_key_set_at"] is None


def test_replacing_is_a_fresh_paste_and_one_key_can_go_to_all_of_a_persons_runners(depl, client, signed_in, fake):
    dev = depl["dev"]["device_id"]
    with get_cursor() as cur:
        cur.execute("SELECT * FROM device_tokens WHERE id = %s", (dev,))
        d = cur.fetchone()
        cur.execute("INSERT INTO device_tokens (account_id, user_id, name, mode, platform, pubkey, pubkey_fp, token_hash, expires_at) "
                    "SELECT account_id, user_id, 'second-pc', mode, platform, 'AAAA', 'ffffffffffff', 'h2', expires_at FROM device_tokens WHERE id = %s RETURNING id",
                    (dev,))
        dev2 = cur.fetchone()["id"]
    r = client.post(f"/acme/machines/{dev}/gemini-key", data={"key": KEY, "everywhere": "1"}, headers=_h(signed_in), follow_redirects=False)
    assert r.status_code == 303 and sorted(c[0] for c in fake.calls) == sorted([dev, dev2])
    assert _row(dev)["gemini_key_last4"] == _row(dev2)["gemini_key_last4"] == KEY[-4:]
    other = "Z" * 40
    client.post(f"/acme/machines/{dev2}/gemini-key", data={"key": other}, headers=_h(signed_in), follow_redirects=False)     # just this one
    assert _row(dev2)["gemini_key_fp"] == hashlib.sha256(other.encode()).hexdigest()[:8] and _row(dev)["gemini_key_fp"] == FP


def test_someone_elses_runner_needs_a_fresh_step_up_and_another_persons_runner_is_not_reachable(depl, client, signed_in, fake, monkeypatch):
    dev = depl["dev"]["device_id"]
    monkeypatch.setattr(web_auth, "resolve_session", lambda tok: (
        {"id": 1, "oid": "bbbbbbbb-0000-4000-8000-000000000002", "name": "Bob", "username": "bob@x.test", "auth_age": 10 ** 7}
        if tok == "good" else None))                                         # signed in long ago: not fresh
    r = client.post(f"/acme/machines/{dev}/gemini-key", data={"key": KEY}, headers=_h(signed_in), follow_redirects=False)
    assert r.status_code == 403 and fake.calls == [] and KEY not in r.text and _row(dev)["gemini_key_fp"] is None
    r = client.post(f"/acme/machines/{dev}/gemini-key/remove", headers=_h(signed_in), follow_redirects=False)
    assert r.status_code == 403 and fake.calls == []
    r = client.post(f"/acme/machines/999999/gemini-key", data={"key": KEY}, headers=_h(signed_in), follow_redirects=False)
    assert r.status_code in (303, 404) and fake.calls == []


def test_the_key_needs_a_signed_in_person_and_the_same_origin_guard(depl, client, fake):
    dev = depl["dev"]["device_id"]
    r = client.post(f"/acme/machines/{dev}/gemini-key", data={"key": KEY}, follow_redirects=False)
    assert r.status_code in (401, 302, 303, 403) and fake.calls == []
    r = client.post(f"/acme/machines/{dev}/gemini-key", data={"key": KEY}, headers={"sec-fetch-site": "cross-site"}, follow_redirects=False)
    assert r.status_code in (401, 403) and fake.calls == []


def test_the_service_layer_checks_the_shape_and_names_no_value(depl):
    dev = depl["dev"]["device_id"]
    with pytest.raises(BadRequest) as e:
        gemini_keys.set_key(depl["acme"]["id"], dev, "bad key with spaces", "t")
    assert "bad key" not in str(e.value)
    with pytest.raises(NotFound):
        gemini_keys.set_key(depl["acme"]["id"], 424242, KEY, "t")


def test_the_activity_rows_hold_the_last_four_never_the_key(depl, client, signed_in, fake):
    dev = depl["dev"]["device_id"]
    client.post(f"/acme/machines/{dev}/gemini-key", data={"key": KEY}, headers=_h(signed_in), follow_redirects=False)
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT summary FROM activity WHERE entity = 'device' AND entity_id = %s", (dev,))
        text = " ".join(r["summary"] for r in cur.fetchall())
    assert KEY[-4:] in text and KEY not in text and FP not in text


def test_a_fingerprint_mismatch_clears_the_file_and_marks_the_record_unknown(depl, client, signed_in, monkeypatch):
    dev = depl["dev"]["device_id"]
    ok = FakeRunner()
    monkeypatch.setattr(runner_ws, "push_gemini_key", ok)
    client.post(f"/acme/machines/{dev}/gemini-key", data={"key": KEY}, headers=_h(signed_in), follow_redirects=False)
    assert _row(dev)["gemini_key_last4"] == KEY[-4:]
    bad = FakeRunner(wrong_fp=True)
    monkeypatch.setattr(runner_ws, "push_gemini_key", bad)
    client.post(f"/acme/machines/{dev}/gemini-key", data={"key": "N" * 40}, headers=_h(signed_in), follow_redirects=False)
    row = _row(dev)
    assert [c[1] for c in bad.calls] == ["set", "clear"]
    assert row["gemini_key_state"] == "unknown" and row["gemini_key_fp"] is None and row["gemini_key_last4"] is None
    page = client.get("/acme/machines?tab=runners", headers=_h(signed_in)).text
    assert 'data-gemini-key-state="unknown"' in page and "paste it again" in page and "Replace key" in page and "N" * 40 not in page


def test_a_silent_runner_leaves_the_record_unconfirmed_not_the_old_one(depl, client, signed_in, monkeypatch):
    dev = depl["dev"]["device_id"]
    monkeypatch.setattr(runner_ws, "push_gemini_key", FakeRunner())
    client.post(f"/acme/machines/{dev}/gemini-key", data={"key": KEY}, headers=_h(signed_in), follow_redirects=False)
    monkeypatch.setattr(runner_ws, "push_gemini_key", FakeRunner(silent=[dev]))
    r = client.post(f"/acme/machines/{dev}/gemini-key", data={"key": "M" * 40}, headers=_h(signed_in), follow_redirects=False)
    row = _row(dev)
    assert row["gemini_key_state"] == "unconfirmed" and row["gemini_key_last4"] is None and "older runner version" in r.headers["set-cookie"].replace("%20", " ")
    assert 'data-gemini-key-state="unconfirmed"' in client.get("/acme/machines?tab=runners", headers=_h(signed_in)).text
    monkeypatch.setattr(runner_ws, "push_gemini_key", FakeRunner())                     # a fresh paste that works clears the state
    client.post(f"/acme/machines/{dev}/gemini-key", data={"key": KEY}, headers=_h(signed_in), follow_redirects=False)
    assert _row(dev)["gemini_key_state"] is None and _row(dev)["gemini_key_fp"] == FP
    monkeypatch.setattr(runner_ws, "push_gemini_key", FakeRunner(silent=[dev]))
    client.post(f"/acme/machines/{dev}/gemini-key/remove", headers=_h(signed_in), follow_redirects=False)
    assert _row(dev)["gemini_key_state"] == "unconfirmed"


def test_all_my_runners_are_sent_to_at_the_same_time_with_one_overall_wait(depl, client, signed_in, monkeypatch):
    dev = depl["dev"]["device_id"]
    ids = [dev]
    with get_cursor() as cur:
        for i in range(5):
            cur.execute("INSERT INTO device_tokens (account_id, user_id, name, mode, platform, pubkey, pubkey_fp, token_hash, expires_at) "
                        "SELECT account_id, user_id, %s, mode, platform, 'AAAA', %s, %s, expires_at FROM device_tokens WHERE id = %s RETURNING id",
                        (f"pc{i}", f"{i:012d}", f"h{i}", dev))
            ids.append(cur.fetchone()["id"])
    f = FakeRunner(delay=0.5, silent=[ids[2]])
    monkeypatch.setattr(runner_ws, "push_gemini_key", f)
    t = time.monotonic()
    r = client.post(f"/acme/machines/{dev}/gemini-key", data={"key": KEY, "everywhere": "1"}, headers=_h(signed_in), follow_redirects=False)
    took = time.monotonic() - t
    assert r.status_code == 303 and sorted(c[0] for c in f.calls) == sorted(ids) and took < 1.8     # 6 x 0.5 s in a row would be 3 s
    cookie = r.headers["set-cookie"].replace("%20", " ")
    assert "5 runners" in cookie and "older runner version" in cookie and "update it" in cookie
    assert _row(ids[2])["gemini_key_state"] == "unconfirmed" and _row(ids[1])["gemini_key_last4"] == KEY[-4:]


def test_another_persons_runner_needs_a_step_up_from_the_last_five_minutes(depl, client, signed_in, monkeypatch):
    dev = depl["dev"]["device_id"]
    f = FakeRunner()
    monkeypatch.setattr(runner_ws, "push_gemini_key", f)
    for age, expect in ((400, 403), (60, 303)):                                          # 400 s: fresh for the 12 h default, not for 5 min
        monkeypatch.setattr(web_auth, "resolve_session", lambda tok, age=age: (
            {"id": 1, "oid": "bbbbbbbb-0000-4000-8000-000000000002", "name": "Bob", "username": "bob@x.test", "auth_age": age}
            if tok == "good" else None))
        r = client.post(f"/acme/machines/{dev}/gemini-key", data={"key": KEY}, headers=_h(signed_in), follow_redirects=False)
        assert r.status_code == expect, age
    assert len(f.calls) == 1
