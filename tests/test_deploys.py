"""Runners phase P5 (task #627): deploy requests, approval cards and the server-side executor. ssh is mocked
(deploys._ssh); the CA and ssh-keygen are real so the certificate's scope is checked for real. No test reaches a host."""
import subprocess

import pytest
from cryptography.fernet import Fernet

from app import config, deploys, job_mcp, jobs, push, repo, server_access
from app.db import get_cursor
from app.repo import BadRequest, Conflict, NotFound

from test_devices import est, signed_in  # noqa: F401  (fixtures)
from test_web_auth import auth_on  # noqa: F401
from test_jobs import _as_job, _queue, _task, world  # noqa: F401  (fixtures/helpers)

SHA = "a" * 40
OTHER_SHA = "b" * 40
HOST_KEY = "mallen-dash-srv-1 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"


@pytest.fixture
def depl(world, monkeypatch, tmp_path):
    """A running job (Alice's) on acme-app, a commissioned server and an enabled deploy target 'prod'; push recorded."""
    keyfile = tmp_path / "ssh-ca-enc-key"
    keyfile.write_text(Fernet.generate_key().decode())
    monkeypatch.setattr(config, "SSH_CA_KEY_FILE", str(keyfile))
    pushes = []
    monkeypatch.setattr(push, "deliver", lambda payload: pushes.append(payload) or {"sent": 0})
    with get_cursor() as cur:
        cur.execute("INSERT INTO commissioned_servers (account_id, name, address, port, login_user, status, host_key, "
                    "created_by) VALUES (%s, 'web-1', '10.0.0.5', 22, 'deploy', 'commissioned', %s, 'test') RETURNING id",
                    (world["acme"]["id"], HOST_KEY))
        world["server_id"] = cur.fetchone()["id"]
    world["target"] = deploys.add_target(world["acme"]["id"], "acme-app", "prod", world["server_id"], "/opt/stacks/app",
                                         "docker compose up -d --build", "main", "user:1")
    # Production is promote-only (change #77): these tests are about requests, approval and the executor, so every commit they use
    # has already "run on dev" (a done dev row for the project).
    with get_cursor() as cur:
        for ch in "0123456789abcdef":
            cur.execute("INSERT INTO server_jobs (account_id, project_id, target_name, sha, state, env) "
                        "SELECT %s, id, 'seed', %s, 'done', 'dev' FROM projects WHERE slug = 'acme-app'",
                        (world["acme"]["id"], ch * 40))
    _queue(world)
    world["claim"] = jobs.claim(world["dev"]["device_id"])
    world["pushes"] = pushes
    return world


def _ask(w, sha=SHA, target="prod", subject=""):
    c = w["claim"]
    return deploys.request_deploy(c["job_id"], c["fence"], target, sha, subject)


def _state(sj_id):
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT * FROM server_jobs WHERE id = %s", (sj_id,))
        return cur.fetchone()


def _approve(w, sj_id, user_id=None):
    user_id = user_id or w["alice"]["id"]
    return deploys.approve(w["acme"]["id"], sj_id, user_id)


class FakeSsh:
    """Replaces deploys._ssh: records the argv, inspects the certificate while it still exists, answers like the box."""

    def __init__(self, head_after=SHA, rc=0, extra="", on_call=None):
        self.known_hosts = []
        self.calls, self.certs, self.head_after, self.rc, self.extra, self.on_call = [], [], head_after, rc, extra, on_call

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        cert = next(a.split("=", 1)[1] for a in argv if a.startswith("CertificateFile="))
        self.certs.append(subprocess.run(["ssh-keygen", "-L", "-f", cert], capture_output=True, text=True).stdout)
        kh = next(a.split("=", 1)[1] for a in argv if a.startswith("UserKnownHostsFile="))
        self.known_hosts.append((open(kh, encoding="utf-8").read(), next(a.split("=", 1)[1] for a in argv if a.startswith("HostKeyAlias="))))
        if self.on_call:
            self.on_call()
        out = f"@@before {'0' * 40}\n@@after {self.head_after}\n{self.extra}"
        return subprocess.CompletedProcess(argv, self.rc, out, "")


