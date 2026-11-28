"""The job card in the task window (#987): what the person sees from the moment they press Run until the job ends.

One pure function, `view(job, project)`, turns the task's current job (jobs.latest_for_task) into plain words: a state key, a title, how
long, who has it, the agent's note, the branch link and, for a job held for unreviewed agent files, the "Waiting for you" banner with a
button to the account's Machines page. The template (partials/job_card_status.html) only prints it, and polls itself while the job is live.
Change #89's live "runner conversation" fills the Activity slot (partials/job_card.html), which is deliberately empty here."""
from __future__ import annotations

import logging
import re

from app import agent_pins, jobs, repo, tree

log = logging.getLogger("crewboss.job_card")
POLL_SECONDS = 3
STEPS = ("Queued", "Claimed", "Running", "Finished")


def duration(seconds) -> str:
    """'25 s', '2 min 05 s', '1 h 03 min'."""
    s = max(int(seconds or 0), 0)
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min {s % 60:02d} s"
    return f"{s // 3600} h {(s % 3600) // 60:02d} min"


def branch_url(remote: str | None, branch: str | None) -> str:
    """A link to the branch on GitHub when the remote is a GitHub one; '' for any other host (the name is shown, not linked)."""
    m = re.match(r"^(?:https://(?:[^@/]+@)?github\.com/|git@github\.com:)([\w.-]+/[\w.-]+?)(?:\.git)?/?$", (remote or "").strip())
    return f"https://github.com/{m.group(1)}/tree/{branch}" if m and branch and re.fullmatch(r"[\w./-]+", branch) else ""


def account_machines_url(project_id: int, anchor: bool = False) -> str:
    """/<account>/machines?tab=runners (+ #agent-files) for the account that owns this project; '' if it has none."""
    root = tree.account_root_id(project_id)
    if root is None:
        return ""
    return f"/{repo.get_project(root)['slug']}/machines?tab=runners" + ("#agent-files" if anchor else "")


def view(job: dict | None, project: dict | None) -> dict | None:
    if not job:
        return None
    state = job["state"]
    held = state == "queued" and bool(job.get("hold_reason"))
    slow = held or (state == "queued" and (job.get("queued_s") or 0) > 120)     # a long wait is polled gently (15 s)
    out = {"key": "waiting_on_you" if held else state, "live": state in jobs.LIVE_STATES, "poll": 15 if slow else POLL_SECONDS,
           "device": job.get("device") or "", "note": "", "banner": None, "branch": job.get("branch") or "",
           "branch_url": "", "step": 0}
    if held:
        n = agent_pins.pending_count(job["task_project_id"]) if job.get("task_project_id") else 0
        n = n or job.get("hold_count") or 0
        files = f"{n} agent file{'' if n == 1 else 's'}" if n else "the agent files"
        out.update(label="Waiting on you", title="Waiting for you to review the agent files",
                   banner={"text": f"Waiting for you: review {files} before this project's jobs can run",
                           "url": "",
                           "n": n})
        out["detail"] = f"Queued {duration(job.get('queued_s'))} ago. It starts by itself once you pin the files."
    elif state == "queued":
        who = job.get("pin_device")
        out.update(label="Queued", title=f"Waiting for {who}…" if who else "Waiting for a runner…",
                   detail=f"for {duration(job.get('queued_s'))}")
        if not who and job.get("task_project_id"):    # say WHY nothing is taking it, plainly, instead of waiting for ever
            try:
                from app import run_plan
                pick = run_plan.choose_runner(job["task_project_id"], job["user_id"], list(job.get("requires_labels") or []))
            except Exception:   # noqa: BLE001 - a hint only: the card never fails for it
                log.exception("runner hint for the job card")
                pick = None
            if pick and not pick["device"]:
                out["detail"] += " \u00b7 " + pick["message"]
                if pick.get("refuse"):
                    out["title"] = "No runner can take this job"
    elif state == "running":
        out.update(label="Running", step=2, title=f"Claimed by {job.get('device') or 'a runner'}, running",
                   detail=f"for {duration(job.get('running_s'))}")
    elif state == "done":
        out.update(label="Done", step=4, title="Done", detail=f"took {duration(job.get('ran_s'))}")
        out["branch_url"] = branch_url((project or {}).get("git_remote"), out["branch"])
    elif state == "failed":
        out.update(label="Failed", step=4, title="Failed", detail=f"after {duration(job.get('ran_s'))}")
    else:
        out.update(label="Stopped", step=4, title="Stopped", detail="")
    if state == "queued":
        out["step"] = 1
    # the job's own words: what the agent said it did (finish_summary, while it is still live) or how the job ended (summary)
    out["note"] = (job.get("summary") if not out["live"] else job.get("finish_summary")) or ""
    out["steps"] = STEPS
    return out


