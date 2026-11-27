"""The server probe and the four states (#810, migration 0088): ssh-keyscan against the pinned key, never a login, compared with the
pinned key; Ready / Not answering / Needs setup derived from it; a changed key fails the server at once; the executor refuses a
server that is not Ready; a publickey refusal on a deploy moves it to Needs setup. ssh-keyscan is mocked (server_access._keyscan);
the sweeper is off in the suite (CREWBOSS_SERVER_PROBE_SECONDS=0, tests/conftest.py) and these tests drive probe_all() by hand."""
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from app import config, deploys, server_access
from app.db import get_cursor

from test_deploys import SHA, FakeSsh as DeployFakeSsh, _approve, _ask, _state, depl, fake_ssh  # noqa: F401
from test_devices import est, signed_in  # noqa: F401
from test_server_access import HOST_KEY_BLOB, OTHER_KEY_BLOB, _commissioned, est as sa_est  # noqa: F401  (this module's est has the CA key file; test_devices' est goes with depl)
from test_servers_deploy_targets import _h
from test_web_auth import auth_on  # noqa: F401
from test_jobs import world  # noqa: F401


class FakeScan:
    """Stands in for server_access._keyscan: records argv, answers like ssh-keyscan (a key line, or nothing)."""

    def __init__(self, blob=HOST_KEY_BLOB, kind="ssh-ed25519", rc=0, timeout=False):
        self.blob, self.kind, self.rc, self.timeout, self.calls = blob, kind, rc, timeout, []

    def __call__(self, argv, timeout):
        self.calls.append(argv)
        if self.timeout:
            raise subprocess.TimeoutExpired(argv, timeout)
        out = f"# 203.0.113.10:22 SSH-2.0-OpenSSH_9.9\n203.0.113.10 {self.kind} {self.blob}\n" if self.blob else ""
        return subprocess.CompletedProcess(argv, self.rc, out, "")


def _row(sa_est, sid):
    return server_access.get(sa_est["acme"]["id"], sid)


def _age(sid, **cols):
    """Back-date the probe columns (the tests never sleep)."""
    sets = ", ".join(f"{k} = %s" for k in cols)
    with get_cursor() as cur:
        cur.execute(f"UPDATE commissioned_servers SET {sets} WHERE id = %s", (*cols.values(), sid))


def test_argv_has_no_login_and_only_validated_values(sa_est, monkeypatch):
    row = _commissioned(sa_est, monkeypatch)
    argv = server_access.probe_argv(row)
    assert argv[0] == "ssh-keyscan" and argv[argv.index("-t") + 1] == "ssh-ed25519" and argv[argv.index("-p") + 1] == "22"
    assert argv[-2:] == ["--", "203.0.113.10"] and "-l" not in argv and "-i" not in argv      # never a login, never a key
    bad = dict(row, host_key=row["host_key"].replace("ssh-ed25519", "-oProxyCommand=sh"))
    with pytest.raises(server_access.BadRequest):
        server_access.probe_argv(bad)


def test_an_answer_with_the_pinned_key_is_ready(sa_est, monkeypatch):
    row = _commissioned(sa_est, monkeypatch)
    scan = FakeScan()
    monkeypatch.setattr(server_access, "_keyscan", scan)
    assert server_access.probe_all() == {"probed": 1, "ok": 1}
    got = _row(sa_est, row["id"])
    assert got["probe_ok"] is True and got["probe_misses"] == 0 and got["unreachable_since"] is None and got["last_probe_at"]
    assert server_access.state(got) == server_access.READY and server_access.is_ready(got)
    assert len(scan.calls) == 1