@pytest.fixture
def fake_ssh(monkeypatch):
    def install(**kw):
        fake = FakeSsh(**kw)
        monkeypatch.setattr(deploys, "_ssh", fake)
        return fake
    monkeypatch.setattr(deploys, "_ssh", lambda *a, **k: pytest.fail("ssh must be mocked in this test"))
    return install


# --- the request --------------------------------------------------------------------------------------------------

def test_request_deploy_from_a_running_job(depl, monkeypatch):
    _as_job(monkeypatch, depl["claim"]["job_token"])
    got = job_mcp.request_deploy(depl["claim"]["fence"], "prod", SHA, "Fix the thing")
    assert got["state"] == "requested" and got["sha"] == SHA and got["target"] == "prod"
    sj = _state(got["server_job_id"])
    assert (sj["job_id"], sj["task_id"], sj["device_id"], sj["account_id"]) == (
        depl["claim"]["job_id"], depl["task"]["id"], depl["dev"]["device_id"], depl["acme"]["id"])
    assert sj["state"] == "requested" and sj["decided_by"] is None
    assert len(depl["pushes"]) == 1 and "/acme/approvals" in depl["pushes"][0]["url"]   # ids and a name, no task text
    assert "SECRET TASK TEXT" not in str(depl["pushes"])
    again = job_mcp.request_deploy(depl["claim"]["fence"], "prod", SHA)   # same target + sha while open: the same request
    assert again["server_job_id"] == got["server_job_id"] and len(depl["pushes"]) == 1
    # the tool takes no host, user or key: nothing of the sort exists on the request
    assert not {"ssh_user", "host", "key", "password"} & set(sj)
    for bad in ("zz", SHA[:39], SHA.upper() + "0", SHA + "0", "--help"):
        with pytest.raises(ValueError):
            job_mcp.request_deploy(depl["claim"]["fence"], "prod", bad)
    with pytest.raises(ValueError):   # a target that is not this project's (or does not exist)
        job_mcp.request_deploy(depl["claim"]["fence"], "staging", OTHER_SHA)


def test_request_deploy_stale_fence_refused(depl, monkeypatch):
    _as_job(monkeypatch, depl["claim"]["job_token"])
    with pytest.raises(ValueError, match="409 Conflict"):
        job_mcp.request_deploy(depl["claim"]["fence"] + 1, "prod", SHA)
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT count(*) AS n FROM server_jobs WHERE target_name <> 'seed'")
        assert cur.fetchone()["n"] == 0 and not depl["pushes"]
    jobs.done(depl["claim"]["job_id"], depl["claim"]["fence"], True)   # finished: its old fence asks for nothing either
    with pytest.raises(jobs.StaleFence):
        _ask(depl)


def test_target_scoping_and_disabled(depl):
    deploys.set_target_enabled(depl["acme"]["id"], depl["target"]["id"], False)
    with pytest.raises(NotFound):
        _ask(depl)
    deploys.set_target_enabled(depl["acme"]["id"], depl["target"]["id"], True)
    with pytest.raises(NotFound):   # beta can't point a target at acme's server, or add one to acme's project
        deploys.add_target(depl["beta"]["id"], "beta-app", "x", depl["server_id"], "/opt/x")
    with pytest.raises(NotFound):
        deploys.add_target(depl["beta"]["id"], "acme-app", "x", depl["server_id"], "/opt/x")
    with pytest.raises(BadRequest):
        deploys.add_target(depl["acme"]["id"], "acme-app", "x", depl["server_id"], "/opt/x/../etc")
    with pytest.raises(BadRequest):
        deploys.add_target(depl["acme"]["id"], "acme-app", "x", depl["server_id"], "/opt/x", "a\nb")


# --- the P2 gap ----------------------------------------------------------------------------------------------------

def test_job_token_may_not_take_deploy_or_migrate_locks(depl, monkeypatch):
    c = depl["claim"]
    _as_job(monkeypatch, c["job_token"])
    for kind in ("deploy", "migrate"):
        with pytest.raises(ValueError, match="may not take"):
            job_mcp.acquire_lock(c["fence"], kind, "acme-app")
        with pytest.raises(BadRequest):
            jobs.acquire_lock(c["job_id"], c["fence"], kind, "acme-app")
        with pytest.raises(ValueError):
            job_mcp.release_lock(c["fence"], kind, "acme-app")
    assert job_mcp.acquire_lock(c["fence"], "merge", "acme-app")["acquired"]   # merge still works
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT kind FROM locks")
        assert [r["kind"] for r in cur.fetchall()] == ["merge"]


