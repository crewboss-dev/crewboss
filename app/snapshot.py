"""The Dashboard page's numbers (#733): one read, every figure scoped to the projects the signed-in person may see.

`visible` is app/access.py visible_ids(): None = every project (the owner), a set = exactly those. Every query below takes it;
nothing here reads a project the person cannot. Things with no project (the open production problems, from the health feed)
are the owner's: for anyone else the tile is absent (`problems is None`), not zero.

Weeks start on Monday and months on the 1st, in the instance's local time (repo.LOCAL_TZ), as the plan's day does.
"""
from __future__ import annotations

from app import access, config, repo
from app import changes as changes_mod
from app.db import get_cursor

STUCK_DAYS = 7      # an open task nobody has touched for this long is stuck
OLDEST_N = 5


def _scope(visible, col: str = "t.project_id") -> tuple[str, list]:
    if visible is None:
        return "", []
    return f" AND {col} = ANY(%s)", [list(visible)]


def snapshot(who: "access.Principal") -> dict:
    visible = access.visible_ids(who)
    sql_scope, p = _scope(visible)
    tz = repo.LOCAL_TZ
    out: dict = {}
    with get_cursor(commit=False) as cur:
        # opened vs closed, this week and this month (local time)
        cur.execute(
            "SELECT "
            "count(*) FILTER (WHERE t.created_at >= date_trunc('week', now() AT TIME ZONE %s) AT TIME ZONE %s) AS opened_week, "
            "count(*) FILTER (WHERE t.created_at >= date_trunc('month', now() AT TIME ZONE %s) AT TIME ZONE %s) AS opened_month, "
            "count(*) FILTER (WHERE t.done_at >= date_trunc('week', now() AT TIME ZONE %s) AT TIME ZONE %s) AS closed_week, "
            "count(*) FILTER (WHERE t.done_at >= date_trunc('month', now() AT TIME ZONE %s) AT TIME ZONE %s) AS closed_month "
            "FROM tasks t JOIN projects pr ON pr.id = t.project_id AND pr.archived_at IS NULL WHERE true" + sql_scope,
            [tz] * 8 + p)
        out["flow"] = dict(cur.fetchone())
        # open tasks by project and by priority (Won't have is not open work, as everywhere else)
        cur.execute(
            "SELECT pr.id, pr.slug, pr.name, count(*) AS open, count(*) FILTER (WHERE t.priority = 'must') AS must "
            "FROM tasks t JOIN projects pr ON pr.id = t.project_id AND pr.archived_at IS NULL "
            "WHERE t.status <> 'done' AND t.priority <> 'wont'" + sql_scope + " GROUP BY pr.id ORDER BY open DESC, pr.name LIMIT 8", p)
        out["by_project"] = [dict(r) for r in cur.fetchall()]
        cur.execute(
            "SELECT t.priority, count(*) AS n FROM tasks t JOIN projects pr ON pr.id = t.project_id AND pr.archived_at IS NULL "
            "WHERE t.status <> 'done' AND t.priority <> 'wont'" + sql_scope + " GROUP BY t.priority", p)
        by_prio = {r["priority"]: r["n"] for r in cur.fetchall()}
        out["by_priority"] = [{"key": k, "label": v["short"], "n": by_prio.get(k, 0)}
                              for k, v in config.PRIORITY_META.items() if k != "wont"]
        # the oldest open tasks, and the stuck ones (open, not touched for STUCK_DAYS)
        base = ("SELECT t.id, t.title, t.status, t.priority, t.created_at, t.updated_at, pr.slug AS project_slug, pr.name AS project_name "
                "FROM tasks t JOIN projects pr ON pr.id = t.project_id AND pr.archived_at IS NULL "
                "WHERE t.status <> 'done' AND t.priority <> 'wont'" + sql_scope)
        cur.execute(base + " ORDER BY t.created_at LIMIT %s", p + [OLDEST_N])
        out["oldest"] = [dict(r) for r in cur.fetchall()]
        cur.execute(base + " AND t.status IN ('doing', 'review') AND t.updated_at < now() - make_interval(days => %s) "
                    "ORDER BY t.updated_at LIMIT %s", p + [STUCK_DAYS, OLDEST_N])
        out["stuck"] = [dict(r) for r in cur.fetchall()]
        # reviews waiting on the owner: tasks in review the owner has to check
        cur.execute(base + " AND t.status = 'review' ORDER BY t.updated_at LIMIT 8", p)
        out["reviews"] = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT count(*) AS n FROM tasks t JOIN projects pr ON pr.id = t.project_id AND pr.archived_at IS NULL "
                    "WHERE t.status = 'review'" + sql_scope, p)
        out["review_count"] = cur.fetchone()["n"]
        # agents (jobs): running now, finished today
        job_scope, jp = ("", []) if visible is None else (
            " AND (t.project_id = ANY(%s) OR (j.task_id IS NULL AND j.account_id = ANY(%s)))", [list(visible), list(visible)])
        cur.execute(
            "SELECT count(*) FILTER (WHERE j.state = 'running') AS running, "
            "count(*) FILTER (WHERE j.finished_at >= date_trunc('day', now() AT TIME ZONE %s) AT TIME ZONE %s) AS finished_today "
            "FROM jobs j LEFT JOIN tasks t ON t.id = j.task_id WHERE true" + job_scope, [tz, tz] + jp)
        out["agents"] = dict(cur.fetchone())
        # open production problems: no project, so the owner's alone
        if visible is None:
            cur.execute("SELECT count(*) AS n FROM health_problems WHERE resolved_at IS NULL AND NOT baseline")
            out["problems"] = cur.fetchone()["n"]
        else:
            out["problems"] = None
    ids = None if visible is None else list(visible)
    out["changes_waiting"] = [] if ids == [] else changes_mod.waiting(project_ids=ids)
    return out


