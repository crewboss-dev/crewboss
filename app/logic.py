"""The logic map, dashboard side (task #715, change #67): stored reports, the release gate, the one-release override, release diffs.

The map itself (parsing, discovery, diff, picture) is app/logic_map.py and runs where the code is: a job or CI runs
`python -m app.cli logic report` on a commit and posts the result (POST /api/logic/report, or the job MCP's record_logic). This module keeps
that report per (project, commit) and answers one question for a PRODUCTION release request: may this commit go?

The gate (per project, `projects.logic_gate` = 'off' | 'enforce'). When enforced, a production release is refused when
  - there is no report for the commit,
  - the map fails its schema check (`errors`),
  - an entry points at code that is gone (`stale`),
  - or it adds uncovered entry points compared with the last release (the baseline: the report of the last commit that went to production,
    else the newest earlier report; with no earlier report the first report is the starting point and nothing is "new").
An Admin may override it for ONE release: a recorded grant (who, why) for one commit, used up by the first release request that needs it.
Rollbacks are exempt (they restore a commit that already went out)."""
from __future__ import annotations

import json

from psycopg.errors import UniqueViolation

from app import access, logic_map, repo
from app.db import get_cursor
from app.repo import BadRequest, Conflict, Forbidden, NotFound

REASON_MIN = 8
SHA_LEN = 40


def _sha(sha: str) -> str:
    sha = (sha or "").strip().lower()
    if len(sha) != SHA_LEN or any(c not in "0123456789abcdef" for c in sha):
        raise BadRequest("sha must be the full 40-character commit id")
    return sha


# --- reports -----------------------------------------------------------------------------------------------------------------

MAX_STALE, MAX_UNCOVERED, MAX_ERRORS = 2000, 5000, 200
JOB_REPORT_COMMITS = 5       # distinct commits one job may report (the dashboard holds no repo, so a job's head cannot be checked)


def _text(v, where: str, limit: int, required: bool = False) -> str:
    if v is None and not required:
        return ""
    if not isinstance(v, str) or (required and not v.strip()):
        raise BadRequest(f"{where} must be text")
    if len(v) > limit:
        raise BadRequest(f"{where} is longer than {limit} characters")
    return v


def clean_report(report) -> dict:
    """The report as the dashboard stores it: the map re-run through the same normalisers the CLI uses (logic_map._workflow), and every
    stale / uncovered / error entry checked for type and length. Raises BadRequest with the first problems; stores nothing else."""
    if not isinstance(report, dict) or report.get("version") != logic_map.REPORT_VERSION:
        raise BadRequest(f"report must be a JSON object with version {logic_map.REPORT_VERSION} (crewboss logic report)")
    for key in ("workflows", "errors", "stale", "uncovered"):
        if not isinstance(report.get(key), list):
            raise BadRequest(f"report.{key} must be a list")
    if len(report["workflows"]) > logic_map.MAX_WORKFLOWS or len(report["stale"]) > MAX_STALE \
            or len(report["uncovered"]) > MAX_UNCOVERED or len(report["errors"]) > MAX_ERRORS:
        raise BadRequest("report is too large")
    errs: list[str] = []
    workflows, seen = [], set()
    for i, raw in enumerate(report["workflows"]):
        wf = logic_map._workflow(raw, f"workflows[{i}]", errs)
        if wf and wf["id"] in seen:
            errs.append(f"workflows[{i}]: id {wf['id']!r} is used twice")
        elif wf:
            seen.add(wf["id"])
            workflows.append(wf)
    if errs:
        raise BadRequest("the report's map is not valid: " + "; ".join(errs[:3]))
    errors = [_text(e, "report.errors[]", 600, True) for e in report["errors"]]
    stale = []
    for e in report["stale"]:
        if not isinstance(e, dict):
            raise BadRequest("report.stale entries must be objects")
        stale.append({k: _text(e.get(k), f"report.stale.{k}", 300, k in ("workflow", "why")) for k in ("workflow", "step", "file", "function", "why")})
    uncovered = []
    for e in report["uncovered"]:
        if not isinstance(e, dict):
            raise BadRequest("report.uncovered entries must be objects")
        line = e.get("line", 0)
        if isinstance(line, bool) or not isinstance(line, int) or not 0 <= line < 10_000_000:
            raise BadRequest("report.uncovered.line must be a number")
        uncovered.append({"key": _text(e.get("key"), "report.uncovered.key", 600, True), "line": line,
                          **{k: _text(e.get(k), f"report.uncovered.{k}", 300) for k in ("kind", "file", "function", "name")}})
    discovered = report.get("discovered")
    discovered = discovered if isinstance(discovered, int) and not isinstance(discovered, bool) and 0 <= discovered < 10_000_000 else len(uncovered)
    return {"workflows": workflows, "errors": errors, "stale": stale, "uncovered": uncovered, "discovered": discovered}


