"""Runners phase P4b (task #627): start a job from a task ("Run on a PC"), the project's git remote, device labels and job
requirements. Builds on the P1/P2 fixtures."""
import pytest

from app import auth, config, devices, jobs, repo, runner_ws, tree, web_auth
from app.db import get_cursor
from app.repo import BadRequest, Forbidden, NotFound

from test_devices import _keypair, _project, _secret, _user, est, signed_in  # noqa: F401  (fixtures)
from test_jobs import _task
from test_web_auth import auth_on  # noqa: F401

BOB_OID = "bbbbbbbb-0000-4000-8000-000000000002"   # the signed_in fixture's person
REMOTE = "https://github.com/acme/mobile-app.git"


def _bob_user():
    return _user(oid=BOB_OID, name="Bob")


def _device(est, user, account="acme", mode="subscription", name="pc"):
    issued = devices.issue_code(user, est[account], mode)
    _key, pub = _keypair()
    return devices.enrol(_secret(issued["code"]), pub, name, "linux", ip="10.0.0.1")


def _set_labels(est, dev, labels, account="acme"):
    return devices.set_labels(est[account]["id"], dev["device_id"], labels, "user:1")


@pytest.fixture
def world(est):
    est["app"] = _project("acme-app", "acmeapp", est["acme"])
    est["bapp"] = _project("beta-app", "betaapp", est["beta"])
    tree.set_details(est["app"]["id"], "owner", git_remote=REMOTE)
    tree.set_details(est["bapp"]["id"], "owner", git_remote=REMOTE)
    est["task"] = _task(est["app"])
    est["btask"] = _task(est["bapp"])
    est["bob"] = _bob_user()
    return est


def _live_jobs(task_id):
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT * FROM jobs WHERE task_id = %s ORDER BY id", (task_id,))
        return cur.fetchall()


def _run(client, headers, w, **data):
    data.setdefault("profile", "builder")
    return client.post(f"/acme-app/tasks/{w['task']['id']}/run", data=data, headers=headers, follow_redirects=False)


# --- the Run control ------------------------------------------------------------------------------------------------------

def test_run_creates_an_approved_job_and_shows_its_state(world, client, signed_in):
    h = {"cookie": signed_in["cookie"]}
    drawer = client.get(f"/acme-app/tasks/{world['task']['id']}/panel", headers=h).text
    assert "data-run-form" in drawer and "android" in drawer and "needs android-usb" in drawer
    r = _run(client, h, world)
    assert r.status_code == 200 and 'data-job-state="queued"' in r.text and "Queued, waiting for a runner" in r.text
    (job,) = _live_jobs(world["task"]["id"])
    assert job["user_id"] == world["bob"]["id"] and job["approved_at"] is not None and job["state"] == "queued"
    assert not job["unattended"] and job["requires_labels"] == []
    # a subscription runner of that person takes it, and the drawer then says where it runs, with the branch
    dev = _device(world, world["bob"], name="bobs-desktop")
    claim = jobs.claim(dev["device_id"])
    assert claim and claim["job_id"] == job["id"]
    page = client.get(f"/acme-app/tasks/{world['task']['id']}/panel", headers=h).text
    assert "Running on bobs-desktop" in page and claim["branch"] in page and "data-run-cancel" in page


def test_run_needs_a_web_session_and_refuses_runner_tokens(world, client, signed_in):
    tid = world["task"]["id"]
    dev = _device(world, world["bob"])
    # no session: the sign-in gate stops it
    r = client.post(f"/acme-app/tasks/{tid}/run", data={"profile": "builder"}, follow_redirects=False)
    assert r.status_code in (302, 401) and not _live_jobs(tid)
    # a device token alone is no session; and a cookie PLUS a device token is refused too
    r = client.post(f"/acme-app/tasks/{tid}/run", data={"profile": "builder"}, follow_redirects=False,
                    headers={"authorization": "Bearer " + dev["device_token"]})
    assert r.status_code in (302, 401) and not _live_jobs(tid)
    r = client.post(f"/acme-app/tasks/{tid}/run", data={"profile": "builder"}, follow_redirects=False,
                    headers={"cookie": signed_in["cookie"], "authorization": "Bearer " + dev["device_token"]})
    assert r.status_code == 403 and not _live_jobs(tid)
    r = client.post(f"/acme-app/tasks/{tid}/run", data={"profile": "builder"}, follow_redirects=False,
                    headers={"cookie": signed_in["cookie"], "authorization": "Bearer mdj_whatever"})
    assert r.status_code == 403 and not _live_jobs(tid)


