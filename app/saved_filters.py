"""Saved filters (#772): per person, per page type, per project (List and Board) or global (Recent). Stored server-side so they follow the
person across devices (db/migrations/0072_saved_filters.sql). Every query is by the principal's user_id, so another person's filters are
never read, renamed, defaulted or deleted: their ids answer NotFound, as a missing one does."""
from __future__ import annotations

import json

import psycopg

from app import access
from app.db import get_cursor
from app.repo import BadRequest, NotFound

PAGES = ("list", "board", "recent", "machines")
GLOBAL_PAGES = ("recent", "machines")      # not tied to a project
# The query parameters a saved set may hold, per page type: the pages' own filter keys (routers/pages.py _task_filters, app/recent.py),
# the List's grouping / sort / done toggle and its visible columns. Anything else is dropped on the way in.
TASK_KEYS = ("assignee", "priority", "stream", "category", "subproject", "status", "q")
KEYS = {
    "board": TASK_KEYS,
    "list": TASK_KEYS + ("group", "sort", "dir", "done", "cols"),
    "recent": ("project", "kind"),
    "machines": ("role",),
}
NAME_MAX = 40


def clean(page: str, data) -> dict:
    if page not in PAGES:
        raise BadRequest(f"page must be one of {PAGES}")
    if isinstance(data, str):
        try:
            data = json.loads(data or "{}")
        except ValueError:
            raise BadRequest("the filter set is not valid JSON")
    if not isinstance(data, dict):
        raise BadRequest("the filter set must be an object")
    return {k: str(v)[:200] for k, v in data.items() if k in KEYS[page] and v not in (None, "")}


def _name(name) -> str:
    name = " ".join(str(name or "").split())[:NAME_MAX]
    if not name:
        raise BadRequest("give the filter a name")
    return name


def _row(r) -> dict:
    return {"id": r["id"], "name": r["name"], "data": r["data"], "is_default": r["is_default"], "page": r["page"],
            "project_id": r["project_id"]}


def list_for(who: "access.Principal", page: str, project_id: int | None) -> list[dict]:
    if page not in PAGES:
        raise BadRequest(f"page must be one of {PAGES}")
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT * FROM saved_filters WHERE user_id IS NOT DISTINCT FROM %s AND page = %s "
                    "AND project_id IS NOT DISTINCT FROM %s ORDER BY is_default DESC, lower(name)", (who.user_id, page, project_id))
        return [_row(r) for r in cur.fetchall()]


def default_for(who: "access.Principal", page: str, project_id: int | None) -> dict | None:
    return next((f for f in list_for(who, page, project_id) if f["is_default"]), None)


def save(who: "access.Principal", page: str, project_id: int | None, name: str, data, make_default: bool = False) -> dict:
    """Save (or, with the same name, replace) a filter set. A project-scoped page needs a project the caller has already checked."""
    data = clean(page, data)
    name = _name(name)
    if page not in GLOBAL_PAGES and project_id is None:
        raise BadRequest("List and Board filters belong to a project")
    pid = project_id if page not in GLOBAL_PAGES else None
    with get_cursor() as cur:
        cur.execute(
            "INSERT INTO saved_filters (user_id, page, project_id, name, data) VALUES (%s, %s, %s, %s, %s::jsonb) "
            "ON CONFLICT (COALESCE(user_id, 0), page, COALESCE(project_id, 0), lower(name)) "
            "DO UPDATE SET data = EXCLUDED.data, name = EXCLUDED.name, updated_at = now() RETURNING *",
            (who.user_id, page, pid, name, json.dumps(data)))
        row = cur.fetchone()
    if make_default:
        return set_default(who, row["id"], True)
    return _row(row)


def _mine(cur, who, filter_id: int) -> dict:
    cur.execute("SELECT * FROM saved_filters WHERE id = %s AND user_id IS NOT DISTINCT FROM %s", (filter_id, who.user_id))
    row = cur.fetchone()
    if row is None:
        raise NotFound("no such saved filter")
    return row


def rename(who: "access.Principal", filter_id: int, name: str) -> dict:
    name = _name(name)
    with get_cursor() as cur:
        _mine(cur, who, filter_id)
        try:
            cur.execute("UPDATE saved_filters SET name = %s, updated_at = now() WHERE id = %s RETURNING *", (name, filter_id))
        except psycopg.errors.UniqueViolation:
            raise BadRequest(f"you already have a saved filter called \u201c{name}\u201d here: pick another name") from None
        return _row(cur.fetchone())


def set_default(who: "access.Principal", filter_id: int, on: bool) -> dict:
    """One default per (person, page, project): marking one clears the others."""
    with get_cursor() as cur:
        row = _mine(cur, who, filter_id)
        if on:
            cur.execute("UPDATE saved_filters SET is_default = false WHERE user_id IS NOT DISTINCT FROM %s AND page = %s "
                        "AND project_id IS NOT DISTINCT FROM %s", (who.user_id, row["page"], row["project_id"]))
        cur.execute("UPDATE saved_filters SET is_default = %s, updated_at = now() WHERE id = %s RETURNING *", (bool(on), filter_id))
        return _row(cur.fetchone())


def delete(who: "access.Principal", filter_id: int) -> None:
    with get_cursor() as cur:
        _mine(cur, who, filter_id)
        cur.execute("DELETE FROM saved_filters WHERE id = %s", (filter_id,))
