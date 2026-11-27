"""P5 review fixes (task #627): the deploy can only land on the approved sha, a timeout keeps the lock, a sha that is not on the
branch yet is a failure (not a flag), person-side requests, one executing process, and small hardening. ssh is mocked."""
import pathlib
import subprocess
import sys

import pytest

from app import deploys, jobs, repo
from app.db import connect, get_cursor
from app.repo import BadRequest

from test_deploys import FakeSsh, HOST_KEY, OTHER_SHA, SHA, _approve, _ask, _state, depl, fake_ssh  # noqa: F401
from test_devices import est, signed_in  # noqa: F401
from test_jobs import world  # noqa: F401
from test_web_auth import auth_on  # noqa: F401


def test_remote_script_can_only_land_on_the_approved_sha():
    cmd = deploys.remote_script("/opt/stacks/app", "main", SHA, "make up", timeout_seconds=100)
    assert cmd.startswith("timeout 100 sh -c ") and "git pull" not in cmd
    assert cmd.index("fetch -q origin main") < cmd.index("is-ancestor") < cmd.index("merge -q --ff-only") < cmd.index("make up")


def test_sha_not_on_the_branch_is_failed_not_flagged(depl, fake_ssh):
    fake = fake_ssh(rc=deploys.EXIT_NOT_ON_BRANCH)
    fake.head_after = ""
    sj_id = _ask(depl)["server_job_id"]
    _approve(depl, sj_id)
    deploys.Executor().run_once()
    sj = _state(sj_id)
    assert sj["state"] == "failed" and not sj["flagged"]
    assert sj["error"] == f"{SHA[:8]} is not on main yet (merge it first)"
    assert not any("FLAGGED" in c["body"] for c in repo.get_task(depl["task"]["id"])["comments"])


def test_timeout_keeps_the_lock_orphaned_and_the_partial_output(depl, monkeypatch):
    def boom(argv, timeout):
        raise subprocess.TimeoutExpired(argv, timeout, output=b"@@before " + b"0" * 40 + b"\nbuilding...\n")
    monkeypatch.setattr(deploys, "_ssh", boom)
    sj_id = _ask(depl)["server_job_id"]
    _approve(depl, sj_id)
    deploys.Executor().run_once()
    sj = _state(sj_id)
    assert sj["state"] == "failed" and "building..." in sj["output_tail"] and "may still be running" in sj["error"]
    assert [l["state"] for l in jobs.list_locks(depl["acme"]["id"])] == ["orphaned"]
    sj2 = _ask(depl, sha=OTHER_SHA)["server_job_id"]   # blocked until a person clears the lock
    _approve(depl, sj2)
    monkeypatch.setattr(deploys, "_ssh", FakeSsh(head_after=OTHER_SHA))
    assert deploys.Executor().run_once() == {"ran": 0, "waiting": 1}
    jobs.clear_orphan(depl["acme"]["id"], "deploy", deploys.lock_resource(depl["target"]["id"]), "user:1")
    assert deploys.Executor().run_once() == {"ran": 1, "waiting": 0}


def _count():
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT count(*) AS n FROM server_jobs WHERE target_name <> 'seed'")
        return cur.fetchone()["n"]