WEEKS = 8


def project_summary(project_ids: list[int]) -> dict:
    """One project's dashboard (#772): the same kinds of figure as the Dashboard (snapshot above), scoped to `project_ids` = the project and
    the live projects under it, already narrowed by the caller to what the person may see (routers/pages.py _descendants). Nothing here
    reads a project outside that list."""
    ids = list(project_ids)
    tz = repo.LOCAL_TZ
    out: dict = {"weeks": [], "by_status": [], "open": 0, "reviews": [], "questions": [], "changes_waiting": [], "jobs": [],
                 "jobs_live": 0, "activity": []}
    if not ids:
        out["by_status"] = [{"key": k, "label": config.STATUS_META[k]["label"], "n": 0} for k in ("backlog", "ready", "doing", "review")]
        return out
    with get_cursor(commit=False) as cur:
        # opened and closed per local week, the last WEEKS weeks (oldest first), so the bars read left to right
        cur.execute(
            "WITH w AS (SELECT generate_series(date_trunc('week', now() AT TIME ZONE %s) - make_interval(weeks => %s), "
            "date_trunc('week', now() AT TIME ZONE %s), interval '1 week') AS start) "
            "SELECT w.start::date AS week, "
            "(SELECT count(*) FROM tasks t WHERE t.project_id = ANY(%s) AND (t.created_at AT TIME ZONE %s) >= w.start "
            " AND (t.created_at AT TIME ZONE %s) < w.start + interval '1 week') AS opened, "
            "(SELECT count(*) FROM tasks t WHERE t.project_id = ANY(%s) AND (t.done_at AT TIME ZONE %s) >= w.start "
            " AND (t.done_at AT TIME ZONE %s) < w.start + interval '1 week') AS closed "
            "FROM w ORDER BY w.start", [tz, WEEKS - 1, tz, ids, tz, tz, ids, tz, tz])
        out["weeks"] = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT t.status, count(*) AS n FROM tasks t WHERE t.project_id = ANY(%s) AND t.status <> 'done' AND t.priority <> 'wont' "
                    "GROUP BY t.status", [ids])
        by = {r["status"]: r["n"] for r in cur.fetchall()}
        out["by_status"] = [{"key": k, "label": config.STATUS_META[k]["label"], "n": by.get(k, 0)} for k in ("backlog", "ready", "doing", "review")]
        out["open"] = sum(x["n"] for x in out["by_status"])
        cur.execute("SELECT t.id, t.title, t.updated_at, pr.slug AS project_slug, pr.name AS project_name FROM tasks t "
                    "JOIN projects pr ON pr.id = t.project_id WHERE t.project_id = ANY(%s) AND t.status = 'review' "
                    "ORDER BY t.updated_at LIMIT 8", [ids])
        out["reviews"] = [dict(r) for r in cur.fetchall()]
        # agent jobs: what is queued or running now, then the latest finished
        cur.execute("SELECT j.id, j.state, j.summary, j.created_at, j.finished_at, t.id AS task_id, t.title, pr.slug AS project_slug "
                    "FROM jobs j JOIN tasks t ON t.id = j.task_id JOIN projects pr ON pr.id = t.project_id WHERE t.project_id = ANY(%s) "
                    "ORDER BY (j.state IN ('queued', 'running')) DESC, COALESCE(j.finished_at, j.claimed_at, j.created_at) DESC LIMIT 6", [ids])
        out["jobs"] = [dict(r) for r in cur.fetchall()]
        out["jobs_live"] = sum(1 for j in out["jobs"] if j["state"] in ("queued", "running"))
    out["questions"] = repo.open_questions(project_ids=ids)[:8]
    out["changes_waiting"] = changes_mod.waiting(project_ids=ids)[:8]
    out["activity"] = repo.recent_activity(project_ids=ids, limit=6)
    return out