def test_run_refused_across_accounts(world, client, signed_in):
    h = {"cookie": signed_in["cookie"]}
    # beta's project URL with acme's task id: not that project's task
    r = client.post(f"/beta-app/tasks/{world['task']['id']}/run", data={"profile": "builder"}, headers=h,
                    follow_redirects=False)
    assert r.status_code in (303, 404) and not _live_jobs(world["task"]["id"])
    # and the job the other account's task makes is in ITS account, never acme's
    assert _run(client, h, world).status_code == 200
    assert jobs.list_for(world["acme"]["id"]) and not jobs.list_for(world["beta"]["id"])
    with pytest.raises(NotFound):   # cancel is scoped to the account too
        jobs.cancel_job(world["beta"]["id"], jobs.list_for(world["acme"]["id"])[0]["id"], world["bob"]["id"])


def test_run_needs_a_git_remote_and_a_known_profile(world, client, signed_in):
    h = {"cookie": signed_in["cookie"]}
    tree.set_details(world["app"]["id"], "owner", git_remote="")
    assert "data-run-needs-remote" in client.get(f"/acme-app/tasks/{world['task']['id']}/panel", headers=h).text
    assert _run(client, h, world).status_code in (303, 400) and not _live_jobs(world["task"]["id"])
    tree.set_details(world["app"]["id"], "owner", git_remote=REMOTE)
    assert _run(client, h, world, profile="nope").status_code in (303, 404) and not _live_jobs(world["task"]["id"])


def test_client_authored_task_never_runs_unattended(world, client, signed_in):
    h = {"cookie": signed_in["cookie"]}
    with get_cursor() as cur:
        cur.execute("UPDATE tasks SET created_by = 'client:acme' WHERE id = %s", (world["task"]["id"],))
        cur.execute("UPDATE agent_profiles SET unattended_ok = true WHERE name = 'builder'")
    try:
        r = _run(client, h, world, unattended="1")
        assert r.status_code in (303, 400) and not _live_jobs(world["task"]["id"])
        with pytest.raises(BadRequest):
            jobs.create_job(world["task"]["id"], world["bob"]["id"], "builder", unattended=True, approved=True)
    finally:
        with get_cursor() as cur:
            cur.execute("UPDATE agent_profiles SET unattended_ok = false WHERE name = 'builder'")


def test_drawer_warns_on_client_text_and_cancel_is_for_the_starter(world, client, signed_in):
    h = {"cookie": signed_in["cookie"]}
    tid = world["task"]["id"]
    url = f"/acme-app/tasks/{tid}/panel"
    assert "written by a client" not in client.get(url, headers=h).text
    with get_cursor() as cur:
        cur.execute("UPDATE tasks SET created_by = 'client:acme' WHERE id = %s", (tid,))
    page = client.get(url, headers=h).text
    assert "This task was written by a client: the agent will act on their text with shell access on your PC." in page
    assert _run(client, h, world).status_code == 200
    assert "data-run-cancel" in client.get(url, headers=h).text   # Bob started it
    other = _user(oid="cccccccc-0000-4000-8000-000000000003", name="Cy")
    with get_cursor() as cur:
        cur.execute("UPDATE jobs SET user_id = %s WHERE task_id = %s", (other["id"], tid))
    page = client.get(url, headers=h).text
    assert "data-run-cancel" not in page and "Started by Cy" in page


def test_a_subscription_job_needs_approval(world):
    dev = _device(world, world["bob"])
    job = jobs.create_job(world["task"]["id"], world["bob"]["id"], "builder")   # started, not approved
    assert jobs.claimable_jobs(dev["device_id"]) == [] and jobs.claim(dev["device_id"]) is None
    jobs.approve_job(world["acme"]["id"], job["id"], world["bob"]["id"])
    assert [j["job_id"] for j in jobs.claimable_jobs(dev["device_id"])] == [job["id"]]
    assert jobs.claim(dev["device_id"])["job_id"] == job["id"]


# --- cancel ---------------------------------------------------------------------------------------------------------------

