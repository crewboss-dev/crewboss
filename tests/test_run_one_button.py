"""Change #90: one-button Run. The profile and the runner are chosen from the task and the project; the outcome card replaces the button after a
job; the agent's finish call carries next steps, remaining work and tests (all optional); a project's allocated runners are the only ones that take
its jobs."""
import base64
import hashlib

import pytest

from app import job_card, jobs, run_plan, runs_on, tree
from app.db import get_cursor
from app.repo import BadRequest, NotFound

from test_devices import est, signed_in  # noqa: F401
from test_jobs import _task
from test_run_on_pc import _device, _set_labels, world as pcworld, _run, REMOTE, BOB_OID  # noqa: F401
from test_web_auth import auth_on  # noqa: F401


# --- the profile --------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("title, profile", [
    ("Fix the login button", "builder"), ("Add dark mode", "builder"),
    ("Review the new billing code", "code-auditor"), ("Code review: auth", "code-auditor"),
    ("Why does the sync double count?", "designer"), ("Research Android 15 permissions", "designer"),
    ("Deploy to production", None), ("Release 0.1.1", None),
])
def test_profile_for_task(title, profile):
    assert run_plan.profile_for({"title": title})["profile"] == profile


# --- the runner ---------------------------------------------------------------------------------------------------------------

@pytest.fixture
def two(pcworld, monkeypatch):
    bob = pcworld["bob"]
    a = _device(pcworld, bob, name="desk")
    b = _device(pcworld, bob, name="laptop")
    online = {a["device_id"], b["device_id"]}
    monkeypatch.setattr(runs_on, "_connected", lambda d: d in online)
    return {**pcworld, "a": a["device_id"], "b": b["device_id"], "online": online}


def _pick(w, requires=()):
    return run_plan.choose_runner(w["task"]["project_id"], w["bob"]["id"], list(requires))


def test_least_busy_online_runner_is_chosen_and_the_line_says_so(two):
    assert _pick(two)["device"]["id"] == two["a"]                              # a tie: the oldest
    with get_cursor() as cur:   # desk is busy with a running job
        cur.execute("INSERT INTO jobs (task_id, account_id, user_id, profile_id, state, device_id) SELECT %s, %s, %s, id, 'running', %s FROM agent_profiles WHERE name = 'builder'",
                    (two["btask"]["id"], two["acme"]["id"], two["bob"]["id"], two["a"]))
    assert _pick(two)["device"]["id"] == two["b"]
    p = run_plan.plan(two["task"], two["bob"]["id"])
    assert p["line"] == "Will run on laptop" and p["profile"] == "builder"


def test_allocation_narrows_the_choice_and_the_claim(two):
    with get_cursor() as cur:
        cur.execute("INSERT INTO project_runners (project_id, device_id) VALUES (%s, %s)", (two["task"]["project_id"], two["b"]))
    assert _pick(two)["device"]["id"] == two["b"]
    job = jobs.create_job(two["task"]["id"], two["bob"]["id"], "builder", approved=True)
    assert jobs.claim(two["a"]) is None, "a runner that is not allocated never takes this project's job"
    claim = jobs.claim(two["b"])
    assert claim and claim["job_id"] == job["id"]


def test_no_runner_online_queues_with_the_plain_line(two):
    two["online"].clear()
    p = run_plan.plan(two["task"], two["bob"]["id"])
    assert p["line"] == "No runner online for this project. It will start when one connects." and p["pin"] is None
    _set_labels(two, {"device_id": two["a"]}, ["android-usb"])
    two["online"].add(two["a"])
    miss = _pick(two, ["gpu"])
    assert miss["device"] is None and "label gpu" in miss["message"]
    assert _pick(two, ["android-usb"])["device"]["id"] == two["a"]


def test_a_manual_unknown_profile_is_refused(two):
    with pytest.raises(NotFound):
        run_plan.plan(two["task"], two["bob"]["id"], profile="nope")


# --- the page -----------------------------------------------------------------------------------------------------------------

def _panel(client, w, h):
    return client.get(f"/acme-app/tasks/{w['task']['id']}/panel", headers=h).text


