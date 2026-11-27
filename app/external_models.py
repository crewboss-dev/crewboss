"""Another provider's model for a job's agent (#861, phase 1): Gemini as a second opinion and for pictures.

Gemini is called from a RUNNER (its API key lives in a private file there), not from here. What Crewboss owns is the boundary and the books:
  * `projects.gemini_allowed` (off by default) decides whether a job of that project gets the two local tools at all. The claim tells the
    runner (jobs.claim), the runner adds its local MCP server only when it also has the capability, and this module refuses to record a call
    when the switch is off. Only the project's Owner flips it (the same right as server reads' production switch).
  * every call is an `external_model_use` row written in two steps: BEGIN (before anything is sent: the switch and the fence are checked here and a
    pending row is written; there are NO usage caps here: people set their limits in Google's console) and FINISH (only that job's own pending row). Sizes only, never the text. The task run panel lists a job's calls; the project page shows the switch and a count.

Nothing in here talks to a model."""
from __future__ import annotations

import re

from app import deploys, jobs, repo
from app.db import get_cursor
from app.repo import BadRequest, Conflict, Forbidden, NotFound

PROVIDERS = ("gemini",)
TOOLS = ("second_opinion", "make_image")
TOOL_LABELS = {"second_opinion": "second opinion", "make_image": "pictures"}
LIMIT_ERROR = "Gemini limit reached"                # the error text of a call Google refused for quota or rate (HTTP 429 / RESOURCE_EXHAUSTED)
MAX_CALLS_PER_JOB = 200                              # a ceiling on ROWS (a runaway job must not grow the table), not on spend
CEILING_ERROR = f"this job has made {MAX_CALLS_PER_JOB} Gemini calls: no more are recorded or allowed"
LIMIT_NOTICE_EVERY = "1 hour"                        # at most one owner notification per project in this time
MAX_REFUSED_ROWS = 20                                # per job: past this a refusal is still a refusal, but no more rows are written
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}\Z")
MAX_BYTES = 2_000_000_000


def _who(who) -> str:
    return f"user:{who.user_id}" if who.user_id else "owner"


def require_manage(who, project: dict) -> None:
    if not deploys.can_manage_targets(who, project):
        raise Forbidden("only the project's Owner decides whether jobs may use Gemini")


def set_allowed(who, project: dict, on: bool) -> None:
    require_manage(who, project)
    with get_cursor() as cur:
        cur.execute("UPDATE projects SET gemini_allowed = %s WHERE id = %s", (bool(on), project["id"]))
        repo.record_activity(cur, project["id"], _who(who), "set", "gemini_allowed", project["id"],
                             "Jobs may now ask Gemini for a second opinion and pictures" if on else "Jobs may no longer use Gemini")


def usage(project_id: int) -> dict:
    """For the project page: {total, ok, last_30_days, by_tool: {tool: n}}. A call still pending counts as sent (its data may have left)."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT tool, count(*) AS n, count(*) FILTER (WHERE at > now() - interval '30 days') AS recent, "
                    "count(*) FILTER (WHERE ok) AS good FROM external_model_use WHERE project_id = %s AND state IN ('done', 'pending') GROUP BY tool", (project_id,))
        rows = cur.fetchall()
    return {"total": sum(r["n"] for r in rows), "ok": sum(r["good"] for r in rows), "last_30_days": sum(r["recent"] for r in rows),
            "by_tool": {r["tool"]: r["n"] for r in rows}}


def for_job(job_id: int, limit: int = 20) -> list[dict]:
    """The task run panel's compact list: what this job asked another model, and how much went each way."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT id, provider, tool, model, bytes_in, bytes_out, ok, state, error, at FROM external_model_use "
                    "WHERE job_id = %s ORDER BY id DESC LIMIT %s", (job_id, limit))
        return [{**dict(r), "label": TOOL_LABELS.get(r["tool"], r["tool"])} for r in cur.fetchall()]


