"""Runs on (#810, migration 0090): which of MY runners a project's jobs go to. Saved per person per project; copied into jobs.pin_device_id; the
claim rule honours it; the header pill, the Run form default and the offline offer. A job is NEVER moved by the system."""
from datetime import datetime, timedelta, timezone

import pytest

from app import devices, jobs, runner_ws, runs_on
from app.db import get_cursor
from app.repo import BadRequest, NotFound

from test_devices import est, signed_in  # noqa: F401
from test_run_on_pc import _device, _live_jobs, _run, _set_labels, world  # noqa: F401
from test_web_auth import auth_on  # noqa: F401


class Conn:
    verified = True

    def post(self, msg):
        pass


def _online(monkeypatch, *devs):
    monkeypatch.setattr(runner_ws, "_conns", {d["device_id"]: Conn() for d in devs})


def _two(world):
    return _device(world, world["bob"], name="desktop"), _device(world, world["bob"], name="laptop")


def _h(signed_in):
    return {"cookie": signed_in["cookie"]}


def test_the_choice_is_saved_per_person_per_project_and_only_for_their_own_runner(world):
    desktop, laptop = _two(world)
    p, bob = world["app"]["id"], world["bob"]["id"]
    assert runs_on.get_pref(p, bob) is None
    runs_on.set_pref(p, bob, desktop["device_id"])
    assert runs_on.get_pref(p, bob) == desktop["device_id"]
    runs_on.set_pref(p, bob, laptop["device_id"])                                   # one row per (project, person)
    assert runs_on.get_pref(p, bob) == laptop["device_id"]
    other = devices_of_someone_else(world)
    with pytest.raises(NotFound):
        runs_on.set_pref(p, bob, other["device_id"])                                # never another person's runner
    other_project = world["bapp"]["id"]
    assert runs_on.get_pref(other_project, bob) is None                             # per project
    runs_on.set_pref(p, bob, None)
    assert runs_on.get_pref(p, bob) is None
    runs_on.set_pref(p, bob, desktop["device_id"])
    devices.revoke(world["acme"]["id"], desktop["device_id"], "user:1")
    assert runs_on.get_pref(p, bob) is None                                         # a revoked runner reads as "any"


def devices_of_someone_else(world):
    from test_devices import _user
    alice = _user(oid="aaaaaaaa-0000-4000-8000-000000000001", name="Alice2")
    return _device(world, alice, name="alices-pc")


def test_a_pinned_job_goes_only_to_its_runner(world):
    desktop, laptop = _two(world)
    job = jobs.create_job(world["task"]["id"], world["bob"]["id"], "builder", approved=True, pin_device_id=desktop["device_id"])
    assert job["pin_device_id"] == desktop["device_id"]
    assert jobs.claimable_devices(job["id"]) == [desktop["device_id"]]              # the nudges use the same rule
    assert jobs.claimable_jobs(laptop["device_id"]) == [] and jobs.claim(laptop["device_id"]) is None
    assert jobs.claim(desktop["device_id"])["job_id"] == job["id"]


def test_an_unpinned_job_still_goes_to_any_of_theirs(world):
    desktop, laptop = _two(world)
    job = jobs.create_job(world["task"]["id"], world["bob"]["id"], "builder", approved=True)
    assert job["pin_device_id"] is None and set(jobs.claimable_devices(job["id"])) == {desktop["device_id"], laptop["device_id"]}


def test_a_job_cannot_be_pinned_to_someone_elses_runner(world):
    theirs = devices_of_someone_else(world)
    with pytest.raises(BadRequest):
        jobs.create_job(world["task"]["id"], world["bob"]["id"], "builder", approved=True, pin_device_id=theirs["device_id"])


def test_the_run_form_chooses_by_itself_and_can_override(world, client, signed_in, monkeypatch):
    h = _h(signed_in)
    desktop, laptop = _two(world)
    _online(monkeypatch, laptop)
    drawer = client.get(f"/acme-app/tasks/{world['task']['id']}/panel", headers=h).text
    assert "data-run-runner" in drawer and "Automatic" in drawer and "Any of my runners" in drawer and "Will run on laptop" in drawer   # change #90: chosen by itself
    _run(client, h, world, runner="")
    (job,) = _live_jobs(world["task"]["id"])
    assert job["pin_device_id"] == laptop["device_id"]
    with get_cursor() as cur:
        cur.execute("DELETE FROM jobs")
    _run(client, h, world, runner="any")
    assert _live_jobs(world["task"]["id"])[0]["pin_device_id"] is None
    with get_cursor() as cur:
        cur.execute("DELETE FROM jobs")
    _run(client, h, world, runner=str(desktop["device_id"]), queue_offline="1")      # More options: this one job
    assert _live_jobs(world["task"]["id"])[0]["pin_device_id"] == desktop["device_id"]