def test_the_run_button_shows_only_when_there_is_something_to_run(two, client, signed_in):
    h = {"cookie": signed_in["cookie"]}
    html = _panel(client, two, h)
    assert "data-run-button" in html and ">Run</button>" in html and "Run on a PC" not in html and "Will run on desk" in html
    assert "data-run-more" in html and "More options" in html and "data-runs-on" not in client.get("/acme-app/list", headers=h).text
    assert _run(client, h, two).status_code == 200
    live = _panel(client, two, h)
    assert "data-run-button" not in live and "data-outcome" not in live and "data-job-activity" in live       # running: the card, not the button
    two["online"].clear()
    # a deploy task is never a job
    dep = _task(two["app"], title="Deploy to production")
    html = client.get(f"/acme-app/tasks/{dep['id']}/panel", headers=h).text
    assert "data-run-no-job" in html and "data-run-button" not in html


def test_no_runner_online_is_said_on_the_button_line(two, client, signed_in):
    two["online"].clear()
    html = _panel(client, two, {"cookie": signed_in["cookie"]})
    assert "No runner online for this project. It will start when one connects." in html and "data-run-no-runner" in html and "data-run-button" in html


def _finish(w, **kw):
    claim = jobs.claim(w["a"])
    assert claim
    jobs.agent_finish(claim["job_id"], claim["fence"], kw.pop("summary", "Fixed it"), kw.pop("outcome", "done"), **kw)
    jobs.finish(w["a"], claim["job_id"], claim["fence"], True, "", outcome="blocked" if kw.get("outcome") == "blocked" else "")
    return claim


def test_finish_call_is_additive_and_the_outcome_cards(two, client, signed_in, monkeypatch):
    h = {"cookie": signed_in["cookie"]}
    tid = two["task"]["id"]
    # DONE with tests and next steps
    jobs.create_job(tid, two["bob"]["id"], "builder", approved=True)
    c = jobs.claim(two["a"])
    jobs.agent_finish(c["job_id"], c["fence"], "Changed the button", "done", ["Review the change", 7], [], "pytest: 12 passed")
    jobs.finish(two["a"], c["job_id"], c["fence"], True, "pushed")
    oc = job_card.outcome(jobs.latest_for_task(tid))
    assert oc["key"] == "done" and oc["tests"] == "pytest: 12 passed" and oc["next_steps"] == ["Review the change"]
    html = _panel(client, two, h)
    for want in ('data-outcome="done"', "pytest: 12 passed", "Review the change", "Run the full tests", "Ready to deploy", "Mark task done", "data-run-button"):
        assert want in html or want == "data-run-button"
    assert "data-run-button" not in html, "the outcome card replaces the Run button"
    # NEEDS MORE WORK: the agent listed what is left
    jobs.create_job(tid, two["bob"]["id"], "builder", approved=True)
    c = jobs.claim(two["a"])
    jobs.agent_finish(c["job_id"], c["fence"], "Half done", "done", None, ["Handle the empty list", "Add a test"])
    jobs.finish(two["a"], c["job_id"], c["fence"], True, "")
    html = _panel(client, two, h)
    assert 'data-outcome="needs_work"' in html and "Handle the empty list" in html and "Add details for the AI" in html and "Ask CA" in html and "data-run-again" in html
    # the next run carries that list in its brief
    jobs.create_job(tid, two["bob"]["id"], "builder", approved=True)
    c = jobs.claim(two["a"])
    assert jobs.brief(c["job_id"], c["fence"])["previous_attempt"]["remaining_work"] == ["Handle the empty list", "Add a test"]
    # WAITING on you: the agent is blocked
    jobs.agent_finish(c["job_id"], c["fence"], "Which API version?", "blocked")
    jobs.finish(two["a"], c["job_id"], c["fence"], True, "", outcome="blocked")
    html = _panel(client, two, h)
    assert 'data-outcome="waiting"' in html and "Which API version?" in html
    # FAILED: plain reason + Run again / Ask CA
    jobs.create_job(tid, two["bob"]["id"], "builder", approved=True)
    c = jobs.claim(two["a"])
    jobs.done(c["job_id"], c["fence"], False, "The runner could not reach the git remote", device_id=two["a"])
    html = _panel(client, two, h)
    assert 'data-outcome="failed"' in html and "could not reach the git remote" in html and "data-run-again" in html and 'data-next="ask-ca"' in html


