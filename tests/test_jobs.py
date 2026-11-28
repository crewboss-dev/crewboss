"""Runners phase P2 (task #627): agent profiles, jobs, one-SQL dispatch, leases, fencing, locks and the job MCP.
Builds on the P1 fixtures (tests/test_devices.py): real users, real enrolled devices, real ed25519 keys."""
import json
import threading

import pytest

from app import auth, config, devices, job_mcp, jobs, mcp_server, repo, tree, web_auth
from app.db import get_cursor
from app.repo import BadRequest, Conflict, NotFound
from app.routers import api

from test_devices import _keypair, _project, _secret, _user, est, signed_in  # noqa: F401  (fixtures)
from test_web_auth import auth_on  # noqa: F401


def _device(est, account="acme", mode="subscription", user=None, name="pc"):
    user = user or _user()
    issued = devices.issue_code(user, est[account], mode)
    _key, pub = _keypair()
    dev = devices.enrol(_secret(issued["code"]), pub, name, "linux", ip="10.0.0.1")
    return dev, user


def _task(project, title="Fix the thing", body="SECRET TASK TEXT", created_by="owner"):
    t = repo.create_task(project["id"], title, body, "should", "bug", [], {}, "owner")
    if created_by != "owner":
        with get_cursor() as cur:
            cur.execute("UPDATE tasks SET created_by = %s WHERE id = %s", (created_by, t["id"]))
    return t


@pytest.fixture
def world(est):
    """acme (with a child project 'acme-app') and beta, each with a task; a subscription device for Alice in acme."""
    est["acme_app"] = _project("acme-app", "acmeapp", est["acme"])
    est["beta_app"] = _project("beta-app", "betaapp", est["beta"])
    est["task"] = _task(est["acme_app"])
    est["btask"] = _task(est["beta_app"])
    est["dev"], est["alice"] = _device(est)
    return est


def _queue(w, task=None, user=None, profile="builder", approved=True, unattended=False):
    return jobs.create_job((task or w["task"])["id"], (user or w["alice"])["id"], profile, unattended=unattended,
                           approved=approved)


def _as_job(monkeypatch, token):
    headers = {} if token is None else {"authorization": f"Bearer {token}"}
    monkeypatch.setattr(job_mcp, "_request_headers", lambda: headers)


# --- profiles ------------------------------------------------------------------------------------------------------

def test_profiles_seeded_from_the_routing_table_with_no_fable(world):
    by_name = {p["name"]: p for p in jobs.list_profiles()}
    assert by_name["builder"]["model"] == "sonnet" and "migration" in by_name["builder"]["task_types"]
    assert by_name["chores"]["model"] == "haiku" and "test-run" in by_name["chores"]["task_types"]
    assert by_name["designer"]["model"] == "opus" and "review" in by_name["designer"]["task_types"]
    assert not [p for p in by_name.values() if p["model"] == "fable"]   # escalation only, chosen by a human
    with pytest.raises(NotFound):
        jobs.create_job(world["task"]["id"], world["alice"]["id"], "nope")


def test_profiles_agent_column_new_profiles_and_skill(world):
    by_name = {p["name"]: p for p in jobs.list_profiles()}
    assert by_name["builder"]["agent"] == "" and "Skill" in by_name["builder"]["allowed_tools"] and "Skill" in by_name["designer"]["allowed_tools"]
    assert "Skill" not in by_name["chores"]["allowed_tools"]
    assert by_name["db-migrator"]["agent"] == "db-migrator" and by_name["db-migrator"]["model"] == "sonnet"
    assert by_name["code-auditor"]["agent"] == "code-auditor" and by_name["code-auditor"]["model"] == "opus"
    assert "Edit" not in by_name["code-auditor"]["allowed_tools"] and "Write" not in by_name["code-auditor"]["allowed_tools"]   # reports, never fixes


def test_claim_carries_the_profile_agent_and_only_the_instance_configured_mcp_servers(world, monkeypatch):
    _queue(world, profile="db-migrator")
    claim = jobs.claim(world["dev"]["device_id"])
    assert claim["profile"]["agent"] == "db-migrator" and claim["mcp_servers"] == []      # no instance setting: nothing extra, no Mallen default
    monkeypatch.setattr(jobs.config, "JOB_MCP_SERVERS", [{"name": "knowledge", "url": "http://k.example:8970/mcp", "projects": ["acme-app"]},
                                                       {"name": "other", "url": "http://o.example/mcp", "projects": ["elsewhere"]},
                                                       {"name": "all", "url": "http://a.example/mcp", "projects": []}])
    assert [m["name"] for m in jobs.job_mcp_servers("acme-app")] == ["knowledge", "all"]
    assert [m["name"] for m in jobs.job_mcp_servers("zzz")] == ["all"]
    from app import config
    assert config._job_mcp_servers(None) == []
    for bad in ([{"name": "crewboss-job", "url": "http://x/"}], [{"name": "Bad", "url": "http://x/"}], [{"name": "k", "url": "ftp://x"}], "no"):
        with pytest.raises(ValueError):
            config._job_mcp_servers(bad)


