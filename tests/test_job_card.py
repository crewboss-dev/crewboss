"""The job card in the task window (#987): states from the moment Run is pressed, "waiting on you" for unreviewed agent files (a hold, not a
failed attempt), and the project Machines page's link to the account's page."""
import base64
import hashlib

import pytest

from app import access, agent_pins, job_card, jobs, tree
from app.db import get_cursor

from test_devices import est, signed_in  # noqa: F401
from test_jobs import _queue, world  # noqa: F401
from test_web_auth import auth_on  # noqa: F401

# what the 0.1.0 runner sends (no code): the rule id is in the reason
LEGACY = ("known-risky agent setup in the project's repository (fix it there): 82 agent/command/skill file(s) not reviewed by the "
          "project Owner (config.unreviewed: a.md, b.md +80 more); the Owner pins them on the Machines page")
AGENT = "---\nname: x\ndescription: d\n---\nGo.\n"


def _state(job_id):
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT state, attempts, hold_reason, hold_count FROM jobs WHERE id = %s", (job_id,))
        return cur.fetchone()


def _claim_release(w, reason=LEGACY, code=""):
    c = jobs.claim(w["dev"]["device_id"])
    assert c, "nothing claimable"
    jobs.release(w["dev"]["device_id"], c["job_id"], c["fence"], reason, code)
    return c


def _pending_file(w, c, path=".claude/agents/x.md"):
    jobs.agent_file_reported(w["dev"]["device_id"], c["job_id"], c["fence"], path, hashlib.sha256(AGENT.encode()).hexdigest(),
                             base64.b64encode(AGENT.encode()).decode(), "preflight")


# --- pure view ---------------------------------------------------------------------------------------------------------

def test_duration_and_branch_link():
    assert [job_card.duration(s) for s in (0, 25, 65, 3780)] == ["0 s", "25 s", "1 min 05 s", "1 h 03 min"]
    assert job_card.branch_url("https://github.com/acme/app.git", "dash/j1-e2") == "https://github.com/acme/app/tree/dash/j1-e2"
    assert job_card.branch_url("git@github.com:acme/app.git", "dash/j1") == "https://github.com/acme/app/tree/dash/j1"
    assert job_card.branch_url("https://git.example.test/a/b.git", "dash/j1") == ""
    assert job_card.branch_url("https://github.com/acme/app.git", "x; rm") == ""


@pytest.mark.parametrize("job, key, text", [
    ({"state": "queued", "queued_s": 12}, "queued", "Waiting for a runner"),
    ({"state": "queued", "queued_s": 12, "pin_device": "bobs-pc"}, "queued", "Waiting for bobs-pc"),
    ({"state": "running", "device": "bobs-pc", "running_s": 130, "branch": "dash/j1-e2"}, "running", "Claimed by bobs-pc"),
    ({"state": "done", "ran_s": 300, "branch": "dash/j1-e2", "summary": "Pushed."}, "done", "Done"),
    ({"state": "failed", "ran_s": 5, "summary": "boom"}, "failed", "Failed"),
    ({"state": "cancelled"}, "cancelled", "Stopped"),
])
def test_view_states(job, key, text):
    v = job_card.view({"id": 1, **job}, {"git_remote": "https://github.com/acme/app.git"})
    assert v["key"] == key and text in v["title"] and v["live"] == (key in ("queued", "running"))
    if key == "queued":
        assert "for 12 s" in v["detail"]
    if key == "done":
        assert v["branch_url"].endswith("/tree/dash/j1-e2") and v["note"] == "Pushed."
    if key == "failed":
        assert v["note"] == "boom"


def test_view_shows_the_agents_note_while_live_and_none_without_a_job():
    assert job_card.view(None, None) is None
    v = job_card.view({"id": 1, "state": "running", "device": "pc", "running_s": 1, "finish_summary": "Tests pass", "summary": "old"}, None)
    assert v["note"] == "Tests pass"


# --- waiting on you ----------------------------------------------------------------------------------------------------

def test_an_unreviewed_release_holds_the_job_and_costs_no_attempt(world):
    job = _queue(world)
    c0 = jobs.claim(world["dev"]["device_id"])
    _pending_file(world, c0)                       # the Owner has something to review: only then is it a hold
    jobs.release(world["dev"]["device_id"], c0["job_id"], c0["fence"], LEGACY)
    for _ in range(jobs.config.JOB_MAX_ATTEMPTS + 2):    # more releases than the cap would ever allow
        c = _claim_release(world) if _state(job["id"])["hold_reason"] is None else None
        assert _state(job["id"])["state"] == "queued"
    row = _state(job["id"])
    assert row["hold_reason"] == "agent_files" and row["attempts"] == 0 and row["hold_count"] == 82
    assert jobs.claim(world["dev"]["device_id"]) is None, "a held job is not claimable until the files are reviewed"


