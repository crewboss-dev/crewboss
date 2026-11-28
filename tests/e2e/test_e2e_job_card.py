"""Journey: the job card in the task window (#987). From the moment a job exists the task shows where it is, and the card updates by itself:
Queued (waiting for a runner, how long) -> Claimed by <runner> / Running -> Done with the branch link; a job a runner gave back for unreviewed agent
files shows "Waiting for you" with a button to the account's Machines page, never a failure. The Activity slot (change #89) is empty and collapsible."""
import hashlib

import pytest

from app import repo, tree
from app.db import get_cursor

TEXT = "---\nname: x\n---\nGo.\n"


@pytest.fixture
def world(seed):
    a = seed["a"]
    tree.set_details(a["id"], "owner", git_remote="https://github.com/acme/app.git")
    with get_cursor() as cur:
        cur.execute("INSERT INTO users (oid, display_name, role) VALUES ('11111111-0000-4000-8000-0000000000aa', 'Ann', 'owner') RETURNING id")
        uid = cur.fetchone()["id"]
        cur.execute("INSERT INTO device_tokens (user_id, account_id, name, mode, token_hash, pubkey, pubkey_fp, expires_at) "
                    "VALUES (%s, %s, 'anns-desktop', 'subscription', 'h', 'k', 'f', now() + interval '1 day') RETURNING id", (uid, a["id"]))
        did = cur.fetchone()["id"]
        cur.execute("SELECT id FROM agent_profiles WHERE name = 'builder'")
        pid = cur.fetchone()["id"]
        cur.execute("INSERT INTO jobs (task_id, account_id, user_id, profile_id, approved_at, approved_by) VALUES (%s, %s, %s, %s, now(), %s) RETURNING id",
                    (seed["t2"]["id"], a["id"], uid, pid, uid))
        jid = cur.fetchone()["id"]
    return {**seed, "job": jid, "dev": did, "task": seed["t2"]}


def _job_row(w, state, **cols):
    """Replace the task's job with one in `state` (columns by name)."""
    with get_cursor() as cur:
        cur.execute("SELECT user_id, profile_id FROM jobs WHERE id = %s", (w["job"],))
        j = cur.fetchone()
        cur.execute("DELETE FROM jobs WHERE task_id = %s", (w["task"]["id"],))
        base = {"task_id": w["task"]["id"], "account_id": w["a"]["id"], "user_id": j["user_id"], "profile_id": j["profile_id"], "state": state,
                "approved_at": None, **cols}
        names = ", ".join(base)
        cur.execute(f"INSERT INTO jobs ({names}) VALUES ({', '.join(['%s'] * len(base))}) RETURNING id", list(base.values()))
        w["job"] = cur.fetchone()["id"]


def _no_job(w):
    with get_cursor() as cur:
        cur.execute("DELETE FROM jobs WHERE task_id = %s", (w["task"]["id"],))


def _set(sql, *args):
    with get_cursor() as cur:
        cur.execute(sql, args)


def _open(page, server, w):
    page.goto(f"{server}/test-proj/tasks/{w['task']['id']}")
    page.wait_for_selector("[data-run-job]", state="attached")
    _unfold(page)
    page.evaluate("document.querySelector('[data-run]').scrollIntoView({block: 'start'})")


def _unfold(page):
    """#91: once a job has ended its card folds into the one compact "Last run" line; these journeys look at the card, so open it."""
    page.evaluate("document.querySelectorAll('[data-run-last]').forEach(function (d) { d.open = true; })")


def test_the_card_follows_the_job_with_no_reload(new_page, server, world, sign_in, device):
    page = new_page(device)
    sign_in(page)
    _open(page, server, world)
    page.evaluate("window.__keep = 1")
    assert "Waiting for a runner" in page.inner_text("[data-run-text]")
    assert page.get_attribute("[data-job-state]", "data-card") == "queued"
    page.shot("queued")
    _set("UPDATE jobs SET state = 'running', device_id = %s, claimed_at = now(), fence = 5, branch = 'dash/j1-e5', finish_summary = 'Reading the code' WHERE id = %s",
         world["dev"], world["job"])
    page.wait_for_function("document.querySelector('[data-run-text]').innerText.includes('Claimed by anns-desktop')", timeout=9000)
    assert "Reading the code" in page.inner_text("[data-run-note]")
    page.shot("running")
    _set("UPDATE jobs SET state = 'done', finished_at = now(), summary = 'Pushed the branch' WHERE id = %s", world["job"])
    page.wait_for_selector("[data-card='done']", state="attached", timeout=9000)
    _unfold(page)
    link = page.locator("a[data-run-branch]")
    assert link.get_attribute("href") == "https://github.com/acme/app/tree/dash/j1-e5"
    assert "Pushed the branch" in page.inner_text("[data-run-note]")
    assert page.evaluate("window.__keep") == 1, "the card updated without a page reload"
    page.shot("done")


