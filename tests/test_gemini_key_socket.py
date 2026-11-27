"""The gemini_key frames over a real runner socket (#861): the server sends the key once to a verified runner and waits for its reply;
an offline runner is refused; the reply is checked for shape; the key is not in the server's logs."""
import hashlib
import json
import logging
import threading

import pytest

from app import runner_ws

from test_runner_ws import Runner, _next, ws_world  # noqa: F401  (fixtures)
from test_devices import est  # noqa: F401
from test_jobs import world  # noqa: F401
from test_web_auth import auth_on  # noqa: F401

KEY = "AIza" + "W" * 35
FP = hashlib.sha256(KEY.encode()).hexdigest()[:8]


def _push_in_thread(device_id, action, key=""):
    out = {}

    def go():
        try:
            out["res"] = runner_ws.push_gemini_key(device_id, action, key, timeout=8)
        except Exception as exc:  # noqa: BLE001
            out["exc"] = exc
    t = threading.Thread(target=go, daemon=True)
    t.start()
    return t, out


def test_a_verified_runner_receives_the_key_once_and_its_answer_comes_back(ws_world, caplog):
    dev_id = ws_world["dev"]["device_id"]
    r = Runner(ws_world["client"], ws_world["dev"], ws_world["key"])
    with caplog.at_level(logging.DEBUG):
        try:
            r.handshake()
            t, out = _push_in_thread(dev_id, "set", KEY)
            frame = _next(r)
            assert frame["type"] == "gemini_key" and frame["action"] == "set" and frame["key"] == KEY and frame["id"]
            r.ws.send_text(json.dumps({"type": "gemini_key_result", "id": frame["id"], "ok": True, "fingerprint": FP}))
            t.join(5)
            assert out["res"] == {"ok": True, "fingerprint": FP, "error": ""}
            t, out = _push_in_thread(dev_id, "clear")
            frame = _next(r)
            assert frame == {"type": "gemini_key", "id": frame["id"], "action": "clear"}
            r.ws.send_text(json.dumps({"type": "gemini_key_result", "id": frame["id"], "ok": False, "error": "x" * 500, "fingerprint": "not hex"}))
            t.join(5)
            assert out["res"]["ok"] is False and out["res"]["fingerprint"] == "" and len(out["res"]["error"]) <= 120
        finally:
            r.close()
    assert KEY not in caplog.text


def test_an_unknown_or_late_result_id_is_ignored_and_the_socket_stays_up(ws_world):
    r = Runner(ws_world["client"], ws_world["dev"], ws_world["key"])
    try:
        r.handshake()
        r.ws.send_text(json.dumps({"type": "gemini_key_result", "id": "nope", "ok": True, "fingerprint": FP}))
        r.ws.send_text(json.dumps({"type": "ping"}))
        assert _next(r) == {"type": "pong"}
    finally:
        r.close()


def test_a_runner_that_is_not_connected_is_offline_nothing_is_queued(ws_world):
    with pytest.raises(runner_ws.RunnerOffline):
        runner_ws.push_gemini_key(ws_world["dev"]["device_id"], "set", KEY)
