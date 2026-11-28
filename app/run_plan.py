"""One-button Run (change #90): what a press of Run does without being asked. The PROFILE comes from the task, the RUNNER from the project's
allocation (Settings, "Runners for this project") and what is online, and the card says it in one line, "Will run on <runner>".

Profile: a fix, feature, tweak or workflow task is built by `builder`; a review by `code-auditor` (reads, comments); a question or research task by
`designer` (read-only: a look, no edits); a deploy is NEVER a job (it stays the deploy card with the owner's OK). The person overrides any of it
under "More options". Runner: the project's allocated runners of the starter's (none allocated = all of their runners, as before) that are online
and hold every label the job needs, least busy first. None online: the job is queued unpinned and starts when one connects (the claim rule in
app/jobs.py only lets an allocated runner take a project that has an allocation)."""
from __future__ import annotations

import re

from app import jobs, runs_on
from app.db import get_cursor
from app.repo import NotFound

DEPLOY_RE = re.compile(r"^\s*(deploy|release|ship|roll ?out|go live|publish)\b", re.I)
REVIEW_RE = re.compile(r"^\s*(code[- ])?(review|audit)\b", re.I)
LOOK_RE = re.compile(r"^\s*(research|investigate|why|how|what|which|does|is there|find out|explain|question)\b|\?\s*$", re.I)


def profile_for(task: dict) -> dict:
    """{profile: name | None, why: text}. None = not a job (a deploy)."""
    title = (task.get("title") or "").strip()
    if DEPLOY_RE.search(title):
        return {"profile": None, "why": "A deploy is never run as a job: it stays the deploy card, with your OK."}
    if REVIEW_RE.search(title) or task.get("category") == "review":
        return {"profile": "code-auditor", "why": "A review: reads the code and comments, changes nothing."}
    if LOOK_RE.search(title):
        return {"profile": "designer", "why": "A question or research: a read-only look, no edits."}
    return {"profile": "builder", "why": "Builds the change on a branch."}


def _profile_exists(name: str) -> bool:
    return any(p["name"] == name for p in jobs.list_profiles())


def allocated(project_id: int) -> list[int]:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT device_id FROM project_runners WHERE project_id = %s ORDER BY device_id", (project_id,))
        return [r["device_id"] for r in cur.fetchall()]


def _busy(device_id: int) -> int:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT count(*) AS n FROM jobs WHERE device_id = %s AND state = 'running'", (device_id,))
        return cur.fetchone()["n"]


def choose_runner(project_id: int, user_id: int, requires: list[str]) -> dict:
    """{device: {id, name} | None, candidates: n, message}. Candidates = the person's live runners, narrowed to the project's allocation when it has one."""
    account_id = runs_on._account_id(project_id)
    mine = runs_on.my_devices(account_id, user_id)
    alloc = set(allocated(project_id))
    cands = [d for d in mine if not alloc or d["id"] in alloc]     # == runs_on.my_devices(account, user, project)
    need = set(requires)
    fit = [d for d in cands if need <= set(d["labels"])]
    online = sorted((d for d in fit if runs_on._connected(d["id"])), key=lambda d: (_busy(d["id"]), d["id"]))
    if online:
        return {"device": online[0], "candidates": len(cands), "message": "", "starved": False}
    if not cands:   # nothing could ever claim this job: say so, never "waiting"
        msg = ("None of your runners is allocated to this project. Ask the owner to add one in Settings, Runners for this project."
               if alloc and mine else ("You have no runner yet. Enrol one on the Machines page." if not mine else
                                       "None of your runners is allocated to this project. Ask the owner to add one in Settings, Runners for this project."))
        return {"device": None, "candidates": 0, "message": msg, "starved": True, "refuse": bool(alloc)}
    if not fit:
        return {"device": None, "candidates": len(cands), "starved": True,
                "message": "No runner of this project has the label " + ", ".join(sorted(need)) + ". Add the label to one on the Machines page."}
    return {"device": None, "candidates": len(cands), "message": "No runner online for this project. It will start when one connects.", "starved": False}


def plan(task: dict, user_id: int, profile: str = "", runner: str = "", labels="") -> dict:
    """{profile, why, requires, device, pin, message, line}. `profile` / `runner` ('' = automatic, else a runner id or 'any') are the "More options"."""
    auto = profile_for(task)
    name = (profile or "").strip() or auto["profile"]
    why = "You chose this profile." if (profile or "").strip() else auto["why"]
    if name and (profile or "").strip() and not _profile_exists(name):
        raise NotFound(f"no profile {name!r}")
    if name and not _profile_exists(name):
        name, why = ("builder", "Builds the change on a branch.") if _profile_exists("builder") else (None, "No profile is set up.")
    prof = next((p for p in jobs.list_profiles() if p["name"] == name), None) if name else None
    requires = [*(prof["requires_labels"] if prof else []), *jobs.parse_labels(labels or [])]
    out = {"profile": name, "why": why, "requires": requires, "device": None, "pin": None, "message": "", "line": "", "starved": False}
    if not name:
        return {**out, "line": why}
    runner = (runner or "").strip()
    if runner and runner not in ("auto", "default"):
        pin = runs_on.resolve(task["project_id"], user_id, runner)     # a runner of theirs, or 'any' (None)
        dev = next((d for d in runs_on.my_devices(runs_on._account_id(task["project_id"]), user_id) if d["id"] == pin), None)
        out.update(pin=pin, device=dev, line=f"Will run on {dev['name']}" if dev else "Will run on any of your runners")
        return out
    pick = choose_runner(task["project_id"], user_id, requires)
    if pick["device"]:
        out.update(device=pick["device"], pin=pick["device"]["id"], line=f"Will run on {pick['device']['name']}")
    else:
        out.update(message=pick["message"], line=pick["message"], starved=pick["starved"], refuse=pick.get("refuse", False))
    return out