def test_failed_shows_the_plain_reason(new_page, server, world, sign_in):
    page = new_page("desktop")
    sign_in(page)
    _set("UPDATE jobs SET state = 'failed', finished_at = now(), claimed_at = now() - interval '10 seconds', summary = 'The runner could not reach the git remote' WHERE id = %s", world["job"])
    _open(page, server, world)
    assert page.get_attribute("[data-job-state]", "data-card") == "failed"
    assert "could not reach the git remote" in page.inner_text("[data-run-note]")
    page.shot("failed")


def test_waiting_on_you_is_handled_in_the_task_window(new_page, server, world, sign_in, device):
    page = new_page(device)
    sign_in(page)
    for i in range(3):
        body = (TEXT + str(i)).encode()
        _set("INSERT INTO project_agent_files (project_id, path, sha256, content) VALUES (%s, %s, %s, %s)",
             world["a"]["id"], f".claude/agents/a{i}.md", hashlib.sha256(body).hexdigest(), body)
    _set("UPDATE jobs SET hold_reason = 'agent_files', hold_count = 3 WHERE id = %s", world["job"])
    _open(page, server, world)
    assert page.get_attribute("[data-job-state]", "data-card") == "waiting_on_you"
    assert "Waiting for you: review 3 agent files before this project's jobs can run" in page.inner_text("[data-run-waiting-you]")
    page.shot("waiting-on-you")
    assert page.locator("[data-run-review-link]").count() == 0                # no jump to Machines
    assert "3 agent files Crewboss hasn't checked yet" in page.inner_text("[data-trust-text]")
    page.locator("[data-trust-files] > summary").click()
    assert "a0.md" in page.inner_text("[data-trust-files]")
    assert page.locator("[data-now-primary]").count() == 1


def test_activity_slot_is_empty_and_collapsible(new_page, server, world, sign_in):
    page = new_page("desktop")
    sign_in(page)
    _open(page, server, world)
    slot = page.locator("[data-job-activity]")
    assert slot.get_attribute("open") is None and page.locator("[data-activity-body]").is_hidden()
    slot.locator("summary").click()
    assert page.locator("[data-activity-body]").is_visible()                 # (#89's live view fills it, so it is no longer always the empty line)
    _set("UPDATE jobs SET state = 'running', claimed_at = now(), device_id = %s WHERE id = %s", world["dev"], world["job"])
    page.wait_for_function("document.querySelector('[data-run-text]').innerText.includes('Claimed by')", timeout=9000)
    assert slot.get_attribute("open") is not None, "the poll must not close the Activity slot"
    page.shot("activity-open")


# --- change #90: the one Run button and the outcome card -----------------------------------------------------------------------

def test_run_button_with_and_without_a_runner_online(new_page, server, world, sign_in, monkeypatch, device):
    from app import runner_ws
    page = new_page(device)
    sign_in(page)
    _set("UPDATE device_tokens SET user_id = (SELECT id FROM users WHERE is_owner LIMIT 1) WHERE id = %s", world["dev"])   # the runner is the signed-in person's
    _no_job(world)
    monkeypatch.setattr(runner_ws, "_conns", {})
    _open_idle(page, server, world)
    assert page.inner_text("[data-run-button]") == "Run" and "No runner online for this project. It will start when one connects." in page.inner_text("[data-run-plan-line]")
    assert "Run on a PC" not in page.content() and page.locator("[data-runs-on]").count() == 0
    page.shot("no-runner-online")
    monkeypatch.setattr(runner_ws, "_conns", {world["dev"]: type("Conn", (), {"verified": True, "post": lambda self, m: None})()})
    _open_idle(page, server, world)
    assert "Will run on anns-desktop" in page.inner_text("[data-run-plan-line]")
    page.locator("[data-run-more] [data-run-gear]").click()                 # the settings icon beside Run (#91)
    page.wait_for_selector("[data-run-profile]", state="visible")
    assert page.locator("[data-run-profile]").is_visible()
    page.shot("run-button-runner-online-more-options")


def _open_idle(page, server, w):
    page.goto(f"{server}/test-proj/tasks/{w['task']['id']}")
    page.wait_for_selector("[data-run] >> visible=true")
    _unfold(page)     # 0.1.2: the outcome card lives inside the folded "Last run" line
    page.evaluate("document.querySelector('[data-run]').scrollIntoView({block: 'start'})")


@pytest.mark.parametrize("key, cols, expect", [
    ("done", {"state": "done", "finish_outcome": "done", "branch": "dash/j9-e1", "finish_summary": "Fixed the button", "tests_run": "pytest: 41 passed",
              "next_steps": '["Review the change"]'}, ["Review the change", "Run the full tests", "Ready to deploy", "Mark task done", "pytest: 41 passed"]),
    ("needs_work", {"state": "done", "finish_outcome": "done", "finish_summary": "Half done", "remaining_work": '["Handle the empty list", "Add a test"]'},
     ["Handle the empty list", "Run again", "Add details for the AI", "Ask CA"]),
    ("waiting", {"state": "done", "finish_outcome": "blocked", "finish_summary": "Which API version should I target?"}, ["Which API version", "Answer in a comment"]),
    ("failed", {"state": "failed", "summary": "The runner could not reach the git remote"}, ["could not reach the git remote", "Run again", "Ask CA"]),
])
def test_outcome_cards(new_page, server, world, sign_in, key, cols, expect, device):
    page = new_page(device)
    sign_in(page)
    base = {"claimed_at": None, "finished_at": None}
    _job_row(world, cols["state"], **{k: v for k, v in cols.items() if k != "state"})
    _set("UPDATE jobs SET finished_at = now(), claimed_at = now() - interval '2 minutes' WHERE id = %s", world["job"])
    _open_idle(page, server, world)
    assert page.get_attribute("[data-outcome]", "data-outcome") == key
    text = page.inner_text("[data-outcome]")
    for e in expect:
        assert e in text, (e, text)
    assert page.locator("[data-run-button]").count() == 0, "the outcome card replaces the Run button"
    page.shot(f"outcome-{key}")


