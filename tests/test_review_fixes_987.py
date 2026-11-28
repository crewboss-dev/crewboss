"""Review fixes for #987 / change #90: a hold only exists while the Owner has files to review and never lasts for ever; the project's runner allocation
holds on EVERY path that pins a job; follow-ups anchor on the builder branch; the poll is lean."""
import base64
import hashlib

import pytest

from app import access, agent_pins, job_card, jobs, run_plan, runs_on
from app.db import get_cursor
from app.repo import BadRequest

from test_devices import est, signed_in  # noqa: F401
from test_jobs import _queue, world  # noqa: F401
from test_job_card import LEGACY, AGENT, _state
from test_run_on_pc import _live_jobs
from test_web_auth import auth_on  # noqa: F401
import test_run_one_button as ob
from test_run_one_button import two, pcworld  # noqa: F401


def _claim(w):
    return jobs.claim(w["dev"]["device_id"])


def _report(w, c, path, text=AGENT, with_bytes=True):
    jobs.agent_file_reported(w["dev"]["device_id"], c["job_id"], c["fence"], path, hashlib.sha256(text.encode()).hexdigest(),
                             base64.b64encode(text.encode()).decode() if with_bytes else None, "preflight")


def _pending():
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT count(*) AS n FROM project_agent_files WHERE state = 'pending'")
        return cur.fetchone()["n"]


# --- 1. holds -------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("why", [
    "too big or not UTF-8: reported with no bytes",
    "more than MAX_AGENT_FILES: none reported at all",
    "socket dropped mid-report: nothing arrived",
    "0.1.0 text fallback only",
])
def test_nothing_to_review_means_a_counted_release_not_a_hold(world, why):
    job = _queue(world)
    c = _claim(world)
    if why.startswith("too big"):
        _report(world, c, ".claude/agents/big.md", with_bytes=False)      # a file the dashboard was sent no bytes for: nothing to pin
    assert _pending() == 0
    jobs.release(world["dev"]["device_id"], c["job_id"], c["fence"], LEGACY, "agent_files_unreviewed" if "0.1.0" not in why else "")
    row = _state(job["id"])
    assert row["hold_reason"] is None and row["attempts"] == 1 and row["state"] == "queued"
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT summary FROM jobs WHERE id = %s", (job["id"],))
    # at the cap it FAILS with the plain reason (never stuck)
    for _ in range(jobs.config.JOB_MAX_ATTEMPTS - 1):
        c = _claim(world)
        jobs.release(world["dev"]["device_id"], c["job_id"], c["fence"], LEGACY)
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT state, summary FROM jobs WHERE id = %s", (job["id"],))
        r = cur.fetchone()
    assert r["state"] == "failed" and "agent files flagged but none could be listed for review" in r["summary"]


def test_max_pending_cycle_pinning_what_was_listed_ends_in_a_counted_release(world, client, signed_in, monkeypatch):
    monkeypatch.setattr(agent_pins, "MAX_PENDING", 1)
    job = _queue(world)
    c = _claim(world)
    _report(world, c, ".claude/agents/a.md", AGENT + "a")
    _report(world, c, ".claude/agents/b.md", AGENT + "b")                    # past the cap: not recorded
    assert _pending() == 1
    jobs.release(world["dev"]["device_id"], c["job_id"], c["fence"], LEGACY)
    assert _state(job["id"])["hold_reason"] == "agent_files"
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT id FROM project_agent_files")
        ids = [r["id"] for r in cur.fetchall()]
    client.post("/acme/machines/agent-files/pin", data={"ids": ids}, headers={"cookie": signed_in["cookie"]}, follow_redirects=False)
    c = _claim(world)                                                         # released: the runner tries again, b.md is still unlisted
    assert c["job_id"] == job["id"]
    jobs.release(world["dev"]["device_id"], c["job_id"], c["fence"], LEGACY)
    assert _state(job["id"])["hold_reason"] is None, "no pending file: a normal release, not a hold that waits for ever"


def test_the_sweeper_releases_a_hold_when_the_files_are_purged_or_gone(world):
    job = _queue(world)
    c = _claim(world)
    _report(world, c, ".claude/agents/a.md")
    jobs.release(world["dev"]["device_id"], c["job_id"], c["fence"], LEGACY)
    assert _state(job["id"])["hold_reason"] == "agent_files"
    with get_cursor() as cur:   # the 30-day purge (or a delete): no pending file is left
        cur.execute("DELETE FROM project_agent_files")
    jobs.Sweeper().run()
    assert _state(job["id"])["hold_reason"] is None and jobs.claim(world["dev"]["device_id"])["job_id"] == job["id"]


def test_a_hold_fails_after_seven_days_with_a_plain_message(world):
    job = _queue(world)
    c = _claim(world)
    _report(world, c, ".claude/agents/a.md")
    jobs.release(world["dev"]["device_id"], c["job_id"], c["fence"], LEGACY)
    jobs.Sweeper().run()
    assert _state(job["id"])["state"] == "queued"                              # a fresh hold stays
    with get_cursor() as cur:
        cur.execute("UPDATE jobs SET hold_at = now() - interval '8 days' WHERE id = %s", (job["id"],))
    jobs.Sweeper().run()
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT state, summary FROM jobs WHERE id = %s", (job["id"],))
        r = cur.fetchone()
    assert r["state"] == "failed" and "7 days" in r["summary"] and "Run again" in r["summary"]