def reporter_label(by: str) -> str:
    """'job:12' -> 'job #12'; a token or person as the API named it."""
    by = (by or "").strip()
    if by.startswith("job:") and by[4:].isdigit():
        return f"job #{by[4:]}"
    return by or "unknown"


def record_report(project_id: int, sha: str, report: dict, by: str = "", job_id: int | None = None) -> dict:
    """Store (or replace) the report of one commit, with who reported it. Refused when the shape is wrong, and once the commit is
    LOCKED: it has a production release request, or the gate already judged it (a report cannot be swapped after a refusal)."""
    sha = _sha(sha)
    clean = clean_report(report)
    with get_cursor() as cur:
        cur.execute("SELECT locked_at FROM logic_reports WHERE project_id = %s AND sha = %s FOR UPDATE", (project_id, sha))
        old = cur.fetchone()
        cur.execute("SELECT 1 FROM server_jobs WHERE project_id = %s AND sha = %s AND env = 'prod' LIMIT 1", (project_id, sha))
        if (old and old["locked_at"]) or cur.fetchone():
            raise Conflict(f"commit {sha[:12]} already has a production release request or was judged by the gate: its logic report can "
                           "no longer change (report the next commit)")
        if job_id is not None:
            cur.execute("SELECT count(DISTINCT sha) AS n FROM logic_reports WHERE project_id = %s AND reported_by = %s AND sha <> %s",
                        (project_id, by[:100], sha))
            if cur.fetchone()["n"] >= JOB_REPORT_COMMITS:
                raise Conflict(f"a job reports at most {JOB_REPORT_COMMITS} commits")
        cur.execute("INSERT INTO logic_reports (project_id, sha, workflows, errors, stale, uncovered, discovered, reported_by) "
                    "VALUES (%s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s) "
                    "ON CONFLICT (project_id, sha) DO UPDATE SET workflows = EXCLUDED.workflows, errors = EXCLUDED.errors, "
                    "stale = EXCLUDED.stale, uncovered = EXCLUDED.uncovered, discovered = EXCLUDED.discovered, "
                    "reported_by = EXCLUDED.reported_by, created_at = now() RETURNING *",
                    (project_id, sha, json.dumps(clean["workflows"]), json.dumps(clean["errors"]), json.dumps(clean["stale"]),
                     json.dumps(clean["uncovered"]), clean["discovered"], by[:100]))
        row = dict(cur.fetchone())
        repo.record_activity(cur, project_id, by or "system", "reported", "logic", row["id"],
                             f"Logic map reported for {sha[:12]} by {reporter_label(by)}: {len(clean['workflows'])} workflows, "
                             f"{len(clean['uncovered'])} uncovered, {len(clean['stale'])} stale, {len(clean['errors'])} errors")
    return row


def get_report(project_id: int, sha: str) -> dict | None:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT * FROM logic_reports WHERE project_id = %s AND sha = %s", (project_id, (sha or "").lower()))
        r = cur.fetchone()
    return dict(r) if r else None


def latest_report(project_id: int) -> dict | None:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT * FROM logic_reports WHERE project_id = %s ORDER BY created_at DESC, id DESC LIMIT 1", (project_id,))
        r = cur.fetchone()
    return dict(r) if r else None


