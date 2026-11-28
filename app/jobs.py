"""Jobs, leases, fencing and locks (task #627, change #50, phase P2). A job is ONE run of ONE task by one agent (made
from a profile) on one user's runner. The rules, from the Fable review (task comments 2258/2259):

* One live (queued/running) job per task: the table's partial unique index, not a check-then-insert.
* Dispatch (claim) is ONE SQL statement: same account only; the runner's own user's jobs only; a SUBSCRIPTION runner takes
  only jobs that user started and that have been approved; an API runner takes only unattended-eligible jobs, and NEVER a
  task whose content a client wrote (the prompt-injection boundary). Revoked/expired devices and disabled users claim nothing.
* A job's payload carries ids only. The task text is fetched through the job MCP (app/job_mcp.py), with the job token.
* Every job write carries the FENCE. A new fence is drawn from fence_seq at every claim and every requeue, so a runner that
  slept through its lease and wakes up holding the old fence gets StaleFence (409) and changes nothing.
* The lease is renewed by heartbeat and judged on the DATABASE clock, never the runner's. The sweeper also watches for a
  clock jump or a sleeping server host (database time moving faster than this process's monotonic clock) and then gives
  every running job one lease of grace instead of expiring anything.
* An expired job requeues with a new fence (up to JOB_MAX_ATTEMPTS). A job that held a deploy or migrate lock is NOT retried:
  the lock is marked orphaned and the job failed; a human clears the lock. A merge lock simply goes with its job.
* Locks are keyed (account, kind, resource); the account comes from the JOB, never from the caller, and a resource of
  another account is NotFound exactly like a missing one.
* P5: a job token may take only JOB_LOCK_KINDS (merge). The deploy lock is taken by the server-side executor
  (app/deploys.py) for an approved server_job, never by a job; a job asks with request_deploy and a person approves it.
  The orphan rule for deploy/migrate locks stays: an executor lock that outlives its process is marked orphaned.
Every refusal is logged."""
from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import threading
import time

from psycopg.errors import UniqueViolation

from app import access, config, repo, tree
from app.db import get_cursor
from app.repo import BadRequest, Conflict, NotFound

log = logging.getLogger("crewboss.jobs")

from app.auth import JOB_PREFIX   # cbj_ (the old mdj_ is still accepted: app/auth.py)
LOCK_KINDS = ("deploy", "merge", "migrate")
JOB_LOCK_KINDS = ("merge",)   # what a JOB token may take (P5). deploy and migrate are taken only by the server-side executor
ORPHANING_KINDS = ("deploy", "migrate")   # an expired holder leaves these orphaned (a human clears them), never retried
LIVE_STATES = ("queued", "running")

# Device/job labels (change #50, P4b): lowercase [a-z0-9-], 1..32 chars, at most 16 per device. Only the owner sets a device's
# labels (the Runners page); a job needs ALL of its requires_labels to be among the claiming device's labels.
LABEL_RE = re.compile(r"[a-z0-9-]{1,32}")
MAX_LABELS = 16


def parse_labels(raw, limit: int = MAX_LABELS) -> list[str]:
    """Labels from a comma/space separated string or a list: lowercased, de-duplicated (order kept), each [a-z0-9-]{1,32}, at
    most `limit`. Anything else is a BadRequest (nothing is silently dropped)."""
    items = re.split(r"[\s,]+", raw) if isinstance(raw, str) else list(raw or [])
    out: list[str] = []
    for item in items:
        label = str(item).strip().lower()
        if not label:
            continue
        if not LABEL_RE.fullmatch(label):
            raise BadRequest(f"label {label[:40]!r} is not allowed: use lowercase letters, digits and - (1 to 32 characters)")
        if label not in out:
            out.append(label)
    if len(out) > limit:
        raise BadRequest(f"at most {limit} labels")
    return out


class StaleFence(Exception):
    """A write by a job that no longer holds the job: its fence is old, or the job is not running any more. API: 409."""


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def actor(job_id: int) -> str:
    return f"job:{job_id}"


def client_authored(task: dict) -> bool:
    """A task whose text a client (or anyone outside) wrote: never run unattended. Same test as app/plan.py, plus
    requested_by (the client who asked)."""
    return ((task.get("created_by") or "").startswith("client:") or bool(task.get("client_request_id"))
            or bool(task.get("requested_by")))


# --- profiles ------------------------------------------------------------------------------------------------------

def list_profiles() -> list[dict]:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT * FROM agent_profiles ORDER BY id")
        return [dict(r) for r in cur.fetchall()]


_BARE_TOOL = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


def tool_pattern_problem(tools) -> str | None:
    """None when every entry of a profile's allowed_tools is a BARE tool name (`Bash`, `Edit`, `Skill`); else the reason. A pattern such as
    `Bash(git *)` is refused: it does not deny Bash, so a user-level allow widens it, and a git alias or `-c` escapes the pattern anyway
    (#829 review 5). A ceiling names a whole tool or nothing."""
    bad = [str(t)[:40] for t in (tools or []) if not (isinstance(t, str) and _BARE_TOOL.fullmatch(t))]
    if bad:
        return ("agent profile allowed_tools may hold only bare tool names (Bash, Edit, ...), never a pattern or an argument: "
                + ", ".join(bad[:5]))
    return None


def _profile(cur, name: str) -> dict:
    cur.execute("SELECT * FROM agent_profiles WHERE name = %s", (name,))
    row = cur.fetchone()
    if not row:
        raise NotFound(f"no agent profile {name!r}")
    return row


# --- making and approving jobs ---------------------------------------------------------------------------------------