# --- approval ------------------------------------------------------------------------------------------------------

def test_approval_needs_a_real_entra_session(depl, client, signed_in, monkeypatch):
    sj_id = _ask(depl)["server_job_id"]
    url = f"/acme/approvals/{sj_id}/approve"
    # no session at all
    r = client.post(url, follow_redirects=False)
    assert r.status_code in (302, 303, 401, 403) and _state(sj_id)["state"] == "requested"
    assert client.get("/acme/approvals", follow_redirects=False).status_code != 200
    # a device token and a job token are not sessions
    for token in (depl["claim"]["job_token"], depl["dev"]["device_token"]):
        r = client.post(url, headers={"authorization": f"Bearer {token}"}, follow_redirects=False)
        assert r.status_code in (302, 303, 401, 403) and _state(sj_id)["state"] == "requested"
        r = client.get("/acme/approvals", headers={"authorization": f"Bearer {token}"}, follow_redirects=False)
        assert r.status_code != 200
    h = {"cookie": signed_in["cookie"]}
    # a cross-site browser post is refused by the same-origin guard
    r = client.post(url, headers={**h, "Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"}, follow_redirects=False)
    assert r.status_code == 403 and _state(sj_id)["state"] == "requested"
    # the signed-in person sees the card and approves; who and when are recorded
    page = client.get("/acme/approvals", headers=h)
    assert page.status_code == 200 and f'data-request="{sj_id}"' in page.text and SHA in page.text and "prod" in page.text
    assert "data-approve" in page.text and "data-refuse" in page.text
    assert f'data-request="{sj_id}"' not in client.get("/beta/approvals", headers=h).text
    assert client.post(f"/beta/approvals/{sj_id}/approve", headers=h, follow_redirects=False).status_code == 303
    assert _state(sj_id)["state"] == "requested"   # another account's person can't touch it
    assert client.post(url, headers=h, follow_redirects=False).status_code == 303
    sj = _state(sj_id)
    assert sj["state"] == "approved" and sj["decided_at"] is not None and sj["decided_by"] is not None
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT oid FROM users WHERE id = %s", (sj["decided_by"],))
        assert str(cur.fetchone()["oid"]) == "bbbbbbbb-0000-4000-8000-000000000002"
    assert client.post(url, headers=h, follow_redirects=False).status_code == 303   # deciding twice changes nothing
    refused = _ask(depl, sha=OTHER_SHA)["server_job_id"]
    client.post(f"/acme/approvals/{refused}/refuse", headers=h, follow_redirects=False)
    assert _state(refused)["state"] == "refused" and _state(refused)["decided_by"] is not None


# --- the executor --------------------------------------------------------------------------------------------------

def test_a_host_key_pinned_under_the_old_alias_still_verifies_for_a_deploy(depl, fake_ssh):
    fake = fake_ssh()
    with get_cursor() as cur:   # pinned before the rename
        cur.execute("UPDATE commissioned_servers SET host_key = regexp_replace(host_key, '^[^ ]+', %s || id) WHERE id = %s",
                    (server_access.LEGACY_ALIAS_PREFIX, depl["server_id"]))
        cur.execute("SELECT host_key FROM commissioned_servers WHERE id = %s", (depl["server_id"],))
        assert cur.fetchone()["host_key"].startswith(server_access.LEGACY_ALIAS_PREFIX)
    sj_id = _ask(depl)["server_job_id"]
    _approve(depl, sj_id)
    assert deploys.Executor().run_once() == {"ran": 1, "waiting": 0}
    content, alias = fake.known_hosts[0]
    assert alias == f"crewboss-srv-{depl['server_id']}" and content.startswith(alias + " ") and server_access.LEGACY_ALIAS_PREFIX not in content


