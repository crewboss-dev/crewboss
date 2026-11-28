"""Runs on (#810 step 5): which of MY runners a project's jobs go to. A person with a desktop and a laptop picks one per project from a pill in the
project header; the choice is saved per person per project (owner q606: `project_runner_prefs`, because a runner belongs to one person and a
job only ever goes to its starter's runners). It is the default in the task's Run form, which can still override it for one job.

A job COPIES the choice into jobs.pin_device_id, and the claim rule (app/jobs.py _ELIGIBLE_WHERE, shared by the claim and the nudges) gains
`pin_device_id IS NULL OR pin_device_id = d.id`. The system NEVER moves a job to another runner by itself: the PC may hold the phone, a USB key
or local state. When the pinned runner is offline the person is OFFERED a move (at Run time, and for a job queued more than STUCK_MINUTES),
and the move is their click (repin)."""
from __future__ import annotations

from app import jobs, repo, tree
from app.db import get_cursor
from app.repo import BadRequest, NotFound

STUCK_MINUTES = 10


def _account_id(project_id: int) -> int:
    account_id = tree.account_root_id(project_id)
    if account_id is None:
        raise NotFound("no such project")
    return account_id


def _connected(device_id: int) -> bool:
    from app import runner_ws
    return bool(runner_ws.connected(device_id))


def my_devices(account_id: int, user_id: int, project_id: int | None = None) -> list[dict]:
    """The person's live runners in this account (not revoked, not expired), oldest first. With `project_id`: only those the project may use, that
    is its allocated runners when it has an allocation (Settings, Runners for this project; the claim enforces the same), else all of them."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT id, name, platform, labels FROM device_tokens WHERE account_id = %s AND user_id = %s "
                    "AND revoked_at IS NULL AND expires_at > now() "
                    "AND (%s::int IS NULL OR NOT EXISTS (SELECT 1 FROM project_runners pr WHERE pr.project_id = %s) "
                    "OR EXISTS (SELECT 1 FROM project_runners pr WHERE pr.project_id = %s AND pr.device_id = device_tokens.id)) ORDER BY id",
                    (account_id, user_id, project_id, project_id, project_id))
        return [dict(r) for r in cur.fetchall()]


def is_allocated(project_id: int, device_id: int) -> bool:
    """May the project's jobs run on this runner? True when the project has no allocation, or the runner is in it."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT NOT EXISTS (SELECT 1 FROM project_runners WHERE project_id = %s) OR EXISTS "
                    "(SELECT 1 FROM project_runners WHERE project_id = %s AND device_id = %s) AS ok", (project_id, project_id, device_id))
        return cur.fetchone()["ok"]


def profile_labels() -> dict[str, list[str]]:
    """label -> the profiles that need it (android-usb -> [android]): what a runner must hold for those jobs to start there."""
    out: dict[str, list[str]] = {}
    for p in jobs.list_profiles():
        for label in p["requires_labels"]:
            out.setdefault(label, []).append(p["name"])
    return out


def get_pref(project_id: int, user_id: int) -> int | None:
    """The saved runner, only while it is still live (a revoked or expired one reads as 'any')."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT p.device_id FROM project_runner_prefs p JOIN device_tokens d ON d.id = p.device_id "
                    "WHERE p.project_id = %s AND p.user_id = %s AND d.revoked_at IS NULL AND d.expires_at > now()",
                    (project_id, user_id))
        row = cur.fetchone()
    # a saved choice the project's allocation no longer allows is ignored (it is shown under More options, never used)
    return row["device_id"] if row and is_allocated(project_id, row["device_id"]) else None


def saved_pref_ignored(project_id: int, user_id: int) -> str:
    """The name of the person's saved "Runs on" runner when the project's allocation excludes it, else ''."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT d.id, d.name FROM project_runner_prefs p JOIN device_tokens d ON d.id = p.device_id WHERE p.project_id = %s AND p.user_id = %s "
                    "AND d.revoked_at IS NULL AND d.expires_at > now()", (project_id, user_id))
        row = cur.fetchone()
    return row["name"] if row and not is_allocated(project_id, row["id"]) else ""