def create_job(task_id: int, user_id: int, profile: str, unattended: bool = False, approved: bool = False,
               extra_labels=None, ca_conversation_id: int | None = None, keep_open: bool = False,
               pin_device_id: int | None = None, base_branch: str = "") -> dict:
    """Queue a job for `task_id`, started by `user_id`. `approved` records the starter's own approval (the approval cards
    are P5). `unattended` makes it eligible for an API runner, which needs a profile with unattended_ok and a task that
    is not client-authored. The job's requires_labels are the profile's plus `extra_labels`. A second live job on the same
    task is a Conflict. `pin_device_id` (#810 "Runs on"): only that runner of the starter's may take it (None = any of theirs)."""
    extra = parse_labels(extra_labels or [])
    with get_cursor() as cur:
        task = repo._task_row(cur, task_id)
        account_id = tree.account_root_id(task["project_id"])
        if account_id is None:
            raise NotFound(f"task {task_id} not found")
        cur.execute("SELECT id, disabled_at FROM users WHERE id = %s", (user_id,))
        user = cur.fetchone()
        if not user or user["disabled_at"]:
            raise NotFound("no such user")
        # Defence in depth (#713): the page already checked, but a job is only ever made for a person who may change the task's project.
        if not access.can_edit(access.principal_for_user_id(user_id), task["project_id"]):
            raise NotFound(f"task {task_id} not found")
        prof = _profile(cur, profile)
        if (why := tool_pattern_problem(prof["allowed_tools"])):
            raise BadRequest(f"profile {profile!r} is refused: {why}")
        if unattended:
            if client_authored(task):
                log.warning("job refused: task %s is client-authored, so it can't run unattended", task_id)
                raise BadRequest("a client-authored task never runs unattended")
            if not prof["unattended_ok"]:
                log.warning("job refused: profile %s is not unattended-eligible (task %s)", profile, task_id)
                raise BadRequest(f"profile {profile!r} is not allowed to run unattended")
        if ca_conversation_id is not None:   # the discussion that started it must be the starter's own (task #727)
            cur.execute("SELECT 1 FROM ca_conversations WHERE id = %s AND user_id = %s", (ca_conversation_id, user_id))
            if not cur.fetchone():
                ca_conversation_id = None
        requires = list(dict.fromkeys([*prof["requires_labels"], *extra]))
        cur.execute("SELECT test_mode FROM projects WHERE id = %s", (task["project_id"],))
        if cur.fetchone()["test_mode"] == "installed":      # #839: an app tested by installing it is tested on ONE runner: always pinned, locked per task
            from app import runs_on
            pin_device_id = runs_on.installed_pin(cur, task_id, task["project_id"], user_id, account_id, pin_device_id, requires)
        if pin_device_id is not None:
            cur.execute("SELECT labels FROM device_tokens WHERE id = %s AND user_id = %s AND account_id = %s AND revoked_at IS NULL "
                        "AND expires_at > now()", (pin_device_id, user_id, account_id))
            pinned = cur.fetchone()
            if not pinned:
                raise BadRequest("that runner is not one of yours")
        if pin_device_id is not None:   # the claim only lets an ALLOCATED runner take a project that has an allocation: never queue what it would refuse
            cur.execute("SELECT (EXISTS (SELECT 1 FROM project_runners WHERE project_id = %s) AND NOT EXISTS "
                        "(SELECT 1 FROM project_runners WHERE project_id = %s AND device_id = %s)) AS refused",
                        (task["project_id"], task["project_id"], pin_device_id))
            if cur.fetchone()["refused"]:
                raise BadRequest("that runner is not allocated to this project (Settings, Runners for this project)")
        if pin_device_id is not None and not set(requires) <= set(pinned["labels"]):
            raise BadRequest("that runner lacks a label the job needs: " + ", ".join(l for l in requires if l not in pinned["labels"]))
        if len(requires) > MAX_LABELS:
            raise BadRequest(f"at most {MAX_LABELS} labels")
        try:
            cur.execute(
                "INSERT INTO jobs (task_id, account_id, user_id, profile_id, unattended, approved_at, approved_by, "
                "requires_labels, ca_conversation_id, keep_open, pin_device_id, base_branch, owner_says) VALUES (%s, %s, %s, %s, %s, CASE WHEN %s THEN now() END, "
                "CASE WHEN %s THEN %s::int END, %s, %s, %s, %s, %s, %s) RETURNING *",
                (task_id, account_id, user_id, prof["id"], unattended, approved, approved, user_id, requires, ca_conversation_id,
                 bool(keep_open), pin_device_id, base_branch if re.fullmatch(r"dash/j\d+-e\d+", base_branch or "") else "", task.get("needs_work_note")))
        except UniqueViolation:
            raise Conflict(f"task {task_id} already has a live job") from None
        job = cur.fetchone()
        cur.execute("UPDATE tasks SET needs_work_note = NULL, needs_work_at = NULL WHERE id = %s", (task_id,))   # the owner's words are carried by this run (brief)
        repo.record_activity(cur, account_id, f"user:{user_id}", "queued", "job", job["id"],
                             f"Queued a {prof['name']} job for task #{task_id}" + (" (unattended)" if unattended else ""))
    nudge([job["id"]])
    return dict(job)


def approve_job(account_id: int, job_id: int, user_id: int) -> dict:
    """Approve a queued job of `account_id` (another account's job is NotFound). Idempotent."""
    approved_now = False
    with get_cursor() as cur:
        cur.execute("SELECT * FROM jobs WHERE id = %s AND account_id = %s FOR UPDATE", (job_id, account_id))
        job = cur.fetchone()
        if not job:
            raise NotFound(f"job {job_id} not found")
        if job["state"] != "queued":
            raise BadRequest("only a queued job can be approved")
        if job["approved_at"] is None:
            cur.execute("UPDATE jobs SET approved_at = now(), approved_by = %s WHERE id = %s RETURNING *",
                        (user_id, job_id))
            job = cur.fetchone()
            repo.record_activity(cur, account_id, f"user:{user_id}", "approved", "job", job_id,
                                 f"Approved job {job_id}")
            approved_now = True
    if approved_now:
        nudge([job_id])
    return dict(job)


# --- dispatch ------------------------------------------------------------------------------------------------------

# Who may take a queued job j: a live device d of the job's account AND user (the device row is joined in, so a revoked or
# expired device and a disabled user match nothing). Shared by the claim and by the WebSocket nudges (app/runner_ws.py),
# so "a job is claimable for this device" is ONE rule.
_ELIGIBLE_FROM = """
    FROM device_tokens d
    JOIN users u ON u.id = d.user_id
    JOIN jobs j ON j.account_id = d.account_id AND j.user_id = d.user_id
    JOIN tasks t ON t.id = j.task_id
    JOIN agent_profiles ap ON ap.id = j.profile_id
"""
_ELIGIBLE_WHERE = """
      d.revoked_at IS NULL AND d.expires_at > now() AND u.disabled_at IS NULL
      AND j.state = 'queued' AND j.hold_reason IS NULL
      AND (NOT EXISTS (SELECT 1 FROM project_runners pr WHERE pr.project_id = t.project_id)
           OR EXISTS (SELECT 1 FROM project_runners pr WHERE pr.project_id = t.project_id AND pr.device_id = d.id))
      AND j.requires_labels <@ d.labels
      AND (j.pin_device_id IS NULL OR j.pin_device_id = d.id)
      AND ( (d.mode = 'subscription' AND j.approved_at IS NOT NULL)
         OR (d.mode = 'api' AND j.unattended AND ap.unattended_ok
             AND COALESCE(t.created_by, '') NOT LIKE 'client:%%'
             AND t.client_request_id IS NULL AND t.requested_by IS NULL) )
"""

# ONE statement: pick the oldest eligible queued job (skipping rows another claim holds), draw a fence, and mark it
# running.
_CLAIM_SQL = """
WITH pick AS (
    SELECT j.id, d.id AS device_id
""" + _ELIGIBLE_FROM + """
    WHERE d.id = %(device)s AND""" + _ELIGIBLE_WHERE + """
    ORDER BY j.id
    LIMIT 1
    FOR UPDATE OF j SKIP LOCKED
), f AS (SELECT nextval('fence_seq') AS n FROM pick)
UPDATE jobs SET state = 'running', device_id = pick.device_id, fence = f.n,
       branch = 'dash/j' || jobs.id || '-e' || f.n, token_hash = %(token_hash)s,
       lease_expires_at = clock_timestamp() + make_interval(secs => %(lease)s),
       claimed_at = clock_timestamp(), attempts = jobs.attempts + 1,
       finish_outcome = NULL, finish_summary = '', finish_requested_at = NULL, finish_fence = NULL
FROM pick, f WHERE jobs.id = pick.id
RETURNING jobs.*
"""