def test_the_runner_code_alone_holds_and_a_mixed_block_does_not(world):
    job = _queue(world)
    c0 = jobs.claim(world["dev"]["device_id"])
    _pending_file(world, c0)
    jobs.release(world["dev"]["device_id"], c0["job_id"], c0["fence"], "files not reviewed", "agent_files_unreviewed")
    assert _state(job["id"])["hold_reason"] == "agent_files"
    assert not jobs.is_unreviewed_release("2 file(s) (config.unreviewed: a.md); symlink.escape in .claude/agents/x.md")
    assert not jobs.is_unreviewed_release("no git credentials")


def test_pinning_the_last_file_releases_the_hold_and_the_job_runs(world, client, signed_in):
    job = _queue(world)
    c = jobs.claim(world["dev"]["device_id"])
    _pending_file(world, c)
    jobs.release(world["dev"]["device_id"], c["job_id"], c["fence"], LEGACY)
    assert _state(job["id"])["hold_reason"] == "agent_files"
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT id FROM project_agent_files")
        ids = [r["id"] for r in cur.fetchall()]
    r = client.post("/acme/machines/agent-files/pin", data={"ids": ids}, headers={"cookie": signed_in["cookie"]}, follow_redirects=False)
    assert r.status_code == 303 and _state(job["id"])["hold_reason"] is None
    again = jobs.claim(world["dev"]["device_id"])
    assert again and again["job_id"] == job["id"]


def test_the_card_route_shows_the_banner_with_the_link_and_stop(world, client, signed_in):
    h = {"cookie": signed_in["cookie"]}
    job = _queue(world)
    tid = world["task"]["id"]
    q = client.get(f"/acme-app/tasks/{tid}/run/card?was=live", headers=h)
    assert q.status_code == 200 and "Waiting for a runner" in q.text and 'hx-trigger="every 3s' in q.text and "data-run-waiting-you" not in q.text
    c = jobs.claim(world["dev"]["device_id"])
    _pending_file(world, c)
    _pending_file(world, c, ".claude/commands/go.md")
    jobs.release(world["dev"]["device_id"], c["job_id"], c["fence"], LEGACY)
    q = client.get(f"/acme-app/tasks/{tid}/run/card?was=live", headers=h)
    assert 'data-card="waiting_on_you"' in q.text and "Waiting on you" in q.text
    assert "Waiting for you: review 2 agent files before this project&#39;s jobs can run" in q.text or \
           "Waiting for you: review 2 agent files before this project's jobs can run" in q.text
    assert 'href="/acme/machines' not in q.text and "data-run-review-link" not in q.text   # 0.1.2: handled in the task's NOW block, no link away
    assert "failed" not in q.text.lower().split("data-run-waiting-you")[0].split("data-run-state")[-1][:120]
    # still live (queued, held): a Stop button, still polling
    jobs.cancel_job(world["acme"]["id"], job["id"], world["alice"]["id"])
    done = client.get(f"/acme-app/tasks/{tid}/run/card?was=live", headers=h)
    assert done.headers["hx-retarget"] == "#drawer-content" and "Stopped" in done.text   # ended: the whole record comes back (the Run form with it)


def test_the_task_panel_has_the_empty_activity_slot_for_89(world, client, signed_in):
    _queue(world)
    html = client.get(f"/acme-app/tasks/{world['task']['id']}/panel", headers={"cookie": signed_in["cookie"]}).text
    assert "data-job-activity" in html and "data-activity-body" in html and "data-activity-empty" in html
    assert "<details" in html.split("data-job-activity")[0][-40:]
    assert html.index("data-job-activity") > html.index("data-run-state")      # below the status, outside the polled element


# --- the project Machines page -----------------------------------------------------------------------------------------

def test_project_machines_page_links_the_accounts_page(world, client, signed_in):
    h = {"cookie": signed_in["cookie"]}
    page = client.get("/acme-app/machines", headers=h)
    assert page.status_code == 200 and "Runners belong to an account" in page.text
    assert 'href="/acme/machines?tab=runners"' in page.text and "Open Acme" in page.text and "#agent-files" not in page.text
    c = jobs.claim(world["dev"]["device_id"]) or (_queue(world), jobs.claim(world["dev"]["device_id"]))[1]
    _pending_file(world, c)
    page = client.get("/acme-app/machines", headers=h)
    assert 'href="/acme/machines?tab=runners#agent-files"' in page.text and "waiting for the Owner" in page.text
    # an account's own page is unchanged
    assert client.get("/acme/machines", headers=h).status_code == 200