def test_old_finish_calls_without_the_new_fields_still_work(two):
    jobs.create_job(two["task"]["id"], two["bob"]["id"], "builder", approved=True)
    c = jobs.claim(two["a"])
    assert jobs.agent_finish(c["job_id"], c["fence"], "ok")["finishing"] is True
    row = jobs.latest_for_task(two["task"]["id"])
    assert row["remaining_work"] == [] and row["next_steps"] == [] and row["tests_run"] == ""


def test_not_fixed_box_sets_needs_more_work_with_the_words_and_run_again_carries_them(two, client, signed_in):
    h = {"cookie": signed_in["cookie"]}
    tid = two["task"]["id"]
    jobs.create_job(tid, two["bob"]["id"], "builder", approved=True)
    c = jobs.claim(two["a"])
    jobs.agent_finish(c["job_id"], c["fence"], "Done", "done")
    jobs.finish(two["a"], c["job_id"], c["fence"], True, "")
    assert 'data-outcome="done"' in _panel(client, two, h)
    r = client.post(f"/acme-app/tasks/{tid}/run/needs-work", data={"words": "Still crashes on a phone"}, headers=h)
    assert r.status_code == 200 and 'data-outcome="needs_work"' in r.text and "You said: Still crashes on a phone" in r.text
    assert client.post(f"/acme-app/tasks/{tid}/run/needs-work", data={"words": " "}, headers=h, follow_redirects=False).status_code in (303, 400)
    # Add details + Run again
    r = client.post(f"/acme-app/tasks/{tid}/run", data={"details": "Check the Android 15 case"}, headers=h)
    assert r.status_code == 200 and "data-run-again" not in r.text
    comments = [x["body"] for x in __import__("app.repo", fromlist=["x"]).get_task(tid)["comments"]]
    assert any("Check the Android 15 case" in b for b in comments) and any("Still crashes on a phone" in b for b in comments)
    assert tid and jobs.latest_for_task(tid)["needs_work_note"] is None            # carried by the run, then cleared


def test_next_step_buttons(two, client, signed_in):
    h = {"cookie": signed_in["cookie"]}
    tid = two["task"]["id"]
    jobs.create_job(tid, two["bob"]["id"], "builder", approved=True)
    c = jobs.claim(two["a"])
    jobs.agent_finish(c["job_id"], c["fence"], "Done", "done")
    jobs.finish(two["a"], c["job_id"], c["fence"], True, "")
    r = client.post(f"/acme-app/tasks/{tid}/run/review", headers=h)           # code-auditor profile exists: a read-only job
    assert r.status_code == 200
    j = jobs.latest_for_task(tid)
    assert j["profile"] == "code-auditor" and j["state"] == "queued"
    jobs.cancel_job(two["acme"]["id"], j["id"], two["bob"]["id"])
    r = client.post(f"/acme-app/tasks/{tid}/run/done", headers=h)
    assert r.status_code == 200 and __import__("app.repo", fromlist=["x"]).get_task(tid)["status"] == "done"


# --- settings -----------------------------------------------------------------------------------------------------------------

def test_project_settings_allocates_runners(two, client, signed_in):
    h = {"cookie": signed_in["cookie"]}
    page = client.get("/acme-app/settings", headers=h).text
    assert "Runners for this project" in page and "desk" in page and "laptop" in page
    r = client.post("/acme-app/settings/runners", data={"device_id": [str(two["b"])]}, headers=h, follow_redirects=False)
    assert r.status_code == 303 and run_plan.allocated(two["task"]["project_id"]) == [two["b"]]
    assert f'value="{two["b"]}" checked' in client.get("/acme-app/settings", headers=h).text
    client.post("/acme-app/settings/runners", data={}, headers=h, follow_redirects=False)
    assert run_plan.allocated(two["task"]["project_id"]) == []