def claim(device_id: int) -> dict | None:
    """The next job this device may run, or None. Returns the ids-only payload plus the job token (shown here once: only
    its hash is stored): {job_id, task_id, project_id, account_id, fence, branch, job_token, lease_seconds, profile,
    git_remote}. No task text, ever (the agent fetches it through the job MCP)."""
    token = JOB_PREFIX + secrets.token_urlsafe(32)
    with get_cursor() as cur:
        cur.execute(_CLAIM_SQL, {"device": device_id, "token_hash": _hash(token), "lease": config.JOB_LEASE_SECONDS})
        job = cur.fetchone()
        if not job:
            return None
        cur.execute("SELECT t.project_id, p.git_remote, p.slug, p.gemini_allowed FROM tasks t JOIN projects p ON p.id = t.project_id "
                    "WHERE t.id = %s", (job["task_id"],))
        task = cur.fetchone()
        cur.execute("SELECT name, model, skills, allowed_tools, agent FROM agent_profiles WHERE id = %s", (job["profile_id"],))
        prof = cur.fetchone()
        if (why := tool_pattern_problem(prof["allowed_tools"])):   # a profile edited (or seeded) with a tool pattern: the job never starts
            why = f"claim refused, profile {prof['name']!r}: {why}"
            cur.execute("UPDATE jobs SET state = 'failed', token_hash = NULL, lease_expires_at = NULL, finished_at = now(), summary = %s "
                        "WHERE id = %s", (why[:2000], job["id"]))
            cur.execute("INSERT INTO task_comments (task_id, author, body) VALUES (%s, %s, %s)", (job["task_id"], actor(job["id"]), f"Job {job['id']} did not start. " + why))
            repo.record_activity(cur, job["account_id"], f"device:{device_id}", "failed", "job", job["id"], f"Job {job['id']}: {why}"[:500])
            log.warning("job %s: %s", job["id"], why)
            return None
        repo.record_activity(cur, job["account_id"], f"device:{device_id}", "claimed", "job", job["id"],
                             f"Job {job['id']} claimed by device {device_id} (fence {job['fence']})")
        from app import skill_delivery          # the project's skills and rules, resolved now; versions recorded per attempt (#718)
        skills = skill_delivery.for_claim(cur, job, task["project_id"], prof["name"])
        from app import agent_pins              # the Owner-reviewed agent / command / skill files (#829): a job may start only on these
        pins = agent_pins.pins_for(cur, task["project_id"])
    return {"job_id": job["id"], "task_id": job["task_id"], "project_id": task["project_id"],
            "account_id": job["account_id"], "fence": job["fence"], "branch": job["branch"], "base_branch": job.get("base_branch") or "", "job_token": token,
            "lease_seconds": config.JOB_LEASE_SECONDS, "max_seconds": config.JOB_MAX_SECONDS, "keep_open": bool(job.get("keep_open")), "git_remote": task["git_remote"],
            "gemini_allowed": bool(task["gemini_allowed"]),   # #861: the project's switch; the runner adds its Gemini tools only if it also can
            "profile": {"name": prof["name"], "model": prof["model"], "skills": list(prof["skills"]),
                        "allowed_tools": list(prof["allowed_tools"]), "agent": prof["agent"] or ""},
            "mcp_servers": job_mcp_servers(task["slug"]),
            "skills": skills, "pins": pins}


def job_mcp_servers(project_slug: str) -> list[dict]:
    """The extra MCP servers (instance.yaml `job_mcp_servers`) this project's jobs get: [{name, url}]. None unless the instance sets them."""
    return [{"name": m["name"], "url": m["url"]} for m in config.JOB_MCP_SERVERS if not m["projects"] or project_slug in m["projects"]]


def agent_file_reported(device_id: int, job_id: int, fence: int, path, sha256, b64, source) -> dict:
    """The runner sends an agent / command / skill file the project Owner has not reviewed (preflight: it blocked the job) or that the agent
    created or changed (source 'job': the job is flagged and ends needing review). The bytes are stored for the Owner to read (app/agent_pins.py)."""
    from app import agent_pins
    with get_cursor() as cur:
        job = _running(cur, job_id, fence, device_id)
        cur.execute("SELECT project_id FROM tasks WHERE id = %s", (job["task_id"],))
        project_id = cur.fetchone()["project_id"]
    agent_pins.report(job, project_id, path, sha256, b64, source)
    return {}


def skills_delivered(device_id: int, job_id: int, fence: int, written: list, skipped: list) -> dict:
    """The runner reports which skills it wrote into the job's worktree (key 'rules' = the combined rules file); the rest are marked missing."""
    from app import skill_delivery
    with get_cursor() as cur:
        _running(cur, job_id, fence, device_id)
        skill_delivery.mark_delivered(cur, job_id, fence, list(written or []), list(skipped or []))
    return {}


def claimable_devices(job_id: int) -> list[int]:
    """The live devices that could claim this job right now (the claim's own rule), for the WebSocket nudge."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT d.id " + _ELIGIBLE_FROM + " WHERE j.id = %s AND" + _ELIGIBLE_WHERE, (job_id,))
        return [r["id"] for r in cur.fetchall()]


def claimable_jobs(device_id: int, limit: int = 20) -> list[dict]:
    """Ids of the queued jobs this device could claim now: [{job_id, task_id}], oldest first."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT j.id AS job_id, j.task_id " + _ELIGIBLE_FROM + " WHERE d.id = %s AND" + _ELIGIBLE_WHERE
                    + " ORDER BY j.id LIMIT %s", (device_id, limit))
        return [dict(r) for r in cur.fetchall()]


def nudge(job_ids: list[int]) -> None:
    """Tell the connected runners that may claim these jobs (ids only, over /runner/ws). Called AFTER the transaction that
    made them claimable has committed. Best effort: a nudge is a hint to claim sooner, the REST claim is the truth."""
    try:
        from app import runner_ws
        if job_ids and runner_ws.any_connected():
            for job_id in job_ids:
                runner_ws.nudge_job(job_id)
    except Exception:  # noqa: BLE001 — a failed hint must never fail the write that caused it
        log.exception("runner nudge")


def nudge_queued(account_id: int) -> None:
    """Re-offer the account's queued jobs (a device was just labelled, so one may be claimable now)."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT id FROM jobs WHERE account_id = %s AND state = 'queued' ORDER BY id LIMIT 50", (account_id,))
        ids = [r["id"] for r in cur.fetchall()]
    nudge(ids)


def _db_now(cur):
    cur.execute("SELECT clock_timestamp() AS now")
    return cur.fetchone()["now"]


def finish_mark(job: dict) -> str | None:
    """The agent's job_finish outcome, only if it was made at THIS attempt's fence (a mark can never leak across a requeue)."""
    return job.get("finish_outcome") if job.get("finish_fence") == job.get("fence") else None


def _running(cur, job_id: int, fence: int, device_id: int | None = None) -> dict:
    """The job row, locked, if it is running with exactly this fence (and on this device, when given); else StaleFence."""
    cur.execute("SELECT * FROM jobs WHERE id = %s FOR UPDATE", (job_id,))
    job = cur.fetchone()
    expired = bool(job and job["state"] == "running" and job.get("lease_expires_at") is not None
                   and job["lease_expires_at"] <= _db_now(cur))
    if (not job or job["state"] != "running" or job["fence"] != fence or expired
            or (device_id is not None and job["device_id"] != device_id)):
        log.warning("stale fence refused: job %s given fence %s (device %s)", job_id, fence, device_id)
        raise StaleFence(f"job {job_id} is not running with fence {fence}")
    return job


def heartbeat(device_id: int, job_id: int, fence: int) -> dict:
    """Renew the lease of a job this device is running. StaleFence (409) if the job moved on. Returns {lease_seconds,
    finish, keep_open}: `finish` is None until the agent called job_finish, then its outcome ('done' | 'blocked'): the
    runner's cue to end the session (unless keep_open) and finish the job. The state is "running + finish set"."""
    with get_cursor() as cur:
        job = _running(cur, job_id, fence, device_id)
        cur.execute("UPDATE jobs SET lease_expires_at = clock_timestamp() + make_interval(secs => %s) WHERE id = %s",
                    (config.JOB_LEASE_SECONDS, job_id))
    return {"lease_seconds": config.JOB_LEASE_SECONDS, "finish": finish_mark(job),
            "keep_open": bool(job.get("keep_open"))}


# --- what a job does (all through the job token; app/job_mcp.py) --------------------------------------------------

def _brief_comment(c: dict) -> dict:
    """A comment as the job sees it. One written by the assistant (author ca:<user>) was composed by a model that may have read
    untrusted text, so it is marked unconfirmed; a person's own comment carries no such mark (task #702)."""
    out = {"author": c["author"], "body": c["body"]}
    if str(c["author"]).startswith("ca:"):
        out["trust"] = "unconfirmed (assistant)"
        out["body"] = "[unconfirmed (assistant)] " + c["body"]
    return out


WHEN_DONE = ("When the work is done (committed on your branch, checks recorded, task commented), call job_finish with a short "
             "summary: that ends your session cleanly. Also pass, when you can: `tests` (the checks you ran and how they came out, one line), "
             "`next_steps` (a short list of what a person should do next, such as review the change or run the full tests) and `remaining_work` "
             "(a list of what is NOT done yet; leave it empty when the work is complete). If you are blocked and need a person, call job_finish with "
             "outcome='blocked' and say what is needed. Do not just stop and wait at the prompt. "
             "Task comments and the summary: lead with the outcome in 1-2 short lines; put details in a folded section "
             "(a line holding only <details>, then the details).")