def test_approved_deploy_runs_and_checks_head(depl, fake_ssh):
    fake = fake_ssh(extra="build ok\ntoken=hunter2supersecret\n")
    sj_id = _ask(depl)["server_job_id"]
    ex = deploys.Executor()
    assert ex.run_once() == {"ran": 0, "waiting": 0} and not fake.calls   # nothing runs before approval
    _approve(depl, sj_id)
    assert ex.run_once() == {"ran": 1, "waiting": 0}
    sj = _state(sj_id)
    assert sj["state"] == "done" and sj["head_after"] == SHA and not sj["flagged"] and sj["finished_at"] is not None
    assert "build ok" in sj["output_tail"] and "hunter2supersecret" not in sj["output_tail"]
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT count(*) AS n FROM locks")
        assert cur.fetchone()["n"] == 0   # the deploy lock was released
    comments = [c["body"] for c in repo.get_task(depl["task"]["id"])["comments"]]
    assert any("done" in c and SHA[:12] in c for c in comments)
    # the command and the certificate
    argv = fake.calls[0]
    command = argv[-1]
    assert "git merge -q --ff-only" in command and "git fetch -q origin main" in command and "git pull" not in command and command.startswith("timeout ") and "docker compose up -d --build" in command and "/opt/stacks/app" in command
    assert argv[argv.index("--") + 1] == "10.0.0.5" and "StrictHostKeyChecking=yes" in argv and "ForwardAgent=no" in argv
    cert = fake.certs[0]
    assert f"srv-{depl['server_id']}" in cert and "force-command" in cert and "merge" in cert and "--ff-only" in cert
    assert "permit-pty" not in cert and "permit-port-forwarding" not in cert and "permit-agent-forwarding" not in cert
    assert "permit-X11-forwarding" not in cert and "permit-user-rc" not in cert
    assert f"deploy:{sj_id}" in cert   # the key id names the request
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT actor, valid_before - issued_at AS span FROM ssh_cert_issues WHERE actor = %s", (f"deploy:{sj_id}",))
        row = cur.fetchone()
    assert row and row["span"].total_seconds() <= (deploys.cert_minutes() * 60 + 1)   # job-length validity, not an hour


def test_a_target_time_limit_sets_the_script_the_ssh_wait_and_the_certificate(depl, monkeypatch):
    """#829: deploy_targets.timeout_seconds (NULL = the instance default) is the limit the executor uses, not a global."""
    seen = []
    fake = FakeSsh(extra="")

    def ssh(argv, timeout):
        seen.append(timeout)
        return fake(argv, timeout)
    monkeypatch.setattr(deploys, "_ssh", ssh)
    assert depl["target"]["timeout_seconds"] is None
    sj_id = _ask(depl)["server_job_id"]
    _approve(depl, sj_id)
    deploys.Executor().run_once()
    assert seen == [deploys.config.DEPLOY_TIMEOUT_SECONDS] and fake.calls[0][-1].startswith(f"timeout {deploys.config.DEPLOY_TIMEOUT_SECONDS - 10} sh -c")
    with get_cursor() as cur:
        cur.execute("UPDATE deploy_targets SET timeout_seconds = 1800 WHERE id = %s", (depl["target"]["id"],))
    fake.head_after = OTHER_SHA
    sj2 = _ask(depl, sha=OTHER_SHA)["server_job_id"]
    _approve(depl, sj2)
    deploys.Executor().run_once()
    assert seen[-1] == 1800 and fake.calls[-1][-1].startswith("timeout 1790 sh -c")
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT valid_before - issued_at AS span FROM ssh_cert_issues WHERE actor = %s", (f"deploy:{sj2}",))
        assert cur.fetchone()["span"].total_seconds() >= 1800            # the certificate outlives the longer deploy


def test_target_time_limit_is_checked(depl):
    assert deploys.clean_timeout("") is None and deploys.clean_timeout(None) is None and deploys.clean_timeout(" 1800 ") == 1800
    for bad in ("59", "7201", "abc", "-5", "10.5"):
        with pytest.raises(BadRequest):
            deploys.clean_timeout(bad)
    assert deploys.target_timeout({"timeout_seconds": 1800}) == 1800
    assert deploys.target_timeout({"timeout_seconds": None}) == deploys.config.DEPLOY_TIMEOUT_SECONDS
    with pytest.raises(Exception):    # the table refuses it too
        with get_cursor() as cur:
            cur.execute("UPDATE deploy_targets SET timeout_seconds = 5")


