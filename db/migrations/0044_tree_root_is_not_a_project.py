"""The project tree has no project root any more (owner, change #76, task #713 scope addition).

Before: the overseer project (the dashboard's own project, e.g. dash / Crewboss) was the ONE root: every account and every
top-level personal project hung under it, and it was "a partition of one" that sat above every account.
After: accounts are top level (parent_id NULL), top-level personal projects are top level, and the overseer's project is an
ordinary project that sits under the ESTATE account (the account that holds the project playing the `health` role) with its own
session and the overseer role. Its own writes are its own routed projects (app/scope.py); its cross-account READS, requests and
comments are unchanged.

What this does, generically (no project names live here; the roles come from instance.yaml, app/roles.py):
  1. every account gets parent_id NULL;
  2. every non-account child of the overseer project is lifted to the top level (each was its own partition under the old
     root and stays one);
  3. the overseer project itself moves under the estate account, when there is one (else it becomes top level).
Runs only where the overseer role is filled and its project exists; a fresh database is left alone apart from step 1. Idempotent.

THE MARKER: every project this moves gets one activity row, verb MOVED_VERB, whose entity_id is the OLD parent id (NULL when it
had none) and whose summary names the new parent. rollback() restores exactly those parents, once.

ROLLBACK (data only): `python -m app.db rollback 0044_tree_root_is_not_a_project.py` inside the app container.
"""
MOVED_VERB = "tree_root_moved_0044"


def _log(conn, project_id, old_parent, summary):
    conn.execute("INSERT INTO activity (project_id, actor, verb, entity, entity_id, summary) VALUES (%s, 'system', %s, 'project', %s, %s)",
                 (project_id, MOVED_VERB, old_parent, summary))


def _estate_account_id(conn, rows, health_slug):
    """The nearest account at or above the project that plays the health role, or None."""
    by_id = {r["id"]: r for r in rows}
    node = next((r for r in rows if r["slug"] == health_slug), None) if health_slug else None
    seen = set()
    while node is not None and node["id"] not in seen:
        seen.add(node["id"])
        if node["kind"] == "account":
            return node["id"]
        node = by_id.get(node["parent_id"])
    return None


def migrate(conn):
    from app import roles

    rows = conn.execute("SELECT id, slug, kind, parent_id FROM projects").fetchall()
    overseer = next((r for r in rows if r["slug"] == roles.slug("overseer")), None) if roles.slug("overseer") else None
    estate = _estate_account_id(conn, rows, roles.slug("health"))
    # 1. accounts are top level
    for r in rows:
        if r["kind"] == "account" and r["parent_id"] is not None:
            conn.execute("UPDATE projects SET parent_id = NULL WHERE id = %s", (r["id"],))
            if overseer is not None and r["parent_id"] == overseer["id"]:
                _log(conn, r["id"], r["parent_id"], "Account moved to the top of the tree (the dashboard is no longer the root)")
    if overseer is None:
        return
    # 2. the old root's other children go to the top level, as their own partitions
    for r in rows:
        if r["parent_id"] == overseer["id"] and r["kind"] != "account" and r["id"] != estate:
            conn.execute("UPDATE projects SET parent_id = NULL WHERE id = %s", (r["id"],))
            _log(conn, r["id"], overseer["id"], "Moved to the top of the tree (the dashboard is no longer the root)")
    # 3. the overseer's own project becomes an ordinary project under the estate account
    if overseer["parent_id"] != estate:
        conn.execute("UPDATE projects SET parent_id = %s WHERE id = %s", (estate, overseer["id"]))
        _log(conn, overseer["id"], overseer["parent_id"],
             "The dashboard project is now an ordinary project" + (" under the estate account" if estate else " at the top level"))


def rollback(conn):
    """Put every project this migration moved back under its old parent (the marker's entity_id), once."""
    marks = conn.execute("SELECT id, project_id, entity_id FROM activity WHERE verb = %s ORDER BY id DESC", (MOVED_VERB,)).fetchall()
    for m in marks:
        conn.execute("UPDATE projects SET parent_id = %s WHERE id = %s", (m["entity_id"], m["project_id"]))
    conn.execute("UPDATE activity SET verb = 'tree_root_moved_0044_undone' WHERE verb = %s", (MOVED_VERB,))
