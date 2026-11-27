"""Runners P4 (task #627): the crewboss-runner program (runner/). A fake dashboard speaks docs/runner-protocol.md over a real
local WebSocket; a fake `claude` script stands in for Claude Code. No network beyond 127.0.0.1, no real Claude, no keyring."""
import asyncio
import base64
import http.client
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from websockets.asyncio.server import serve

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runner"))

from crewboss_runner import enrol as enrol_mod, link as link_mod, settings as settings_mod, spawn, store, worker as worker_mod  # noqa: E402
from crewboss_runner.ui import StatusServer  # noqa: E402

DEVICE_ID = 7
b64 = lambda raw: base64.b64encode(raw).decode()  # noqa: E731
raw_pub = lambda key: key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)  # noqa: E731


@pytest.fixture(autouse=True)
def runner_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CREWBOSS_RUNNER_NO_KEYRING", "1")
    monkeypatch.setenv("CREWBOSS_RUNNER_ALLOW_FILE", "1")   # local-path "remotes" in these tests; never set in production
    monkeypatch.setenv("CREWBOSS_RUNNER_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("CREWBOSS_RUNNER_STATE_DIR", str(tmp_path / "state"))


# --- a fake dashboard ------------------------------------------------------------------------------------------------

class FakeDash:
    def __init__(self, dev_key, git_remote="", server_key=None):
        self.server_key = server_key or Ed25519PrivateKey.generate()
        self.proof_key = self.server_key          # tests swap this to impersonate
        self.dev_pub = dev_key.public_key()
        self.git_remote = git_remote
        self.jobs: list[dict] = []
        self.heartbeat_reply = lambda n: {"ok": True, "lease_seconds": 90}
        self.heartbeats, self.finishes, self.hosts, self.releases = [], [], [], []
        self.agent_files = []
        self.frames, self.chunk_buf, self.agent_file_error = [], [], ""
        self.claims = 0
        self.close_after_ready = None
        self.attention = []
        self.device_proof_seen = None
        self.legacy_labels = False                # True = a server that predates the label rename

    def job_payload(self, jid=5, fence=11, max_seconds=None, pins=None):
        return {**({"max_seconds": max_seconds} if max_seconds else {}), **({"pins": pins} if pins is not None else {}), "job_id": jid, "task_id": 3, "project_id": 1, "account_id": 1, "fence": fence,
                "branch": f"dash/j{jid}-e{fence}", "job_token": "mdj_SECRETTOKEN", "lease_seconds": 90,
                "git_remote": self.git_remote, "profile": {"name": "builder", "model": "sonnet", "skills": [],
                                                           "allowed_tools": ["Bash", "Edit"]}}

    async def handler(self, ws):
        self.hosts.append(ws.request.headers["Host"])
        hello = json.loads(await ws.recv())
        self.hello_labels = hello.get("labels")
        cn = base64.b64decode(hello["client_nonce"])
        sn = os.urandom(32)
        host = ws.request.headers["Host"]
        sp = self.proof_key.sign(link_mod.transcript(
            link_mod.LEGACY_SERVER_LABEL if self.legacy_labels else link_mod.SERVER_LABEL, cn, sn, DEVICE_ID, host))
        await ws.send(json.dumps({"type": "challenge", "server_nonce": b64(sn), "server_proof": b64(sp),
                                  "server_public_key": b64(raw_pub(self.server_key)), "device_id": DEVICE_ID}))
        try:
            proof = json.loads(await asyncio.wait_for(ws.recv(), 1.5))
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001 — the runner refused to answer
            self.device_proof_seen = False
            return
        try:
            self.dev_pub.verify(base64.b64decode(proof["signature"]),
                                link_mod.transcript(link_mod.LEGACY_DEVICE_LABEL if self.legacy_labels else link_mod.DEVICE_LABEL,
                                                    cn, sn, DEVICE_ID, host))
            self.device_proof_seen = True
        except Exception:  # noqa: BLE001
            await ws.close(4401)
            return
        await ws.send(json.dumps({"type": "ready", "v": 1, "challenge_seconds": 20}))
        if self.close_after_ready:
            await ws.close(self.close_after_ready)
            return
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "claim":
                self.claims += 1
                job = self.jobs.pop(0) if self.jobs else None
                await ws.send(json.dumps({"type": "claim_result", "id": msg["id"], "ok": True, "job": job}))
            elif msg["type"] == "heartbeat":
                self.heartbeats.append(msg)
                await ws.send(json.dumps({"type": "heartbeat_result", "id": msg["id"],
                                          **self.heartbeat_reply(len(self.heartbeats))}))
            elif msg["type"] == "release":
                self.releases.append(msg)
                await ws.send(json.dumps({"type": "release_result", "id": msg["id"], "ok": True, "state": "queued"}))
            elif msg["type"] == "agent_file":
                self.frames.append(len(raw))
                reply = {"type": "agent_file_result", "id": msg["id"], "ok": True}
                if self.agent_file_error:
                    reply = {**reply, "ok": False, "status": 400, "detail": self.agent_file_error}
                elif "part" in msg:       # a chunk: reassemble like the dashboard does (the last one completes the file)
                    self.chunk_buf.append(base64.b64decode(msg["b64"]))
                    assert msg["part"] == len(self.chunk_buf) - 1
                    if msg["part"] == msg["parts"] - 1:
                        whole = {**msg, "b64": base64.b64encode(b"".join(self.chunk_buf)).decode(), "chunks": msg["parts"]}
                        self.chunk_buf = []
                        self.agent_files.append(whole)
                else:
                    self.agent_files.append(msg)
                await ws.send(json.dumps(reply))
            elif msg["type"] == "attention":
                self.attention.append(msg["kinds"])
            elif msg["type"] == "finish":
                self.finishes.append(msg)
                await ws.send(json.dumps({"type": "finish_result", "id": msg["id"], "ok": True, "state": "done"}))


async def _with_server(dash, fn):
    server = await serve(dash.handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        return await fn(port)
    finally:
        server.close()
        await server.wait_closed()


def _cfg(port):
    return {"server": f"127.0.0.1:{port}", "scheme": "http", "device_id": DEVICE_ID, "mode": "api", "name": "t",
            "server_public_key": ""}


# --- enrolment, keys ---------------------------------------------------------------------------------------------------

def test_enrol_aborts_on_fp8_mismatch_and_sends_nothing(tmp_path):
    server_key = Ed25519PrivateKey.generate()
    calls = []

    def http(method, url, body=None):
        calls.append((method, url))
        return 200, {"public_key": b64(raw_pub(server_key)), "fp8": "whatever"}   # its stated fp8 is not trusted

    secrets, config = store.Secrets(), store.Config()
    with pytest.raises(enrol_mod.EnrolError, match="does not match"):
        enrol_mod.enrol("dash.example:443#aaaaaaaa-secretsecret", "pc", "linux", secrets, config, http=http)
    assert calls == [("GET", "https://dash.example:443/runner/identity")]   # the secret and our key were never sent
    assert config.load() is None and secrets.get("device_key") is None
    with pytest.raises(enrol_mod.EnrolError):   # a bare secret has no fingerprint to pin
        enrol_mod.parse_code("justasecret")


def test_enrol_stores_key_and_token_in_a_0600_file_when_there_is_no_keyring(tmp_path):
    server_key = Ed25519PrivateKey.generate()
    fp = enrol_mod.fp8_of(raw_pub(server_key))
    sent = {}

    def http(method, url, body=None):
        if url.endswith("/identity"):
            return 200, {"public_key": b64(raw_pub(server_key))}
        sent.update(body)
        return 200, {"device_id": 9, "device_token": "mdr_tok", "expires_at": "2027-01-01", "mode": "subscription"}

    secrets, config = store.Secrets(), store.Config()
    cfg = enrol_mod.enrol(f"dash.example:443#{fp}-sekret", "pc", "linux", secrets, config, http=http)
    assert cfg["device_id"] == 9 and cfg["mode"] == "subscription" and cfg["fp8"] == fp
    assert sent["code"].endswith("-sekret") and len(base64.b64decode(sent["pubkey"])) == 32
    assert secrets.get("device_token") == "mdr_tok" and secrets.backend == "file"
    assert stat.S_IMODE(secrets.file.stat().st_mode) == 0o600 and secrets.file_mode_ok()
    assert "no OS keyring" in secrets.describe()   # `status` says so
    # the private key is the one whose public half we sent
    priv = Ed25519PrivateKey.from_private_bytes(base64.b64decode(secrets.get("device_key")))
    assert raw_pub(priv) == base64.b64decode(sent["pubkey"])


# --- the socket --------------------------------------------------------------------------------------------------------

def test_transcript_matches_the_server():
    from app import runner_ws
    args = (b"c" * 32, b"s" * 32, 42, "Dash.Example:8443")
    assert link_mod.transcript(link_mod.SERVER_LABEL, *args) == runner_ws.transcript(runner_ws.SERVER_LABEL, *args)
    assert link_mod.transcript(link_mod.DEVICE_LABEL, *args) == runner_ws.transcript(runner_ws.DEVICE_LABEL, *args)
    assert link_mod.host_header("dash.example:443", "https") == "dash.example"
    assert link_mod.host_header("dash.example:8443", "https") == "dash.example:8443"


def _link(dash, port, dev_key, **kw):
    cfg = {**_cfg(port), "server_public_key": b64(raw_pub(dash.server_key))}
    return link_mod.Link(cfg, dev_key, "mdr_tok", **kw), cfg


def test_handshake_is_mutual_and_signs_the_host_it_dialled():
    dev_key = Ed25519PrivateKey.generate()
    dash = FakeDash(dev_key)

    async def go(port):
        lk, _ = _link(dash, port, dev_key)
        task = asyncio.create_task(lk.run())
        await asyncio.wait_for(lk.ready.wait(), 5)
        assert dash.device_proof_seen and dash.hosts == [f"127.0.0.1:{port}"]
        reply = await lk.rpc("claim")
        assert reply == {"type": "claim_result", "id": reply["id"], "ok": True, "job": None}
        await lk.stop()
        await asyncio.wait_for(task, 5)

    asyncio.run(_with_server(dash, go))


def test_a_server_that_predates_the_label_rename_is_still_answered_under_the_old_labels():
    dev_key = Ed25519PrivateKey.generate()
    dash = FakeDash(dev_key)
    dash.legacy_labels = True

    async def go(port):
        lk, _ = _link(dash, port, dev_key)
        task = asyncio.create_task(lk.run())
        await asyncio.wait_for(lk.ready.wait(), 5)
        assert dash.device_proof_seen is True and dash.hello_labels == "crewboss"
        lk.stopping = True
        task.cancel()

    asyncio.run(_with_server(dash, go))


class _RawDash(FakeDash):
    """A peer that sends the given frames at once, with no challenge first, and records whether the runner ever speaks."""
    def __init__(self, dev_key, frames):
        super().__init__(dev_key)
        self.frames, self.got = frames, []

    async def handler(self, ws):
        await ws.recv()                                  # the hello
        for f in self.frames:
            await ws.send(json.dumps(f))
        try:
            async for raw in ws:
                self.got.append(json.loads(raw))
        except Exception:  # noqa: BLE001
            pass


@pytest.mark.parametrize("frames", [
    [{"type": "ready", "v": 1, "challenge_seconds": 20}],
    [{"type": "job_available", "job_id": 1, "task_id": 1}],
    [{"type": "claim_result", "id": "r1", "ok": True, "job": None}],
])
def test_a_frame_before_the_server_proof_closes_the_socket_and_nothing_is_acted_on(frames, caplog):
    dev_key = Ed25519PrivateKey.generate()
    dash = _RawDash(dev_key, frames)

    async def go(port):
        async def fake_sleep(d):
            lk.stopping = True

        lk, _ = _link(dash, port, dev_key, sleep=fake_sleep)
        await asyncio.wait_for(lk.run(), 5)
        assert not lk.ready.is_set() and lk.sessions == 0 and not lk.nudges.is_set() and lk.state != "ready"
        assert dash.got == []                              # the runner said nothing: no claim, no proof

    with caplog.at_level("DEBUG"):
        asyncio.run(_with_server(dash, go))
    ours = " ".join(r.getMessage() for r in caplog.records if r.name.startswith("crewboss-runner"))   # not the websockets library's own debug
    assert "before the server proved" in ours and "job_available" not in ours and "claim_result" not in ours and "ready" not in ours.replace("server proved", "")


def test_a_challenge_with_a_bad_proof_closes_and_a_later_ready_is_never_reached():
    dev_key = Ed25519PrivateKey.generate()
    dash = FakeDash(dev_key)
    dash.proof_key = Ed25519PrivateKey.generate()
    dash.legacy_labels = True                              # the legacy-label path must refuse a bad proof too

    async def go(port):
        async def fake_sleep(d):
            lk.stopping = True

        lk, _ = _link(dash, port, dev_key, sleep=fake_sleep)
        await asyncio.wait_for(lk.run(), 5)
        assert not lk.ready.is_set() and lk._proven is False and dash.claims == 0

    asyncio.run(_with_server(dash, go))


def test_both_label_variants_set_the_proof_flag_and_reach_ready():
    for legacy in (False, True):
        dev_key = Ed25519PrivateKey.generate()
        dash = FakeDash(dev_key)
        dash.legacy_labels = legacy

        async def go(port):
            lk, _ = _link(dash, port, dev_key)
            task = asyncio.create_task(lk.run())
            await asyncio.wait_for(lk.ready.wait(), 5)
            assert lk._proven is True
            reply = await lk.rpc("claim")
            assert reply["ok"] is True and dash.claims == 1
            await lk.stop()
            await asyncio.wait_for(task, 5)

        asyncio.run(_with_server(dash, go))


def test_an_unknown_or_odd_enrol_mode_fails_closed_to_subscription(monkeypatch):
    for odd in ("API", " Api ", "apis", "", None, 5, "garbage", "subscription"):
        sent = {}

        def http(method, url, body=None, odd=odd):
            if method == "GET":
                return 200, {"public_key": base64.b64encode(raw_pub(skey)).decode()}
            return 200, {"device_id": 9, "device_token": "mdr_tok", "expires_at": "2027-01-01", "mode": odd}

        skey = Ed25519PrivateKey.generate()
        fp = enrol_mod.fp8_of(raw_pub(skey))
        cfg = enrol_mod.enrol(f"dash.example:443#{fp}-sekret", "pc", "linux", store.Secrets(), store.Config(), http=http)
        want = "api" if isinstance(odd, str) and odd.strip().lower() == "api" else "subscription"
        assert cfg["mode"] == want, odd
    # a config that was saved before this fix may hold anything: only exactly "api" keeps ANTHROPIC_API_KEY
    base = {"PATH": "/bin", "ANTHROPIC_API_KEY": "sk-ant-x", "ANTHROPIC_AUTH_TOKEN": "t"}
    for odd in ("API", "Api", "garbage", "", None, "subscription"):
        assert "ANTHROPIC_API_KEY" not in spawn.agent_env(odd, base), odd
        assert spawn.argv_for(odd, FakeDash(Ed25519PrivateKey.generate()).job_payload(), "/s/mcp.json", "claude")[1] == "--remote-control"
    assert spawn.agent_env("api", base)["ANTHROPIC_API_KEY"] == "sk-ant-x"
    assert settings_mod.normalise_mode("API") == "api" and settings_mod.normalise_mode("garbage") == "subscription"


def test_insecure_http_is_refused_for_a_host_that_is_not_this_computer(capsys, monkeypatch):
    from crewboss_runner import cli
    called = []
    monkeypatch.setattr(enrol_mod, "enrol", lambda *a, **k: called.append(a) or {"device_id": 1, "mode": "api", "server": "x", "fp8": "y"})
    ns = lambda code, remote=False: type("A", (), {"code": code, "name": "pc", "insecure_http": True, "allow_insecure_remote": remote})()  # noqa: E731
    assert cli.cmd_enrol(ns("dash.example.test:8000#abcdefgh-sec")) == 1 and not called
    assert "refused" in capsys.readouterr().err
    assert cli.cmd_enrol(ns("192.0.2.5:8000#abcdefgh-sec")) == 1 and not called
    for ok in ("localhost:8000", "127.0.0.1:8000", "[::1]:8000"):
        assert cli.cmd_enrol(ns(ok + "#abcdefgh-sec")) == 0
    assert len(called) == 3
    assert cli.cmd_enrol(ns("dash.example.test:8000#abcdefgh-sec", remote=True)) == 0 and len(called) == 4


def test_a_userinfo_trick_is_not_seen_as_loopback(capsys, monkeypatch):
    """`localhost:80@evil.example` is the host evil.example (localhost:80 is a user name), so --insecure-http must refuse it."""
    from crewboss_runner import cli
    for tricky in ("localhost:80@evil.example", "localhost:80@evil.example:8000", "127.0.0.1:80@evil.example:8000", "[::1]:80@evil.example"):
        assert cli.is_loopback_host(tricky) is False, tricky
    for ok in ("localhost:8000", "127.0.0.1:8000", "[::1]:8000", "LOCALHOST:1", "localhost"):
        assert cli.is_loopback_host(ok) is True, ok
    assert cli.is_loopback_host("evil.example:8000") is False and cli.is_loopback_host("") is False
    called = []
    monkeypatch.setattr(enrol_mod, "enrol", lambda *a, **k: called.append(a) or {"device_id": 1, "mode": "api", "server": "x", "fp8": "y"})
    ns = type("A", (), {"code": "localhost:80@evil.example:8000#abcdefgh-sec", "name": "pc", "insecure_http": True, "allow_insecure_remote": False})()
    assert cli.cmd_enrol(ns) == 1 and not called


def test_server_proof_from_the_wrong_key_is_not_answered():
    dev_key = Ed25519PrivateKey.generate()
    dash = FakeDash(dev_key)
    dash.proof_key = Ed25519PrivateKey.generate()   # an impostor signs; the pinned key is the real one

    async def go(port):
        sleeps = []

        async def fake_sleep(d):
            sleeps.append(d)
            lk.stopping = True

        lk, _ = _link(dash, port, dev_key, sleep=fake_sleep)
        await asyncio.wait_for(lk.run(), 5)
        await asyncio.sleep(0.3)
        assert dash.device_proof_seen is False and not lk.ready.is_set()
        assert sleeps and sleeps[0] > 200   # and it waits long before trying that server again

    asyncio.run(_with_server(dash, go))


@pytest.mark.parametrize("code,state", [(4401, "revoked"), (4409, "backoff")])
def test_close_codes_stop_or_back_off_long(code, state):
    dev_key = Ed25519PrivateKey.generate()
    dash = FakeDash(dev_key)
    dash.close_after_ready = code

    async def go(port):
        sleeps = []

        async def fake_sleep(d):
            sleeps.append(d)
            lk.stopping = True

        lk, _ = _link(dash, port, dev_key, sleep=fake_sleep)
        await asyncio.wait_for(lk.run(), 5)
        assert lk.state == ("revoked" if state == "revoked" else "stopped")
        if code == 4401:
            assert sleeps == []            # revoked: stop, no reconnect
        else:
            assert sleeps and sleeps[0] >= 240   # replaced: minutes, not a loop

    asyncio.run(_with_server(dash, go))


def test_backoff_doubles_to_a_cap():
    d = None
    seen = []
    for _ in range(9):
        d = link_mod.next_delay(d, None)
        seen.append(d)
    assert seen[:4] == [1, 2, 4, 8] and seen[-1] == 60 and link_mod.next_delay(3, 4409) == 300


# --- spawning ----------------------------------------------------------------------------------------------------------

def test_agent_environment_strips_dashboard_and_subscription_secrets(monkeypatch):
    base = {"PATH": "/bin", "ANTHROPIC_API_KEY": "sk-ant-x", "ANTHROPIC_AUTH_TOKEN": "t", "MALLEN_DASH_TOKEN": "d",
            "CREWBOSS_TOKEN": "m", "CREWBOSS_RUNNER_CLAUDE": "/x", "HOME": "/h"}
    sub = spawn.agent_env("subscription", base)
    assert set(sub) == {"PATH", "HOME", "GIT_ALLOW_PROTOCOL"}
    assert spawn.git_protocols() == "file:https:ssh"   # the fixture's test-only switch
    monkeypatch.delenv("CREWBOSS_RUNNER_ALLOW_FILE")
    assert spawn.git_protocols() == "https:ssh" and spawn.agent_env("api", {})["GIT_ALLOW_PROTOCOL"] == "https:ssh"
    api = spawn.agent_env("api", base)
    assert api["ANTHROPIC_API_KEY"] == "sk-ant-x" and "MALLEN_DASH_TOKEN" not in api and "CREWBOSS_TOKEN" not in api


def test_launch_lines_per_mode_keep_the_token_out_of_argv():
    job = FakeDash(Ed25519PrivateKey.generate()).job_payload()
    api = spawn.argv_for("api", job, "/s/mcp.json", "claude")
    sub = spawn.argv_for("subscription", job, "/s/mcp.json", "claude")
    assert api[:2] == ["claude", "-p"] and "--permission-mode" in api and "dontAsk" in api
    assert sub[:3] == ["claude", "--remote-control", "dash-j5"] and "-p" not in sub
    for argv in (api, sub):
        assert "--strict-mcp-config" in argv and argv[argv.index("--mcp-config") + 1] == "/s/mcp.json"
        assert argv[argv.index("--model") + 1] == "sonnet"
        assert argv[argv.index("--settings") + 1] == spawn.JOB_SETTINGS     # no hook runs in a job (the repository's file loads anyway; agentscan blocks the rest)
        assert "--setting-sources" not in argv                                    # it would drop the repository's agents and skills too
        assert "mdj_SECRETTOKEN" not in " ".join(argv)
    tools = [a for a in api if a.startswith("--allowedTools=")][0]
    assert tools == ("--allowedTools=mcp__crewboss-job__job_brief,mcp__crewboss-job__job_comment,mcp__crewboss-job__job_tick_subtask,mcp__crewboss-job__job_finish,"
                     "mcp__crewboss-job__acquire_lock,mcp__crewboss-job__release_lock,mcp__crewboss-job__request_deploy,mcp__crewboss-job__record_preview,mcp__crewboss-job__record_check,mcp__crewboss-job__report_merge,mcp__crewboss-job__list_server_reads,mcp__crewboss-job__read_logs,mcp__crewboss-job__probe,mcp__crewboss-job__begin_external_model_use,mcp__crewboss-job__finish_external_model_use,Bash,Edit")
    assert "job_done" not in tools and "job_done" not in " ".join(api)   # the agent can't end its own job
    assert "mdj_SECRETTOKEN" in spawn.mcp_config("https://dash.example", "mdj_SECRETTOKEN")


def test_no_tool_outside_the_ceiling_can_run_silently_in_either_mode():
    """#829 review 4 (verified on Claude Code 2.1.330 against a mock Messages API): a subscription session started in AUTO mode and ran a Write
    that --allowedTools did not name. Now: an explicit `--permission-mode default` (prompts: a person approves or the watcher releases the job),
    `dontAsk` for API mode, and every dangerous tool the ceiling does not name is also --disallowedTools (a deny beats the user's own allow rules)."""
    job = FakeDash(Ed25519PrivateKey.generate()).job_payload()           # ceiling: Bash, Edit and the job's MCP tools
    api = spawn.argv_for("api", job, "/s/mcp.json", "claude")
    sub = spawn.argv_for("subscription", job, "/s/mcp.json", "claude")
    assert api[api.index("--permission-mode") + 1] == "dontAsk"
    assert sub[sub.index("--permission-mode") + 1] == "default" and "auto" not in sub and "bypassPermissions" not in " ".join(sub)
    assert sub[-1] == spawn.prompt_for(job)                                  # the prompt stays last: the variadic flags cannot swallow it
    for argv in (api, sub):
        deny = [a for a in argv if a.startswith("--disallowedTools=")]
        assert len(deny) == 1 and deny[0] == "--disallowedTools=Write,NotebookEdit,WebFetch,WebSearch,SendMessage,ListAgents,Workflow,EnterPlanMode,ExitPlanMode,AskUserQuestion,Bash(gemini:*)"   # Bash and Edit are in the ceiling; the Gemini CLI is denied in every job (#861)
        denied = set(deny[0].split("=", 1)[1].split(","))
        assert "MultiEdit" not in denied and {"SendMessage", "ListAgents"} <= denied                     # 2.1.330 has no MultiEdit; peers are never messaged
        allowed = {t for t in [a for a in argv if a.startswith("--allowedTools=")][0].split("=", 1)[1].split(",")}
        assert not denied & {t.split("(")[0] for t in allowed}               # nothing the job may use is denied
    job["profile"]["allowed_tools"] = []
    deny = [a for a in spawn.argv_for("subscription", job, "/s/mcp.json", "claude") if a.startswith("--disallowedTools=")][0]
    assert deny == "--disallowedTools=" + ",".join((*spawn.DANGEROUS_TOOLS, *spawn.ALWAYS_DENIED))    # nothing named: all of them, and the Gemini CLI
    job["profile"]["allowed_tools"] = list(spawn.DANGEROUS_TOOLS)
    assert [a for a in spawn.argv_for("api", job, "/s/mcp.json", "claude") if a.startswith("--disallowedTools")] == ["--disallowedTools=Bash(gemini:*)"]   # only the always-denied Gemini CLI


def test_a_job_cannot_message_other_sessions_or_open_a_dialog_nobody_answers():
    """#829 review 5: SendMessage / ListAgents reach the OTHER Claude sessions on the PC (silently, in both modes), and the Workflow, plan-mode
    and AskUserQuestion dialogs are not tool permission prompts the watcher releases. All are denied in both modes; the settings refuse inbound
    peer messages; a profile pattern is not part of the ceiling."""
    job = FakeDash(Ed25519PrivateKey.generate()).job_payload()
    for mode in ("api", "subscription"):
        argv = spawn.argv_for(mode, job, "/s/mcp.json", "claude")
        deny = set([a for a in argv if a.startswith("--disallowedTools=")][0].split("=", 1)[1].split(","))
        assert {"SendMessage", "ListAgents", "Workflow", "EnterPlanMode", "ExitPlanMode", "AskUserQuestion"} <= deny
        settings = json.loads(argv[argv.index("--settings") + 1])
        assert settings == {"disableAllHooks": True, "crossSessionInbound": "refuse"}
    assert "MultiEdit" not in spawn.DANGEROUS_TOOLS
    job["profile"]["allowed_tools"] = ["Read", "Bash(git *)", "Edit"]
    assert [t for t in spawn.tool_ceiling(job) if not t.startswith("mcp__")] == ["Read", "Edit"]          # a pattern is dropped, so Bash is denied
    assert "Bash" in spawn.disallowed_tools(job)


def test_the_watcher_recognises_the_workflow_plan_mode_and_question_dialogs():
    from crewboss_runner import guard
    wf = "Some output\n\u256d\u2500\u2500\n\u2502 Run a dynamic workflow?\n\u2502 steps: a, b\n\n 1. Yes\n 2. No\n"
    assert guard.permission_prompt_tool(wf) == "Workflow"
    plan = "plan text\n\u256d\u2500\n Exit plan mode?\n\n Here is the plan\n\n 1. Yes, and auto-accept edits\n 2. Yes, and manually approve edits\n 3. No, keep planning\n"
    assert guard.permission_prompt_tool(plan) == "ExitPlanMode"
    pick = "Which database?\n\n 1. Postgres\n 2. SQLite\n 3. Type something\n 4. Chat about this\n\nEnter to select \u00b7 \u2191/\u2193 to navigate \u00b7 Esc to cancel\n"
    assert guard.permission_prompt_tool(pick) == "AskUserQuestion"
    # prose that only mentions them, or a dialog drawn over, is not one
    assert guard.permission_prompt_tool("I will ask: Run a dynamic workflow? and then carry on with a lot of other work " + "x " * 400) is None
    assert guard.permission_prompt_tool(wf + "\n" + "later output line " * 60) is None
    assert guard.permission_prompt_tool("I like to Chat about this. Enter to select the file, then more text follows here and on.") is None


# --- a whole job -------------------------------------------------------------------------------------------------------

def _remote(tmp_path, files=None):
    repo = tmp_path / "remote"
    repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    for rel, text in (files or {}).items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-q", "--allow-empty", "-m", "base"]):
        subprocess.run(["git", *args], cwd=repo, check=True, env=env)
    return repo


def _fake_claude(tmp_path, how):
    script = tmp_path / "fake-claude"
    script.write_text(f"""#!{sys.executable}
import json, os, subprocess, sys, time
if sys.argv[1:3] == ["auth", "status"]:   # the signed-in check: only the exit code is used; the output is never read
    print('{{"loggedIn": false, "email": "someone@example.test"}}')
    time.sleep(float(os.environ.get("FAKE_CLAUDE_AUTH_SLEEP", "0")))
    sys.exit(int(os.environ.get("FAKE_CLAUDE_AUTH", "0")))
if {how!r} == "login":   # signed out: the login screen, then it waits for a person
    print("Welcome to Claude Code\\nSelect login method:\\n 1. Claude account with subscription", flush=True)
    time.sleep(120)
if {how!r}.startswith("trust"):   # the folder-trust dialog, words split by cursor moves like the real one
    shown = "/somewhere/else/work" if {how!r} == "trust_other" else os.getcwd()
    def dialog():
        print("Quick safety check\\r\\nAccessing workspace: " + shown + "\\r\\n1. No, exit\\r\\n2. Yes,\\x1b[9GI\\x1b[11Gtrust this folder", flush=True)
    dialog()
    for _ in range(100):
        line = sys.stdin.readline()
        if {how!r} == "trust_stuck":
            dialog()   # keys ignored: the dialog is drawn again
            continue
        if "\\x1b[B" in line:
            break
        if {how!r} == "trust_other":
            open(os.environ["FAKE_CLAUDE_OUT"] + ".answered", "w").write("answered")
            continue
if {how!r} == "exit":    # dies at once, says nothing
    sys.exit(3)
if {how!r} == "silent":  # alive but never registers
    time.sleep(120)
print("\\x1b[1mRemote Control active\\x1b[0m", flush=True)
if {how!r}.startswith("perm"):   # a tool outside --allowedTools: the permission prompt, which nobody answers
    tool = "Skill(mallen-design)" if {how!r} == "perm" else "server - lookup (MCP)(q: 1)"
    def prompt():
        print("Tool use\\r\\n  " + tool + "\\r\\n\\r\\nDo you want to proceed?\\r\\n> 1. Yes\\r\\n  2. Yes, and don't ask again for " + tool + "\\r\\n  3. No", flush=True)
    prompt()
    if {how!r} == "perm_answered":   # a person approves it on the phone within the grace: more output follows, the job goes on
        time.sleep(0.5)
        print("Skill ran. " * 30, flush=True)
    else:
        time.sleep(120)
out = os.environ["FAKE_CLAUDE_OUT"]
cwd = os.getcwd()
json.dump({{"argv": sys.argv[1:], "cwd": cwd, "env": sorted(os.environ), "pid": os.getpid(),
           "tty": os.isatty(0),
           "mcp": open(sys.argv[sys.argv.index("--mcp-config") + 1]).read(),
           "mcp_mode": oct(os.stat(sys.argv[sys.argv.index("--mcp-config") + 1]).st_mode & 0o777)}}, open(out, "w"))
if {how!r} == "hang":
    time.sleep(120)
open("work.txt", "w").write("done by the fake agent")
if {how!r} in ("agentfile", "agentfile_hang"):   # the agent writes a new agent file and changes a pinned one
    os.makedirs(".claude/agents", exist_ok=True)
    open(".claude/agents/new.md", "w").write("---\\nname: new\\n---\\nhello\\n")
    open(".claude/agents/erp-builder.md", "a").write("more\\n")
g = ["git", "-c", "user.name=a", "-c", "user.email=a@a"]
subprocess.run(g + ["add", "-A"], check=True)
subprocess.run(g + ["commit", "-q", "-m", "agent work"], check=True)
if {how!r} in ("commit_hang", "agentfile_hang"):   # committed, never called job_finish, then sits at the prompt until the time limit
    time.sleep(120)
if {how!r}.startswith("finish"):   # called job_finish and now sits at the prompt, like the real session
    mark = os.environ["FAKE_CLAUDE_OUT"] + ".typed"
    if {how!r} == "finish_ignore":   # never reads the prompt: only a signal ends it
        time.sleep(120)
    if {how!r} == "finish_perm":     # after job_finish the kept-open session shows a permission dialog nobody answers
        print("Tool use\\r\\n  Skill(x)\\r\\n\\r\\nDo you want to proceed?\\r\\n> 1. Yes\\r\\n  2. Yes, and don't ask again for Skill\\r\\n  3. No", flush=True)
        time.sleep(120)
    import select
    typed = ""
    end = time.time() + float(os.environ.get("FAKE_CLAUDE_WAIT", "60"))
    while time.time() < end:    # a runner that TYPES anything into the session shows up in the marker file
        if select.select([sys.stdin], [], [], 0.1)[0]:
            typed += sys.stdin.readline()
            open(mark, "w").write(typed)
    if {how!r} == "finish_crash":
        sys.exit(3)
    if {how!r} == "finish_143":
        sys.exit(143)
""")
    script.chmod(0o755)
    return str(script)


async def _run_one(tmp_path, monkeypatch, how, mode="api", hb=lambda n: {"ok": True, "lease_seconds": 90}, hb_every=0.1,
                   max_seconds=None, pins=None, remote_files=None, remote=None, agent_file_error="", **worker_kw):
    out = tmp_path / "claude-out.json"
    monkeypatch.setenv("FAKE_CLAUDE_OUT", str(out))
    monkeypatch.setenv("CREWBOSS_TEST_SECRET", "x")
    monkeypatch.setenv("MALLEN_DASH_TOKEN", "should-not-reach-the-agent")
    dev_key = Ed25519PrivateKey.generate()
    remote = remote or _remote(tmp_path, remote_files)
    dash = FakeDash(dev_key, git_remote=str(remote))
    dash.jobs.append(dash.job_payload(max_seconds=max_seconds, pins=pins))
    dash.heartbeat_reply = hb
    dash.agent_file_error = agent_file_error
    state = tmp_path / "state"

    async def go(port):
        lk, cfg = _link(dash, port, dev_key)
        cfg["mode"] = mode
        w = worker_mod.Worker(lk, cfg, state, worker_mod.Status(), f"http://127.0.0.1:{port}",
                              claude=_fake_claude(tmp_path, how), heartbeat_every=hb_every, idle_poll=0.2, **worker_kw)
        t1, t2 = asyncio.create_task(lk.run()), asyncio.create_task(w.loop())
        deadline = time.monotonic() + 30
        while not w.results and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        w.stopping = True
        await lk.stop()
        await asyncio.wait_for(t1, 5)
        t2.cancel()
        return w

    w = await _with_server(dash, go)
    return w, dash, remote, out, state


def _branches(remote):
    return subprocess.run(["git", "branch", "--list", "dash/*"], cwd=remote, capture_output=True, text=True).stdout.split()


def test_job_runs_in_a_worktree_pushes_the_branch_and_finishes(tmp_path, monkeypatch):
    w, dash, remote, out, state = asyncio.run(_run_one(tmp_path, monkeypatch, "commit"))
    assert w.results == [(5, "done")]
    assert _branches(remote) == ["dash/j5-e11"]
    assert subprocess.run(["git", "show", "dash/j5-e11:work.txt"], cwd=remote, capture_output=True, text=True).stdout \
        == "done by the fake agent"
    assert [(f["job_id"], f["fence"], f["ok"]) for f in dash.finishes] == [(5, 11, True)]
    seen = json.loads(out.read_text())
    assert seen["cwd"].endswith("j5-e11/work") and "mdj_SECRETTOKEN" not in " ".join(seen["argv"])
    assert "mdj_SECRETTOKEN" in seen["mcp"] and seen["mcp_mode"] == "0o600"
    assert not (set(seen["env"]) & {"MALLEN_DASH_TOKEN", "CREWBOSS_TEST_SECRET"})
    # the token file (mcp.json) lived OUTSIDE the worktree and is gone after the job
    assert not list(state.rglob("mcp.json"))
    assert "-p" in seen["argv"] and seen["tty"] is False


def test_subscription_mode_runs_remote_control_under_a_pty(tmp_path, monkeypatch):
    w, dash, remote, out, state = asyncio.run(_run_one(tmp_path, monkeypatch, "commit", mode="subscription"))
    assert w.results == [(5, "done")]
    seen = json.loads(out.read_text())
    assert seen["argv"][:2] == ["--remote-control", "dash-j5"] and seen["tty"] is True


def test_stale_fence_kills_the_agent_and_pushes_nothing(tmp_path, monkeypatch):
    stale = lambda n: {"ok": True, "lease_seconds": 90} if n < 2 else {"ok": False, "status": 409, "detail": "stale"}  # noqa: E731
    w, dash, remote, out, state = asyncio.run(_run_one(tmp_path, monkeypatch, "hang", hb=stale))
    assert w.results == [(5, "lost")]
    assert _branches(remote) == [] and dash.finishes == []   # nothing pushed, nothing finished
    pid = json.loads(out.read_text())["pid"]
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)   # the agent is gone
    assert not list(state.rglob("mcp.json"))


def test_stale_fence_at_the_end_pushes_nothing(tmp_path, monkeypatch):
    """The agent finished its commit, but the job was given away meanwhile: the final heartbeat 409s, so no push."""
    late = lambda n: {"ok": True, "lease_seconds": 90} if n < 1 else {"ok": False, "status": 409, "detail": "stale"}  # noqa: E731
    w, dash, remote, out, state = asyncio.run(_run_one(tmp_path, monkeypatch, "commit", hb=late, hb_every=30))
    assert w.results == [(5, "lost")] and _branches(remote) == [] and dash.finishes == []


# --- job_finish: the session ends when the agent is done (#775 / #769) -----------------------------------------------------

def _finish_hb(outcome="done", after=5, **extra):
    return lambda n: {"ok": True, "lease_seconds": 90, **({"finish": outcome, **extra} if n >= after else {})}


def _typed(out):
    p = Path(str(out) + ".typed")
    return p.read_text() if p.exists() else ""


def test_job_finish_stops_the_session_by_signal_types_nothing_then_pushes_and_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_mod, "FINISH_GRACE", 1.0)
    sent = []
    orig = spawn.Agent.send
    monkeypatch.setattr(spawn.Agent, "send", lambda self, data: (sent.append(data), orig(self, data))[1])
    w, dash, remote, out, state = asyncio.run(_run_one(tmp_path, monkeypatch, "finish_exit", mode="subscription", hb=_finish_hb()))
    assert sent == [] and _typed(out) == ""    # the runner wrote NOTHING to the pty (no /exit, no Enter)
    assert w.results == [(5, "done")]
    assert _branches(remote) == ["dash/j5-e11"]    # the push still ran
    assert [(f["ok"], f.get("outcome")) for f in dash.finishes] == [(True, "done")]
    assert "agent finished (done)" in dash.finishes[0]["summary"] and "pushed 1 commit" in dash.finishes[0]["summary"]