def test_two_misses_are_not_answering_and_sixty_minutes_is_needs_setup(sa_est, monkeypatch):
    row = _commissioned(sa_est, monkeypatch)
    monkeypatch.setattr(server_access, "_keyscan", FakeScan(blob="", rc=1))
    server_access.probe_all()
    assert server_access.state(_row(sa_est, row["id"])) == server_access.READY             # one miss is not an outage
    server_access.probe_all()
    got = _row(sa_est, row["id"])
    assert got["probe_misses"] == 2 and server_access.state(got) == server_access.NOT_ANSWERING
    assert not server_access.is_ready(got) and "not answered" in server_access.state_reason(got)
    _age(row["id"], unreachable_since=datetime.now(timezone.utc) - timedelta(minutes=61))
    got = _row(sa_est, row["id"])
    assert server_access.state(got) == server_access.NEEDS_SETUP and "no answer for 61 min" in server_access.state_reason(got)
    monkeypatch.setattr(server_access, "_keyscan", FakeScan(timeout=True))
    server_access.probe_all()                                                          # a timeout is a miss too, never an exception
    assert _row(sa_est, row["id"])["probe_misses"] == 3


def test_a_changed_host_key_is_needs_setup_at_once(sa_est, monkeypatch):
    row = _commissioned(sa_est, monkeypatch)
    monkeypatch.setattr(server_access, "_keyscan", FakeScan(blob=OTHER_KEY_BLOB))
    server_access.probe_all()
    got = _row(sa_est, row["id"])
    assert got["status"] == "failed" and got["last_check_error"] == server_access.HOST_KEY_CHANGED
    assert server_access.state(got) == server_access.NEEDS_SETUP and not server_access.is_ready(got)


def test_recovery_from_a_quiet_hour_runs_one_full_check(sa_est, monkeypatch):
    row = _commissioned(sa_est, monkeypatch)
    _age(row["id"], unreachable_since=datetime.now(timezone.utc) - timedelta(minutes=90), probe_misses=18, probe_ok=False)
    assert server_access.state(_row(sa_est, row["id"])) == server_access.NEEDS_SETUP
    checks = []
    real = server_access.check
    monkeypatch.setattr(server_access, "check", lambda account, sid: checks.append(sid) or real(account, sid))
    monkeypatch.setattr(server_access, "_keyscan", FakeScan())
    server_access.probe_all()
    # status stayed 'commissioned', so the sweep still probed it, saw it answer, and re-proved it with a real check
    assert checks == [row["id"]]
    got = _row(sa_est, row["id"])
    assert server_access.state(got) == server_access.READY and got["unreachable_since"] is None


def test_a_server_that_failed_its_check_is_not_probed(sa_est, monkeypatch):
    row = server_access.add(sa_est["acme"], "web03", "203.0.113.11", 22, "mallen", "owner")      # pending: no pinned key yet
    monkeypatch.setattr(server_access, "_keyscan", lambda *a, **k: pytest.fail("a pending server has nothing to compare"))
    assert server_access.probe_all() == {"probed": 0, "ok": 0}
    assert server_access.state(row) == server_access.NEEDS_SETUP


def test_a_jump_host_server_is_skipped_and_shows_its_hosts_state(sa_est, monkeypatch):
    jump = _commissioned(sa_est, monkeypatch, name="jump01")
    behind = server_access.add(sa_est["acme"], "behind01", "10.0.0.7", 22, "mallen", "owner", via_server_id=jump["id"])
    with get_cursor() as cur:
        cur.execute("UPDATE commissioned_servers SET status = 'commissioned', host_key = %s WHERE id = %s",
                    (f"mallen-dash-srv-{behind['id']} ssh-ed25519 {HOST_KEY_BLOB}", behind["id"]))
    scan = FakeScan()
    monkeypatch.setattr(server_access, "_keyscan", scan)
    assert server_access.probe_all()["probed"] == 1 and len(scan.calls) == 1               # only the direct one
    assert not server_access.is_ready(_row(sa_est, behind["id"]))                             # a hop is access only, never a deploy target
    cards = {c["name"]: c for c in server_access.page_model(sa_est["acme"])["servers"]}
    assert cards["behind01"]["state"] == cards["jump01"]["state"] == "ready"


