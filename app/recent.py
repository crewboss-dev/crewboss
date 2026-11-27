"""The Recent page's feed (#771): activity across every project the person may see, newest first, filterable, paged by a cursor.

One read over the `activity` table (what every write already appends to), scoped by app/access.py visible_ids: a person never gets a
line from a project they cannot see, and filtering by a project they cannot see returns nothing. The kinds are groups of
(entity, verb) pairs the table really holds; jobs and deploys are not in the activity log, so they are not a kind here."""
from __future__ import annotations

from app import access, repo
from app.db import get_cursor

PAGE = 30

# kind key -> (label, SQL condition on activity `a`)
KINDS = {
    "comments": ("Comments and questions", "a.entity = 'task' AND a.verb IN ('commented', 'asked', 'answered')"),
    "created": ("New tasks", "a.entity = 'task' AND a.verb IN ('created', 'requested')"),
    "status": ("Changes to tasks", "a.entity = 'task' AND a.verb IN ('updated', 'moved', 'assigned', 'accepted', 'overrode')"),
    "knowledge": ("Knowledge", "a.entity = 'knowledge'"),
    "projects": ("Projects", "a.entity = 'project'"),
}


def link(row: dict) -> str:
    """Where an entry goes: the task's drawer on its board, the knowledge page, else the project."""
    base = f"/{row['project_slug']}"
    if row["entity"] == "task" and row.get("entity_id"):
        return f"{base}/board?open={row['entity_id']}"
    if row["entity"] == "knowledge":
        return f"{base}/knowledge"
    return base


def feed(who: "access.Principal", project_slug: str = "", kind: str = "", before: int | None = None, limit: int = PAGE) -> dict:
    """{"entries": [...], "next": id | None}. `before` is the last id of the previous page (a cursor, so a new line arriving
    never shifts the next page). Entries carry project_slug, project_name and `href`."""
    visible = access.visible_list(who)
    sql = ("SELECT a.id, a.at, a.actor, a.verb, a.entity, a.entity_id, a.summary, p.slug AS project_slug, p.name AS project_name "
           "FROM activity a JOIN projects p ON p.id = a.project_id WHERE true")
    params: list = []
    if visible is not None:
        sql += " AND a.project_id = ANY(%s)"
        params.append(visible)
    if project_slug:
        sql += " AND p.slug = %s"
        params.append(project_slug)
    if kind in KINDS:
        sql += " AND (" + KINDS[kind][1] + ")"
    if before:
        sql += " AND a.id < %s"
        params.append(before)
    sql += " ORDER BY a.id DESC LIMIT %s"
    params.append(limit + 1)
    with get_cursor(commit=False) as cur:
        cur.execute(sql, params)
        rows = [dict(r) for r in cur.fetchall()]
    more = len(rows) > limit
    rows = rows[:limit]
    for r in rows:
        r["href"] = link(r)
    return {"entries": rows, "next": rows[-1]["id"] if (more and rows) else None}