def outcome(job: dict | None) -> dict | None:
    """The OUTCOME CARD that replaces the Run button after a job (change #90). None = there is something to run (no job, or a stopped one).
    key: done | needs_work | waiting | failed. Needs more work = the agent listed what is left, or the owner said so (CA, or the box on the card)."""
    if not job or job["state"] in jobs.LIVE_STATES:
        return None
    if job["state"] == "cancelled" and not job.get("needs_work_note"):
        return None
    left = [x for x in (job.get("remaining_work") or []) if isinstance(x, str)]
    steps = [x for x in (job.get("next_steps") or []) if isinstance(x, str)]
    note = job.get("needs_work_note") or ""
    base = {"summary": job.get("finish_summary") or job.get("summary") or "", "branch": job.get("branch") or "", "tests": job.get("tests_run") or "",
            "remaining": left, "next_steps": steps, "owner_note": note, "task_done": job.get("task_status") == "done"}
    if job["state"] == "failed":
        summ = job.get("summary") or ""
        out = {**base, "key": "failed", "label": "Failed", "summary": summ, "unreviewed": False}
        # a job that failed on unreviewed agent files BEFORE 0.1.1 (it was counted and failed, not held): the same plain next step as the new hold
        out["unreviewed"] = jobs.is_unreviewed_release(summ)
        return out
    if job.get("finish_outcome") == "blocked":
        return {**base, "key": "waiting", "label": "Waiting on you"}
    if left or note:
        return {**base, "key": "needs_work", "label": "Needs more work"}
    return {**base, "key": "done", "label": "Done"}


def job_events(task_id: int, limit: int = 20) -> list[dict]:
    """The task's job history as timeline items, oldest first: [{at, kind, title, body, link, job_id}] (change #91's timeline; the run rows
    expand into the live activity view, change #89). kind: job_started | job_claimed | job_finished | job_failed | job_stopped |
    job_waiting | job_needs_work. `link` is the branch URL for a finished job, else ''."""
    with jobs.get_cursor(commit=False) as cur:
        cur.execute("SELECT j.id, j.state, j.created_at, j.claimed_at, j.finished_at, j.branch, j.summary, j.finish_summary, j.finish_outcome, "
                    "j.remaining_work, j.hold_reason, j.tests_run, p.name AS profile, d.name AS device, pr.git_remote, u.display_name AS starter "
                    "FROM jobs j JOIN agent_profiles p ON p.id = j.profile_id JOIN tasks t ON t.id = j.task_id JOIN projects pr ON pr.id = t.project_id "
                    "JOIN users u ON u.id = j.user_id LEFT JOIN device_tokens d ON d.id = j.device_id WHERE j.task_id = %s ORDER BY j.id DESC LIMIT %s",
                    (task_id, limit))
        rows = [dict(r) for r in cur.fetchall()][::-1]
    out = []
    for j in rows:
        out.append({"at": j["created_at"], "kind": "job_started", "job_id": j["id"], "link": "",
                    "title": f"{j['starter']} started a {j['profile']} job", "body": ""})
        if j["claimed_at"]:
            out.append({"at": j["claimed_at"], "kind": "job_claimed", "job_id": j["id"], "link": "",
                        "title": f"Claimed by {j['device'] or 'a runner'}", "body": ""})
        if j["finished_at"]:
            oc = outcome({**j, "next_steps": [], "task_status": ""})
            kind = {"done": "job_finished", "needs_work": "job_needs_work", "waiting": "job_waiting", "failed": "job_failed"}.get(oc["key"] if oc else "", "job_stopped")
            title = {"job_finished": "Finished", "job_needs_work": "Finished, needs more work", "job_waiting": "Waiting on you",
                     "job_failed": "Failed", "job_stopped": "Stopped"}[kind]
            body = "\n".join(x for x in [(oc or {}).get("summary") or j["summary"], ("Tests: " + j["tests_run"]) if j["tests_run"] else ""] if x)
            out.append({"at": j["finished_at"], "kind": kind, "job_id": j["id"], "title": title, "body": body,
                        "link": branch_url(j["git_remote"], j["branch"]) if kind in ("job_finished", "job_needs_work") else ""})
    return out