def test_the_sweeper_is_off_in_the_suite():
    assert config.SERVER_PROBE_SECONDS == 0


def test_the_card_says_the_state_in_words(sa_est, client, monkeypatch, signed_in):
    row = _commissioned(sa_est, monkeypatch)
    html = client.get("/acme/machines?tab=servers", headers=_h(signed_in)).text
    assert 'data-state="ready"' in html and ">Ready<" in html and "data-check-now" not in html
    assert "<details class=\"mt-2 border-t border-line pt-1\" data-access>" in html           # a ready card folds its setup
    _age(row["id"], probe_misses=3, unreachable_since=datetime.now(timezone.utc) - timedelta(minutes=14))
    html = client.get("/acme/machines?tab=servers", headers=_h(signed_in)).text
    assert 'data-state="not_answering"' in html and "Not answering for 14 min" in html and "data-check-now" in html
    assert "<details class=\"mt-2 border-t border-line pt-1\" data-access>" in html           # a reboot is not a rebuild
    _age(row["id"], unreachable_since=datetime.now(timezone.utc) - timedelta(minutes=70))
    html = client.get("/acme/machines?tab=servers", headers=_h(signed_in)).text
    assert 'data-state="needs_setup"' in html and ">Needs setup<" in html and "data-access open" in html


# --- the executor -------------------------------------------------------------------------------------------------------

def test_the_executor_refuses_a_server_that_is_not_ready(depl, fake_ssh):
    fake = fake_ssh()
    sj_id = _ask(depl)["server_job_id"]
    _approve(depl, sj_id)
    _age(depl["server_id"], probe_misses=2, unreachable_since=datetime.now(timezone.utc) - timedelta(minutes=12))
    deploys.Executor().run_once()
    sj = _state(sj_id)
    assert sj["state"] == "failed" and "has not answered" in (sj["error"] or "") and not fake.calls     # ssh never started


def test_a_publickey_refusal_moves_the_server_to_needs_setup(depl, monkeypatch):
    def refuse(argv, timeout):
        return subprocess.CompletedProcess(argv, 255, "", "deploy@10.0.0.5: Permission denied (publickey).\n")
    monkeypatch.setattr(deploys, "_ssh", refuse)
    sj_id = _ask(depl)["server_job_id"]
    _approve(depl, sj_id)
    deploys.Executor().run_once()
    sj = _state(sj_id)
    assert sj["state"] == "failed" and "refused the dashboard's pass" in (sj["error"] or "")
    row = server_access.get(depl["acme"]["id"], depl["server_id"])
    assert row["status"] == "failed" and server_access.state(row) == server_access.NEEDS_SETUP


def test_the_remote_scripts_own_git_failure_is_a_failed_deploy_not_a_server_needing_setup(depl, monkeypatch):
    """`git fetch` inside the deploy script can fail with the same words ("Permission denied (publickey)", a deploy key GitHub refused). That
    arrives with the script's own @@before marker (or a non-255 exit) and must NOT mark the server Needs setup for every project."""
    for sha, (rc, out) in zip("123", ((255, "@@before 0000\n"), (128, ""), (1, "@@before 0000\n"))):
        def fake(argv, timeout, rc=rc, out=out):
            return subprocess.CompletedProcess(argv, rc, out, "git@github.com: Permission denied (publickey).\nfatal: Could not read from remote repository.\n")
        monkeypatch.setattr(deploys, "_ssh", fake)
        sj_id = _ask(depl, sha=sha * 40)["server_job_id"]
        _approve(depl, sj_id)
        deploys.Executor().run_once()
        assert _state(sj_id)["state"] == "failed"
        row = server_access.get(depl["acme"]["id"], depl["server_id"])
        assert row["status"] == "commissioned" and server_access.state(row) == server_access.READY, (rc, out)