def list_reports(project_id: int, limit: int = 20) -> list[dict]:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT id, sha, discovered, created_at, jsonb_array_length(workflows) AS n_workflows, jsonb_array_length(errors) AS n_errors, "
                    "jsonb_array_length(stale) AS n_stale, jsonb_array_length(uncovered) AS n_uncovered FROM logic_reports "
                    "WHERE project_id = %s ORDER BY created_at DESC, id DESC LIMIT %s", (project_id, limit))
        return [dict(r) for r in cur.fetchall()]


def baseline_report(cur, project_id: int, sha: str) -> dict | None:
    """The report to compare with: the report of the last commit that actually reached production (a different one), else None: then the
    commit's own gaps are the starting point. Never "the newest other report" (anyone can post one)."""
    cur.execute("SELECT r.* FROM logic_reports r WHERE r.project_id = %s AND r.sha <> %s AND EXISTS ("
                "SELECT 1 FROM server_jobs s WHERE s.project_id = r.project_id AND s.sha = r.sha AND s.env = 'prod' AND s.state = 'done') "
                "ORDER BY (SELECT max(s.finished_at) FROM server_jobs s WHERE s.project_id = r.project_id AND s.sha = r.sha "
                "AND s.env = 'prod' AND s.state = 'done') DESC NULLS LAST LIMIT 1", (project_id, sha))
    row = cur.fetchone()
    return dict(row) if row else None


# --- the gate ----------------------------------------------------------------------------------------------------------------

UNREADABLE = "the stored logic report is unreadable: report this commit again"


def evaluate(cur, project_id: int, sha: str) -> dict:
    """{ok, reasons: [text], new_uncovered: [entries], report, baseline}: whether this commit passes, with every reason it does not.
    A stored row that cannot be read never raises: the commit just does not pass."""
    try:
        return _evaluate(cur, project_id, sha)
    except (KeyError, TypeError, AttributeError, ValueError):
        return {"ok": False, "reasons": [UNREADABLE], "new_uncovered": [], "report": None, "baseline": None, "unreadable": True}


def _evaluate(cur, project_id: int, sha: str) -> dict:
    cur.execute("SELECT * FROM logic_reports WHERE project_id = %s AND sha = %s", (project_id, sha))
    rep = cur.fetchone()
    if not rep:
        return {"ok": False, "reasons": ["no logic map report for this commit: run `crewboss logic report` on it and post the result"],
                "new_uncovered": [], "report": None, "baseline": None}
    base = baseline_report(cur, project_id, sha)
    reasons: list[str] = []
    if rep["errors"]:
        reasons.append(f"the logic map does not pass its schema check ({len(rep['errors'])} problem{'s' if len(rep['errors']) != 1 else ''}): "
                       + "; ".join(rep["errors"][:3]))
    if rep["stale"]:
        reasons.append(f"{len(rep['stale'])} map entr{'ies point' if len(rep['stale']) != 1 else 'y points'} at code that is gone: "
                       + "; ".join(f"{s['workflow']}/{s['step']} ({s['why']})" for s in rep["stale"][:3]))
    known = {u["key"] for u in base["uncovered"]} if base else {u["key"] for u in rep["uncovered"]}
    new = [u for u in rep["uncovered"] if u["key"] not in known]
    if new:
        reasons.append(f"{len(new)} new entry point{'s' if len(new) != 1 else ''} no workflow covers since the last release: "
                       + "; ".join(f"{u['name']} ({u['file']})" for u in new[:3]))
    return {"ok": not reasons, "reasons": reasons, "new_uncovered": new, "report": dict(rep), "baseline": base}


def open_override(cur, project_id: int, sha: str) -> dict | None:
    cur.execute("SELECT * FROM logic_overrides WHERE project_id = %s AND sha = %s AND used_at IS NULL AND revoked_at IS NULL FOR UPDATE", (project_id, sha))
    r = cur.fetchone()
    return dict(r) if r else None