def test_cancel_ends_the_lease_cleanly(world, client, signed_in):
    h = {"cookie": signed_in["cookie"]}
    dev = _device(world, world["bob"])
    assert _run(client, h, world).status_code == 200
    claim = jobs.claim(dev["device_id"])
    with get_cursor() as cur:   # it holds a merge lock and a deploy lock
        cur.execute("INSERT INTO locks (account_id, kind, resource, job_id, fence) VALUES (%s, 'merge', 'x', %s, %s), "
                    "(%s, 'deploy', 'y', %s, %s)", (world["acme"]["id"], claim["job_id"], claim["fence"],
                                                    world["acme"]["id"], claim["job_id"], claim["fence"]))
    r = client.post(f"/acme-app/tasks/{world['task']['id']}/run/cancel", headers=h, follow_redirects=False)
    assert r.status_code == 200 and 'data-job-state="cancelled"' in r.text and "data-run-form" in r.text   # may run again
    (job,) = _live_jobs(world["task"]["id"])
    assert job["state"] == "cancelled" and job["token_hash"] is None and job["lease_expires_at"] is None
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT kind, state FROM locks ORDER BY kind")
        assert [(l["kind"], l["state"]) for l in cur.fetchall()] == [("deploy", "orphaned")]   # the merge lock went, deploy needs a human
    with pytest.raises(jobs.StaleFence):   # the runner's next heartbeat / finish is told to stop
        jobs.heartbeat(dev["device_id"], claim["job_id"], claim["fence"])
    with pytest.raises(jobs.StaleFence):
        jobs.finish(dev["device_id"], claim["job_id"], claim["fence"], True)
    assert jobs.claim(dev["device_id"]) is None
    # a queued job cancels too; a finished one does not; and only its starter may
    again = jobs.create_job(world["task"]["id"], world["bob"]["id"], "builder", approved=True)
    other = _user(oid="cccccccc-0000-4000-8000-000000000003", name="Cy")
    with pytest.raises(NotFound):
        jobs.cancel_job(world["acme"]["id"], again["id"], other["id"])
    assert jobs.cancel_job(world["acme"]["id"], again["id"], world["bob"]["id"])["state"] == "cancelled"
    with pytest.raises(BadRequest):
        jobs.cancel_job(world["acme"]["id"], again["id"], world["bob"]["id"])


def test_cancel_refused_for_runner_tokens(world, client, signed_in):
    h = {"cookie": signed_in["cookie"]}
    dev = _device(world, world["bob"])
    assert _run(client, h, world).status_code == 200
    r = client.post(f"/acme-app/tasks/{world['task']['id']}/run/cancel", follow_redirects=False,
                    headers={"cookie": signed_in["cookie"], "authorization": "Bearer " + dev["device_token"]})
    assert r.status_code == 403 and _live_jobs(world["task"]["id"])[0]["state"] == "queued"


# --- the project's git remote --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value", [
    "https://github.com/acme/mobile-app.git", "git@github.com:acme/mobile-app.git",
    "https://github.com/a/b.git", "  git@github.com:o_1/r.x-y.git  "])
def test_git_remote_accepts(world, value):
    row = tree.set_details(world["app"]["id"], "owner", git_remote=value)
    assert row["git_remote"] == value.strip()
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT summary FROM activity WHERE project_id = %s ORDER BY id DESC LIMIT 1", (world["app"]["id"],))
        assert "git remote" in cur.fetchone()["summary"]


@pytest.mark.parametrize("value", [
    "-https://github.com/a/b.git", "--upload-pack=x", "https://github.com/-a/b.git", "https://github.com/a/-b.git",
    "https://gitlab.com/a/b.git", "https://github.com.evil.test/a/b.git", "https://evil.test/github.com/a/b.git",
    "http://github.com/a/b.git", "ssh://git@github.com/a/b.git", "git@gitlab.com:a/b.git", "git@github.com:a/b",
    "https://github.com/a/b", "https://github.com/a/b.git extra", "https://github.com/a/b.git\n--x",
    "https://user@github.com/a/b.git", "https://github.com/a/../b.git", "https://github.com//b.git",
    "file:///etc/passwd", "git@github.com:a/b/c.git", "https://github.com/a/b.git?x=1", "ext::sh -c id"])