def set_pref(project_id: int, user_id: int, device_id: int | None, by: str = "") -> None:
    """Save (or, with None, clear) the person's runner for the project. Only their own live runner of the project's account."""
    account_id = _account_id(project_id)
    with get_cursor() as cur:
        if device_id is None:
            cur.execute("DELETE FROM project_runner_prefs WHERE project_id = %s AND user_id = %s", (project_id, user_id))
            return
        cur.execute("SELECT name FROM device_tokens WHERE id = %s AND account_id = %s AND user_id = %s AND revoked_at IS NULL "
                    "AND expires_at > now()", (device_id, account_id, user_id))
        dev = cur.fetchone()
        if not dev:
            raise NotFound(f"runner {device_id} not found")
        if not is_allocated(project_id, device_id):
            raise BadRequest("that runner is not allocated to this project (Settings, Runners for this project)")
        cur.execute("INSERT INTO project_runner_prefs (project_id, user_id, device_id) VALUES (%s, %s, %s) "
                    "ON CONFLICT (project_id, user_id) DO UPDATE SET device_id = EXCLUDED.device_id, updated_at = now()",
                    (project_id, user_id, device_id))
        repo.record_activity(cur, project_id, by or f"user:{user_id}", "set", "runs_on", device_id,
                             f"Jobs of this project now run on runner {dev['name']!r}")


def options(project_id: int, user_id: int) -> dict:
    """What the header pill and the Run form show: {devices: [{id, name, connected, labels, missing, selected}], current, any}.
    `missing` = [{label, profiles}] the project's profiles ask for that this runner lacks (the labels guard); `current` the saved runner
    (or None = any of my runners)."""
    account_id = _account_id(project_id)
    pref = get_pref(project_id, user_id)
    needs = profile_labels()
    from app import environment
    has_env = environment.count(project_id) > 0
    devs = []
    for d in my_devices(account_id, user_id, project_id):
        devs.append({**d, "connected": _connected(d["id"]), "selected": d["id"] == pref,
                     "missing": [{"label": l, "profiles": ps} for l, ps in needs.items() if l not in d["labels"]],
                     # the environment badge (#810 step 6): Compliant / Missing N / Not checked, only when the project lists requirements
                     "env": environment.latest(project_id, d["id"]) if has_env else None})
    current = next((d for d in devs if d["selected"]), None)
    return {"devices": devs, "current": current, "any": current is None, "default_id": pref, "ignored_pref": saved_pref_ignored(project_id, user_id)}


def resolve(project_id: int, user_id: int, choice: str) -> int | None:
    """The pin for a new job from the Run form's choice: '' / 'default' = the saved runner; 'any' = none; else a runner id."""
    choice = (choice or "").strip()
    if choice in ("", "default"):
        return get_pref(project_id, user_id)
    if choice == "any":
        return None
    try:
        device_id = int(choice)
    except ValueError:
        raise BadRequest("pick one of your runners, or any") from None
    if device_id not in {d["id"] for d in my_devices(_account_id(project_id), user_id)}:
        raise NotFound(f"runner {device_id} not found")
    if not is_allocated(project_id, device_id):
        raise BadRequest("that runner is not allocated to this project (Settings, Runners for this project)")
    return device_id


def _alternates(account_id: int, user_id: int, not_id: int, requires: list[str], project_id: int | None = None) -> list[dict]:
    """Other live runners of the person that are ONLINE and hold every label the job needs: who could take it now."""
    need = set(requires)
    return [d for d in my_devices(account_id, user_id, project_id) if d["id"] != not_id and need <= set(d["labels"]) and _connected(d["id"])]