def test_a_profile_with_a_tool_pattern_is_refused_and_the_seeds_are_bare(world):
    """#829 review 5: `Bash(git *)` in a ceiling does not deny Bash and a git alias escapes it, so profiles hold bare tool names only."""
    for p in jobs.list_profiles():
        assert jobs.tool_pattern_problem(p["allowed_tools"]) is None, p["name"]
        assert not {"SendMessage", "ListAgents", "Workflow", "ExitPlanMode", "EnterPlanMode", "AskUserQuestion"} & set(p["allowed_tools"])
    assert "Bash(git *)" in jobs.tool_pattern_problem(["Read", "Bash(git *)"])
    try:
        with get_cursor() as cur:
            cur.execute("UPDATE agent_profiles SET allowed_tools = '{Read,\"Bash(git *)\"}' WHERE name = 'chores'")
        with pytest.raises(jobs.BadRequest, match="bare tool names"):
            _queue(world, profile="chores")
        with get_cursor() as cur:   # a profile edited AFTER the job was queued: the claim fails the job with the reason
            cur.execute("UPDATE agent_profiles SET allowed_tools = '{Read,Glob}' WHERE name = 'chores'")
        j = _queue(world, profile="chores")
        with get_cursor() as cur:
            cur.execute("UPDATE agent_profiles SET allowed_tools = '{Read,\"Bash(git *)\"}' WHERE name = 'chores'")
        assert jobs.claim(world["dev"]["device_id"]) is None
        with get_cursor(commit=False) as cur:
            cur.execute("SELECT state, summary FROM jobs WHERE id = %s", (j["id"],))
            row = cur.fetchone()
        assert row["state"] == "failed" and "bare tool names" in row["summary"] and "Bash(git *)" in row["summary"]
    finally:   # profiles are shared by the whole suite
        with get_cursor() as cur:
            cur.execute("UPDATE agent_profiles SET allowed_tools = '{Read,Glob,Grep,Bash}' WHERE name = 'chores'")


# --- one live job per task -----------------------------------------------------------------------------------------

def test_one_live_job_per_task(world):
    first = _queue(world)
    with pytest.raises(Conflict):
        _queue(world)
    claim = jobs.claim(world["dev"]["device_id"])
    with pytest.raises(Conflict):   # running is live too
        _queue(world)
    jobs.done(first["id"], claim["fence"], True)
    assert _queue(world)["id"] != first["id"]   # finished: a new one may be queued
    with pytest.raises(Exception):   # and the table itself refuses a second live row
        with get_cursor() as cur:
            cur.execute("INSERT INTO jobs (task_id, account_id, user_id, profile_id) SELECT task_id, account_id, "
                        "user_id, profile_id FROM jobs WHERE state = 'queued'")


# --- dispatch ------------------------------------------------------------------------------------------------------

