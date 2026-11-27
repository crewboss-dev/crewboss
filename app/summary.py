"""Read-only summary for an external digest (#828): GET /api/summary with a 'digest' role token.

A morning digest (or any other consumer) wants three short lists and nothing else: open CODE FAULTS (health problems the ERP
tagged kind=code_fault), RELEASES WAITING for an approval, and the tasks in REVIEW assigned to the owner. Each is a count plus at
most ITEM_CAP items. Items carry titles and a URL path, never a task body, a comment, a commit message or any secret: a digest
that is pasted into mail or chat must not be able to leak more than a title. Instance-scoped, like the other role tokens, and it serves
the INSTANCE OWNER's brief: the review queue and the waiting releases cover only the owner's own, unarchived projects
(owner_user_id NULL or an owner user, as app/access.py defines "the owner's own project"), never a designer's private project
or another person's queue. Code faults are the health project's. Nothing here writes."""
from __future__ import annotations

from datetime import datetime, timezone

from app import access, erp_health, roles
from app.db import get_cursor

ITEM_CAP = 10
TITLE_MAX = 200


def _iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def _title(text) -> str:
    return " ".join(str(text or "").split())[:TITLE_MAX]


def code_faults() -> dict:
    """Open, tracked ERP events tagged code_fault, newest first."""
    slug = roles.slug("health")
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT count(*) AS n FROM health_problems WHERE source = %s AND kind = 'code_fault' "
                    "AND resolved_at IS NULL", (erp_health.SOURCE,))
        n = cur.fetchone()["n"]
        cur.execute("SELECT name, started_at FROM health_problems WHERE source = %s AND kind = 'code_fault' "
                    "AND resolved_at IS NULL ORDER BY started_at DESC, eventid DESC LIMIT %s",
                    (erp_health.SOURCE, ITEM_CAP))
        rows = cur.fetchall()
    return {"count": n, "items": [{"title": _title(r["name"]), "project": slug, "since": _iso(r["started_at"]),
                                   "path": f"/{slug}/health" if slug else "/"} for r in rows]}


def _owner_scope() -> tuple[str, list]:
    """SQL (over projects aliased p) + params limiting to active projects that belong to the instance owner."""
    return ("p.archived_at IS NULL AND (p.owner_user_id IS NULL OR p.owner_user_id = ANY(%s))",
            [sorted(access.owner_user_ids())])


def releases_waiting() -> dict:
    """Deploy requests waiting for an approval (server_jobs state 'requested'), oldest first."""
    scope, params = _owner_scope()
    frm = "FROM server_jobs s JOIN projects p ON p.id = s.project_id JOIN projects a ON a.id = s.account_id "
    where = f"WHERE s.state = 'requested' AND {scope} "
    with get_cursor(commit=False) as cur:
        cur.execute(f"SELECT count(*) AS n {frm}{where}", params)
        n = cur.fetchone()["n"]
        cur.execute(f"SELECT s.target_name, s.env, s.sha, s.requested_at, p.slug AS project, a.slug AS account "
                    f"{frm}{where}ORDER BY s.requested_at, s.id LIMIT %s", params + [ITEM_CAP])
        rows = cur.fetchall()
    return {"count": n, "items": [{"title": _title(f"{r['project']} to {r['target_name']} ({r['env']}) {r['sha'][:7]}"),
                                   "project": r["project"], "since": _iso(r["requested_at"]),
                                   "path": f"/{r['account']}/approvals"} for r in rows]}


def owner_reviews() -> dict:
    """Tasks in review that are the owner's to test, oldest first (they have waited longest)."""
    scope, params = _owner_scope()
    where = f"FROM tasks t JOIN projects p ON p.id = t.project_id WHERE t.status = 'review' AND t.assignee = 'owner' AND {scope}"
    with get_cursor(commit=False) as cur:
        cur.execute(f"SELECT count(*) AS n {where}", params)
        n = cur.fetchone()["n"]
        cur.execute(f"SELECT t.id, t.title, p.slug AS project {where} ORDER BY t.updated_at, t.id LIMIT %s",
                    params + [ITEM_CAP])
        rows = cur.fetchall()
    return {"count": n, "items": [{"id": r["id"], "title": _title(r["title"]), "project": r["project"],
                                   "path": f"/{r['project']}/tasks/{r['id']}"} for r in rows]}


def build(now: datetime | None = None) -> dict:
    return {"generated_at": (now or datetime.now(timezone.utc)).isoformat(),
            "code_faults": code_faults(), "releases_waiting": releases_waiting(), "owner_reviews": owner_reviews()}
