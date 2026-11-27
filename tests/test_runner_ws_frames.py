"""The runner socket's frame limit is REAL (#829 review 3): the live container runs `uvicorn --ws-max-size 8192`, which closes the socket on any larger
frame before the app sees it. So an agent file travels in chunks, and this test runs a real uvicorn with the same limit to prove both halves: the largest
frame the runner can send is accepted, an over-limit one kills the socket (the reviewer's wst/t.py)."""
import asyncio
import base64
import json
import re
import socket
import sys
import threading
import time
from pathlib import Path

import uvicorn
import websockets
from starlette.applications import Starlette
from starlette.routing import WebSocketRoute

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runner"))

from crewboss_runner import worker  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_the_largest_runner_chunk_frame_fits_the_live_limit_and_a_bigger_one_does_not():
    dockerfile = (ROOT / "Dockerfile").read_text()
    limit = int(re.search(r'"--ws-max-size", "(\d+)"', dockerfile).group(1))
    assert limit == 8192                                                                  # nobody raised it to make room
    worst = json.dumps({"type": "agent_file", "id": "r999999", "job_id": 99999999, "fence": 99999999, "path": "p" * 300, "sha256": "f" * 64,
                        "source": "preflight", "part": 99, "parts": 99, "b64": base64.b64encode(b"\xff" * worker.REPORT_CHUNK).decode()}, separators=(",", ":"))
    assert len(worst) < 6 * 1024 < limit
    from app import runner_ws
    assert len(worst) <= runner_ws.MAX_BIG_FRAME < limit and runner_ws.MAX_BIG_FRAME > runner_ws.MAX_FRAME

    got = []

    async def endpoint(ws):
        await ws.accept()
        try:
            while True:
                got.append(len(await ws.receive_text()))
                await ws.send_text("ok")
        except Exception as exc:  # noqa: BLE001
            got.append(type(exc).__name__)

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(Starlette(routes=[WebSocketRoute("/ws", endpoint)]), host="127.0.0.1", port=port, ws_max_size=limit, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(50):
        if server.started:
            break
        time.sleep(0.1)

    async def go():
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws", max_size=None) as c:
            await c.send(worst)
            assert await c.recv() == "ok"                                                 # the worst chunk frame goes through
            await c.send('{"type":"agent_file","b64":"' + "A" * 20000 + '"}')
            try:
                await asyncio.wait_for(c.recv(), 5)
                return "answered"
            except websockets.ConnectionClosed as exc:
                return exc.rcvd.code if exc.rcvd else "closed"

    try:
        assert asyncio.run(go()) == 1009                                                   # message too big: what the old 160 KB frame met
    finally:
        server.should_exit = True
    assert got[0] == len(worst)


def _long(ch: str, n: int) -> str:
    """A path of n characters made of directory parts of 40 (a filesystem name holds 255 BYTES, so one 300-character component cannot exist)."""
    return "/".join(ch * min(40, n - i) for i in range(0, n, 40))[:n + n // 40]


def _frames_for(tmp_path, rel: str, size: int):
    """Run the runner's real _report_files for one file with a path `rel` and `size` bytes; -> the frames exactly as link.rpc serialises them."""
    import hashlib
    from types import SimpleNamespace
    wt = tmp_path / "wt"
    f = wt / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    data = bytes((i * 7 + 1) % 256 for i in range(size))
    f.write_bytes(data)
    sent = []

    class FakeLink:
        n = 0

        async def rpc(self, mtype, timeout=30.0, **fields):
            FakeLink.n += 1
            sent.append(json.dumps({"type": mtype, "id": f"r{FakeLink.n}", **fields}, separators=(",", ":")))
            return {"ok": True}

    w = worker.Worker.__new__(worker.Worker)
    w.link = FakeLink()
    meta = {"path": rel, "sha256": hashlib.sha256(data).hexdigest(), "size": size, "reviewable": True}
    why = asyncio.run(w._report_files({"job_id": 99999999, "fence": 99999999}, wt, [meta], "job"))
    return why, sent, data


def test_a_chunk_frame_never_exceeds_the_budget_whatever_the_path(tmp_path):
    from app import runner_ws
    for name, rel in (("ascii", _long("p", 300)), ("cjk", _long("\u6f22", 300)), ("emoji", _long("\U0001F600", 300)), ("mixed", "/".join(["d\u00e9\U0001F600x" * 10] * 10))):
        why, sent, data = _frames_for(tmp_path / name, rel, 90_000)
        assert why == "", name
        assert len(sent) > 1
        for fr in sent:
            n = len(fr.encode("utf-8"))
            assert n <= runner_ws.MAX_BIG_FRAME < 8192 and n <= 8192, (name, n)
            assert len(json.loads(fr)["b64"]) <= runner_ws.MAX_CHUNK_B64
        got = b"".join(base64.b64decode(json.loads(fr)["b64"]) for fr in sent)
        assert got == data, name                                              # every byte still arrives, in order
    assert worker.chunk_size(1, 1, {"path": _long("\U0001F600", 300), "sha256": "f" * 64, "source": "preflight"}) < worker.REPORT_CHUNK   # the emoji path shrinks the chunk


def test_a_path_too_long_for_any_frame_is_skipped_not_sent_oversize(tmp_path):
    why, sent, _d = _frames_for(tmp_path, _long("\U0001F600", 560), 100)         # ~6.7 KB of path alone
    assert sent == [] and why == ""
    assert worker.chunk_size(1, 1, {"path": "p" * 10, "sha256": "f" * 64, "source": "job"}) == worker.REPORT_CHUNK


# ---- #976 slice D, server side: the pinned code, tls_rotate and its acknowledgement --------------------------------------------

def _lan_state(monkeypatch, pin="q" * 26, public=None):
    from app import front_door
    st = {"version": 1, "lan": {"hosts": ["192.0.2.20", "office-pc.local"], "https_port": 443, "cert_sha256": "AA", "spki_pin26": pin, "not_after": ""},
          "public": public}
    monkeypatch.setattr(front_door, "state", lambda: st)
    monkeypatch.setattr(front_door, "public_host", lambda: public["domain"] if public else "")
    return st


def _issue(est, where="auto"):
    from test_devices import _user
    from app import devices
    return devices.issue_code(_user(), est["acme"], "subscription", where)


def test_a_lan_runner_address_makes_a_pinned_code_and_a_public_one_keeps_the_old_format(est, monkeypatch):
    from app import config, runner_identity
    fp = runner_identity.fp8()
    _lan_state(monkeypatch)
    monkeypatch.setattr(config, "RUNNER_BASE_URL", "https://192.0.2.20")
    issued = _issue(est)
    assert issued["pinned"] and issued["code"].startswith(f"192.0.2.20:443#{fp}.{'q' * 26}-")
    monkeypatch.setattr(config, "RUNNER_BASE_URL", "https://crew.example.com")   # Let's Encrypt: the old format, backward compatible
    issued = _issue(est)
    assert not issued["pinned"] and issued["code"].startswith(f"crew.example.com:443#{fp}-")


def test_where_is_this_computer_picks_the_address(est, monkeypatch):
    from app import config, runner_identity
    fp = runner_identity.fp8()
    _lan_state(monkeypatch, public={"domain": "crew.example.com", "port": 443, "email": "", "since": ""})
    monkeypatch.setattr(config, "RUNNER_BASE_URL", "https://crew.example.com")
    on_net, anywhere = _issue(est, "lan"), _issue(est, "public")
    assert on_net["code"].startswith(f"192.0.2.20:443#{fp}.{'q' * 26}-")   # the first LAN host: RUNNER_BASE_URL is not one
    assert anywhere["code"].startswith(f"crew.example.com:443#{fp}-") and not anywhere["pinned"]


def test_a_lan_code_without_a_certificate_pin_is_not_made(est, monkeypatch):
    from app import config
    from app.repo import BadRequest
    _lan_state(monkeypatch, pin="")
    monkeypatch.setattr(config, "RUNNER_BASE_URL", "https://192.0.2.20")
    with pytest.raises(BadRequest):
        _issue(est)


def test_the_enrol_route_accepts_a_pinned_code_and_refuses_a_malformed_pin(est, client, monkeypatch):
    from app import config, runner_identity
    from test_devices import _secret
    _lan_state(monkeypatch)
    monkeypatch.setattr(config, "RUNNER_BASE_URL", "https://192.0.2.20")
    pub = base64.b64encode(Ed25519PrivateKey.generate().public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    code = _issue(est)["code"]
    assert "." in code.partition("#")[2].partition("-")[0]
    r = client.post("/runner/enrol", json={"code": code, "pubkey": pub})
    assert r.status_code == 200 and r.json()["device_id"]
    bad = _issue(est)["code"].replace("#" + runner_identity.fp8() + "." + "q" * 26, "#" + runner_identity.fp8() + ".SHORT")
    assert client.post("/runner/enrol", json={"code": bad, "pubkey": pub}).status_code == 403


def _cert():
    from test_runner_tlspin import make_cert
    return make_cert()


def test_tls_rotate_is_signed_per_connection_and_only_the_matching_ack_is_stored(ws_world):
    from test_runner_ws import Runner, HOST, _next
    from test_runner_tlspin import tlspin
    old_pem, old_der, _ = _cert()
    new_pem, new_der, _ = _cert()
    r = Runner(ws_world["client"], ws_world["dev"], ws_world["key"])
    r.handshake()
    try:
        assert runner_ws_rotate(new_pem, old_der) == 1
        frame = _next(r)
        assert frame["type"] == "tls_rotate" and frame["cert_pem"] == new_pem and re.fullmatch(r"[0-9a-f]{16}", frame["id"])
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(runner_identity_pub()))
        pub.verify(base64.b64decode(frame["sig"]), tlspin.rotate_message(new_der, tlspin.spki_sha256(old_der), HOST))   # the exact bytes of D7
        # a wrong fingerprint in the ack is not stored; the right one is
        r.ws.send_text(json.dumps({"type": "tls_rotate_result", "id": frame["id"], "ok": True, "fp": "z" * 26}))
        r.ws.send_text(json.dumps({"type": "ping"}))
        assert _next(r) == {"type": "pong"}
        assert _tls_fp(ws_world["dev"]["device_id"]) is None
        frame2_count = runner_ws_rotate(new_pem, old_der)
        assert frame2_count == 1
        frame2 = _next(r)
        r.ws.send_text(json.dumps({"type": "tls_rotate_result", "id": frame2["id"], "ok": True, "fp": tlspin.pin26_of(new_der)}))
        r.ws.send_text(json.dumps({"type": "ping"}))
        assert _next(r) == {"type": "pong"}
        assert _tls_fp(ws_world["dev"]["device_id"]) == tlspin.pin26_of(new_der)
        from app import runner_ws
        assert runner_ws.tls_ready(tlspin.pin26_of(new_der)) == {"ready": 1, "total": 1, "pending": []}
    finally:
        r.close()


def test_tls_rotate_is_never_sent_to_a_socket_that_has_not_proven_its_key(ws_world):
    from test_runner_ws import Runner
    new_pem, _nd, _ = _cert()
    _op, old_der, _ = _cert()
    r = Runner(ws_world["client"], ws_world["dev"], ws_world["key"])
    r.connect()
    try:
        ch = r.hello()
        r.check_server(ch)                                   # challenged, but the runner has not answered yet
        assert runner_ws_rotate(new_pem, old_der) == 0      # not in the verified registry: no frame
    finally:
        r.close()


def runner_ws_rotate(pem, old_der):
    from app import runner_ws
    from test_runner_tlspin import tlspin
    return runner_ws.rotate_tls(pem, tlspin.spki_sha256(old_der))


def runner_identity_pub():
    from app import runner_identity
    return runner_identity.public_key()


def _tls_fp(device_id):
    from app.db import get_cursor
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT tls_fp FROM device_tokens WHERE id = %s", (device_id,))
        return cur.fetchone()["tls_fp"]


# fixtures used above come from the runner WS suite
from base64 import b64decode as _b64d  # noqa: E402,F401
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey  # noqa: E402
import pytest  # noqa: E402
from test_runner_ws import ws_world  # noqa: E402,F401
from test_devices import est  # noqa: E402,F401


def test_rotate_from_prepare_checks_the_sidecars_hashes_against_the_certificate():
    import hashlib
    from app import runner_ws
    from test_runner_tlspin import tlspin
    pem, der, _ = _cert()
    mat = {"new_spki_sha256": tlspin.spki_sha256(der).hex(), "new_cert_sha256": hashlib.sha256(der).hexdigest(),
           "old_spki_sha256": tlspin.spki_sha256(der).hex(), "host": "192.0.2.20"}   # key reused: old == new
    assert runner_ws.rotate_tls_from_prepare({"ok": True, "next_cert_pem": pem, "sig_material": mat}) == 0   # nobody connected
    for bad in ({**mat, "new_cert_sha256": "00" * 32}, {**mat, "new_spki_sha256": "11" * 32}):
        with pytest.raises(ValueError):
            runner_ws.rotate_tls_from_prepare({"ok": True, "next_cert_pem": pem, "sig_material": bad})


def test_tls_ready_counts_only_pinned_runners_seen_in_the_last_30_days(ws_world):
    from app import runner_ws
    from app.db import get_cursor
    dev = ws_world["dev"]["device_id"]
    with get_cursor() as cur:
        cur.execute("UPDATE device_tokens SET tls_fp = NULL, last_seen = now() WHERE id = %s", (dev,))
    assert runner_ws.tls_ready("a" * 26)["total"] == 0                       # never pinned: not waited for
    with get_cursor() as cur:
        cur.execute("UPDATE device_tokens SET tls_fp = %s, last_seen = now() - interval '40 days' WHERE id = %s", ("b" * 26, dev))
    assert runner_ws.tls_ready("a" * 26)["total"] == 0                       # silent for 40 days: not waited for
    with get_cursor() as cur:
        cur.execute("UPDATE device_tokens SET last_seen = now() WHERE id = %s", (dev,))
    r = runner_ws.tls_ready("a" * 26)
    assert r["total"] == 1 and r["ready"] == 0 and len(r["pending"]) == 1
    assert runner_ws.tls_ready("b" * 26)["ready"] == 1