def test_git_remote_rejects(world, value):
    with pytest.raises(BadRequest):
        tree.set_details(world["app"]["id"], "owner", git_remote=value)
    assert repo.get_project(world["app"]["id"])["git_remote"] == REMOTE   # unchanged


def test_git_remote_owner_only_clearable_and_host_list_is_config(world, monkeypatch):
    with pytest.raises(Forbidden):
        tree.set_details(world["app"]["id"], "session:acme-app", git_remote="https://github.com/a/b.git")
    assert tree.set_details(world["app"]["id"], "owner", git_remote="")["git_remote"] == ""
    monkeypatch.setattr(config, "GIT_REMOTE_HOSTS", ("git.example.test",))
    assert tree.set_details(world["app"]["id"], "owner", git_remote="https://git.example.test/a/b.git")["git_remote"]
    with pytest.raises(BadRequest):
        tree.set_details(world["app"]["id"], "owner", git_remote="https://github.com/a/b.git")


def test_details_form_saves_and_suggests_the_remote(world, client, signed_in):
    h = {"cookie": signed_in["cookie"]}
    tree.set_details(world["app"]["id"], "owner", repo_name="acme/mobile-app", git_remote="")
    page = client.get("/acme-app/settings", headers=h).text
    assert "data-git-remote-hint" in page and REMOTE in page
    r = client.post("/acme-app/details", data={"repo": "acme/mobile-app", "git_remote": REMOTE}, headers=h,
                    follow_redirects=False)
    assert r.status_code == 303 and repo.get_project(world["app"]["id"])["git_remote"] == REMOTE
    bad = client.post("/acme-app/details", data={"repo": "acme/mobile-app", "git_remote": "-x"}, headers=h,
                      follow_redirects=False)
    assert repo.get_project(world["app"]["id"])["git_remote"] == REMOTE and bad.status_code in (303, 400)
    # a form without the field leaves the remote alone
    client.post("/acme-app/details", data={"repo": "acme/mobile-app"}, headers=h, follow_redirects=False)
    assert repo.get_project(world["app"]["id"])["git_remote"] == REMOTE


# --- labels ---------------------------------------------------------------------------------------------------------------

def test_android_profile_is_seeded():
    p = {x["name"]: x for x in jobs.list_profiles()}["android"]
    assert p["model"] == "sonnet" and p["task_types"] == ["android"] and p["requires_labels"] == ["android-usb"]
    assert p["allowed_tools"] == ["Read", "Edit", "Write", "Glob", "Grep", "Bash"] and not p["unattended_ok"]


def test_labels_gate_claim_claimable_and_nudge(world):
    plain = _device(world, world["bob"], name="plain")
    phone = _device(world, world["bob"], name="with-phone")
    _set_labels(world, phone, "android-usb")
    job = jobs.create_job(world["task"]["id"], world["bob"]["id"], "android", approved=True)
    assert job["requires_labels"] == ["android-usb"]
    assert jobs.claimable_devices(job["id"]) == [phone["device_id"]]   # the nudge goes to the labelled PC only
    assert jobs.claimable_jobs(plain["device_id"]) == [] and jobs.claim(plain["device_id"]) is None
    assert [j["job_id"] for j in jobs.claimable_jobs(phone["device_id"])] == [job["id"]]
    claim = jobs.claim(phone["device_id"])
    assert claim and claim["job_id"] == job["id"] and claim["profile"]["name"] == "android"


def test_labels_nudge_reaches_only_a_labelled_socket(world, monkeypatch):
    plain = _device(world, world["bob"], name="plain")
    phone = _device(world, world["bob"], name="with-phone")
    _set_labels(world, phone, "android-usb")
    posted = []

    class Conn:
        verified = True

        def __init__(self, name):
            self.name = name

        def post(self, msg):
            posted.append((self.name, msg))

    monkeypatch.setattr(runner_ws, "_conns", {plain["device_id"]: Conn("plain"), phone["device_id"]: Conn("phone")})
    job = jobs.create_job(world["task"]["id"], world["bob"]["id"], "android", approved=True)
    assert posted and {name for name, _ in posted} == {"phone"}   # create_job nudged already: only the labelled PC
    posted.clear()
    assert runner_ws.nudge_job(job["id"]) == 1 and posted == [("phone", ("nudge", job["id"]))]