LOGIC_RULE = ("Update the logic map in the same change: if you add, change or remove a workflow, button action, timer, webhook, queue "
              "consumer or AI call, edit `.crewboss/logic/*.yaml` to match (trigger, steps, decisions, data and side effects, AI calls). "
              "Check it with `crewboss logic check` (the Crewboss launcher, run in the app's folder), then record the commit with record_logic "
              "(`crewboss logic report --sha <sha>`): a release is refused when the map is stale or something new is uncovered.")


def brief(job_id: int, fence: int) -> dict:
    """The task text for a running job: title, body, checklist, recent comments. The only way the agent gets text."""
    with get_cursor() as cur:
        job = _running(cur, job_id, fence)
        cur.execute("SELECT p.slug, p.git_remote, (p.logic_gate = 'enforce' OR EXISTS (SELECT 1 FROM logic_reports r WHERE r.project_id = p.id)) AS uses_logic "
                    "FROM projects p JOIN tasks t ON t.project_id = p.id WHERE t.id = %s", (job["task_id"],))
        project = cur.fetchone()
    uses_logic = bool(project.pop("uses_logic"))
    task = repo.get_task(job["task_id"])
    out = {"job_id": job_id, "fence": fence, "branch": job["branch"], "base_branch": job.get("base_branch") or "", "project": dict(project),
           "task": {k: task[k] for k in ("id", "title", "body", "priority", "stream", "status") if k in task},
           "subtasks": [{"id": s["id"], "text": s["text"], "done": s["done"]} for s in task["subtasks"]],
           "recent_comments": [_brief_comment(c) for c in task["comments"][-10:]]}
    from app import ca   # discussions the starter linked to this task (task #727): ids and titles only; job_read_discussion fetches one
    out["when_done"] = WHEN_DONE
    prev = _previous_outcome(job["task_id"], job_id, job.get("owner_says"))
    if prev:
        out["previous_attempt"] = prev
    if uses_logic:      # only where the gate is on or a map was ever reported: nobody else is asked to keep one
        out["logic_map"] = LOGIC_RULE
    out["discussions"] = [{"id": d["id"], "title": d["title"], "updated_at": d["updated_at"].isoformat()} for d in ca.discussions_for_job(job)]
    if job.get("ca_conversation_id"):
        out["started_from_discussion"] = job["ca_conversation_id"]
    if task.get("client_authored"):
        out["task"]["client_authored"] = True
        out["task"]["trust_note"] = task["trust_note"]
    return out


def mark_needs_work(task_id: int, words: str, by: str) -> None:
    """The task needs more work, in the owner's words (the outcome card's box, or the assistant): kept on the task for the Run-again card and
    posted as a comment, which the next job's brief carries. Needs a note."""
    words = re.sub(r"[ \t]+", " ", (words or "")).strip()[:2000]
    if not words:
        raise BadRequest("say what is not right, so the next run knows")
    with get_cursor() as cur:
        repo._task_row(cur, task_id)
        cur.execute("UPDATE tasks SET needs_work_note = %s, needs_work_at = now() WHERE id = %s", (words, task_id))
    repo.comment_task(task_id, "Needs more work: " + words, by)
    task = repo.get_task(task_id)
    if task["status"] in ("done", "review"):    # "it is not fixed": the task is open work again (Doing), not a finished one
        try:
            repo.update_task(task_id, {"status": "doing"}, by)
        except Conflict:
            pass   # a work-in-progress limit: the note and the comment stand; the person moves it