def offline_offer(project_id: int, user_id: int, pin: int | None, requires: list[str]) -> dict | None:
    """At Run time: the pinned runner is offline AND another of the person's could take the job now. {device, alternates}; None = just
    queue it (the runner is online, nothing is pinned, or there is nowhere else to offer)."""
    if pin is None:
        return None
    account_id = _account_id(project_id)
    dev = next((d for d in my_devices(account_id, user_id, project_id) if d["id"] == pin), None)
    if dev is not None and _connected(pin):
        return None
    if dev is None:
        # a DEAD pin: the runner was revoked or has expired, so it will never come back (a revoke unpins queued jobs, an expiry cannot)
        with get_cursor(commit=False) as cur:
            cur.execute("SELECT name FROM device_tokens WHERE id = %s AND account_id = %s AND user_id = %s", (pin, account_id, user_id))
            row = cur.fetchone()
        dev = {"id": pin, "name": row["name"] if row else "a removed runner", "labels": [], "dead": True}
    alts = _alternates(account_id, user_id, pin, requires, project_id)
    return {"device": dev, "alternates": alts} if alts else None


def stuck_offer(job: dict) -> dict | None:
    """For a QUEUED, pinned job waiting over STUCK_MINUTES while its runner is offline and another eligible one is online: {device,
    alternates}. The same offer as at Run time; moving is the person's click (repin), never automatic."""
    if not job or job["state"] != "queued" or not job.get("pin_device_id"):
        return None
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT now() - created_at > make_interval(mins => %s) AS old FROM jobs WHERE id = %s", (STUCK_MINUTES, job["id"]))
        row = cur.fetchone()
    offer = offline_offer(_project_of_task(job["task_id"]), job["user_id"], job["pin_device_id"], list(job["requires_labels"] or []))
    if offer and (offer["device"].get("dead") or (row and row["old"])):     # a dead pin is offered at once: it will never start by waiting
        return offer
    return None


def _project_of_task(task_id: int) -> int:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT project_id FROM tasks WHERE id = %s", (task_id,))
        row = cur.fetchone()
    if not row:
        raise NotFound("no such task")
    return row["project_id"]


def label_gap(project_id: int, user_id: int, pin: int | None, requires: list[str]) -> dict | None:
    """Pinning to a runner that lacks a label the job needs would strand it (the claim needs `requires_labels <@ labels`). {device, missing,
    have}: the pinned runner, the labels it lacks, and the person's runners that DO hold them all. None = fine."""
    if pin is None:
        return None
    account_id = _account_id(project_id)
    mine = my_devices(account_id, user_id, project_id)
    dev = next((d for d in mine if d["id"] == pin), None)
    if dev is None:
        return None
    missing = [l for l in dict.fromkeys(requires) if l not in dev["labels"]]
    if not missing:
        return None
    have = [d for d in mine if set(requires) <= set(d["labels"])]
    return {"device": dev, "missing": missing, "have": have}


def plan_for_task(task_id: int, user_id: int, profile: str, extra_labels="") -> dict:
    """The assistant's start_job (and retry): the same rules as the Run form, for a path with no form to show an offer on. {pin, message}:
    pin = the project's saved runner (or None = any); `message` non-empty = do NOT start the job, say this instead (the pinned runner lacks a
    label the job needs, or it is offline while another could take it; the person picks from the task's Run form)."""
    project_id = _project_of_task(task_id)
    pin = get_pref(project_id, user_id)
    if pin is None:
        from app import run_plan       # no saved runner: the same automatic choice as the Run button; a project nobody's runner is allocated to is said plainly
        prof = next((p for p in jobs.list_profiles() if p["name"] == profile), None)
        pick = run_plan.choose_runner(project_id, user_id, [*(prof["requires_labels"] if prof else []), *jobs.parse_labels(extra_labels or [])])
        if pick["device"]:
            return {"pin": pick["device"]["id"], "message": ""}
        return {"pin": None, "message": pick["message"] if pick["starved"] else ""}
    prof = next((p for p in jobs.list_profiles() if p["name"] == profile), None)
    requires = [*(prof["requires_labels"] if prof else []), *jobs.parse_labels(extra_labels or [])]
    gap = label_gap(project_id, user_id, pin, requires)
    if gap:
        who = ", ".join(d["name"] for d in gap["have"]) or "none of your runners"
        return {"pin": pin, "message": f"this project runs on {gap['device']['name']}, which has no {', '.join(gap['missing'])} label this job needs "
                                       f"(runners that have it: {who}). Pick a runner on the task's Run form."}
    offer = offline_offer(project_id, user_id, pin, requires)
    if offer:
        return {"pin": pin, "message": f"this project runs on {offer['device']['name']}, which is offline, and {', '.join(a['name'] for a in offer['alternates'])} "
                                       "is online. Choose on the task's Run form (run there, or queue for the offline one); nothing was queued."}
    return {"pin": pin, "message": ""}