def test_head_mismatch_is_failed_and_flagged(depl, fake_ssh, monkeypatch):
    fake_ssh(head_after=OTHER_SHA, rc=deploys.EXIT_MISMATCH)
    sj_id = _ask(depl)["server_job_id"]
    _approve(depl, sj_id)
    deploys.Executor().run_once()
    sj = _state(sj_id)
    assert sj["state"] == "failed" and sj["flagged"] and sj["head_after"] == OTHER_SHA
    assert "not the approved" in sj["error"]
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT count(*) AS n FROM locks")
        assert cur.fetchone()["n"] == 0
    assert any("FLAGGED" in c["body"] for c in repo.get_task(depl["task"]["id"])["comments"])
    # exit 0 with the wrong HEAD is also refused (the dashboard does not trust the exit code alone)
    sj2 = _ask(depl, sha=SHA)["server_job_id"]
    _approve(depl, sj2)
    monkeypatch.setattr(deploys, "_ssh", FakeSsh(head_after=OTHER_SHA, rc=0))
    deploys.Executor().run_once()
    assert _state(sj2)["state"] == "failed" and _state(sj2)["flagged"]


def test_restart_mid_deploy_is_orphaned_and_never_retried(depl, fake_ssh):
    fake = fake_ssh()
    sj_id = _ask(depl)["server_job_id"]
    _approve(depl, sj_id)
    assert deploys.Executor().claim(sj_id)["state"] == "running"   # the process dies here: no finish ever comes
    assert deploys.recover(startup=True) == 1
    sj = _state(sj_id)
    assert sj["state"] == "orphaned" and "restarted" in sj["error"]
    assert [l["state"] for l in jobs.list_locks(depl["acme"]["id"])] == ["orphaned"]
    assert deploys.Executor().run_once() == {"ran": 0, "waiting": 0} and not fake.calls   # never retried
    assert _state(sj_id)["state"] == "orphaned"
    # a new request for the same target waits behind the orphaned lock until a person clears it
    sj2 = _ask(depl, sha=OTHER_SHA)["server_job_id"]
    _approve(depl, sj2)
    assert deploys.Executor().run_once() == {"ran": 0, "waiting": 1} and not fake.calls
    jobs.clear_orphan(depl["acme"]["id"], "deploy", deploys.lock_resource(depl["target"]["id"]), "user:1")
    fake.head_after = OTHER_SHA
    assert deploys.Executor().run_once() == {"ran": 1, "waiting": 0} and _state(sj2)["state"] == "done"


def test_second_approved_deploy_waits_on_the_lock(depl, fake_ssh):
    sj1 = _ask(depl)["server_job_id"]
    sj2 = _ask(depl, sha=OTHER_SHA)["server_job_id"]
    _approve(depl, sj1)
    _approve(depl, sj2)
    inner = {}

    def while_first_runs():   # a second executor pass while the first deploy holds the target's lock
        inner["out"] = deploys.Executor().run_once()
        inner["state2"] = _state(sj2)["state"]

    fake = fake_ssh(on_call=while_first_runs)
    # run only the first request (claim order is by id), so the nested pass is what tries the second
    assert deploys.Executor().run_one(sj1) == "done"
    assert inner["out"] == {"ran": 0, "waiting": 1} and inner["state2"] == "approved"
    assert len(fake.calls) == 1
    fake.on_call, fake.head_after = None, OTHER_SHA
    assert deploys.Executor().run_once() == {"ran": 1, "waiting": 0}   # the lock is free now
    assert _state(sj1)["state"] == "done" and _state(sj2)["state"] == "done"


def test_redaction_keeps_a_commit_and_drops_secrets():
    text = f"HEAD {SHA}\nPASSWORD=abc123\nAuthorization: Bearer abcdef\n-----BEGIN OPENSSH " "PRIVATE KEY-----\nxx\n-----END OPENSSH " "PRIVATE KEY-----\n"
    out = deploys.redact(text)
    assert SHA in out and "abc123" not in out and "abcdef" not in out and "xx" not in out