# --- 2. allocation on every pin path --------------------------------------------------------------------------------------------

@pytest.fixture
def alloc_b(two):
    with get_cursor() as cur:
        cur.execute("INSERT INTO project_runners (project_id, device_id) VALUES (%s, %s)", (two["task"]["project_id"], two["b"]))
    return two


def test_no_pin_path_queues_what_the_claim_would_refuse(alloc_b):
    w, pid, uid = alloc_b, alloc_b["task"]["project_id"], alloc_b["bob"]["id"]
    with pytest.raises(BadRequest, match="not allocated"):
        jobs.create_job(w["task"]["id"], uid, "builder", approved=True, pin_device_id=w["a"])
    with pytest.raises(BadRequest, match="not allocated"):
        runs_on.resolve(pid, uid, str(w["a"]))
    with pytest.raises(BadRequest, match="not allocated"):
        run_plan.plan(w["task"], uid, runner=str(w["a"]))
    with pytest.raises(BadRequest, match="not allocated"):
        runs_on.set_pref(pid, uid, w["a"])
    with get_cursor() as cur:
        cur.execute("DELETE FROM project_runners")
    job = jobs.create_job(w["task"]["id"], uid, "builder", approved=True, pin_device_id=w["a"])      # fine while nothing is allocated
    with get_cursor() as cur:
        cur.execute("INSERT INTO project_runners (project_id, device_id) VALUES (%s, %s)", (pid, w["b"]))
    with pytest.raises(BadRequest, match="not allocated"):
        runs_on.repin(job["id"], uid, w["a"])
    assert runs_on.repin(job["id"], uid, w["b"])["pin_device_id"] == w["b"]
    assert [d["id"] for d in runs_on.my_devices(w["acme"]["id"], uid, pid)] == [w["b"]]


def test_a_saved_runner_the_project_no_longer_allows_is_ignored_and_shown(alloc_b, client, signed_in):
    w, pid, uid = alloc_b, alloc_b["task"]["project_id"], alloc_b["bob"]["id"]
    with get_cursor() as cur:
        cur.execute("INSERT INTO project_runner_prefs (project_id, user_id, device_id) VALUES (%s, %s, %s)", (pid, uid, w["a"]))
    assert runs_on.get_pref(pid, uid) is None and runs_on.saved_pref_ignored(pid, uid) == "desk"
    html = client.get(f"/acme-app/tasks/{w['task']['id']}/panel", headers={"cookie": signed_in["cookie"]}).text
    assert "data-run-ignored-pref" in html and "desk" in html
    # the assistant's start/retry uses the allocated runner, not the ignored preference
    assert runs_on.plan_for_task(w["task"]["id"], uid, "builder")["pin"] == w["b"]


def _someone(w, name="Carol"):
    from test_devices import _user
    return _user(oid="cccccccc-0000-4000-8000-000000000003", name=name)


def test_starvation_is_said_plainly_and_nothing_is_queued_for_ever(two, client, signed_in):
    w, pid = two, two["task"]["project_id"]
    carol = _someone(w)
    with get_cursor() as cur:   # the project allocates only Carol's runner: none of bob's is allocated
        cur.execute("INSERT INTO device_tokens (user_id, account_id, name, mode, token_hash, pubkey, pubkey_fp, expires_at) "
                    "VALUES (%s, %s, 'theirs', 'subscription', 'zz', 'k', 'f', now() + interval '1 day') RETURNING id", (carol["id"], w["acme"]["id"]))
        cur.execute("INSERT INTO project_runners (project_id, device_id) VALUES (%s, %s)", (pid, cur.fetchone()["id"]))
    p = run_plan.plan(w["task"], w["bob"]["id"])
    assert "None of your runners is allocated to this project. Ask the owner to add one in Settings, Runners for this project" in p["message"] and p["refuse"]
    r = ob._run(client, {"cookie": signed_in["cookie"]}, w, follow_redirects=False) if False else ob._run(client, {"cookie": signed_in["cookie"]}, w)
    assert r.status_code in (303, 400) and not _live_jobs(w["task"]["id"]), "never queued for ever"
    # an already queued job says so on its card
    with get_cursor() as cur:
        cur.execute("INSERT INTO jobs (task_id, account_id, user_id, profile_id, approved_at) SELECT %s, %s, %s, id, now() FROM agent_profiles WHERE name = 'builder'",
                    (w["task"]["id"], w["acme"]["id"], w["bob"]["id"]))
    card = job_card.view(jobs.latest_for_task(w["task"]["id"]), None)
    assert card["title"] == "No runner can take this job" and "Ask the owner" in card["detail"]