def test_an_offline_runner_chosen_by_hand_offers_a_choice_and_queues_nothing(world, client, signed_in, monkeypatch):
    h = _h(signed_in)
    desktop, laptop = _two(world)
    runs_on.set_pref(world["app"]["id"], world["bob"]["id"], desktop["device_id"])
    _online(monkeypatch, laptop)                                                    # the desktop is off, the laptop is on
    r = _run(client, h, world, runner=str(desktop["device_id"]))                    # chosen by hand under More options
    assert r.status_code == 200 and "data-run-offer" in r.text and "desktop</strong> is offline" in r.text
    assert "Run on laptop" in r.text and "Queue for desktop" in r.text and "data-make-default" in r.text
    assert _live_jobs(world["task"]["id"]) == []                                    # nothing was queued behind the person's back
    # "Queue for desktop": it waits for that runner, pinned
    _run(client, h, world, runner=str(desktop["device_id"]), queue_offline="1")
    (job,) = _live_jobs(world["task"]["id"])
    assert job["pin_device_id"] == desktop["device_id"] and job["state"] == "queued"
    assert jobs.claim(laptop["device_id"]) is None                                  # and the laptop does NOT take it
    with get_cursor() as cur:
        cur.execute("DELETE FROM jobs")
    # "Run on laptop" with the tick: the laptop becomes the default
    _run(client, h, world, runner=str(laptop["device_id"]), make_default="1")
    assert _live_jobs(world["task"]["id"])[0]["pin_device_id"] == laptop["device_id"]
    assert runs_on.get_pref(world["app"]["id"], world["bob"]["id"]) == laptop["device_id"]


def test_no_offer_when_the_runner_is_online_or_there_is_nowhere_else(world, client, signed_in, monkeypatch):
    h = _h(signed_in)
    desktop, laptop = _two(world)
    runs_on.set_pref(world["app"]["id"], world["bob"]["id"], desktop["device_id"])
    _online(monkeypatch, desktop, laptop)
    assert "data-run-offer" not in _run(client, h, world).text and len(_live_jobs(world["task"]["id"])) == 1
    with get_cursor() as cur:
        cur.execute("DELETE FROM jobs")
    _online(monkeypatch)                                                            # everything is off: queue it, nothing to offer
    assert "data-run-offer" not in _run(client, h, world).text and len(_live_jobs(world["task"]["id"])) == 1


def test_the_offer_respects_labels(world, client, signed_in, monkeypatch):
    h = _h(signed_in)
    desktop, laptop = _two(world)
    _set_labels(world, desktop, "android-usb")                                      # only the desktop has the phone
    runs_on.set_pref(world["app"]["id"], world["bob"]["id"], desktop["device_id"])
    _online(monkeypatch, laptop)
    r = _run(client, h, world, profile="android")
    assert "data-run-offer" not in r.text                                           # the laptop could not take an android job: no offer


def test_a_job_stuck_behind_an_offline_runner_is_offered_a_move_never_moved(world, client, signed_in, monkeypatch):
    h = _h(signed_in)
    desktop, laptop = _two(world)
    job = jobs.create_job(world["task"]["id"], world["bob"]["id"], "builder", approved=True, pin_device_id=desktop["device_id"])
    _online(monkeypatch, laptop)
    panel = f"/acme-app/tasks/{world['task']['id']}/panel"
    assert "data-run-stuck" not in client.get(panel, headers=h).text               # not yet: it has waited under 10 minutes
    with get_cursor() as cur:
        cur.execute("UPDATE jobs SET created_at = %s WHERE id = %s", (datetime.now(timezone.utc) - timedelta(minutes=11), job["id"]))
    page = client.get(panel, headers=h).text
    assert "data-run-stuck" in page and "Run on laptop" in page and "Waiting for desktop" in page
    assert jobs.claim(laptop["device_id"]) is None and _live_jobs(world["task"]["id"])[0]["pin_device_id"] == desktop["device_id"]
    r = client.post(f"/acme-app/tasks/{world['task']['id']}/run/move", data={"device_id": laptop["device_id"]}, headers=h)
    assert r.status_code == 200
    assert _live_jobs(world["task"]["id"])[0]["pin_device_id"] == laptop["device_id"]
    assert jobs.claim(laptop["device_id"])["job_id"] == job["id"]