def close_pending(cur, job_id: int) -> int:
    """When a job ends: every call it began and never finished is closed as done, not ok (its data may have gone; the size sent stays)."""
    cur.execute("UPDATE external_model_use SET state = 'done', ok = false, error = 'not finished (job ended)' "
                "WHERE job_id = %s AND state = 'pending'", (job_id,))
    return cur.rowcount


def _size(value, name: str) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise BadRequest(f"{name} must be a number") from None
    if not 0 <= n <= MAX_BYTES:
        raise BadRequest(f"{name} is out of range")
    return n


def _job_and_project(cur, job_id: int, fence: int):
    job = jobs._running(cur, job_id, fence)          # locks the job row FOR UPDATE: two calls of one job queue up here
    cur.execute("SELECT p.* FROM projects p JOIN tasks t ON t.project_id = p.id WHERE t.id = %s", (job["task_id"],))
    project = cur.fetchone()
    if not project:
        raise NotFound("the job's task is gone")
    return job, project


def begin_use(job_id: int, fence: int, tool: str, model: str, bytes_in=0, provider: str = "gemini") -> dict:
    """Step one, BEFORE any data leaves the runner. Under the job's lock: the fence and lease and the project's switch are checked, then a
    'pending' row is written and its id returned. A switch that is off is kept as a 'refused' row and raises Forbidden. There is no spend cap
    (Google's console limits that); only a ceiling of MAX_CALLS_PER_JOB rows per job, refused with ONE refused row and ONE activity row. The runner calls the provider only after this succeeds."""
    provider, tool, model = (provider or "").strip().lower(), (tool or "").strip(), (model or "").strip()
    if provider not in PROVIDERS:
        raise BadRequest(f"provider must be one of {', '.join(PROVIDERS)}")
    if tool not in TOOLS:
        raise BadRequest(f"tool must be one of {', '.join(TOOLS)}")
    if model and not MODEL_RE.fullmatch(model):
        raise BadRequest("model is not a plain model name")
    n_in = _size(bytes_in, "bytes_in")
    problem = None
    with get_cursor() as cur:
        job, project = _job_and_project(cur, job_id, fence)
        cur.execute("SELECT count(*) FILTER (WHERE state = 'refused') AS r, count(*) FILTER (WHERE state <> 'refused') AS used, "
                    "count(*) FILTER (WHERE state = 'refused' AND error = %s) AS ceiling FROM external_model_use WHERE job_id = %s", (CEILING_ERROR, job_id))
        counts = cur.fetchone()
        refused_rows = counts["r"]
        at_ceiling = counts["used"] >= MAX_CALLS_PER_JOB and bool(project["gemini_allowed"])
        row_id = None
        if at_ceiling:        # one refused row and ONE activity row for the job, however many more tries; nothing else is written
            if not counts["ceiling"]:
                cur.execute("INSERT INTO external_model_use (job_id, task_id, project_id, provider, tool, model, bytes_in, state, error) "
                            "VALUES (%s, %s, %s, %s, %s, %s, 0, 'refused', %s)", (job_id, job["task_id"], project["id"], provider, tool, model, CEILING_ERROR))
                repo.record_activity(cur, project["id"], jobs.actor(job_id), "refused", "task", job["task_id"], f"Job {job_id}: {CEILING_ERROR}")
        else:
            if not project["gemini_allowed"]:
                problem = "Gemini is switched off for this project (an Owner turns it on on the project's Where it lives page)"
            if not (problem and refused_rows >= MAX_REFUSED_ROWS):      # a job hammering a refusal does not grow the table without bound
                cur.execute("INSERT INTO external_model_use (job_id, task_id, project_id, provider, tool, model, bytes_in, state, error) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
                            (job_id, job["task_id"], project["id"], provider, tool, model, n_in, "refused" if problem else "pending",
                             problem[:200] if problem else ""))
                row_id = cur.fetchone()["id"]
    if at_ceiling:
        raise Conflict(CEILING_ERROR)
    if problem:    # raised after the row is committed, so the refusal stays on the books
        raise Forbidden(problem)
    return {"use_id": row_id}