def _previous_outcome(task_id: int, job_id: int, owner_says: str | None = None) -> dict | None:
    """What the task's last finished job left over (its remaining work) and the owner's own words, so 'Run again' carries them forward."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT j.id, j.state, j.summary, j.finish_summary, j.remaining_work, j.branch FROM tasks t "
                    "LEFT JOIN jobs j ON j.task_id = t.id AND j.id <> %s AND j.state IN ('done', 'failed') WHERE t.id = %s ORDER BY j.id DESC NULLS LAST LIMIT 1",
                    (job_id, task_id))
        r = cur.fetchone()
    if not r or not (r["id"] or owner_says):
        return None
    out = {"job_id": r["id"], "state": r["state"], "summary": (r["finish_summary"] or r["summary"] or "")[:600], "branch": r["branch"] or "",
           "remaining_work": r["remaining_work"] or []}
    if owner_says:
        out["owner_says"] = owner_says[:1500]
    return out


def record_logic(job_id: int, fence: int, sha: str, report: dict) -> dict:
    """A running job posts the logic map report of its own commit (app/logic.py stores it for the release gate)."""
    from app import logic
    with get_cursor() as cur:
        job = _running(cur, job_id, fence)
        cur.execute("SELECT project_id FROM tasks WHERE id = %s", (job["task_id"],))
        task = cur.fetchone()
    if not task:
        raise NotFound("the job's task is gone")
    row = logic.record_report(task["project_id"], sha, report, actor(job_id), job_id=job_id)
    return {"id": row["id"], "workflows": len(row["workflows"]), "uncovered": len(row["uncovered"]), "stale": len(row["stale"]),
            "errors": len(row["errors"])}


def read_discussion(job_id: int, fence: int, conversation_id: int) -> dict:
    """One discussion the job's STARTER linked to this job's task (listed in the brief), for the agent to read as context. Scoped to
    the job's user and task by ca.read_discussion; another person's chat, or one on another task, is NotFound (task #727)."""
    from app import ca
    with get_cursor() as cur:
        job = _running(cur, job_id, fence)
    return ca.read_discussion(job, conversation_id)


def comment(job_id: int, fence: int, body: str) -> dict:
    """Comment on the job's task. The insert is in the SAME transaction as the fence check (the job row stays locked), so a
    job that lost its fence can't slip a comment in. As any session's comment, it may satisfy an awaited reply: a job acts
    for its project."""
    body = (body or "").strip()
    if not body:
        raise BadRequest("a comment needs text")
    body = body[:8000]
    with get_cursor() as cur:
        job = _running(cur, job_id, fence)
        task = repo._task_row(cur, job["task_id"])
        cur.execute("INSERT INTO task_comments (task_id, author, body) VALUES (%s, %s, %s) RETURNING id",
                    (task["id"], actor(job_id), body))
        comment_id = cur.fetchone()["id"]
        repo.record_activity(cur, task["project_id"], actor(job_id), "commented", "task", task["id"],
                             f"Comment on task #{task['id']} '{task['title']}': {body}")
        repo._deliver_awaited_reply(cur, task, comment_id)
    return {"id": comment_id}


FINISH_OUTCOMES = ("done", "blocked")
AGENT_SUMMARY_CAP = 1500


def _clean_list(items, n: int = 10, width: int = 240) -> list[str]:
    """An optional list of short plain strings from the agent (next_steps / remaining_work): trimmed, capped, anything else dropped."""
    if isinstance(items, str):
        items = [items]
    return [re.sub(r"\s+", " ", x).strip()[:width] for x in (items or [])[:n] if isinstance(x, str) and x.strip()] if isinstance(items, (list, tuple)) else []


def agent_finish(job_id: int, fence: int, summary: str, outcome: str = "done", next_steps=None, remaining_work=None, tests: str = "") -> dict:
    """The agent says its work is over (job_finish). It does NOT end the job: the runner still has to stop the session, push
    the branch and finish the job (done()), so the agent can never end its own job or skip the push. This records the
    summary on the job and as a task comment (same transaction as the fence check) and marks the job "finishing"
    (state stays running, finish_outcome set), which the runner reads from its next heartbeat. 'blocked' = it needs a person."""
    if outcome not in FINISH_OUTCOMES:
        raise BadRequest(f"outcome must be one of {', '.join(FINISH_OUTCOMES)}")
    summary = (summary or "").strip()
    if not summary:
        raise BadRequest("job_finish needs a short summary" + (" of what is needed" if outcome == "blocked" else ""))
    summary = summary[:AGENT_SUMMARY_CAP]
    with get_cursor() as cur:
        job = _running(cur, job_id, fence)
        task = repo._task_row(cur, job["task_id"])
        steps, left = _clean_list(next_steps), _clean_list(remaining_work)
        cur.execute("UPDATE jobs SET finish_outcome = %s, finish_summary = %s, finish_requested_at = now(), finish_fence = %s, "
                    "next_steps = %s::jsonb, remaining_work = %s::jsonb, tests_run = %s WHERE id = %s",
                    (outcome, summary, fence, json.dumps(steps), json.dumps(left), re.sub(r"[ \t]+", " ", str(tests or "")).strip()[:600], job_id))
        text = ("Job finished: " if outcome == "done" else "Job blocked, needs a person: ") + summary
        cur.execute("INSERT INTO task_comments (task_id, author, body) VALUES (%s, %s, %s) RETURNING id",
                    (task["id"], actor(job_id), text))
        comment_id = cur.fetchone()["id"]
        repo.record_activity(cur, task["project_id"], actor(job_id), "commented", "task", task["id"],
                             f"Job {job_id} {'finished' if outcome == 'done' else 'blocked'} on task #{task['id']} '{task['title']}': {summary}")
        repo._deliver_awaited_reply(cur, task, comment_id)
        keep_open = bool(job.get("keep_open"))
    return {"job_id": job_id, "finishing": True, "outcome": outcome, "keep_open": keep_open, "comment_id": comment_id}


def set_keep_open(account_id: int, job_id: int, keep_open: bool) -> dict:
    """Leave the session open after job_finish (a person steering it over Remote Control), or close it again. Account-scoped."""
    with get_cursor() as cur:
        cur.execute("UPDATE jobs SET keep_open = %s WHERE id = %s AND account_id = %s AND state IN ('queued', 'running') "
                    "RETURNING id", (bool(keep_open), job_id, account_id))
        if not cur.fetchone():
            raise NotFound(f"job {job_id} not found")
    return {"job_id": job_id, "keep_open": bool(keep_open)}


def tick_subtask(job_id: int, fence: int, subtask_id: int, done: bool = True) -> dict:
    """Tick (or untick) one checklist step of THIS job's task. A step of another task is NotFound."""
    with get_cursor() as cur:
        job = _running(cur, job_id, fence)
        cur.execute("SELECT * FROM subtasks WHERE id = %s AND task_id = %s FOR UPDATE", (subtask_id, job["task_id"]))
        sub = cur.fetchone()
        if not sub or (sub.get("proposal") or {}).get("op") == "add":
            log.warning("job %s: subtask %s refused (not a step of task %s)", job_id, subtask_id, job["task_id"])
            raise NotFound(f"subtask {subtask_id} not found")
        if bool(done) != sub["done"]:
            if done:
                cur.execute("UPDATE subtasks SET done = true, done_at = now(), done_by = %s WHERE id = %s",
                            (actor(job_id), subtask_id))
            else:
                cur.execute("UPDATE subtasks SET done = false, done_at = NULL, done_by = NULL WHERE id = %s",
                            (subtask_id,))
            repo._touch_task(cur, job["task_id"], actor(job_id))
            repo.record_activity(cur, job["account_id"], actor(job_id), "checklist", "task", job["task_id"],
                                 f"#{job['task_id']}: {'ticked' if done else 'unticked'} '{sub['text']}'")
    return {"id": subtask_id, "done": bool(done)}


def _lock_project(job: dict, resource: str) -> dict:
    """The project a lock names, by slug. It must be in the JOB's account; another account's is NotFound."""
    try:
        project = repo.get_project_by_slug((resource or "").strip())
    except NotFound:
        project = None
    if project is None or tree.account_root_id(project["id"]) != job["account_id"]:
        log.warning("job %s: lock resource %r refused (not in account %s)", job["id"], resource, job["account_id"])
        raise NotFound(f"no such resource {resource!r}")
    return project


def _check_job_kind(kind: str) -> None:
    if kind in LOCK_KINDS and kind not in JOB_LOCK_KINDS:
        log.warning("job lock refused: a job token may not take a %s lock", kind)
        raise BadRequest(f"a job may not take a {kind} lock: ask for it with request_deploy and a person approves it")
    if kind not in JOB_LOCK_KINDS:
        raise BadRequest(f"kind must be one of {', '.join(JOB_LOCK_KINDS)}")


def acquire_lock(job_id: int, fence: int, kind: str, resource: str) -> dict:
    """Take the (account, kind, resource) lock for this job. {acquired: True} or {acquired: False, state, held_by}.
    Re-acquiring your own lock is idempotent. The account is the job's. A job may take only JOB_LOCK_KINDS: deploy and
    migrate locks belong to the server-side executor, and a job asks for those with request_deploy."""
    _check_job_kind(kind)
    with get_cursor() as cur:
        job = _running(cur, job_id, fence)
        project = _lock_project(job, resource)
        key = (job["account_id"], kind, str(project["id"]))
        cur.execute("INSERT INTO locks (account_id, kind, resource, job_id, fence) VALUES (%s, %s, %s, %s, %s) "
                    "ON CONFLICT DO NOTHING RETURNING 1", (*key, job_id, fence))
        if cur.fetchone():
            repo.record_activity(cur, job["account_id"], actor(job_id), "locked", "job", job_id,
                                 f"Job {job_id} took the {kind} lock on {project['slug']}")
            return {"acquired": True, "kind": kind, "resource": project["slug"]}
        cur.execute("SELECT job_id, fence, state FROM locks WHERE account_id = %s AND kind = %s AND resource = %s", key)
        held = cur.fetchone()
        if held and held["job_id"] == job_id and held["fence"] == fence and held["state"] == "held":
            return {"acquired": True, "kind": kind, "resource": project["slug"]}
        return {"acquired": False, "kind": kind, "resource": project["slug"], "state": held["state"] if held else "held",
                "held_by": held["job_id"] if held else None}


def release_lock(job_id: int, fence: int, kind: str, resource: str) -> dict:
    """Release a lock THIS job holds (at this fence). Anything else releases nothing."""
    _check_job_kind(kind)
    with get_cursor() as cur:
        job = _running(cur, job_id, fence)
        project = _lock_project(job, resource)
        cur.execute("DELETE FROM locks WHERE account_id = %s AND kind = %s AND resource = %s AND job_id = %s "
                    "AND fence = %s AND state = 'held' RETURNING 1",
                    (job["account_id"], kind, str(project["id"]), job_id, fence))
        return {"released": cur.fetchone() is not None}


def done(job_id: int, fence: int, ok: bool, summary: str = "", device_id: int | None = None, outcome: str = "") -> dict:
    """Finish the job (done or failed). Its locks are released and its token dies with it. Not a job-MCP tool any more: a
    device job ends through the runner's `finish` (which passes its device_id, checked in the same transaction)."""
    with get_cursor() as cur:
        job = _running(cur, job_id, fence, device_id)
        state = "done" if ok else "failed"
        if ok and outcome == "blocked" and finish_mark(job) == "blocked":
            # the agent said it needs a person: still a clean end (state done), flagged in the summary. The agent's part is capped
            # so the runner's own push note always fits in the 2000 characters.
            summary = ("Blocked, needs a person: " + (job.get("finish_summary") or "")[:AGENT_SUMMARY_CAP] + " | "
                       + (summary or "")[:2000 - AGENT_SUMMARY_CAP - 60])
        cur.execute("DELETE FROM locks WHERE job_id = %s", (job_id,))
        _close_model_calls(cur, job_id)
        cur.execute("UPDATE jobs SET state = %s, token_hash = NULL, lease_expires_at = NULL, finished_at = now(), "
                    "summary = %s WHERE id = %s", (state, (summary or "")[:2000], job_id))
        repo.record_activity(cur, job["account_id"], actor(job_id), state, "job", job_id,
                             f"Job {job_id} {state} (task #{job['task_id']})")
        cur.execute("SELECT review_paths FROM jobs WHERE id = %s", (job_id,))
        flagged = cur.fetchone()["review_paths"]
        if flagged:   # the agent created or changed agent / command / skill files: say so where the task's people look
            cur.execute("INSERT INTO task_comments (task_id, author, body) VALUES (%s, %s, %s)",
                        (job["task_id"], actor(job_id), f"Job {job_id} created or changed {len(flagged)} agent, command or skill file(s): "
                         + ", ".join(flagged[:5]) + ". They need the project Owner's review (Machines page, Agent files to review) before any job will run them."))
    _tell_finished(job_id)
    return {"job_id": job_id, "state": state}


def _tell_finished(job_id: int) -> None:
    """The person who started the job hears it ended (CA toast, drawer badge, Web Push; task #702). After the commit; a failure
    here never fails the job (the CA scheduler's sweep tells it a moment later)."""
    try:
        from app import ca_notify
        ca_notify.notify_job_finished(job_id)
    except Exception:  # noqa: BLE001
        log.exception("ca: job-finished notice for job %s", job_id)


def finish(device_id: int, job_id: int, fence: int, ok: bool, summary: str = "", outcome: str = "") -> dict:
    """The runner ends its own job after pushing the branch (over the verified socket): running on THIS device at this
    fence, else StaleFence. `outcome` is 'blocked' when the agent's job_finish said so."""
    out = done(job_id, fence, ok, summary, device_id=device_id, outcome=outcome if outcome in FINISH_OUTCOMES else "")
    _wake_views(job_id)
    return out


def _wake_views(job_id: int) -> None:
    """The job ended: open live views (#89) look at it now instead of at their next check."""
    try:
        from app import job_activity
        job_activity.wake(job_id)
    except Exception:  # noqa: BLE001
        pass


HOLD_AGENT_FILES = "agent_files"


def is_unreviewed_release(reason: str, code: str = "") -> bool:
    """The runner gave the job back only because agent / command / skill files are not reviewed (#987). A newer runner says so with
    code 'agent_files_unreviewed'; the 0.1.0 runner is recognised by the rule id its reason always carries. Other blocks are not holds."""
    if code == "agent_files_unreviewed":
        return True
    reason = reason or ""
    return "config.unreviewed" in reason and not re.search(r"\b(?!config\.unreviewed)[a-z_]+\.[a-z_]+ in \S", reason)   # no other block mixed in


def _unreviewed_count(reason: str) -> int | None:
    m = re.search(r"(\d+) agent/command/skill file", reason or "")
    return int(m.group(1)) if m else None


HOLD_MAX_DAYS = 7


def sweep_holds() -> tuple[list[int], int]:
    """Holds that must not outlive their cause (the sweeper runs this): a job held while its project has NO pending agent file left (pinned, purged
    after 30 days, deleted, however it went) is released; a hold older than HOLD_MAX_DAYS fails with a plain message. -> (job ids released, failed)."""
    with get_cursor() as cur:
        cur.execute("UPDATE jobs j SET hold_reason = NULL, hold_count = NULL, hold_at = NULL WHERE j.hold_reason IS NOT NULL AND j.state = 'queued' "
                    "AND NOT EXISTS (SELECT 1 FROM project_agent_files f JOIN tasks t ON t.project_id = f.project_id WHERE t.id = j.task_id AND f.state = 'pending') "
                    "RETURNING j.id")
        freed = [r["id"] for r in cur.fetchall()]
        cur.execute("UPDATE jobs SET state = 'failed', hold_reason = NULL, hold_at = NULL, finished_at = now(), "
                    "summary = %s WHERE hold_reason IS NOT NULL AND state = 'queued' AND hold_at < now() - make_interval(days => %s) "
                    "RETURNING id, account_id, task_id", (f"Waiting for the agent files to be reviewed for {HOLD_MAX_DAYS} days, so the job was stopped. "
                                                          "Review them on the Machines page and press Run again.", HOLD_MAX_DAYS))
        stale = cur.fetchall()
        for r in stale:
            repo.record_activity(cur, r["account_id"], "runner:sweeper", "failed", "job", r["id"],
                                 f"Job {r['id']} failed: held {HOLD_MAX_DAYS} days waiting for agent file review")
    return freed, len(stale)


def release_holds(cur, project_id: int) -> list[int]:
    """The project has no unreviewed agent file left: its held jobs may be claimed again. -> the job ids (to nudge after the commit)."""
    cur.execute("SELECT 1 FROM project_agent_files WHERE project_id = %s AND state = 'pending' LIMIT 1", (project_id,))
    if cur.fetchone():
        return []
    cur.execute("UPDATE jobs SET hold_reason = NULL, hold_count = NULL WHERE hold_reason = %s AND state = 'queued' "
                "AND task_id IN (SELECT id FROM tasks WHERE project_id = %s) RETURNING id", (HOLD_AGENT_FILES, project_id))
    return [r["id"] for r in cur.fetchall()]


def cap_words(text: str, n: int) -> str:
    """Cap text at n characters on a WORD boundary with an ellipsis, never in the middle of a word (0.1.2: "...the Owner pi")."""
    text = text or ""
    if len(text) <= n:
        return text
    cut = text[:n - 1].rsplit(" ", 1)[0] if " " in text[:n - 1] else text[:n - 1]
    return cut.rstrip(" ,;:(") + "\u2026"


REASON_CAP = 300


def _clean_reason(reason: str) -> str:
    """The runner's reason is text from a semi-trusted PC: printable characters only, one line, capped on a word boundary."""
    return cap_words(re.sub(r"\s+", " ", "".join(ch for ch in str(reason or "") if ch.isprintable() or ch.isspace())).strip(), REASON_CAP)


def release(device_id: int, job_id: int, fence: int, reason: str = "", code: str = "") -> dict:
    """The runner gives a job back because it could not start it (no claude, remote unreachable, no git credentials, Claude
    Code signed out). The same rules as a lapsed lease (end_lease): requeued with a NEW fence and no token, attempts counted,
    failed for a human after JOB_MAX_ATTEMPTS. Running on THIS device at this fence, else StaleFence. A stated reason also
    becomes a comment on the task and a push to the owner's phone (the job would otherwise fail silently)."""
    requeued: list[int] = []
    reason = _clean_reason(reason)
    note = None
    wants_hold = is_unreviewed_release(reason, code)   # #987: waiting on the Owner's review, not a failure
    hold = None
    with get_cursor() as cur:
        job = _running(cur, job_id, fence, device_id)
        if wants_hold:
            # Only hold when the Owner has something to review. A file the dashboard could not list (over 96 KB, not text, past the cap, a socket
            # that dropped mid-report) leaves nothing to pin: a hold would last for ever, so it is a normal, counted release.
            cur.execute("SELECT 1 FROM project_agent_files f JOIN tasks t ON t.project_id = f.project_id WHERE t.id = %s AND f.state = 'pending' LIMIT 1",
                        (job["task_id"],))
            if cur.fetchone():
                hold = HOLD_AGENT_FILES
            else:
                reason = "agent files flagged but none could be listed for review: " + reason
        verb = end_lease(cur, job, "released by its runner: " + cap_words(reason or "could not start", REASON_CAP), requeued_ids=requeued,
                         hold=hold, hold_count=_unreviewed_count(reason) if hold else None)
        if reason:
            task = repo._task_row(cur, job["task_id"])
            cur.execute("SELECT name FROM device_tokens WHERE id = %s", (device_id,))
            dev = cur.fetchone()
            cur.execute("INSERT INTO task_comments (task_id, author, body) VALUES (%s, %s, %s)",
                        (task["id"], "runner:" + str((dev or {}).get("name") or device_id)[:60],
                         f"Job {job_id} is waiting for you: {reason}" if hold else f"Job {job_id} was released ({'requeued' if verb == 'requeued' else 'failed'}): {reason}"))
            cur.execute("SELECT slug FROM projects WHERE id = %s", (job["account_id"],))
            acct = cur.fetchone()
            note = {"reason": reason, "task_id": task["id"], "job_id": job_id, "slug": (acct or {}).get("slug")}
    nudge([] if hold else requeued)
    if note:
        _notify_owner(note)
    return {"job_id": job_id, "state": "queued" if verb == "requeued" else "failed", **({"hold": hold} if hold else {})}


def _notify_owner(note: dict) -> None:
    """One Web Push (the dashboard's existing helper, app/push.py) saying why a job was given back. In a thread: delivery
    can take seconds and must not hold up the runner's reply. Never raises."""
    def send() -> None:
        try:
            from app import push
            base = config.PUBLIC_URL + (f"/{note['slug']}/machines?role=jobs" if note.get("slug") else "")
            push.deliver({"title": "Runner needs attention", "body": f"Task #{note['task_id']}: {note['reason']}",
                          "url": base, "tag": f"mdd-job-released-{note['job_id']}"})
        except Exception:  # noqa: BLE001 — a push problem must never fail the release
            log.exception("push for released job %s failed", note.get("job_id"))
    threading.Thread(target=send, name="job-released-push", daemon=True).start()


# --- locks a human clears -------------------------------------------------------------------------------------------

def clear_orphan(account_id: int, kind: str, resource: str, by: str) -> None:
    """A human clears an orphaned lock of `account_id` (a held lock, or another account's, is NotFound)."""
    with get_cursor() as cur:
        cur.execute("DELETE FROM locks WHERE account_id = %s AND kind = %s AND resource = %s AND state = 'orphaned' "
                    "RETURNING 1", (account_id, kind, resource))
        if not cur.fetchone():
            raise NotFound("no such orphaned lock")
        repo.record_activity(cur, account_id, by[:100], "cleared", "lock", None,
                             f"Cleared the orphaned {kind} lock on resource {resource}")


def list_locks(account_id: int) -> list[dict]:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT l.kind, l.resource, l.job_id, l.fence, l.state, l.acquired_at, p.slug "
                    "FROM locks l LEFT JOIN projects p ON p.id::text = l.resource WHERE l.account_id = %s "
                    "ORDER BY l.acquired_at DESC", (account_id,))
        return [dict(r) for r in cur.fetchall()]