def test_a_project_admin_allocates_the_runners_of_a_member(two):
    from app.routers import pages
    w, pid = two, two["task"]["project_id"]
    with get_cursor() as cur:
        admin = _someone(w, "Dee")["id"]
        cur.execute("INSERT INTO project_members (project_id, user_id, role) VALUES (%s, %s, 'admin')", (pid, admin))
    project = tree_project(pid)
    ra = pages._runner_alloc(access.principal_for_user_id(admin), project)
    assert ra and {d["name"] for d in ra["devices"]} >= {"desk", "laptop"}, "the admin sees the runners of a person who works this project"


def tree_project(pid):
    from app import repo
    return repo.get_project(pid)


# --- 3. follow-ups anchor on the builder branch -------------------------------------------------------------------------------

def test_followups_check_out_the_builder_branch_and_the_card_stays_on_the_builder_job(two, client, signed_in):
    w, tid = two, two["task"]["id"]
    h = {"cookie": signed_in["cookie"]}
    jobs.create_job(tid, w["bob"]["id"], "builder", approved=True)
    c = jobs.claim(w["a"])
    jobs.agent_finish(c["job_id"], c["fence"], "Built it", "done", None, [], "41 passed")
    jobs.finish(w["a"], c["job_id"], c["fence"], True, "")
    builder_branch = c["branch"]
    r = client.post(f"/acme-app/tasks/{tid}/run/review", headers=h)
    assert r.status_code == 200
    rc = jobs.claim(w["a"])
    assert rc["base_branch"] == builder_branch, "the review starts FROM the branch under review"
    jobs.agent_finish(rc["job_id"], rc["fence"], "No problems found", "done")
    jobs.finish(w["a"], rc["job_id"], rc["fence"], True, "")
    html = client.get(f"/acme-app/tasks/{tid}/panel", headers=h).text
    assert 'data-outcome="done"' in html and builder_branch in html and "No problems found" in html and 'data-followup="code-auditor"' in html
    assert "41 passed" in html                                         # still the BUILDER's outcome, with the review as a sub-result
    assert jobs.latest_builder_for_task(tid)["branch"] == builder_branch
    # Run again builds FROM the previous builder branch
    client.post(f"/acme-app/tasks/{tid}/run", headers=h)
    again = jobs.claim(w["a"])
    assert again["base_branch"] == builder_branch and jobs.brief(again["job_id"], again["fence"])["base_branch"] == builder_branch
    # a branch name that is not one of ours is never carried
    with get_cursor() as cur:
        cur.execute("UPDATE jobs SET state = 'cancelled' WHERE id = %s", (again["job_id"],))
    odd = jobs.create_job(tid, w["bob"]["id"], "builder", approved=True, base_branch="main; rm -rf /")
    assert odd["base_branch"] == ""


def test_the_owners_words_survive_until_the_brief_is_built_and_the_task_reopens(two, client, signed_in):
    w, tid, h = two, two["task"]["id"], {"cookie": signed_in["cookie"]}
    jobs.create_job(tid, w["bob"]["id"], "builder", approved=True)
    c = jobs.claim(w["a"])
    jobs.agent_finish(c["job_id"], c["fence"], "Done", "done")
    jobs.finish(w["a"], c["job_id"], c["fence"], True, "")
    from app import repo
    repo.update_task(tid, {"status": "done"}, "owner")
    client.post(f"/acme-app/tasks/{tid}/run/needs-work", data={"words": "Still crashes"}, headers=h)
    assert repo.get_task(tid)["status"] == "doing"                      # "it is not fixed": open work again
    client.post(f"/acme-app/tasks/{tid}/run", headers=h)
    n = jobs.claim(w["a"])
    assert jobs.brief(n["job_id"], n["fence"])["previous_attempt"]["owner_says"] == "Still crashes"
    assert jobs.brief(n["job_id"], n["fence"])["previous_attempt"]["owner_says"] == "Still crashes"       # a second brief read still has it


# --- 7. the lean poll -----------------------------------------------------------------------------------------------------------

def test_the_poll_backs_off_and_the_status_route_is_lean(two, client, signed_in, monkeypatch):
    w, tid, h = two, two["task"]["id"], {"cookie": signed_in["cookie"]}
    jobs.create_job(tid, w["bob"]["id"], "builder", approved=True)
    assert job_card.view({"id": 1, "state": "queued", "queued_s": 10}, None)["poll"] == 3
    assert job_card.view({"id": 1, "state": "queued", "queued_s": 300}, None)["poll"] == 15
    assert job_card.view({"id": 1, "state": "queued", "hold_reason": "agent_files", "queued_s": 5}, None)["poll"] == 15
    called = []
    from app.routers import pages
    monkeypatch.setattr(pages, "_task_ctx", lambda *a, **k: called.append(1) or {})
    r = client.get(f"/acme-app/tasks/{tid}/run/card?was=live", headers=h)
    assert r.status_code == 200 and 'data-job-state="queued"' in r.text and not called, "no full task context while the job is live"
    assert job_card.view({"id": 1, "state": "done", "ran_s": 3}, None)["live"] is False      # a finished card carries no poll