def finish_use(job_id: int, fence: int, use_id: int, bytes_out=0, ok=True, error: str = "", bytes_in=None, limited=False) -> dict:
    """Step two: close THIS job's own pending row. Nothing else can be written: a row of another job, one already finished or a refused one is NotFound."""
    n_out = _size(bytes_out, "bytes_out")
    n_in = _size(bytes_in, "bytes_in") if bytes_in is not None and bytes_in != -1 else None
    with get_cursor() as cur:
        job, project = _job_and_project(cur, job_id, fence)
        cur.execute("UPDATE external_model_use SET state = 'done', ok = %s, bytes_out = %s, bytes_in = COALESCE(%s, bytes_in), error = %s "
                    "WHERE id = %s AND job_id = %s AND state = 'pending' RETURNING tool, model, bytes_in",
                    (bool(ok), n_out, n_in, (LIMIT_ERROR if limited and not ok else deploys.redact(error or ""))[:300], int(use_id), job_id))
        row = cur.fetchone()
        if not row:
            raise NotFound("no pending call of this job with that id")
        if limited and not ok:
            _limit_reached(cur, job, project, int(use_id))
        cur.execute("SELECT 1 FROM activity WHERE project_id = %s AND actor = %s AND entity = 'task' AND entity_id = %s AND verb = 'used' LIMIT 1",
                    (project["id"], jobs.actor(job_id), job["task_id"]))
        if ok and not cur.fetchone():       # once per job (the first good call), not one row per call: the usage view has the rest
            repo.record_activity(cur, project["id"], jobs.actor(job_id), "used", "task", job["task_id"],
                                 f"Job {job_id} used gemini {TOOL_LABELS[row['tool']]} ({row['model'] or 'default model'}, "
                                 f"{row['bytes_in']} bytes sent, {n_out} back)")
    return {"recorded": int(use_id)}


def _limit_reached(cur, job: dict, project: dict, use_id: int) -> None:
    """Google refused a call for quota or rate (the runner says so; its row's error is LIMIT_ERROR). A warning goes on the task once per job, and the
    person who started the job gets ONE notification per project per LIMIT_NOTICE_EVERY. Nothing is blocked: the next call is simply tried."""
    cur.execute("SELECT count(*) AS n FROM external_model_use WHERE job_id = %s AND error = %s AND id <> %s", (job["id"], LIMIT_ERROR, use_id))
    if cur.fetchone()["n"] == 0:
        cur.execute("INSERT INTO task_comments (task_id, author, body) VALUES (%s, %s, %s)", (job["task_id"], jobs.actor(job["id"]),
                    "Warning: Gemini refused a call because its quota or rate limit was reached, so the job carried on without that second opinion "
                    "or picture. Limits are set in the Google console."))
    if not job.get("user_id"):
        return
    cur.execute("SELECT pg_advisory_xact_lock(hashtext('gemini_limit'), %s)", (project["id"],))     # two jobs of a project queue up here
    cur.execute("SELECT 1 FROM ca_notices WHERE kind = 'gemini_limit' AND ref LIKE %s AND created_at > now() - %s::interval LIMIT 1",
                (f"p{project['id']}:%", LIMIT_NOTICE_EVERY))
    if cur.fetchone():
        return
    cur.execute("INSERT INTO ca_notices (user_id, kind, ref, title, body, url) VALUES (%s, 'gemini_limit', %s, %s, %s, %s) ON CONFLICT DO NOTHING "
                "RETURNING *", (job["user_id"], f"p{project['id']}:{use_id}", "Gemini limit reached",
                                f"Jobs of {project['name'][:60]} are being refused by Gemini's quota or rate limit. Check the limits in the Google console."[:400],
                                f"/{project['slug']}/where"))
    n = cur.fetchone()
    if n:
        from app import ca_notify
        ca_notify.announce(n)