def list_for(account_id: int, limit: int = 50, user_id: int | None = None) -> list[dict]:
    """The account's jobs, newest first, with profile, device and owner names. `user_id`: only that person's (#713)."""
    with get_cursor(commit=False) as cur:
        cur.execute(
            "SELECT j.id, j.task_id, j.state, j.fence, j.branch, j.unattended, j.approved_at, j.lease_expires_at, "
            "j.attempts, j.created_at, j.claimed_at, j.finished_at, j.requires_labels, j.device_id, p.name AS profile, p.model, "
            "j.review_paths, d.name AS device, u.display_name AS user_name, t.title AS task_title, pr.slug AS project_slug "
            "FROM jobs j JOIN agent_profiles p ON p.id = j.profile_id JOIN users u ON u.id = j.user_id "
            "JOIN tasks t ON t.id = j.task_id JOIN projects pr ON pr.id = t.project_id "
            "LEFT JOIN device_tokens d ON d.id = j.device_id WHERE j.account_id = %s "
            "AND (%s::int IS NULL OR j.user_id = %s) ORDER BY j.id DESC LIMIT %s",
            (account_id, user_id, user_id, limit))
        rows = [dict(r) for r in cur.fetchall()]
    from app import skill_delivery        # the skill and rule versions each attempt was given (#718)
    used = skill_delivery.uses_for_jobs([r["id"] for r in rows])
    for r in rows:
        r["skills"] = used.get(r["id"], [])
    return rows