def test_only_the_starter_moves_a_queued_job_to_a_runner_that_has_the_labels(world):
    desktop, laptop = _two(world)
    job = jobs.create_job(world["task"]["id"], world["bob"]["id"], "android", approved=True)
    with pytest.raises(BadRequest):
        runs_on.repin(job["id"], world["bob"]["id"], laptop["device_id"])           # the laptop lacks android-usb
    with pytest.raises(NotFound):
        runs_on.repin(job["id"], world["bob"]["id"] + 99, laptop["device_id"])      # not their job
    _set_labels(world, laptop, "android-usb")
    assert runs_on.repin(job["id"], world["bob"]["id"], laptop["device_id"])["pin_device_id"] == laptop["device_id"]
    jobs.claim(laptop["device_id"])
    with pytest.raises(BadRequest):
        runs_on.repin(job["id"], world["bob"]["id"], desktop["device_id"])          # already running: left alone


def test_the_header_pill_is_gone_but_the_old_forms_and_label_button_still_work(world, client, signed_in, monkeypatch):
    h = _h(signed_in)
    desktop, laptop = _two(world)
    _set_labels(world, desktop, "android-usb")
    _online(monkeypatch, desktop)
    assert "data-runs-on" not in client.get("/acme-app/list", headers=h).text      # change #90: runners belong to the project, Run chooses
    # picking from the pill saves; the guard's button adds the label
    r = client.post("/acme-app/runs-on", data={"device_id": str(desktop["device_id"]), "next": "/acme-app/list"}, headers=h, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/acme-app/list"
    assert runs_on.get_pref(world["app"]["id"], world["bob"]["id"]) == desktop["device_id"]
    r = client.post("/acme-app/runs-on/label", data={"device_id": laptop["device_id"], "label": "android-usb", "next": "/acme-app/list"},
                    headers=h, follow_redirects=False)
    assert r.status_code == 303 and "android-usb" in runs_on.my_devices(world["acme"]["id"], world["bob"]["id"])[1]["labels"]
    r = client.post("/acme-app/runs-on", data={"device_id": "", "next": "//evil.example"}, headers=h, follow_redirects=False)
    assert r.headers["location"] == "/acme-app" and runs_on.get_pref(world["app"]["id"], world["bob"]["id"]) is None   # open redirects refused


def test_the_label_button_only_adds_labels_a_profile_asks_for(world, client, signed_in):
    desktop, _ = _two(world)
    r = client.post("/acme-app/runs-on/label", data={"device_id": desktop["device_id"], "label": "root-access"}, headers=_h(signed_in),
                    follow_redirects=False)
    assert r.status_code in (303, 400) and "root-access" not in runs_on.my_devices(world["acme"]["id"], world["bob"]["id"])[0]["labels"]


def test_the_machines_page_says_what_a_runner_is_the_default_for(world, client, signed_in):
    desktop, laptop = _two(world)
    runs_on.set_pref(world["app"]["id"], world["bob"]["id"], desktop["device_id"])
    html = client.get("/acme/machines", headers=_h(signed_in)).text
    assert "data-default-for" in html and ">acme-app</a>" in html


# --- review fixes ---------------------------------------------------------------------------------------------------------------------------------

def test_revoking_a_runner_unpins_the_jobs_queued_for_it(world):
    desktop, laptop = _two(world)
    job = jobs.create_job(world["task"]["id"], world["bob"]["id"], "builder", approved=True, pin_device_id=desktop["device_id"])
    assert jobs.claim(laptop["device_id"]) is None                                  # pinned: the laptop may not take it
    devices.revoke(world["acme"]["id"], desktop["device_id"], "user:1")
    assert _live_jobs(world["task"]["id"])[0]["pin_device_id"] is None             # not stranded behind a dead runner
    assert jobs.claim(laptop["device_id"])["job_id"] == job["id"]


def test_a_job_pinned_to_an_expired_runner_is_offered_a_move_at_once(world, client, signed_in, monkeypatch):
    h = _h(signed_in)
    desktop, laptop = _two(world)
    job = jobs.create_job(world["task"]["id"], world["bob"]["id"], "builder", approved=True, pin_device_id=desktop["device_id"])
    with get_cursor() as cur:
        cur.execute("UPDATE device_tokens SET expires_at = now() - interval '1 day' WHERE id = %s", (desktop["device_id"],))   # expiry: nothing unpins it
    _online(monkeypatch, laptop)
    page = client.get(f"/acme-app/tasks/{world['task']['id']}/panel", headers=h).text
    assert "data-run-stuck" in page and "revoked or has expired" in page and "Run on laptop" in page      # no 10-minute wait for a runner that is gone
    assert _live_jobs(world["task"]["id"])[0]["pin_device_id"] == desktop["device_id"]                  # offered, never moved by itself
    assert client.post(f"/acme-app/tasks/{world['task']['id']}/run/move", data={"device_id": laptop["device_id"]}, headers=h).status_code == 200
    assert jobs.claim(laptop["device_id"])["job_id"] == job["id"]


def test_pinning_to_a_runner_without_the_needed_label_is_refused_with_who_has_it(world, client, signed_in):
    h = _h(signed_in)
    desktop, laptop = _two(world)
    _set_labels(world, desktop, "android-usb")                                      # only the desktop has the phone
    with pytest.raises(BadRequest, match="android-usb"):
        jobs.create_job(world["task"]["id"], world["bob"]["id"], "android", approved=True, pin_device_id=laptop["device_id"])
    r = _run(client, h, world, profile="android", runner=str(laptop["device_id"]))
    assert r.status_code == 200 and "data-run-gap" in r.text and "android-usb" in r.text and "would never start" in r.text
    assert "Run on desktop" in r.text and "data-run-on-has" in r.text               # the runner that has it is offered
    assert _live_jobs(world["task"]["id"]) == []                                    # and nothing was queued, nothing silently stranded
    assert runs_on.get_pref(world["app"]["id"], world["bob"]["id"]) is None
    r = _run(client, h, world, profile="android", runner=str(desktop["device_id"]))
    assert "data-run-gap" not in r.text and _live_jobs(world["task"]["id"])[0]["pin_device_id"] == desktop["device_id"]


def test_the_gap_says_so_when_no_runner_has_the_label(world, client, signed_in):
    _two(world)
    r = _run(client, _h(signed_in), world, profile="android", runner="any")          # unpinned: the job just waits for a labelled runner
    assert "data-run-gap" not in r.text and len(_live_jobs(world["task"]["id"])) == 1
    with get_cursor() as cur:
        cur.execute("DELETE FROM jobs")
    desk = runs_on.my_devices(world["acme"]["id"], world["bob"]["id"])[0]
    r = _run(client, _h(signed_in), world, profile="android", runner=str(desk["id"]))
    assert "data-run-gap-none" in r.text and _live_jobs(world["task"]["id"]) == []


def test_the_assistant_start_job_uses_the_saved_runner_with_the_run_forms_rules(world, monkeypatch):
    from app import ca_tools
    desktop, laptop = _two(world)
    who = {"user_id": world["bob"]["id"], "account_id": world["acme"]["id"], "conversation_id": None}
    monkeypatch.setattr(ca_tools.ca, "check_task_visible", lambda *a, **k: None)
    task = {"task_id": world["task"]["id"], "profile": "builder"}
    runs_on.set_pref(world["app"]["id"], world["bob"]["id"], laptop["device_id"])
    _online(monkeypatch, laptop, desktop)
    assert ca_tools._run_confirm(who, {"action": "start_job", "params": task}).startswith("job ")
    assert _live_jobs(world["task"]["id"])[0]["pin_device_id"] == laptop["device_id"]                # pinned to the project's runner
    with get_cursor() as cur:
        cur.execute("DELETE FROM jobs")
    _online(monkeypatch, desktop)                                                    # the laptop is off, the desktop is on
    out = ca_tools._run_confirm(who, {"action": "start_job", "params": task})
    assert out.startswith("not started:") and "laptop" in out and "offline" in out and _live_jobs(world["task"]["id"]) == []
    _online(monkeypatch)                                                             # nothing is on: nowhere to offer, so it queues for the laptop
    assert ca_tools._run_confirm(who, {"action": "start_job", "params": task}).startswith("job ")
    with get_cursor() as cur:
        cur.execute("DELETE FROM jobs")
    _set_labels(world, desktop, "android-usb")
    out = ca_tools._run_confirm(who, {"action": "start_job", "params": {**task, "profile": "android"}})
    assert out.startswith("not started:") and "android-usb" in out and "desktop" in out                # label gap: names who has it
    runs_on.set_pref(world["app"]["id"], world["bob"]["id"], None)
    assert ca_tools._run_confirm(who, {"action": "start_job", "params": task}).startswith("job ")
    assert _live_jobs(world["task"]["id"])[0]["pin_device_id"] is None                                # no saved runner: any