def installed_pin(cur, task_id: int, project_id: int, user_id: int, account_id: int, requested: int | None = None,
                  requires: list[str] | None = None) -> int:
    """The runner an INSTALLED-mode project's job is pinned to (#839). A task is LOCKED to the runner of its last job (a build is tested on the
    device and key it came from) until the task is done, that runner is gone (revoked or expired), or it lacks a label the job needs (the lock yields
    then, so a task is never wedged). Naming another runner while the lock holds is refused. With no prior runner: the one asked for, else the
    saved "Runs on" runner, else the only runner that holds the labels. Several runners and no choice: BadRequest."""
    need = set(requires or [])
    mine = my_devices(account_id, user_id, project_id)
    fit = {d["id"] for d in mine if need <= set(d["labels"])}
    cur.execute("SELECT t.status, (SELECT j.pin_device_id FROM jobs j JOIN device_tokens d ON d.id = j.pin_device_id WHERE j.task_id = t.id "
                "AND j.user_id = %s AND d.revoked_at IS NULL AND d.expires_at > now() ORDER BY j.id DESC LIMIT 1) AS prior "
                "FROM tasks t WHERE t.id = %s", (user_id, task_id))
    row = cur.fetchone()
    prior = row["prior"] if row and row["status"] != "done" else None
    if prior is not None and prior in fit:
        if requested not in (None, prior):
            raise BadRequest("this task is locked to the runner its build is tested on until it is committed (done); "
                             "queue the next job on that runner, or finish the task first")
        return prior
    if requested is not None:
        if not is_allocated(project_id, requested):
            raise BadRequest("that runner is not allocated to this project (Settings, Runners for this project)")
        return requested
    pref = get_pref(project_id, user_id)
    if pref is not None and pref in fit:
        return pref
    if len(fit) == 1:
        return next(iter(fit))
    raise BadRequest("this app is tested by installing it, so the task is pinned to one of your runners: pick which one"
                     if mine else "this app is tested by installing it on a runner, and you have no runner yet: enrol one on the Machines page")


def repin(job_id: int, user_id: int, device_id: int) -> dict:
    """The starter moves their QUEUED job to another of their runners (a click). It must be live, theirs, in the job's account and hold the
    job's labels; a job that already started is left alone."""
    with get_cursor() as cur:
        cur.execute("SELECT * FROM jobs WHERE id = %s AND user_id = %s FOR UPDATE", (job_id, user_id))
        job = cur.fetchone()
        if not job:
            raise NotFound(f"job {job_id} not found")
        if job["state"] != "queued":
            raise BadRequest("only a queued job can be moved")
        cur.execute("SELECT labels FROM device_tokens WHERE id = %s AND account_id = %s AND user_id = %s AND revoked_at IS NULL "
                    "AND expires_at > now()", (device_id, job["account_id"], user_id))
        dev = cur.fetchone()
        if not dev:
            raise NotFound(f"runner {device_id} not found")
        if not set(job["requires_labels"]) <= set(dev["labels"]):
            raise BadRequest("that runner lacks a label the job needs")
        cur.execute("SELECT t.project_id FROM tasks t WHERE t.id = %s", (job["task_id"],))
        if not is_allocated(cur.fetchone()["project_id"], device_id):
            raise BadRequest("that runner is not allocated to this project (Settings, Runners for this project)")
        cur.execute("UPDATE jobs SET pin_device_id = %s WHERE id = %s RETURNING *", (device_id, job_id))
        job = cur.fetchone()
        repo.record_activity(cur, job["account_id"], f"user:{user_id}", "moved", "job", job_id, f"Moved queued job {job_id} to runner {device_id}")
    jobs.nudge([job_id])
    return dict(job)