def _lock_report(project_id: int, sha: str) -> None:
    """The gate has judged this commit: its report is frozen from now on. Written on its own connection so a refusal (which rolls the
    request back) still freezes it."""
    with get_cursor() as c2:
        c2.execute("UPDATE logic_reports SET locked_at = now() WHERE project_id = %s AND sha = %s AND locked_at IS NULL", (project_id, sha))


def gate(cur, project_id: int, sha: str) -> int | None:
    """Called by deploys._create_request for a production release (not a rollback). Raises BadRequest when the gate refuses and no
    override covers this commit. Returns the id of the override this request would use up, else None (gate off, or it passed)."""
    cur.execute("SELECT logic_gate FROM projects WHERE id = %s", (project_id,))
    row = cur.fetchone()
    if not row or row["logic_gate"] != "enforce":
        return None
    result = evaluate(cur, project_id, sha)
    _lock_report(project_id, sha)
    if result["ok"]:
        return None
    ov = open_override(cur, project_id, sha)
    if ov:
        return ov["id"]
    raise BadRequest("the logic map gate refuses this release: " + " | ".join(result["reasons"])
                     + ". Update the map in the same change, or an Admin overrides it for this one release on the Logic page.")


def use_override(cur, override_id: int, server_job_id: int) -> None:
    """Use the override up; raises when it was already used (two requests raced for it), so the second request is refused."""
    cur.execute("UPDATE logic_overrides SET used_server_job_id = %s, used_at = now() WHERE id = %s AND used_at IS NULL AND revoked_at IS NULL "
                "RETURNING id", (server_job_id, override_id))
    if cur.fetchone() is None:
        raise Conflict("the logic gate override for this release was already used")


def grant_override(who, project: dict, sha: str, reason: str) -> dict:
    """An Admin allows ONE release past the gate: recorded with who and why, used up by the first release request of that commit."""
    if not access.is_admin(who, project["id"]):
        raise Forbidden("only a project Admin overrides the logic gate")
    sha = _sha(sha)
    reason = " ".join((reason or "").split())
    if len(reason) < REASON_MIN:
        raise BadRequest(f"say why in at least {REASON_MIN} characters: the reason is kept with the release")
    if len(reason) > 300:
        raise BadRequest("the reason is at most 300 characters")
    try:
        with get_cursor() as cur:
            name = "owner"
            if who.user_id:
                cur.execute("SELECT display_name FROM users WHERE id = %s", (who.user_id,))
                u = cur.fetchone()
                name = u["display_name"] if u else ""
            cur.execute("INSERT INTO logic_overrides (project_id, sha, reason, granted_by, granted_by_name) VALUES (%s, %s, %s, %s, %s) RETURNING *",
                        (project["id"], sha, reason, who.user_id, name or ""))
            row = dict(cur.fetchone())
            repo.record_activity(cur, project["id"], f"user:{who.user_id}" if who.user_id else "owner", "overrode", "logic", row["id"],
                                 f"Overrode the logic map gate for {sha[:12]} (one release): {reason}")
    except UniqueViolation:           # the one-open-override index: a second press, or two Admins at once
        with get_cursor(commit=False) as cur:
            cur.execute("SELECT sha FROM logic_overrides WHERE project_id = %s AND used_at IS NULL AND revoked_at IS NULL", (project["id"],))
            existing = cur.fetchone()
        raise Conflict(f"an override{' for ' + existing['sha'][:12] if existing else ''} is already waiting to be used: "
                       "only one release is overridden at a time") from None
    return row


def revoke_override(who, project: dict, override_id: int) -> dict:
    if not access.is_admin(who, project["id"]):
        raise Forbidden("only a project Admin changes the logic gate")
    with get_cursor() as cur:
        cur.execute("UPDATE logic_overrides SET revoked_at = now() WHERE id = %s AND project_id = %s AND used_at IS NULL AND revoked_at IS NULL "
                    "RETURNING *", (override_id, project["id"]))
        row = cur.fetchone()
        if not row:
            raise NotFound("no such unused override")
    return dict(row)


