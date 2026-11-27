"""Favourite projects (#778; db/migrations/0074_project_favourites.sql): a person's own starred projects, shown at the top of the left menu.

Every query is by the principal's user_id (None = the sign-in-off owner, matched with IS NOT DISTINCT FROM). Only a project the person may SEE can be starred, and
a project that stops being visible to them (or is archived) is simply not returned, so their list never names a hidden record."""
from __future__ import annotations

from app import access, tree
from app.db import get_cursor
from app.repo import NotFound


def _ids(who: "access.Principal") -> list[int]:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT project_id FROM project_favourites WHERE user_id IS NOT DISTINCT FROM %s ORDER BY position, id", (who.user_id,))
        return [r["project_id"] for r in cur.fetchall()]


def visible_ids(who: "access.Principal", idx: dict | None = None) -> set[int]:
    """The starred project ids this person may still see (an unseen one is dropped here, whatever the table holds)."""
    idx = idx if idx is not None else access.filtered_index(who)
    return {i for i in _ids(who) if i in idx}


def rows(who: "access.Principal", idx: dict | None = None, want: set[int] | None = None) -> list[dict]:
    """The starred LIVE projects, alphabetical. `want` = visible_ids already computed by the caller (once per request). TODO #778: drag to reorder, by `position`."""
    idx = idx if idx is not None else access.filtered_index(who)
    want = want if want is not None else visible_ids(who, idx)
    out = [r for r in tree.project_tree(include_archived=False, idx=idx) if r["id"] in want and not tree.is_account(r) and not tree.is_global_root(r)]
    return sorted(out, key=lambda r: r["name"].lower())


def toggle(who: "access.Principal", project: dict | None, slug: str = "") -> bool:
    """Star or unstar `project` for this person; returns True when it is starred now. A project they cannot see is the same NotFound a missing one gets,
    worded with the slug that was ASKED for (never the project's current slug: a renamed hidden project must not leak its new name)."""
    access.require_view(who, project, f"project {slug or (project or {}).get('slug')!r} not found")
    with get_cursor() as cur:
        cur.execute("DELETE FROM project_favourites WHERE user_id IS NOT DISTINCT FROM %s AND project_id = %s", (who.user_id, project["id"]))
        if cur.rowcount:
            return False
        cur.execute("SELECT coalesce(max(position), 0) + 1 AS n FROM project_favourites WHERE user_id IS NOT DISTINCT FROM %s", (who.user_id,))
        pos = cur.fetchone()["n"]
        cur.execute("INSERT INTO project_favourites (user_id, project_id, position) VALUES (%s, %s, %s)", (who.user_id, project["id"], pos))
        return True