def test_job_finish_blocked_is_reported_as_blocked_not_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_mod, "FINISH_GRACE", 1.0)
    w, dash, remote, out, state = asyncio.run(
        _run_one(tmp_path, monkeypatch, "finish_exit", mode="subscription", hb=_finish_hb("blocked")))
    assert w.results == [(5, "blocked")]
    assert [(f["ok"], f.get("outcome")) for f in dash.finishes] == [(True, "blocked")]


def test_a_kill_after_job_finish_is_not_a_failure(tmp_path, monkeypatch):
    """The session ignores /exit, so SIGTERM ends it (exit -15 / 143): still a success, and the commits are pushed."""
    monkeypatch.setattr(worker_mod, "FINISH_GRACE", 1.0)
    w, dash, remote, out, state = asyncio.run(_run_one(tmp_path, monkeypatch, "finish_ignore", mode="subscription", hb=_finish_hb()))
    assert w.results == [(5, "done")] and _branches(remote) == ["dash/j5-e11"]
    assert dash.finishes[0]["ok"] is True and "agent exit" not in dash.finishes[0]["summary"]


def test_without_job_finish_a_killed_agent_is_still_a_failure(tmp_path, monkeypatch):
    w, dash, remote, out, state = asyncio.run(_run_one(tmp_path, monkeypatch, "hang", max_seconds=1.0))
    assert w.results == [(5, "failed")] and dash.finishes[0]["ok"] is False