def list_overrides(project_id: int, limit: int = 15) -> list[dict]:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT * FROM logic_overrides WHERE project_id = %s ORDER BY id DESC LIMIT %s", (project_id, limit))
        return [dict(r) for r in cur.fetchall()]


def set_gate(who, project: dict, mode: str) -> None:
    if not access.is_admin(who, project["id"]):
        raise Forbidden("only a project Admin changes the logic gate")
    if mode not in ("off", "enforce"):
        raise BadRequest("the gate is off or enforce")
    with get_cursor() as cur:
        cur.execute("UPDATE projects SET logic_gate = %s WHERE id = %s", (mode, project["id"]))
        repo.record_activity(cur, project["id"], f"user:{who.user_id}" if who.user_id else "owner", "changed", "logic", None,
                             f"Logic map gate set to {mode}")


# --- release diff ------------------------------------------------------------------------------------------------------------

def release_diff(project_id: int, sha: str) -> dict | None:
    """What this commit changed in the logic, against the last release: {diff, sha, baseline_sha, first, reported_by}. None without a
    readable report."""
    sha = (sha or "").lower()
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT sha, workflows, reported_by FROM logic_reports WHERE project_id = %s AND sha = %s", (project_id, sha))
        rep = cur.fetchone()
        if not rep:
            return None
        cur.execute("SELECT r.sha, r.workflows FROM logic_reports r WHERE r.project_id = %s AND r.sha <> %s AND EXISTS ("
                    "SELECT 1 FROM server_jobs s WHERE s.project_id = r.project_id AND s.sha = r.sha AND s.env = 'prod' AND s.state = 'done') "
                    "ORDER BY (SELECT max(s.finished_at) FROM server_jobs s WHERE s.project_id = r.project_id AND s.sha = r.sha "
                    "AND s.env = 'prod' AND s.state = 'done') DESC NULLS LAST LIMIT 1", (project_id, sha))
        base = cur.fetchone()
    try:
        d = logic_map.diff(base["workflows"] if base else [], rep["workflows"])
    except (KeyError, TypeError, AttributeError):
        return {"unreadable": True, "sha": sha, "reported_by": reporter_label(rep["reported_by"])}
    return {"diff": d, "sha": sha, "baseline_sha": base["sha"] if base else None, "first": base is None,
            "reported_by": reporter_label(rep["reported_by"])}


def release_summaries(project_id: int, shas: list[str]) -> dict:
    """{sha: {summary, reported_by, unreadable}} for a page of release rows, with the baseline read ONCE and only the needed columns."""
    shas = sorted({(x or "").lower() for x in shas})
    out: dict = {}
    if not shas:
        return out
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT sha, workflows, reported_by FROM logic_reports WHERE project_id = %s AND sha = ANY(%s)", (project_id, shas))
        reps = {r["sha"]: r for r in cur.fetchall()}
        cur.execute("SELECT r.sha, r.workflows, max(s.finished_at) AS at FROM logic_reports r JOIN server_jobs s ON s.project_id = r.project_id "
                    "AND s.sha = r.sha AND s.env = 'prod' AND s.state = 'done' WHERE r.project_id = %s GROUP BY r.sha, r.workflows "
                    "ORDER BY at DESC NULLS LAST LIMIT 2", (project_id,))
        bases = cur.fetchall()
    for sha in shas:
        rep = reps.get(sha)
        if rep is None:
            out[sha] = None
            continue
        base = next((b for b in bases if b["sha"] != sha), None)
        label = reporter_label(rep["reported_by"])
        try:
            out[sha] = {"summary": logic_map.diff(base["workflows"] if base else [], rep["workflows"])["summary"], "reported_by": label}
        except (KeyError, TypeError, AttributeError):
            out[sha] = {"summary": None, "reported_by": label, "unreadable": True}
    return out


def step_exists(project_id: int, workflow_id: str, step_id: str) -> bool:
    """Best effort for a fault link: is this workflow step in the newest report?"""
    rep = latest_report(project_id)
    if not rep:
        return False
    return any(w["id"] == workflow_id and any(s["id"] == step_id for s in w["steps"]) for w in rep["workflows"])