def test_person_request_and_ask_again(depl, client, signed_in, fake_ssh):
    h = {"cookie": signed_in["cookie"]}
    tid = depl["target"]["id"]
    r = client.post("/acme/approvals/request", data={"target_id": tid, "sha": SHA}, follow_redirects=False)   # no session
    assert r.status_code in (302, 303, 401, 403) and _count() == 0
    r = client.post("/acme/approvals/request", data={"target_id": tid, "sha": SHA},
                    headers={**h, "Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"}, follow_redirects=False)
    assert r.status_code == 403 and _count() == 0
    assert client.post("/acme/approvals/request", data={"target_id": tid, "sha": "xyz"}, headers=h).status_code == 400
    assert client.post("/acme/approvals/request", data={"target_id": tid, "sha": SHA}, headers=h,
                       follow_redirects=False).status_code == 303
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT * FROM server_jobs WHERE target_name <> 'seed'")
        sj = cur.fetchone()
    assert sj["state"] == "requested" and sj["job_id"] is None and sj["requested_by"] is not None
    assert "Asked by Bob" in client.get("/acme/approvals", headers=h).text
    # Ask again on a request that is not failed/refused changes nothing
    assert client.post(f"/acme/approvals/{sj['id']}/again", headers=h, follow_redirects=False).status_code == 303
    assert _count() == 1
    # fail it (sha not on the branch), then Ask again from the card
    fake_ssh(rc=deploys.EXIT_NOT_ON_BRANCH).head_after = ""
    deploys.approve(depl["acme"]["id"], sj["id"], depl["alice"]["id"])
    deploys.Executor().run_once()
    assert _state(sj["id"])["state"] == "failed"
    assert "data-ask-again" in client.get("/acme/approvals", headers=h).text
    assert client.post(f"/beta/approvals/{sj['id']}/again", headers=h, follow_redirects=False).status_code == 303
    assert _count() == 1   # another account's page asks nothing
    assert client.post(f"/acme/approvals/{sj['id']}/again", headers=h, follow_redirects=False).status_code == 303
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT * FROM server_jobs WHERE state = 'requested'")
        again = cur.fetchone()
    assert again["sha"] == SHA and again["target_id"] == tid and again["requested_by"] is not None and again["job_id"] is None


def test_runner_prompt_says_only_request_a_sha_already_on_the_branch():
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "runner"))
    from crewboss_runner import spawn
    text = spawn.prompt_for({"job_id": 1, "task_id": 2, "fence": 3, "branch": "dash/j1-e3"})
    assert "request_deploy only for a commit that is ALREADY on the" in text and "needs merging" in text


def test_only_one_process_executes(depl, fake_ssh):
    fake = fake_ssh()
    sj_id = _ask(depl)["server_job_id"]
    _approve(depl, sj_id)
    other = connect()   # "another app process" holds the executor lock
    try:
        other.execute("SELECT pg_advisory_lock(%s)", (deploys.EXEC_LOCK_KEY,))
        assert deploys.Executor().run_once() == {"ran": 0, "waiting": 0, "skipped": True} and not fake.calls
        with get_cursor() as cur:   # its live deploy is not orphaned by our startup; only a late one is
            cur.execute("UPDATE server_jobs SET state = 'running', started_at = now(), owner = 'other' WHERE id = %s", (sj_id,))
        assert deploys.recover(startup=True) == 0 and _state(sj_id)["state"] == "running"
        with get_cursor() as cur:
            cur.execute("UPDATE server_jobs SET started_at = now() - interval '1 day' WHERE id = %s", (sj_id,))
        assert deploys.recover() == 1 and _state(sj_id)["state"] == "orphaned"
    finally:
        other.close()


def test_enable_disable_is_recorded_and_jump_host_target_refused(depl):
    deploys.set_target_enabled(depl["acme"]["id"], depl["target"]["id"], False, "user:7")
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT 1 AS x FROM activity WHERE verb = 'disabled' AND entity = 'deploy_target'")
        assert cur.fetchone()
    with get_cursor() as cur:
        cur.execute("INSERT INTO commissioned_servers (account_id, name, address, port, login_user, status, host_key, "
                    "created_by, via_server_id) VALUES (%s, 'inner', '10.9.9.9', 22, 'deploy', 'commissioned', %s, 't', %s) "
                    "RETURNING id", (depl["acme"]["id"], HOST_KEY, depl["server_id"]))
        inner = cur.fetchone()["id"]
    with pytest.raises(BadRequest, match="jump host"):
        deploys.add_target(depl["acme"]["id"], "acme-app", "viajump", inner, "/opt/x")


def test_duplicate_open_request_returns_the_existing_one(depl):
    first = _ask(depl)["server_job_id"]
    with get_cursor() as cur:   # the race: the pre-check missed, the unique index catches it
        cur.execute("SELECT * FROM deploy_targets WHERE id = %s", (depl["target"]["id"],))
        tgt = cur.fetchone()
        row, fresh = deploys._create_request(cur, depl["acme"]["id"], tgt["project_id"], tgt, SHA, "", "user:1")
    assert not fresh and row["id"] == first