def test_needs_work_by_the_owners_words_and_the_now_line(new_page, server, world, sign_in):
    page = new_page("desktop")
    sign_in(page)
    _job_row(world, "done", finish_outcome="done", finish_summary="Fixed", branch="dash/j9-e1")
    _set("UPDATE jobs SET finished_at = now() WHERE id = %s", world["job"])
    _open_idle(page, server, world)
    page.locator("[data-not-fixed] summary").click()
    page.fill("[data-not-fixed] textarea", "It still crashes on my phone")
    page.locator("[data-needs-work]").click()
    page.wait_for_selector("[data-outcome='needs_work']", state="attached")
    _unfold(page)      # the card stays folded in the Last run line after the swap
    page.wait_for_selector("[data-outcome='needs_work']")
    assert "You said: It still crashes on my phone" in page.inner_text("[data-outcome-owner-note]")
    page.shot("outcome-needs-work-owner-words")


LONG_TITLE = "Cancelling a paused case with no/invalid reason ends its SLA pause but leaves the case closed when it should be reopened"
OLD_FAIL = ("released by its runner: 83 agent/command/skill file(s) not reviewed by the project Owner (config.unreviewed: settings.json, "
            "SKILL.md +80 more); the Owner pins… and the job has been tried 3 times")


@pytest.mark.parametrize("device", ["desktop", "phone"])
def test_failed_old_unreviewed_job_task_window(new_page, server, world, sign_in, device):
    """0.1.2: a failed job (the pre-0.1.1 unreviewed-files failure): the title wraps, NOW has the ONE primary button (Trust these files and run, in place),
    and the full outcome card stays folded inside the Last run line."""
    page = new_page(device)
    sign_in(page)
    page.context.add_init_script("try { localStorage.setItem('crewboss-theme', 'dark'); } catch (e) {}")      # the screenshots are the dark theme
    repo.update_task(world["task"]["id"], {"title": LONG_TITLE}, "owner")
    for i in range(3):
        body = (TEXT + str(i)).encode()
        _set("INSERT INTO project_agent_files (project_id, path, sha256, content) VALUES (%s, %s, %s, %s)",
             world["a"]["id"], f".claude/skills/s{i}/SKILL.md", hashlib.sha256(body).hexdigest(), body)
    _job_row(world, "failed", finished_at="now()", summary=OLD_FAIL)
    page.goto(f"{server}/test-proj/tasks/{world['task']['id']}")
    page.wait_for_selector("[data-task-now]")
    page.shot("failed-job")                                           # the screenshot comes first, so the same test can run on the old code
    box = page.locator("[data-task-title]").bounding_box()
    assert page.evaluate("(() => { const t = document.querySelector('[data-task-title]'); return t.scrollHeight <= t.clientHeight + 2; })()")
    assert box["height"] > 30                                         # wrapped onto more than one line, nothing clipped
    assert page.locator("[data-task-now] .btn-primary").count() == 1
    assert page.locator("[data-now-primary]").inner_text().strip() == "Trust these files and run"
    assert page.locator("[data-run-last]").get_attribute("open") is None
    assert page.locator("[data-outcome]").is_hidden()
    _set("INSERT INTO project_agent_files (project_id, path, sha256, content) VALUES (%s, %s, %s, %s)", world["a"]["id"], ".claude/settings.json",
         hashlib.sha256(b'{"hooks": {"Stop": "echo SETTINGS-INLINE"}}').hexdigest(), b'{"hooks": {"Stop": "echo SETTINGS-INLINE"}}')
    page.goto(f"{server}/test-proj/tasks/{world['task']['id']}")             # drawn again with the settings file in the list
    page.wait_for_selector("[data-task-now]")
    if page.locator("[data-trust-files]").get_attribute("open") is None:
        page.locator("[data-trust-files] > summary").click()
    assert "SKILL.md" in page.inner_text("[data-trust-files]") and "SETTINGS-INLINE" in page.inner_text("[data-trust-files]")
    assert page.locator("a[href*='/machines']").filter(has_text="Review").count() == 0
    page.shot("failed-job-files-open")
    page.locator("[data-now-primary]").click()                        # pins and starts a fresh run, right on this page
    page.wait_for_selector("[data-trust-run]", state="detached")
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT count(*) n FROM project_agent_files WHERE project_id = %s AND state = 'pinned'", (world["a"]["id"],))
        assert cur.fetchone()["n"] == 4