def test_extra_labels_on_the_run_form_are_added_to_the_profiles(world, client, signed_in):
    h = {"cookie": signed_in["cookie"]}
    dev = _device(world, world["bob"])
    _set_labels(world, dev, "android-usb gpu")
    assert _run(client, h, world, profile="android", labels="GPU, fast").status_code == 200
    (job,) = _live_jobs(world["task"]["id"])
    assert job["requires_labels"] == ["android-usb", "gpu", "fast"]
    assert jobs.claim(dev["device_id"]) is None   # lacks "fast"
    _set_labels(world, dev, "fast, gpu, android-usb")
    assert jobs.claim(dev["device_id"])["job_id"] == job["id"]


def test_labelling_a_device_makes_a_waiting_job_claimable(world):
    dev = _device(world, world["bob"])
    job = jobs.create_job(world["task"]["id"], world["bob"]["id"], "android", approved=True)
    assert jobs.claim(dev["device_id"]) is None
    _set_labels(world, dev, ["android-usb"])
    assert jobs.claim(dev["device_id"])["job_id"] == job["id"]


@pytest.mark.parametrize("bad", ["UPPER_case", "has space!", "x" * 33, "a/b", "ünï", "-", ",".join(f"l{i}" for i in range(17))])
def test_label_validation_rejects(bad):
    if bad == "-":
        assert jobs.parse_labels(bad) == ["-"]   # '-' alone is within [a-z0-9-]
        return
    with pytest.raises(BadRequest):
        jobs.parse_labels(bad)


def test_label_validation_accepts_and_normalises():
    assert jobs.parse_labels("Android-USB, gpu  gpu\nx1") == ["android-usb", "gpu", "x1"]
    assert jobs.parse_labels("") == [] and jobs.parse_labels(None) == []
    assert len(jobs.parse_labels(",".join(f"l{i}" for i in range(16)))) == 16
    assert jobs.parse_labels(["a" * 32]) == ["a" * 32]


def test_only_the_owner_sets_labels_not_the_runner(world, client, signed_in):
    h = {"cookie": signed_in["cookie"]}
    dev = _device(world, world["bob"])
    did = dev["device_id"]
    # the page route (signed-in person) sets them, in the right account only
    r = client.post(f"/acme/runners/{did}/labels", data={"labels": "Android-USB"}, headers=h, follow_redirects=False)
    assert r.status_code == 303 and devices.list_for(world["acme"]["id"])[0]["labels"] == ["android-usb"]
    assert "android-usb" in client.get("/acme/machines", headers=h).text
    r = client.post(f"/beta/runners/{did}/labels", data={"labels": "evil"}, headers=h, follow_redirects=False)
    assert r.status_code == 303 and devices.list_for(world["acme"]["id"])[0]["labels"] == ["android-usb"]
    bad = client.post(f"/acme/runners/{did}/labels", data={"labels": "NOPE!"}, headers=h)
    assert bad.status_code == 400 and devices.list_for(world["acme"]["id"])[0]["labels"] == ["android-usb"]
    # the device token cannot reach the page route, with or without a cookie
    r = client.post(f"/acme/runners/{did}/labels", data={"labels": "root"}, follow_redirects=False,
                    headers={"authorization": "Bearer " + dev["device_token"]})
    assert r.status_code in (302, 401)
    r = client.post(f"/acme/runners/{did}/labels", data={"labels": "root"}, follow_redirects=False,
                    headers={"cookie": signed_in["cookie"], "authorization": "Bearer " + dev["device_token"]})
    assert r.status_code == 403 and devices.list_for(world["acme"]["id"])[0]["labels"] == ["android-usb"]
    # enrolment carries no labels: an extra field is ignored, a fresh device has none
    issued = devices.issue_code(world["bob"], world["acme"], "subscription")
    _key, pub = _keypair()
    out = client.post("/runner/enrol", json={"code": issued["code"], "pubkey": pub, "name": "sneaky", "platform": "linux",
                                             "labels": ["android-usb"]})
    assert out.status_code == 200
    sneaky = [d for d in devices.list_for(world["acme"]["id"]) if d["name"] == "sneaky"][0]
    assert sneaky["labels"] == []
    # no runner endpoint writes labels
    import app.routers.runner as runner_router
    import inspect
    assert "labels" not in inspect.getsource(runner_router)