def test_claim_is_atomic_two_devices_one_job(world):
    dev2, _ = _device(world, name="pc2", user=world["alice"])
    _queue(world)
    results, barrier = [], threading.Barrier(2)

    def go(d):
        barrier.wait()
        results.append(jobs.claim(d["device_id"]))

    threads = [threading.Thread(target=go, args=(d,)) for d in (world["dev"], dev2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    won = [r for r in results if r]
    assert len(results) == 2 and len(won) == 1   # exactly one runner got it
    assert won[0]["branch"] == f"dash/j{won[0]['job_id']}-e{won[0]['fence']}"
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT state, attempts FROM jobs")
        assert [(r["state"], r["attempts"]) for r in cur.fetchall()] == [("running", 1)]


def test_claim_never_crosses_accounts_or_users(world):
    _queue(world)
    beta_dev, _ = _device(world, account="beta", user=world["alice"])   # same person, another account
    assert jobs.claim(beta_dev["device_id"]) is None
    bob = _user(oid="bbbbbbbb-0000-4000-8000-000000000002", name="Bob")
    bob_dev, _ = _device(world, user=bob, name="bobs-pc")   # same account, another user: not his job
    assert jobs.claim(bob_dev["device_id"]) is None
    assert jobs.claim(world["dev"]["device_id"])["account_id"] == world["acme"]["id"]
    # a beta job is invisible to acme's runner even when queued by the same person
    _queue(world, task=world["btask"])
    assert jobs.claim(world["dev"]["device_id"]) is None


def test_subscription_runner_takes_only_approved_jobs(world):
    job = _queue(world, approved=False)
    assert jobs.claim(world["dev"]["device_id"]) is None   # started but not approved
    jobs.approve_job(world["acme"]["id"], job["id"], world["alice"]["id"])
    assert jobs.claim(world["dev"]["device_id"])["job_id"] == job["id"]
    with pytest.raises(NotFound):   # another account can't approve it
        jobs.approve_job(world["beta"]["id"], job["id"], world["alice"]["id"])


def test_api_runner_takes_only_unattended_eligible_jobs(world):
    api_dev, _ = _device(world, mode="api", user=world["alice"], name="api-box")
    attended = _queue(world, profile="chores", unattended=False)
    assert jobs.claim(api_dev["device_id"]) is None   # not marked unattended
    with get_cursor() as cur:
        cur.execute("DELETE FROM jobs")
    with pytest.raises(BadRequest):   # a profile that isn't unattended-eligible can't make an unattended job
        _queue(world, profile="builder", unattended=True)
    ok = _queue(world, profile="chores", unattended=True, approved=False)
    # eligibility is in the claim statement itself: a profile switched off after queueing stops it too
    with get_cursor() as cur:
        cur.execute("UPDATE agent_profiles SET unattended_ok = false WHERE name = 'chores'")
    try:
        assert jobs.claim(api_dev["device_id"]) is None
    finally:
        with get_cursor() as cur:
            cur.execute("UPDATE agent_profiles SET unattended_ok = true WHERE name = 'chores'")
    # and a raw insert of an unattended job on a profile that never was eligible is not claimable either
    with get_cursor() as cur:
        cur.execute("DELETE FROM jobs")
        cur.execute("INSERT INTO jobs (task_id, account_id, user_id, profile_id, unattended, approved_at) "
                    "SELECT %s, %s, %s, id, true, now() FROM agent_profiles WHERE name = 'builder'",
                    (world["task"]["id"], world["acme"]["id"], world["alice"]["id"]))
    assert jobs.claim(api_dev["device_id"]) is None
    with get_cursor() as cur:
        cur.execute("DELETE FROM jobs")
    ok = _queue(world, profile="chores", unattended=True, approved=False)
    claimed = jobs.claim(api_dev["device_id"])
    assert claimed["job_id"] == ok["id"] and attended["id"] != ok["id"]


def test_client_authored_tasks_never_run_unattended(world):
    api_dev, _ = _device(world, mode="api", user=world["alice"], name="api-box")
    for kind in ("created_by", "request_id", "requested_by"):
        task = _task(world["acme_app"], title=f"client {kind}", created_by="client:acme" if kind == "created_by" else "owner")
        with get_cursor() as cur:
            if kind == "request_id":
                cur.execute("UPDATE tasks SET client_request_id = 'r1' WHERE id = %s", (task["id"],))
            if kind == "requested_by":
                cur.execute("UPDATE tasks SET requested_by = 'a client' WHERE id = %s", (task["id"],))
        with pytest.raises(BadRequest):
            _queue(world, task=task, profile="chores", unattended=True)
        # even a row slipped in around create_job is not claimable by an API runner
        with get_cursor() as cur:
            cur.execute("INSERT INTO jobs (task_id, account_id, user_id, profile_id, unattended, approved_at) "
                        "SELECT %s, %s, %s, id, true, now() FROM agent_profiles WHERE name = 'chores'",
                        (task["id"], world["acme"]["id"], world["alice"]["id"]))
        assert jobs.claim(api_dev["device_id"]) is None
        # ...while a person who started and approved it on their own subscription runner may run it
        assert jobs.claim(world["dev"]["device_id"])["task_id"] == task["id"]
        with get_cursor() as cur:
            cur.execute("DELETE FROM jobs")


def test_payload_is_ids_only_and_revoked_device_claims_nothing(world):
    _queue(world)
    devices.revoke(world["acme"]["id"], world["dev"]["device_id"], "user:1")
    assert jobs.claim(world["dev"]["device_id"]) is None
    dev2, _ = _device(world, user=world["alice"], name="pc2")
    payload = jobs.claim(dev2["device_id"])
    blob = json.dumps(payload)
    assert "SECRET TASK TEXT" not in blob and "Fix the thing" not in blob
    assert set(payload) == {"job_id", "task_id", "project_id", "account_id", "fence", "branch", "base_branch", "job_token",
                            "lease_seconds", "max_seconds", "keep_open", "git_remote", "gemini_allowed", "profile", "mcp_servers", "skills", "pins"}
    assert payload["job_token"].startswith("cbj_") and payload["profile"]["model"] == "sonnet"


# --- job tokens ----------------------------------------------------------------------------------------------------

def test_job_token_is_a_runner_principal_every_session_surface_refuses(world, client, monkeypatch, mcp_caller):
    _queue(world)
    claim = jobs.claim(world["dev"]["device_id"])
    token = claim["job_token"]
    row = auth.resolve_token(token)
    assert row["stream"] == "job" and row["job_id"] == claim["job_id"] and row["account_id"] == world["acme"]["id"]
    assert auth.resolve_session_token(token) is None
    h = {"Authorization": f"Bearer {token}"}
    assert client.post("/api/servers/1/ssh-cert", json={"public_key": "ssh-ed25519 AAAA"}, headers=h).status_code == 403
    assert client.get("/api/servers", headers=h).status_code == 403
    with pytest.raises(Exception) as exc:   # the handler's own refusal too, not only require_session's
        api.api_ssh_cert(1, api.SshCertRequest(public_key="ssh-ed25519 AAAA"), row)
    assert getattr(exc.value, "status_code", None) == 403
    mcp_caller(token)   # the main /mcp refuses it
    assert mcp_server._current_session() is None
    # ...and the runner's device endpoints are not for a job token
    assert client.post("/runner/jobs/claim", headers=h).status_code == 403
    # the job MCP takes ONLY a job token
    _as_job(monkeypatch, world["dev"]["device_token"])
    with pytest.raises(PermissionError):
        job_mcp.job_brief(claim["fence"])
    _as_job(monkeypatch, None)
    with pytest.raises(PermissionError):
        job_mcp.job_brief(claim["fence"])


def test_job_token_dies_with_the_job(world):
    _queue(world)
    claim = jobs.claim(world["dev"]["device_id"])
    assert auth.resolve_token(claim["job_token"])
    jobs.done(claim["job_id"], claim["fence"], True)
    assert auth.resolve_token(claim["job_token"]) is None


# --- fencing -------------------------------------------------------------------------------------------------------

def test_stale_fence_is_409_on_heartbeat(world):
    """Fencing at the jobs layer (the socket tests in test_runner_socket_jobs.py carry the same over the wire; REST claim and
    heartbeat now refuse every caller)."""
    _queue(world)
    claim = jobs.claim(world["dev"]["device_id"])
    assert claim["fence"] > 0
    assert jobs.heartbeat(world["dev"]["device_id"], claim["job_id"], claim["fence"])["lease_seconds"] == config.JOB_LEASE_SECONDS
    with pytest.raises(jobs.StaleFence):
        jobs.heartbeat(world["dev"]["device_id"], claim["job_id"], claim["fence"] - 1)
    assert jobs.claim(world["dev"]["device_id"]) is None   # nothing else to do


def _run_after(world, first):
    """Claim the requeued job again (a newer fence, a new token)."""
    again = jobs.claim(world["dev"]["device_id"])
    assert again and again["fence"] > first["fence"]
    return again


def test_sweeper_requeues_with_a_new_fence_and_the_old_holder_gets_409(world, monkeypatch):
    job = _queue(world)
    first = jobs.claim(world["dev"]["device_id"])
    with get_cursor() as cur:
        cur.execute("UPDATE jobs SET lease_expires_at = clock_timestamp() - interval '1 second'")
    assert jobs.Sweeper().run() == {"requeued": 1, "failed": 0, "orphaned": 0, "graced": 0}
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT state, fence, device_id, branch FROM jobs WHERE id = %s", (job["id"],))
        row = cur.fetchone()
    assert row["state"] == "queued" and row["fence"] > first["fence"] and row["device_id"] is None and row["branch"] == ""
    # the sleeper wakes holding the old fence and its old token: the heartbeat is a 409 and the token resolves to nothing
    with pytest.raises(jobs.StaleFence):
        jobs.heartbeat(world["dev"]["device_id"], job["id"], first["fence"])
    assert auth.resolve_token(first["job_token"]) is None
    _as_job(monkeypatch, first["job_token"])
    for call in (lambda: job_mcp.job_comment(first["fence"], "late write"),
                 lambda: job_mcp.job_brief(first["fence"]),
                 lambda: job_mcp.release_lock(first["fence"], "merge", "acme-app")):
        with pytest.raises(PermissionError):
            call()
    # a VALID token presenting a stale fence is the 409 path (every MCP write checks it)
    live = _run_after(world, first)
    _as_job(monkeypatch, live["job_token"])
    for call in (lambda: job_mcp.job_comment(first["fence"], "late write"),
                 lambda: job_mcp.acquire_lock(first["fence"], "merge", "acme-app")):
        with pytest.raises(ValueError, match="409 Conflict"):
            call()
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT count(*) AS n FROM task_comments WHERE body = 'late write'")
        assert cur.fetchone()["n"] == 0
    # a fresh claim draws a newer fence again and a new branch
    second = live
    assert second["fence"] > first["fence"] and second["branch"].endswith(f"-e{second['fence']}")
    assert second["job_token"] != first["job_token"] and auth.resolve_token(first["job_token"]) is None


def test_sweeper_grants_grace_on_a_clock_jump(world):
    _queue(world)
    jobs.claim(world["dev"]["device_id"])
    clock = {"t": 1000.0}
    sweeper = jobs.Sweeper(monotonic=lambda: clock["t"])
    assert sweeper.run()["graced"] == 0   # first look just records the time
    with get_cursor() as cur:
        cur.execute("UPDATE jobs SET lease_expires_at = clock_timestamp() - interval '1 second'")
    # the database clock moved ~0s but the monotonic clock says an hour passed (or the reverse: a sleeping host)
    clock["t"] += 3600
    out = sweeper.run()
    assert out["graced"] == 1 and out["requeued"] == 0
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT state, lease_expires_at > clock_timestamp() AS live FROM jobs")
        assert dict(cur.fetchone()) == {"state": "running", "live": True}
    # steady time again, and a genuinely expired lease does expire
    with get_cursor() as cur:
        cur.execute("UPDATE jobs SET lease_expires_at = clock_timestamp() - interval '1 second'")
    assert sweeper.run()["requeued"] == 1


def test_a_job_tried_too_often_is_failed_for_a_human(world, monkeypatch):
    monkeypatch.setattr(config, "JOB_MAX_ATTEMPTS", 2)
    job = _queue(world)
    for expected in ("requeued", "failed"):
        jobs.claim(world["dev"]["device_id"])
        with get_cursor() as cur:
            cur.execute("UPDATE jobs SET lease_expires_at = clock_timestamp() - interval '1 second'")
        assert jobs.Sweeper().run()[expected] == 1
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT state FROM jobs WHERE id = %s", (job["id"],))
        assert cur.fetchone()["state"] == "failed"


# --- locks ---------------------------------------------------------------------------------------------------------

def _hold(claim, kind, slug="acme-app"):
    """A job holding a deploy/migrate lock. P5 refuses that through acquire_lock; an older or hand-made row can still exist,
    and the orphan rule must still hold."""
    with get_cursor() as cur:
        cur.execute("INSERT INTO locks (account_id, kind, resource, job_id, fence) SELECT j.account_id, %s, p.id::text, "
                    "j.id, j.fence FROM jobs j, projects p WHERE j.id = %s AND p.slug = %s",
                    (kind, claim["job_id"], slug))


def _run(world, task=None):
    _queue(world, task=task)
    return jobs.claim(world["dev"]["device_id"])


def test_expired_deploy_lock_is_orphaned_and_never_retried(world):
    claim = _run(world)
    _hold(claim, "deploy")
    jobs.acquire_lock(claim["job_id"], claim["fence"], "merge", "acme-app")
    with get_cursor() as cur:
        cur.execute("UPDATE jobs SET lease_expires_at = clock_timestamp() - interval '1 second'")
    out = jobs.Sweeper().run()
    assert out["orphaned"] == 1 and out["failed"] == 1 and out["requeued"] == 0   # NOT requeued: a human decides
    locks = {l["kind"]: l for l in jobs.list_locks(world["acme"]["id"])}
    assert set(locks) == {"deploy"} and locks["deploy"]["state"] == "orphaned"   # the merge lock simply went
    assert jobs.Sweeper().run()["requeued"] == 0
    # another job can't take it while it's orphaned...
    other = _run(world, task=_task(world["acme_app"], title="another"))
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT state FROM locks WHERE kind = 'deploy'")
        assert cur.fetchone()["state"] == "orphaned"
    # ...until a person clears it (and only that account's person)
    with pytest.raises(NotFound):
        jobs.clear_orphan(world["beta"]["id"], "deploy", str(world["acme_app"]["id"]), "user:1")
    jobs.clear_orphan(world["acme"]["id"], "deploy", str(world["acme_app"]["id"]), "user:1")
    assert jobs.list_locks(world["acme"]["id"]) == []


def test_locks_are_account_namespaced(world):
    claim = _run(world)
    with pytest.raises(NotFound):   # another account's project is NotFound exactly like a missing one
        jobs.acquire_lock(claim["job_id"], claim["fence"], "merge", "beta-app")
    with pytest.raises(NotFound):
        jobs.acquire_lock(claim["job_id"], claim["fence"], "merge", "no-such-project")
    with pytest.raises(BadRequest):
        jobs.acquire_lock(claim["job_id"], claim["fence"], "reboot", "acme-app")
    # each account holds its own lock at once: the key is (account, kind, resource)
    beta_dev, alice = _device(world, account="beta", user=world["alice"], name="beta-pc")
    _queue(world, task=world["btask"])
    bclaim = jobs.claim(beta_dev["device_id"])
    assert jobs.acquire_lock(claim["job_id"], claim["fence"], "merge", "acme-app")["acquired"]
    assert jobs.acquire_lock(bclaim["job_id"], bclaim["fence"], "merge", "beta-app")["acquired"]
    assert [l["resource"] for l in jobs.list_locks(world["beta"]["id"])] == [str(world["beta_app"]["id"])]
    # same account, same resource: the second job waits; the holder re-acquiring is idempotent; release frees it
    other = _run(world, task=_task(world["acme_app"], title="another"))
    assert jobs.acquire_lock(other["job_id"], other["fence"], "merge", "acme-app")["acquired"] is False
    assert jobs.acquire_lock(claim["job_id"], claim["fence"], "merge", "acme-app")["acquired"]
    assert jobs.release_lock(other["job_id"], other["fence"], "merge", "acme-app") == {"released": False}
    assert jobs.release_lock(claim["job_id"], claim["fence"], "merge", "acme-app") == {"released": True}
    assert jobs.acquire_lock(other["job_id"], other["fence"], "merge", "acme-app")["acquired"]


# --- the job MCP ---------------------------------------------------------------------------------------------------

def test_job_mcp_flow(world, monkeypatch):
    sub = repo.add_subtasks(world["task"]["id"], ["first step", "second step"], "owner")
    other_sub = repo.add_subtasks(world["btask"]["id"], ["not yours"], "owner")
    claim = _run(world)
    _as_job(monkeypatch, claim["job_token"])
    f = claim["fence"]
    brief = job_mcp.job_brief(f)
    assert brief["task"]["title"] == "Fix the thing" and brief["task"]["body"] == "SECRET TASK TEXT"
    assert [s["text"] for s in brief["subtasks"]] == ["first step", "second step"] and brief["branch"] == claim["branch"]
    assert "client_authored" not in brief["task"]
    sid = brief["subtasks"][0]["id"]
    assert job_mcp.job_tick_subtask(f, sid) == {"id": sid, "done": True}
    with pytest.raises(ValueError):   # a step of another task (another account's) is NotFound
        job_mcp.job_tick_subtask(f, other_sub[0]["id"])
    assert job_mcp.job_comment(f, "did it")["id"]
    assert job_mcp.acquire_lock(f, "merge", "acme-app")["acquired"]
    assert job_mcp.release_lock(f, "merge", "acme-app") == {"released": True}
    full = repo.get_task(world["task"]["id"])
    assert full["comments"][-1]["author"] == f"job:{claim['job_id']}"
    assert [s["done"] for s in full["subtasks"]] == [True, False] and sub
    assert not hasattr(job_mcp, "job_done")   # a device job ends only through the runner's `finish` (task text is untrusted)
    assert jobs.done(claim["job_id"], f, True, "all good") == {"job_id": claim["job_id"], "state": "done"}
    _as_job(monkeypatch, claim["job_token"])   # the token died with the job
    with pytest.raises(PermissionError):
        job_mcp.job_brief(f)


def _state_row(job_id):
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT state, finish_outcome, finish_summary, keep_open, summary FROM jobs WHERE id = %s", (job_id,))
        return cur.fetchone()


def test_job_finish_checks_the_fence_and_changes_nothing_when_stale(world, monkeypatch):
    claim = _run(world)
    _as_job(monkeypatch, claim["job_token"])
    before = len(repo.get_task(world["task"]["id"])["comments"])
    with pytest.raises(ValueError, match="409 Conflict"):
        job_mcp.job_finish(claim["fence"] + 1, "sneaky")
    assert _state_row(claim["job_id"])["finish_outcome"] is None
    assert len(repo.get_task(world["task"]["id"])["comments"]) == before
    for bad in (lambda: job_mcp.job_finish(claim["fence"], "x", "ended"), lambda: job_mcp.job_finish(claim["fence"], "  ")):
        with pytest.raises(ValueError):
            bad()
    assert _state_row(claim["job_id"])["finish_outcome"] is None


def test_job_finish_marks_finishing_comments_and_the_heartbeat_tells_the_runner(world, monkeypatch):
    claim = _run(world)
    f, jid = claim["fence"], claim["job_id"]
    assert jobs.heartbeat(world["dev"]["device_id"], jid, f)["finish"] is None
    _as_job(monkeypatch, claim["job_token"])
    out = job_mcp.job_finish(f, "built it and recorded checks")
    assert out["finishing"] and out["outcome"] == "done"
    row = _state_row(jid)
    assert row["state"] == "running" and row["finish_outcome"] == "done"   # finishing = running + finish mark: the agent cannot end its own job
    assert repo.get_task(world["task"]["id"])["comments"][-1]["body"] == "Job finished: built it and recorded checks"
    hb = jobs.heartbeat(world["dev"]["device_id"], jid, f)
    assert hb["finish"] == "done" and hb["keep_open"] is False
    jobs.finish(world["dev"]["device_id"], jid, f, True, "pushed")   # the runner ends it
    assert _state_row(jid)["state"] == "done"
    with pytest.raises(PermissionError):
        job_mcp.job_finish(f, "again")   # the token died with the job


def test_job_finish_blocked_needs_a_person(world, monkeypatch):
    claim = _run(world)
    _as_job(monkeypatch, claim["job_token"])
    job_mcp.job_finish(claim["fence"], "need the staging password", outcome="blocked")
    assert repo.get_task(world["task"]["id"])["comments"][-1]["body"].startswith("Job blocked, needs a person: need the staging")
    assert jobs.heartbeat(world["dev"]["device_id"], claim["job_id"], claim["fence"])["finish"] == "blocked"
    jobs.finish(world["dev"]["device_id"], claim["job_id"], claim["fence"], True, "no commits", outcome="blocked")
    row = _state_row(claim["job_id"])
    assert row["state"] == "done" and row["summary"].startswith("Blocked, needs a person: need the staging")


def test_a_finish_mark_never_survives_a_requeue(world, monkeypatch):
    claim = _run(world)
    f, jid = claim["fence"], claim["job_id"]
    _as_job(monkeypatch, claim["job_token"])
    job_mcp.job_finish(f, "done at the first attempt")
    assert jobs.heartbeat(world["dev"]["device_id"], jid, f)["finish"] == "done"
    with get_cursor() as cur:   # the lease lapses; the sweeper requeues it with a new fence
        cur.execute("UPDATE jobs SET lease_expires_at = now() - interval '1 minute' WHERE id = %s", (jid,))
    with get_cursor() as cur:
        cur.execute("SELECT * FROM jobs WHERE id = %s", (jid,))
        jobs.end_lease(cur, cur.fetchone(), "lapsed")
    row = _state_row(jid)
    assert row["state"] == "queued" and row["finish_outcome"] is None
    claim2 = jobs.claim(world["dev"]["device_id"])
    assert claim2["job_id"] == jid and claim2["fence"] != f
    assert jobs.heartbeat(world["dev"]["device_id"], jid, claim2["fence"])["finish"] is None
    # even a stale mark left in the row (finish_fence of the old attempt) is not honoured
    with get_cursor() as cur:
        cur.execute("UPDATE jobs SET finish_outcome = 'done', finish_fence = %s WHERE id = %s", (f, jid))
    assert jobs.heartbeat(world["dev"]["device_id"], jid, claim2["fence"])["finish"] is None


def test_a_job_whose_lease_expired_is_refused(world, monkeypatch):
    claim = _run(world)
    _as_job(monkeypatch, claim["job_token"])
    with get_cursor() as cur:
        cur.execute("UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id = %s", (claim["job_id"],))
    with pytest.raises(ValueError, match="409"):
        job_mcp.job_finish(claim["fence"], "late")
    with pytest.raises(jobs.StaleFence):
        jobs.heartbeat(world["dev"]["device_id"], claim["job_id"], claim["fence"])


def test_a_long_blocked_summary_cannot_crowd_out_the_runners_note(world, monkeypatch):
    claim = _run(world)
    _as_job(monkeypatch, claim["job_token"])
    job_mcp.job_finish(claim["fence"], "x" * 5000, outcome="blocked")
    assert len(_state_row(claim["job_id"])["finish_summary"]) == jobs.AGENT_SUMMARY_CAP
    jobs.finish(world["dev"]["device_id"], claim["job_id"], claim["fence"], True, "pushed 2 commit(s) to dash/jX; agent finished", outcome="blocked")
    s = _state_row(claim["job_id"])["summary"]
    assert len(s) <= 2000 and s.endswith("pushed 2 commit(s) to dash/jX; agent finished")


def test_keep_open_flag_rides_the_claim_and_heartbeat(world):
    job = jobs.create_job(world["task"]["id"], world["alice"]["id"], "builder", approved=True, keep_open=True)
    claim = jobs.claim(world["dev"]["device_id"])
    assert claim["job_id"] == job["id"] and claim["keep_open"] is True
    assert jobs.heartbeat(world["dev"]["device_id"], claim["job_id"], claim["fence"])["keep_open"] is True
    jobs.set_keep_open(world["acme"]["id"], job["id"], False)
    assert jobs.heartbeat(world["dev"]["device_id"], claim["job_id"], claim["fence"])["keep_open"] is False
    with pytest.raises(jobs.NotFound):   # another account's job
        jobs.set_keep_open(world["beta"]["id"], job["id"], True)


def test_the_brief_and_prompt_tell_the_agent_to_call_job_finish(world, monkeypatch):
    claim = _run(world)
    _as_job(monkeypatch, claim["job_token"])
    when = job_mcp.job_brief(claim["fence"])["when_done"]
    assert "job_finish" in when and "blocked" in when
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runner"))
    from crewboss_runner import spawn   # noqa: E402
    text = spawn.prompt_for({"job_id": 1, "task_id": 2, "fence": 3, "branch": "dash/j1-e3"})
    assert "job_finish" in text and "outcome='blocked'" in text
    assert "job_finish" in spawn.JOB_TOOLS


def test_client_authored_brief_carries_the_trust_note(world, monkeypatch):
    task = _task(world["acme_app"], title="client ask", created_by="client:acme")
    claim = _run(world, task=task)   # a person started and approved it: allowed, but flagged
    _as_job(monkeypatch, claim["job_token"])
    brief = job_mcp.job_brief(claim["fence"])
    assert brief["task"]["client_authored"] is True and "untrusted" in brief["task"]["trust_note"]


def test_job_mcp_is_mounted_and_refuses_without_a_job_token(world, client):
    assert client.get("/runner/mcp", follow_redirects=False).status_code == 307
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    h = {"accept": "application/json, text/event-stream", "content-type": "application/json", "host": "localhost"}
    r = client.post("/runner/mcp/", json=body, headers=h)
    assert r.status_code in (200, 400, 421)   # answered by the MCP app itself (no session yet: 400), not a project page or a 404
    assert "job_brief" in {t.name for t in job_mcp.mcp._tool_manager.list_tools()}


# --- the page ------------------------------------------------------------------------------------------------------

def test_runners_page_lists_jobs_and_clears_orphans(world, client, signed_in):
    h = {"cookie": signed_in["cookie"]}
    assert "Nothing running or queued" in client.get("/acme/machines", headers=h).text
    claim = _run(world)
    _hold(claim, "deploy")
    page = client.get("/acme/machines", headers=h).text
    assert f'data-job="{claim["job_id"]}"' in page and 'data-state="running"' in page and "builder" in page
    assert f'data-job-fence>{claim["fence"]}<' in page and claim["branch"] in page and "pc" in page
    assert f'data-job="{claim["job_id"]}"' not in client.get("/beta/machines", headers=h).text
    with get_cursor() as cur:
        cur.execute("UPDATE jobs SET lease_expires_at = clock_timestamp() - interval '1 second'")
    jobs.Sweeper().run()
    page = client.get("/acme/machines", headers=h).text
    assert 'data-lock-state="orphaned"' in page and "data-clear-lock" in page and 'data-state="failed"' in page
    r = client.post("/acme/runners/locks/clear", data={"kind": "deploy", "resource": str(world["acme_app"]["id"])},
                    headers=h, follow_redirects=False)
    assert r.status_code == 303 and jobs.list_locks(world["acme"]["id"]) == []


# --- revoke and disable reach the job token ------------------------------------------------------------------------

def test_revoke_mid_job_kills_the_token_and_requeues(world, monkeypatch):
    claim = _run(world)
    assert auth.resolve_token(claim["job_token"])
    devices.revoke(world["acme"]["id"], world["dev"]["device_id"], "user:1")
    assert auth.resolve_token(claim["job_token"]) is None   # 401 on every surface
    _as_job(monkeypatch, claim["job_token"])
    with pytest.raises(PermissionError):
        job_mcp.acquire_lock(claim["fence"], "merge", "acme-app")
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT state, fence, token_hash, device_id FROM jobs WHERE id = %s", (claim["job_id"],))
        row = cur.fetchone()
    assert row["state"] == "queued" and row["fence"] > claim["fence"] and row["token_hash"] is None and row["device_id"] is None
    with pytest.raises(jobs.StaleFence):
        jobs.heartbeat(world["dev"]["device_id"], claim["job_id"], claim["fence"])


def test_revoke_while_holding_a_deploy_lock_orphans_it(world):
    claim = _run(world)
    _hold(claim, "deploy")
    jobs.acquire_lock(claim["job_id"], claim["fence"], "merge", "acme-app")
    devices.revoke(world["acme"]["id"], world["dev"]["device_id"], "user:1")
    locks = {l["kind"]: l["state"] for l in jobs.list_locks(world["acme"]["id"])}
    assert locks == {"deploy": "orphaned"}   # merge went; deploy waits for a human
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT state FROM jobs WHERE id = %s", (claim["job_id"],))
        assert cur.fetchone()["state"] == "failed"   # never retried


def test_disabled_user_job_token_is_refused(world):
    claim = _run(world)
    with get_cursor() as cur:
        cur.execute("UPDATE users SET disabled_at = now() WHERE id = %s", (world["alice"]["id"],))
    try:
        assert auth.resolve_token(claim["job_token"]) is None
    finally:
        with get_cursor() as cur:
            cur.execute("UPDATE users SET disabled_at = NULL WHERE id = %s", (world["alice"]["id"],))
    assert auth.resolve_token(claim["job_token"])


def test_job_comment_satisfies_an_awaited_reply_in_the_fenced_transaction(world, monkeypatch):
    claim = _run(world)
    with get_cursor() as cur:
        cur.execute("INSERT INTO task_comments (task_id, author, body) VALUES (%s, 'owner', 'q') RETURNING id",
                    (world["task"]["id"],))
        cid = cur.fetchone()["id"]
        cur.execute("INSERT INTO awaited_replies (task_id, comment_id) VALUES (%s, %s)", (world["task"]["id"], cid))
    _as_job(monkeypatch, claim["job_token"])
    job_mcp.job_comment(claim["fence"], "answer")
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT replied_at FROM awaited_replies")
        assert cur.fetchone()["replied_at"] is not None
        cur.execute("SELECT count(*) AS n FROM activity WHERE actor = %s AND verb = 'commented'", (f"job:{claim['job_id']}",))
        assert cur.fetchone()["n"] == 1