READ_ONLY_PROFILES = ("code-auditor", "designer", "chores")    # reviews, looks and test runs: they never carry the work, a builder does


def latest_builder_for_task(task_id: int) -> dict | None:
    """The task's latest job that CHANGED CODE (any profile but the read-only ones), in latest_for_task's shape: the outcome card and the next steps
    anchor on it, so a review or a test run never becomes 'the change'."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT j.id FROM jobs j JOIN agent_profiles p ON p.id = j.profile_id WHERE j.task_id = %s AND NOT (p.name = ANY(%s)) "
                    "ORDER BY j.id DESC LIMIT 1", (task_id, list(READ_ONLY_PROFILES)))
        row = cur.fetchone()
    return latest_for_task(task_id, job_id=row["id"]) if row else None


def carry_branch(task_id: int) -> str:
    """The branch a new job on this task starts FROM: the latest builder job's, if it finished (it pushed). '' = the default branch."""
    b = latest_builder_for_task(task_id)
    return (b["branch"] or "") if b and b["state"] == "done" else ""


def followups_since(task_id: int, job_id: int) -> list[dict]:
    """Read-only jobs (a review, a test run) started after job `job_id`: [{id, profile, state, summary, tests, branch}], oldest first (sub-results of the outcome card)."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT j.id, j.state, j.summary, j.finish_summary, j.tests_run, p.name AS profile FROM jobs j JOIN agent_profiles p ON p.id = j.profile_id "
                    "WHERE j.task_id = %s AND j.id > %s AND p.name = ANY(%s) ORDER BY j.id", (task_id, job_id, list(READ_ONLY_PROFILES)))
        return [{"id": r["id"], "profile": r["profile"], "state": r["state"], "summary": r["finish_summary"] or r["summary"] or "", "tests": r["tests_run"] or ""}
                for r in cur.fetchall()]


def latest_for_task(task_id: int, job_id: int | None = None) -> dict | None:
    """The task's current job (a live one if there is one, else the most recent), for the task drawer: state, profile,
    device name, branch, who started it, summary. None if it never ran."""
    with get_cursor(commit=False) as cur:
        cur.execute(
            "SELECT j.id, j.task_id, j.account_id, j.user_id, j.state, j.branch, j.unattended, j.approved_at, j.attempts, "
            "j.requires_labels, j.summary, j.created_at, j.finished_at, j.pin_device_id, p.name AS profile, d.name AS device, "
            "u.display_name AS user_name, pd.name AS pin_device, t.project_id AS task_project_id, t.status AS task_status, t.needs_work_note, "
            "j.next_steps, j.remaining_work, j.tests_run, j.finish_outcome, j.claimed_at, j.finish_summary, j.hold_reason, j.hold_count, "
            "EXTRACT(EPOCH FROM clock_timestamp() - j.created_at)::int AS queued_s, "
            "EXTRACT(EPOCH FROM clock_timestamp() - j.claimed_at)::int AS running_s, "
            "EXTRACT(EPOCH FROM j.finished_at - COALESCE(j.claimed_at, j.created_at))::int AS ran_s "
            "FROM jobs j JOIN agent_profiles p ON p.id = j.profile_id JOIN users u ON u.id = j.user_id JOIN tasks t ON t.id = j.task_id "
            "LEFT JOIN device_tokens d ON d.id = j.device_id LEFT JOIN device_tokens pd ON pd.id = j.pin_device_id "
            "WHERE j.task_id = %s AND (%s::int IS NULL OR j.id = %s) ORDER BY (j.state IN ('queued', 'running')) DESC, j.id DESC LIMIT 1", (task_id, job_id, job_id))
        row = cur.fetchone()
        return dict(row) if row else None


def cancel_job(account_id: int, job_id: int, user_id: int) -> dict:
    """The person who started a job cancels it (anyone else's, or another account's, is NotFound). A queued job simply
    ends. A running job's lease ends cleanly: state cancelled, token dead, lease cleared, merge locks deleted, deploy/migrate
    locks left ORPHANED for a human (the agent may have been mid-way), no retry. The runner's next heartbeat or finish is a
    StaleFence 409, which is its signal to stop. A finished job is a BadRequest."""
    with get_cursor() as cur:
        cur.execute("SELECT * FROM jobs WHERE id = %s AND account_id = %s AND user_id = %s FOR UPDATE",
                    (job_id, account_id, user_id))
        job = cur.fetchone()
        if not job:
            raise NotFound(f"job {job_id} not found")
        if job["state"] not in LIVE_STATES:
            raise BadRequest(f"job {job_id} is already {job['state']}")
        cur.execute("UPDATE locks SET state = 'orphaned' WHERE job_id = %s AND kind = ANY(%s)",
                    (job_id, list(ORPHANING_KINDS)))
        cur.execute("DELETE FROM locks WHERE job_id = %s AND kind = 'merge'", (job_id,))
        _close_model_calls(cur, job_id)
        cur.execute("UPDATE jobs SET state = 'cancelled', token_hash = NULL, lease_expires_at = NULL, "
                    "finished_at = now(), summary = 'cancelled by its starter' WHERE id = %s RETURNING *", (job_id,))
        job = cur.fetchone()
        repo.record_activity(cur, account_id, f"user:{user_id}", "cancelled", "job", job_id,
                             f"Cancelled job {job_id} (task #{job['task_id']})")
    _wake_views(job_id)
    return dict(job)


def _close_model_calls(cur, job_id: int) -> None:
    """A job is ending (finished, cancelled, lost its runner): its Gemini calls that never reported back are closed as not finished (#861)."""
    from app import external_models
    external_models.close_pending(cur, job_id)


def end_lease(cur, job: dict, reason: str, out: dict | None = None, requeued_ids: list | None = None,
              hold: str | None = None, hold_count: int | None = None) -> str:
    """A running job loses its runner (lease expired, or the device was revoked). Deploy/migrate locks it held are marked
    orphaned and its merge locks deleted; the job is then FAILED (never retried) if it held a deploy/migrate lock or has been
    tried JOB_MAX_ATTEMPTS times, else requeued with a NEW fence. Its token hash is cleared either way, so the old token
    resolves to nothing. Returns 'failed' or 'requeued'."""
    out = out if out is not None else {"requeued": 0, "failed": 0, "orphaned": 0}
    cur.execute("SELECT kind FROM locks WHERE job_id = %s AND state = 'held'", (job["id"],))
    risky = [h for h in cur.fetchall() if h["kind"] in ORPHANING_KINDS]
    if risky:
        cur.execute("UPDATE locks SET state = 'orphaned' WHERE job_id = %s AND kind = ANY(%s)",
                    (job["id"], list(ORPHANING_KINDS)))
        out["orphaned"] += len(risky)
    cur.execute("DELETE FROM locks WHERE job_id = %s AND kind = 'merge'", (job["id"],))
    _close_model_calls(cur, job["id"])
    if risky or (hold is None and job["attempts"] >= config.JOB_MAX_ATTEMPTS):
        why = (f"{reason} while holding a deploy/migrate lock: the lock is orphaned and needs a human"
               if risky else f"{reason} and the job has been tried {job['attempts']} times")
        cur.execute("UPDATE jobs SET state = 'failed', token_hash = NULL, lease_expires_at = NULL, "
                    "finished_at = now(), summary = %s WHERE id = %s", (why, job["id"]))
        out["failed"] += 1
        verb = "failed"
    else:   # back to the queue with a NEW fence and no token
        cur.execute("UPDATE jobs SET state = 'queued', device_id = NULL, branch = '', lease_expires_at = NULL, "
                    "token_hash = NULL, fence = nextval('fence_seq'), finish_outcome = NULL, finish_summary = '', "
                    "finish_requested_at = NULL, finish_fence = NULL, next_steps = NULL, remaining_work = NULL, tests_run = '', hold_reason = %s, hold_count = %s, hold_at = CASE WHEN %s::text IS NULL THEN NULL ELSE now() END, "
                    "attempts = CASE WHEN %s::text IS NULL THEN attempts ELSE GREATEST(attempts - 1, 0) END WHERE id = %s",
                    (hold, hold_count if hold else None, hold, hold, job["id"]))   # a hold is waiting on a person: the attempt is not counted
        out["requeued"] += 1
        if requeued_ids is not None:
            requeued_ids.append(job["id"])
        why, verb = (f"{reason}; waiting on a person (attempt not counted)" if hold else f"{reason}; requeued with a new fence"), "requeued"
    log.warning("job %s %s (%s)", job["id"], verb, why)
    repo.record_activity(cur, job["account_id"], "runner:sweeper", verb, "job", job["id"], f"Job {job['id']}: {why}")
    return verb


def end_for_device(cur, device_id: int, reason: str, requeued_ids: list | None = None) -> int:
    """End every running job of a device (it was revoked): the same handling as an expired lease. Returns how many.
    `requeued_ids`, when given, collects the jobs that went back to the queue, for nudge() once the caller has committed.
    Queued jobs PINNED to this device (#810 "Runs on") are unpinned too: nothing else could ever claim them, so they go back to any of the
    starter's runners (and are collected for the nudge)."""
    cur.execute("SELECT * FROM jobs WHERE device_id = %s AND state = 'running' ORDER BY id FOR UPDATE", (device_id,))
    rows = cur.fetchall()
    for job in rows:
        end_lease(cur, job, reason, requeued_ids=requeued_ids)
    cur.execute("UPDATE jobs SET pin_device_id = NULL WHERE pin_device_id = %s AND state = 'queued' RETURNING id", (device_id,))
    freed = [r["id"] for r in cur.fetchall()]
    if requeued_ids is not None:
        requeued_ids.extend(freed)
    return len(rows)


# --- the sweeper ---------------------------------------------------------------------------------------------------

class Sweeper:
    """Expires leases. Time is the DATABASE's. Each run compares how far database time moved since the last run with how
    far this process's monotonic clock moved: a gap over JOB_CLOCK_JUMP_SECONDS is a clock jump or a sleeping host (the
    monotonic clock stops in suspend), and the run then gives every running job one lease of grace and expires nothing."""

    def __init__(self, monotonic=time.monotonic):
        self._mono = monotonic
        self._last: tuple | None = None

    def run(self) -> dict:
        """One pass. Returns {requeued, failed, orphaned, graced} counts."""
        out = {"requeued": 0, "failed": 0, "orphaned": 0, "graced": 0}
        requeued_ids: list[int] = []
        with get_cursor() as cur:
            cur.execute("SELECT clock_timestamp() AS now")
            db_now, mono = cur.fetchone()["now"], self._mono()
            last, self._last = self._last, (db_now, mono)
            if last is not None:
                drift = abs((db_now - last[0]).total_seconds() - (mono - last[1]))
                if drift > config.JOB_CLOCK_JUMP_SECONDS:
                    cur.execute("UPDATE jobs SET lease_expires_at = clock_timestamp() + make_interval(secs => %s) "
                                "WHERE state = 'running'", (config.JOB_LEASE_SECONDS,))
                    out["graced"] = cur.rowcount
                    log.warning("job sweeper: clock jump of %.0fs (or a sleeping host); gave %d running job(s) a lease of grace",
                                drift, out["graced"])
                    return out
            cur.execute("SELECT * FROM jobs WHERE state = 'running' AND lease_expires_at <= clock_timestamp() "
                        "ORDER BY id FOR UPDATE SKIP LOCKED")
            for job in cur.fetchall():
                end_lease(cur, job, "lease expired", out, requeued_ids)
        nudge(requeued_ids)
        try:
            from app import job_activity
            job_activity.purge()   # #89: the live view keeps 30 days (throttled to once an hour inside)
        except Exception:  # noqa: BLE001 - a failed cleanup must never stop lease expiry
            log.exception("job activity purge")
        freed, failed = sweep_holds()
        nudge(freed)
        out["failed"] += failed
        return out