def test_keep_open_leaves_the_session_open_after_job_finish(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_WAIT", "2")   # the "person" closes it themselves after 2 s
    w, dash, remote, out, state = asyncio.run(
        _run_one(tmp_path, monkeypatch, "finish_exit", mode="subscription", hb=_finish_hb(keep_open=True)))
    assert _typed(out) == ""   # the runner typed nothing
    assert w.results == [(5, "done")] and dash.finishes[0]["ok"] is True      # its own exit, after the finish: a success
    assert _branches(remote) == ["dash/j5-e11"]


@pytest.mark.parametrize("how", ["finish_crash", "finish_143"])
def test_an_agent_crash_after_job_finish_is_still_a_failure(tmp_path, monkeypatch, how):
    """keep_open: the runner never signalled it, so a non-zero exit (even 143) is the agent's own and keeps its code."""
    monkeypatch.setenv("FAKE_CLAUDE_WAIT", "1.5")
    w, dash, remote, out, state = asyncio.run(
        _run_one(tmp_path, monkeypatch, how, mode="subscription", hb=_finish_hb(keep_open=True)))
    assert w.results == [(5, "failed")] and dash.finishes[0]["ok"] is False and "agent exit" in dash.finishes[0]["summary"]
    assert "outcome" not in dash.finishes[0]


def test_the_runner_writes_to_the_pty_only_in_the_trust_prompt_answer():
    import inspect
    from crewboss_runner import guard as guard_mod
    pkg = Path(worker_mod.__file__).parent
    writers = {f.name: f.read_text().count("agent.send") for f in pkg.glob("*.py") if "agent.send" in f.read_text()}
    assert writers == {"guard.py": 1}
    src = inspect.getsource(guard_mod.watch)   # and that one write is the trust answer, for the job's own worktree only
    assert "trust_prompt_is_for(full, worktree)" in src and 'verdict == "trust"' in src and "TRUST_KEYS" in src


def test_keep_open_session_that_hits_the_cap_after_finish_is_not_a_failure(tmp_path, monkeypatch):
    w, dash, remote, out, state = asyncio.run(
        _run_one(tmp_path, monkeypatch, "finish_ignore", mode="subscription", hb=_finish_hb(keep_open=True), max_seconds=2.0))
    assert w.results == [(5, "done")] and dash.finishes[0]["ok"] is True


def test_a_permission_prompt_on_a_kept_open_session_after_job_finish_does_not_release_the_job(tmp_path, monkeypatch):
    """keep_open + job_finish seen: the work is done. The permission watcher must not requeue it (that would remove the worktree and
    lose the finished commits); the job ends through the normal push/finish path."""
    w, dash, remote, out, state = asyncio.run(
        _run_one(tmp_path, monkeypatch, "finish_perm", mode="subscription", hb=_finish_hb(keep_open=True), watch_window=20,
                 watch_poll=0.1, permission_grace=0.5))
    assert dash.releases == []
    assert w.results == [(5, "done")] and dash.finishes[0]["ok"] is True
    assert _branches(remote) == ["dash/j5-e11"]


# --- the local status page ---------------------------------------------------------------------------------------------

def _req(port, method, path, headers=None, body=None, host=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    h = {"Host": host or f"127.0.0.1:{port}", **(headers or {})}
    c.request(method, path, body=body, headers=h)
    r = c.getresponse()
    data = r.read()
    return r.status, dict(r.getheaders()), data


def test_status_page_host_origin_bootstrap_and_no_cookies():
    ui = StatusServer(lambda: {"link": "ready", "events": []}).start()
    try:
        p = ui.port
        assert ui.url.startswith(f"http://127.0.0.1:{p}/#") and ui.httpd.server_address[0] == "127.0.0.1"
        assert _req(p, "GET", "/")[0] == 200
        assert _req(p, "GET", "/", host="evil.example")[0] == 403                     # DNS rebinding
        assert _req(p, "GET", "/", host=f"evil.example:{p}")[0] == 403
        ex = json.dumps({"token": ui.bootstrap})
        assert _req(p, "POST", "/api/exchange", {"Origin": "http://evil.example"}, ex)[0] == 403   # cross-origin
        assert _req(p, "POST", "/api/exchange", {"Sec-Fetch-Site": "cross-site"}, ex)[0] == 403
        assert _req(p, "POST", "/api/exchange", body=json.dumps({"token": "wrong"}))[0] == 403
        status, headers, body = _req(p, "POST", "/api/exchange", {"Origin": f"http://127.0.0.1:{p}"}, ex)
        assert status == 200
        session = json.loads(body)["session"]
        assert _req(p, "POST", "/api/exchange", body=ex)[0] == 403                    # one use only
        assert _req(p, "GET", "/api/status")[0] == 401
        assert _req(p, "GET", "/api/status", {"Authorization": "Bearer nope"})[0] == 401
        assert _req(p, "GET", "/api/status", {"Cookie": f"cb_session_{p}={session}"})[0] == 401   # a cookie is never a login
        s, h, b = _req(p, "GET", "/api/status", {"Authorization": f"Bearer {session}"})
        assert s == 200 and json.loads(b)["link"] == "ready"
        assert _req(p, "GET", "/api/status", {"Authorization": f"Bearer {session}", "Origin": "http://evil.example"})[0] == 403
        for _s, hdrs, _b in (_req(p, "GET", "/"), (s, h, b), (status, headers, body)):
            assert not [k for k in hdrs if k.lower() == "set-cookie"]
    finally:
        ui.stop()


def test_max_duration_kills_a_hung_agent_and_finishes_it_failed(tmp_path, monkeypatch):
    w, dash, remote, out, state = asyncio.run(_run_one(tmp_path, monkeypatch, "hang", max_seconds=1))
    assert w.results == [(5, "failed")]
    assert [(f["ok"], "maximum duration" in f["summary"]) for f in dash.finishes] == [(False, True)]
    assert _branches(remote) == []
    pid = json.loads(out.read_text())["pid"]
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_cancelling_the_worker_kills_the_agent_before_the_worktree_goes(tmp_path, monkeypatch):
    out = tmp_path / "claude-out.json"
    monkeypatch.setenv("FAKE_CLAUDE_OUT", str(out))
    dev_key = Ed25519PrivateKey.generate()
    dash = FakeDash(dev_key, git_remote=str(_remote(tmp_path)))
    dash.jobs.append(dash.job_payload())

    async def go(port):
        lk, cfg = _link(dash, port, dev_key)
        w = worker_mod.Worker(lk, cfg, tmp_path / "state", worker_mod.Status(), f"http://127.0.0.1:{port}",
                              claude=_fake_claude(tmp_path, "hang"), heartbeat_every=0.1, idle_poll=0.2)
        t1, t2 = asyncio.create_task(lk.run()), asyncio.create_task(w.loop())
        deadline = time.monotonic() + 15
        while not out.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        assert out.exists(), "agent never started"
        pid = json.loads(out.read_text())["pid"]
        w.stopping = True   # what run_async does on revoke / SIGINT / SIGTERM
        t2.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(t2, 10)
        await lk.stop()
        await asyncio.wait_for(t1, 5)
        try:
            os.kill(pid, 0)
            alive = True
        except ProcessLookupError:
            alive = False
        return alive

    assert asyncio.run(_with_server(dash, go)) is False, "agent orphaned after the worker was cancelled"
    assert not list((tmp_path / "state").rglob("mcp.json"))


def test_preflight_failure_claims_nothing_and_is_reported(tmp_path, monkeypatch):
    dev_key = Ed25519PrivateKey.generate()
    dash = FakeDash(dev_key, git_remote=str(_remote(tmp_path)))
    dash.jobs.append(dash.job_payload())

    async def go(port):
        lk, cfg = _link(dash, port, dev_key)
        status = worker_mod.Status(tmp_path / "status.json")
        w = worker_mod.Worker(lk, cfg, tmp_path / "state", status, f"http://127.0.0.1:{port}",
                              claude=str(tmp_path / "no-such-claude"), idle_poll=0.1)
        t1, t2 = asyncio.create_task(lk.run()), asyncio.create_task(w.loop())
        await asyncio.wait_for(lk.ready.wait(), 5)
        await asyncio.sleep(0.6)
        w.stopping = True
        await lk.stop()
        await asyncio.wait_for(t1, 5)
        t2.cancel()
        return status

    status = asyncio.run(_with_server(dash, go))
    assert dash.claims == 0 and len(dash.jobs) == 1   # the queued job was never taken
    assert "not installed" in status.data["preflight"][0]
    assert "not installed" in json.loads((tmp_path / "status.json").read_text())["preflight"][0]   # what `status` prints


def test_start_failure_releases_the_job_and_backs_off(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_OUT", str(tmp_path / "o.json"))
    dev_key = Ed25519PrivateKey.generate()
    dash = FakeDash(dev_key, git_remote=str(tmp_path / "does-not-exist"))   # the remote is unreachable
    dash.jobs.append(dash.job_payload(jid=5, fence=11))
    dash.jobs.append(dash.job_payload(jid=6, fence=12))

    async def go(port):
        lk, cfg = _link(dash, port, dev_key)
        w = worker_mod.Worker(lk, cfg, tmp_path / "state", worker_mod.Status(), f"http://127.0.0.1:{port}",
                              claude=_fake_claude(tmp_path, "commit"), idle_poll=0.2, failure_backoff=3.0)
        t1, t2 = asyncio.create_task(lk.run()), asyncio.create_task(w.loop())
        deadline = time.monotonic() + 15
        while not w.results and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        await asyncio.sleep(1.0)   # well inside the 3 s backoff: the second job must not have been claimed
        claims_during_backoff = dash.claims
        w.stopping = True
        await lk.stop()
        await asyncio.wait_for(t1, 5)
        t2.cancel()
        return w, claims_during_backoff

    w, claims = asyncio.run(_with_server(dash, go))
    assert w.results == [(5, "released")] and claims == 1 and len(dash.jobs) == 1
    assert [(r["job_id"], r["fence"]) for r in dash.releases] == [(5, 11)] and dash.finishes == []   # given back, not failed
    assert "could not start" in dash.releases[0]["reason"]


# --- subscription mode: signed-in preflight and the session-start watchdog (#627) --------------------------------------

def test_signed_in_uses_only_the_exit_code(tmp_path, monkeypatch):
    from crewboss_runner import guard
    monkeypatch.setenv("FAKE_CLAUDE_OUT", str(tmp_path / "o.json"))
    claude = _fake_claude(tmp_path, "commit")
    assert guard.signed_in(claude) is True
    monkeypatch.setenv("FAKE_CLAUDE_AUTH", "1")
    assert guard.signed_in(claude) is False
    monkeypatch.setenv("FAKE_CLAUDE_AUTH", "2")
    assert guard.signed_in(claude) is None                    # undecided never blocks
    monkeypatch.setenv("FAKE_CLAUDE_AUTH", "0")
    monkeypatch.setenv("FAKE_CLAUDE_AUTH_SLEEP", "5")
    assert guard.signed_in(claude, timeout=0.5) is None       # a hung check never blocks either
    assert guard.signed_in(str(tmp_path / "nope")) is None


def test_log_scan_strips_escapes_and_finds_the_patterns(tmp_path):
    from crewboss_runner import guard
    log = tmp_path / "agent.log"
    log.write_bytes(b"\x1b[1mSelect\x1b[0m  login\r\n method:")
    assert guard.scan(guard.log_text(str(log))) == "login"
    log.write_bytes(b"Remote Control cannot start (x). Run")
    assert guard.scan(guard.log_text(str(log))) == "failure"
    log.write_bytes(b"Yes, I trust this folder")
    assert guard.scan(guard.log_text(str(log))) == "trust"
    log.write_bytes(b"connecting\n\x1b[32mRemote Control active\x1b[0m")
    assert guard.scan(guard.log_text(str(log))) == "registered" and guard.scan("") == ""


def test_preflight_blocks_a_signed_out_subscription_runner_but_not_an_api_one(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_OUT", str(tmp_path / "o.json"))
    monkeypatch.setenv("FAKE_CLAUDE_AUTH", "1")
    dev_key = Ed25519PrivateKey.generate()
    dash = FakeDash(dev_key)
    lk, cfg = _link(dash, 1, dev_key)
    claude = _fake_claude(tmp_path, "commit")
    sub = worker_mod.Worker(lk, {**cfg, "mode": "subscription"}, tmp_path / "s", worker_mod.Status(), "http://x", claude=claude)
    problems = sub.preflight()
    assert problems == ["Claude Code is not signed in: run `claude` on this PC and log in"] and sub.problem_kinds == ["not_signed_in"]
    api = worker_mod.Worker(lk, {**cfg, "mode": "api"}, tmp_path / "s", worker_mod.Status(), "http://x", claude=claude)
    assert api.preflight() == [] and api.problem_kinds == []
    monkeypatch.setenv("FAKE_CLAUDE_AUTH", "0")                # the person logs in: the next check clears it
    assert sub.preflight() == [] and sub.problem_kinds == []


def test_signed_out_runner_claims_nothing_tells_the_dashboard_then_clears(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_OUT", str(tmp_path / "o.json"))
    monkeypatch.setenv("FAKE_CLAUDE_AUTH", "1")
    dev_key = Ed25519PrivateKey.generate()
    dash = FakeDash(dev_key, git_remote=str(_remote(tmp_path)))
    dash.jobs.append(dash.job_payload())

    async def go(port):
        lk, cfg = _link(dash, port, dev_key)
        cfg["mode"] = "subscription"
        status = worker_mod.Status()
        w = worker_mod.Worker(lk, cfg, tmp_path / "state", status, f"http://127.0.0.1:{port}",
                              claude=_fake_claude(tmp_path, "commit"), idle_poll=0.2, heartbeat_every=0.1)
        t1, t2 = asyncio.create_task(lk.run()), asyncio.create_task(w.loop())
        await asyncio.wait_for(lk.ready.wait(), 5)
        await asyncio.sleep(0.8)
        blocked = (dash.claims, list(dash.attention), list(status.data["preflight"]))
        monkeypatch.setenv("FAKE_CLAUDE_AUTH", "0")            # signed in now
        deadline = time.monotonic() + 15
        while not w.results and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        w.stopping = True
        await lk.stop()
        await asyncio.wait_for(t1, 5)
        t2.cancel()
        return blocked, w

    blocked, w = asyncio.run(_with_server(dash, go))
    assert blocked[0] == 0 and blocked[1] == [["not_signed_in"]] and "not signed in" in blocked[2][0]   # sent once, not every poll
    assert dash.attention == [["not_signed_in"], []] and w.results == [(5, "done")]


def test_watchdog_releases_a_job_whose_claude_sits_at_a_login_prompt(tmp_path, monkeypatch):
    t0 = time.monotonic()
    w, dash, remote, out, state = asyncio.run(_run_one(tmp_path, monkeypatch, "login", mode="subscription",
                                                       watch_window=20, watch_poll=0.1))
    assert time.monotonic() - t0 < 15                          # seen at once, not after the window
    assert w.results == [(5, "released")] and dash.finishes == [] and _branches(remote) == []
    assert [r["reason"] for r in dash.releases] == ["Claude Code needs you to sign in on t"]


def _sub(tmp_path, n):
    d = tmp_path / f"run{n}"
    d.mkdir()
    return d


def test_watchdog_releases_a_session_that_exits_or_never_registers(tmp_path, monkeypatch):
    w, dash, *_ = asyncio.run(_run_one(_sub(tmp_path, 1), monkeypatch, "exit", mode="subscription", watch_window=20, watch_poll=0.1))
    assert w.results == [(5, "released")] and dash.finishes == []
    assert [r["reason"] for r in dash.releases] == ["the Claude Code session did not start"]
    monkeypatch.setenv("CREWBOSS_RUNNER_REGISTER_CHECK", "1")    # the no-marker rule is off by default until the markers are confirmed
    w, dash, *_ = asyncio.run(_run_one(_sub(tmp_path, 2), monkeypatch, "silent", mode="subscription", watch_window=1.0, watch_poll=0.1))
    assert w.results == [(5, "released")] and dash.finishes == []
    assert [r["reason"] for r in dash.releases] == ["the Claude Code session did not start"]


def test_watchdog_leaves_a_registered_session_alone_and_can_be_switched_off(tmp_path, monkeypatch):
    w, dash, *_ = asyncio.run(_run_one(_sub(tmp_path, 3), monkeypatch, "commit", mode="subscription", watch_window=20, watch_poll=0.1))
    assert w.results == [(5, "done")] and dash.releases == []
    monkeypatch.setenv("CREWBOSS_RUNNER_REGISTER_CHECK", "0")    # no marker needed: only login text or an early exit counts
    w, dash, *_ = asyncio.run(_run_one(_sub(tmp_path, 4), monkeypatch, "silent", mode="subscription", watch_window=1.0, watch_poll=0.1,
                                       max_seconds=3))
    assert dash.releases == [] and [f["ok"] for f in dash.finishes] == [False]   # ran to its max duration instead


# --- folder trust: the runner answers it for its OWN worktree only, and never treats a visible prompt as healthy ------------

def test_trust_prompt_for_the_jobs_own_worktree_is_answered_once(tmp_path, monkeypatch):
    w, dash, remote, out, state = asyncio.run(_run_one(tmp_path, monkeypatch, "trust", mode="subscription",
                                                       watch_window=20, watch_poll=0.1))
    assert w.results == [(5, "done")] and dash.releases == [] and _branches(remote) != []   # answered, session went on


def test_trust_prompt_for_another_path_is_never_answered(tmp_path, monkeypatch):
    w, dash, remote, out, state = asyncio.run(_run_one(tmp_path, monkeypatch, "trust_other", mode="subscription",
                                                       watch_window=2.0, watch_poll=0.1))
    assert w.results == [(5, "released")] and dash.finishes == []
    assert [r["reason"] for r in dash.releases] == ["Claude Code is waiting for you to trust the job folder on t"]
    assert not (tmp_path / "claude-out.json.answered").exists()   # no key was ever typed (the fake notes any line it got)


def test_trust_prompt_still_showing_after_the_answer_fails_the_job(tmp_path, monkeypatch):
    from crewboss_runner import guard
    monkeypatch.setattr(guard, "TRUST_SETTLE", 1.0)
    w, dash, *_ = asyncio.run(_run_one(tmp_path, monkeypatch, "trust_stuck", mode="subscription", watch_window=30, watch_poll=0.1))
    assert w.results == [(5, "released")] and dash.finishes == []
    assert [r["reason"] for r in dash.releases] == ["Claude Code is waiting for you to trust the job folder on t"]


def test_unanswered_trust_prompt_with_the_register_check_off_is_not_healthy(tmp_path, monkeypatch):
    from crewboss_runner import guard
    log = tmp_path / "agent.log"
    log.write_bytes(b"Yes,\x1b[9GI\x1b[11Gtrust this folder")
    assert guard.scan(guard.log_text(str(log))) == "trust"
    assert guard.trust_prompt_is_for(guard.log_text(str(log)) + " /a/b/work", "/a/b/work")
    assert not guard.trust_prompt_is_for("/a/b/other", "/a/b/work")

    class A:   # no worktree given: nothing is typed and the verdict is trust at the end of the window
        proc = type("P", (), {"poll": lambda s: None})()
        log_path = str(log)
        sent = []
        def send(self, d): self.sent.append(d)
    a = A()
    assert asyncio.run(guard.watch(a, "dash-j1", window=0.5, poll=0.1)) == "trust" and a.sent == []


# --- a tool outside --allowedTools raises a permission prompt: the job is given back with the tool named; never auto-approved (#829) ----

def test_permission_prompt_detection_names_the_tool_and_ignores_a_prompt_that_was_answered():
    from crewboss_runner import guard
    ask = "Tool use\n  Skill(mallen-design)\n\nDo you want to proceed?\n> 1. Yes\n  2. Yes, and don't ask again for Skill\n  3. No"
    assert guard.permission_prompt_tool(ask) == "Skill"
    assert guard.permission_prompt_tool(ask.replace("Skill(mallen-design)", "Bash(rm -rf build)")) == "Bash"
    assert guard.permission_prompt_tool(ask.replace("Skill(mallen-design)", "knowledge - search (MCP)(term: x)")) == "mcp__knowledge__search"
    assert guard.permission_prompt_tool(ask.replace("Skill(mallen-design)", "mcp__knowledge__get_doc")) == "mcp__knowledge__get_doc"
    assert guard.permission_prompt_tool("Do you want to proceed?\n1. Yes\n2. Yes, and don't ask again\n3. No") == "an unlisted tool"
    assert guard.permission_prompt_tool(ask + " " + "later output " * 60) is None     # drawn over: not pending any more
    assert guard.permission_prompt_tool("Quick safety check ... Yes, I trust this folder") is None
    assert guard.scan(guard.log_text("/nonexistent")) == ""


def test_agent_prose_with_the_question_is_not_a_permission_prompt():
    from crewboss_runner import guard
    prose = ("I drafted migration 0110 and ran Bash(pytest) on it, then updated Edit(app/x.py).\n"
             "The schema change adds a column. Do you want to proceed?")
    assert guard.permission_prompt_tool(prose) is None
    assert guard.permission_prompt_tool(prose + "\n> ") is None
    # the real dialog after such prose: the tool comes from inside the dialog, not from the prose above it
    real = prose + "\n\n\u256d\u2500\u2500\u2500\u2500\u256e\n\u2502 Bash command\n\u2502   rm -rf build\n\u2502 Do you want to proceed?\n\u2502 \u276f 1. Yes\n\u2502   2. No\n"
    assert guard.permission_prompt_tool(prose.replace("Bash(pytest)", "Write(f)") + "\nTool use\n  Skill(x)\n\nDo you want to proceed?\n> 1. Yes\n  2. No") == "Skill"
    assert guard.permission_prompt_tool(real) == "Bash"      # a real "Bash command" dialog (box + choices) has no Word( inside it: the header names the tool, prose's Bash( is ignored
    mkdir = "\u256d\u2500\u2500\u256e\n\u2502 Bash command\n\u2502\n\u2502   mkdir -p /tmp/x\n\u2502   Create the folder\n\u2502 Do you want to proceed?\n\u2502 \u276f 1. Yes\n\u2502   2. Yes, and don't ask again for mkdir commands\n\u2502   3. No\n"
    assert guard.permission_prompt_tool(mkdir) == "Bash"
    assert guard.permission_prompt_tool("\u256d\u2500\u2500\u256e\n\u2502 Edit file\n\u2502 Do you want to make this edit?\n1. Yes\n2. No") == "Edit"
    assert guard.permission_prompt_tool("\u256d\u2500\u2500\u256e\n\u2502 Something new\n\u2502 Do you want to proceed?\n1. Yes\n2. No") == "an unlisted tool"


def test_a_permission_prompt_nobody_answers_releases_the_job_naming_the_tool(tmp_path, monkeypatch):
    t0 = time.monotonic()
    w, dash, remote, out, state = asyncio.run(_run_one(tmp_path, monkeypatch, "perm", mode="subscription", watch_window=20,
                                                       watch_poll=0.1, permission_grace=1.0))
    assert time.monotonic() - t0 < 20                           # not the 120 s the session would have sat there
    assert w.results == [(5, "released")] and dash.finishes == [] and _branches(remote) == []
    reason = dash.releases[0]["reason"]
    assert reason.startswith("blocked on the permission prompt: ") and "Skill" in reason and "allowed tools" in reason
    assert not (tmp_path / "claude-out.json.typed").exists()    # nothing was typed into the session: never auto-approved


def test_a_permission_prompt_for_an_mcp_tool_names_it(tmp_path, monkeypatch):
    w, dash, *_ = asyncio.run(_run_one(tmp_path, monkeypatch, "perm_mcp", mode="subscription", watch_window=20, watch_poll=0.1,
                                       permission_grace=1.0))
    assert w.results == [(5, "released")] and "mcp__server__lookup" in dash.releases[0]["reason"]


def test_a_permission_prompt_answered_by_a_person_in_time_is_left_alone(tmp_path, monkeypatch):
    w, dash, *_ = asyncio.run(_run_one(tmp_path, monkeypatch, "perm_answered", mode="subscription", watch_window=20, watch_poll=0.1,
                                       permission_grace=5.0, max_seconds=4))
    assert dash.releases == []                                  # output moved on inside the grace: no release


def test_the_permission_watch_never_writes_to_the_session():
    import inspect
    from crewboss_runner import guard as guard_mod
    assert "agent.send" not in inspect.getsource(guard_mod.watch_permission)


# --- profile agent, extra MCP servers (#829 item 3) ----------------------------------------------------------------------------------

def test_a_profile_agent_becomes_dash_dash_agent_and_extra_mcp_servers_join_the_strict_config():
    job = FakeDash(Ed25519PrivateKey.generate()).job_payload()
    plain = spawn.argv_for("subscription", job, "/s/mcp.json", "claude")
    assert "--agent" not in plain and "mcp__knowledge" not in " ".join(plain)
    job["profile"]["agent"] = "db-migrator"
    job["mcp_servers"] = [{"name": "knowledge", "url": "http://k.example:8970/mcp"}, {"name": "crewboss-job", "url": "http://evil/"},
                          {"name": "bad name", "url": "http://x/"}, {"name": "ftp", "url": "ftp://x/"}]
    argv = spawn.argv_for("subscription", job, "/s/mcp.json", "claude")
    assert argv[argv.index("--agent") + 1] == "db-migrator" and "--strict-mcp-config" in argv
    tools = [a for a in argv if a.startswith("--allowedTools=")][0]
    assert tools.endswith(",mcp__knowledge") and tools.count("mcp__knowledge") == 1 and "evil" not in tools
    cfg = json.loads(spawn.mcp_config("https://dash.example", "mdj_T", spawn.extra_mcp_servers(job)))["mcpServers"]
    assert sorted(cfg) == ["crewboss-job", "knowledge"] and cfg["knowledge"] == {"type": "http", "url": "http://k.example:8970/mcp"}
    assert "mdj_T" not in json.dumps(cfg["knowledge"])          # the job token is never sent to the extra server
    job["profile"]["agent"] = "x; rm -rf /"
    assert "--agent" not in spawn.argv_for("subscription", job, "/s/mcp.json", "claude")


# --- agent, command and skill files: pins decide, nothing is parsed to approve (#829) ---------------------------------------------

AGENT = "---\nname: erp-builder\ndescription: builds\n---\nbody\n"


def _pin(rel, text):
    import hashlib
    return {"path": rel, "sha256": hashlib.sha256(text.encode()).hexdigest()}


def test_an_unpinned_agent_file_releases_the_job_and_is_sent_to_the_owner_for_review(tmp_path, monkeypatch):
    files = {".claude/agents/erp-builder.md": AGENT, "pkg/.claude/commands/go.md": "run it\n"}
    w, dash, remote, out, state = asyncio.run(_run_one(tmp_path, monkeypatch, "commit", pins=[], remote_files=files))
    assert w.results == [(5, "released")] and dash.finishes == [] and not out.exists()          # the agent never started
    reason = dash.releases[0]["reason"]
    assert "config.unreviewed" in reason and "not reviewed by the project Owner" in reason and "fix it there" in reason
    sent = {m["path"]: m for m in dash.agent_files}
    assert set(sent) == set(files) and all(m["source"] == "preflight" for m in sent.values())
    assert base64.b64decode(sent[".claude/agents/erp-builder.md"]["b64"]).decode() == AGENT
    import hashlib
    assert sent["pkg/.claude/commands/go.md"]["sha256"] == hashlib.sha256(b"run it\n").hexdigest()


def test_pinned_agent_files_let_the_job_start_and_a_changed_one_is_flagged_at_the_end(tmp_path, monkeypatch):
    files = {".claude/agents/erp-builder.md": AGENT}
    w, dash, remote, out, state = asyncio.run(_run_one(tmp_path, monkeypatch, "agentfile", pins=[_pin(".claude/agents/erp-builder.md", AGENT)],
                                                       remote_files=files))
    assert w.results == [(5, "done")] and dash.releases == []
    assert out.exists()                                                                          # it started
    assert sorted(m["path"] for m in dash.agent_files) == [".claude/agents/erp-builder.md", ".claude/agents/new.md"]
    assert all(m["source"] == "job" for m in dash.agent_files)
    summary = dash.finishes[0]["summary"]
    assert summary.startswith("NEEDS OWNER REVIEW") and "2 agent, command or skill file(s)" in summary
    assert _branches(remote) == ["dash/j5-e11"]                                                  # the work is pushed, not lost


def test_a_job_that_touches_no_agent_files_is_not_flagged(tmp_path, monkeypatch):
    files = {".claude/agents/erp-builder.md": AGENT}
    w, dash, remote, out, state = asyncio.run(_run_one(tmp_path, monkeypatch, "commit", pins=[_pin(".claude/agents/erp-builder.md", AGENT)],
                                                       remote_files=files))
    assert w.results == [(5, "done")] and dash.agent_files == [] and not dash.finishes[0]["summary"].startswith("NEEDS")


def test_an_older_dashboard_without_pins_pins_nothing_and_says_so(tmp_path, monkeypatch):
    w, dash, remote, out, state = asyncio.run(_run_one(tmp_path, monkeypatch, "commit", remote_files={".claude/agents/a.md": AGENT}))
    assert w.results == [(5, "released")] and dash.agent_files == [] and "too old" in dash.releases[0]["reason"]


def test_a_symlinked_agent_in_the_repo_blocks_and_is_never_sent(tmp_path, monkeypatch):
    r = _remote(tmp_path, {"docs/a.md": AGENT})
    (r / ".claude" / "agents").mkdir(parents=True)
    (r / ".claude" / "agents" / "x.md").symlink_to("../../docs/a.md")
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "add", "-A"], cwd=r, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "link"], cwd=r, check=True, env=env)
    w, dash, _r, out, state = asyncio.run(_run_one(tmp_path, monkeypatch, "commit", pins=[_pin("docs/a.md", AGENT)], remote=r))
    assert w.results == [(5, "released")] and "config.symlink" in dash.releases[0]["reason"] and dash.agent_files == [] and not out.exists()


def test_a_change_to_a_file_the_owner_has_already_pinned_in_that_form_is_not_flagged(tmp_path, monkeypatch):
    new = "---\nname: new\n---\nhello\n"
    files = {".claude/agents/erp-builder.md": AGENT}
    pins = [_pin(".claude/agents/erp-builder.md", AGENT), _pin(".claude/agents/erp-builder.md", AGENT + "more\n"), _pin(".claude/agents/new.md", new)]
    w, dash, remote, out, state = asyncio.run(_run_one(tmp_path, monkeypatch, "agentfile", pins=pins, remote_files=files))
    assert w.results == [(5, "done")] and dash.agent_files == [] and not dash.finishes[0]["summary"].startswith("NEEDS")


# --- review frames stay under the socket's 8 KiB, and a failed report never costs the job its release or its finish (#829 review 3) ----------

def test_a_big_agent_file_is_sent_in_chunks_under_6_kb_and_arrives_whole(tmp_path, monkeypatch):
    big = AGENT + ("x" * 79 + "\n") * 400            # ~32 KB: the old single frame was over the socket's 8 KiB and killed it
    w, dash, remote, out, state = asyncio.run(_run_one(tmp_path, monkeypatch, "commit", pins=[], remote_files={".claude/agents/erp-builder.md": big}))
    assert w.results == [(5, "released")]
    sent = dash.agent_files[0]
    assert base64.b64decode(sent["b64"]).decode() == big and sent["chunks"] > 1
    assert dash.frames and max(dash.frames) < 6 * 1024, dash.frames


def test_a_dashboard_that_refuses_a_report_releases_the_job_with_a_clear_reason(tmp_path, monkeypatch):
    w, dash, remote, out, state = asyncio.run(_run_one(tmp_path, monkeypatch, "commit", pins=[], agent_file_error="sha256 does not match the bytes received",
                                                       remote_files={".claude/agents/erp-builder.md": AGENT}))
    assert w.results == [(5, "released")] and not out.exists()
    reason = dash.releases[0]["reason"]
    assert "could not send" in reason and "sha256 does not match" in reason and "config.unreviewed" in reason


def test_a_failed_end_of_job_report_is_not_fatal_the_job_still_pushes_and_finishes(tmp_path, monkeypatch):
    files = {".claude/agents/erp-builder.md": AGENT}
    w, dash, remote, out, state = asyncio.run(_run_one(tmp_path, monkeypatch, "agentfile", pins=[_pin(".claude/agents/erp-builder.md", AGENT)],
                                                       remote_files=files, agent_file_error="server said no"))
    assert w.results == [(5, "done")] and len(dash.finishes) == 1 and dash.releases == []
    assert _branches(remote) == ["dash/j5-e11"]
    assert "NEEDS OWNER REVIEW" in dash.finishes[0]["summary"] and "could not send" in dash.finishes[0]["summary"]


def test_time_limit_still_pushes_the_commits_the_agent_made(tmp_path, monkeypatch):
    """Job 4's shape: the agent did its work, never called job_finish and sat at the prompt. The job fails at the limit, but its
    commits are pushed rather than thrown away with the worktree."""
    w, dash, remote, out, state = asyncio.run(_run_one(tmp_path, monkeypatch, "commit_hang", max_seconds=2))
    assert [f["ok"] for f in dash.finishes] == [False]
    assert "maximum duration" in dash.finishes[0]["summary"] and "pushed 1 commit(s)" in dash.finishes[0]["summary"]
    assert "agent work" in subprocess.run(["git", "-C", str(remote), "log", "--all", "--format=%s"], capture_output=True, text=True).stdout


def test_time_limit_with_a_stale_fence_pushes_nothing(tmp_path, monkeypatch):
    """A job cancelled just before its cap: the final heartbeat 409s, so the committed work is not pushed."""
    late = lambda n: {"ok": True, "lease_seconds": 90} if n < 1 else {"ok": False, "status": 409, "detail": "stale"}  # noqa: E731
    w, dash, remote, out, state = asyncio.run(_run_one(tmp_path, monkeypatch, "commit_hang", hb=late, hb_every=30, max_seconds=2))
    assert w.results == [(5, "lost")] and _branches(remote) == [] and dash.finishes == []


def test_time_limit_flags_agent_files_for_review_like_the_normal_end(tmp_path, monkeypatch):
    files = {".claude/agents/erp-builder.md": AGENT}
    w, dash, remote, out, state = asyncio.run(_run_one(tmp_path, monkeypatch, "agentfile_hang", pins=[_pin(".claude/agents/erp-builder.md", AGENT)],
                                                       remote_files=files, max_seconds=2))
    assert [f["ok"] for f in dash.finishes] == [False]
    assert "NEEDS OWNER REVIEW" in dash.finishes[0]["summary"] and "maximum duration" in dash.finishes[0]["summary"]
    assert any(m["source"] == "job" for m in dash.agent_files), "no review report was sent"


def test_time_limit_keeps_the_real_push_failure_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_mod.Worker, "_push", lambda self, wt, job, base, rc, finish=None: (False, "push failed: remote said no"))
    w, dash, remote, out, state = asyncio.run(_run_one(tmp_path, monkeypatch, "commit_hang", max_seconds=2))
    assert "push failed: remote said no" in dash.finishes[0]["summary"]
