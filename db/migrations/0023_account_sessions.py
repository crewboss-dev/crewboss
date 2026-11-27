"""Standing sessions attach to ACCOUNTS, not projects (owner, 2026-10-12; dash task #253).

Before: the Mallen account (kind 'account', short 'mallen') ran no session; mallen-erp ran `erp`, which worked the
ERP's branch, and dash worked the rest of the account (Website, Appliance, LinkedIn) and the account itself.
After: the account runs ONE session, named by its short name (`mallen`), that works its whole subtree (the ERP and
its children, Website, Appliance, LinkedIn). mallen-erp's session config moves to it, so `erp` leaves the plan.
dash stays the overseer: the global plan, its own project, READ access everywhere, no Mallen project's events.

Nothing else changes: routing, token reach and briefings already follow "the nearest ancestor that runs a session"
(app/tree.py), so moving the flag moves the work. Tokens are NOT touched: the running `erp` session's token stays
bound to mallen-erp and keeps its reach over the ERP branch while it finishes, but it no longer receives events
(repo._outbox_scope: a session whose project left the plan only gets its own stop notice). The `mallen` session
gets its own token, $TOKEN_DIR/mallen/all (the launcher mints it, or the parent creates it first; see the README).

Runs only where the account exists (production); a fresh database (the test suite) is left alone. Idempotent:
an account that already runs a session is left as it is.

THE MARKER: when migrate() moves anything it writes ONE activity row on the account, verb MOVED_VERB, whose
entity_id is mallen-erp's id when it cleared mallen-erp's session (NULL when mallen-erp had none to clear, and the
account just got the default ['all']). rollback() reverses only a move that marker records, once (it writes a
BACK_VERB row) — never a session the owner gave the account, or one this migration found already in place.

ROLLBACK (data only, no schema change): `python -m app.db rollback 0023_account_sessions.py` inside the app
container runs rollback(conn) below. By hand, only when the marker exists —

    SELECT id, entity_id, at FROM activity WHERE verb = 'session_moved_0023'
     AND project_id = (SELECT id FROM projects WHERE slug = 'mallen');
    -- then, if entity_id is set (mallen-erp's session was moved):
    UPDATE projects SET session_roles = (SELECT session_roles FROM projects WHERE slug = 'mallen')
     WHERE slug = 'mallen-erp';
    UPDATE projects SET session_roles = '{}' WHERE slug = 'mallen';

The schema_migrations row can stay (this file then won't re-run); delete it only to apply the move again. After a
rollback the sync timer starts `erp` again with its old token file ($TOKEN_DIR/erp/all, never deleted by this
change) and winds `mallen` down.
"""

ACCOUNT = "mallen"
FROM_PROJECT = "mallen-erp"
NOTE = "(owner, 2026-10-12, task #253)"
MOVED_VERB = "session_moved_0023"
BACK_VERB = "session_moved_back_0023"


def _row(conn, slug):
    return conn.execute("SELECT id, name, kind, session_roles, archived_at FROM projects WHERE slug = %s",
                        (slug,)).fetchone()


def _activity(conn, project_id, summary, verb="updated", entity_id=None):
    conn.execute("INSERT INTO activity (project_id, actor, verb, entity, entity_id, summary) "
                 "VALUES (%s, 'system', %s, 'project', %s, %s)", (project_id, verb, entity_id, summary))


def _marker(conn, account_id):
    """The latest MOVED_VERB row on the account that no BACK_VERB row has reversed since, else None."""
    row = conn.execute("SELECT id, entity_id FROM activity WHERE project_id = %s AND verb = %s "
                       "ORDER BY id DESC LIMIT 1", (account_id, MOVED_VERB)).fetchone()
    if not row:
        return None
    back = conn.execute("SELECT 1 FROM activity WHERE project_id = %s AND verb = %s AND id > %s",
                        (account_id, BACK_VERB, row["id"])).fetchone()
    return None if back else row


def migrate(conn) -> None:
    account = _row(conn, ACCOUNT)
    if not account or account["kind"] != "account" or account["archived_at"] is not None:
        return  # a fresh database (tests), or no live Mallen account: nothing to move
    if account["session_roles"]:
        return  # already runs a session: nothing moved, no marker
    source = _row(conn, FROM_PROJECT)
    moved = bool(source and source["session_roles"])
    roles = list(source["session_roles"]) if moved else ["all"]
    conn.execute("UPDATE projects SET session_roles = %s WHERE id = %s", (roles, account["id"]))
    if moved:
        conn.execute("UPDATE projects SET session_roles = '{}' WHERE id = %s", (source["id"],))
        _activity(conn, source["id"], f"Standing session moved to the account '{account['name']}' {NOTE}",
                  entity_id=source["id"])
    _activity(conn, account["id"], f"Standing session moved here from {FROM_PROJECT}: the account's session "
                                   f"works every project under it {NOTE}" if moved else
              f"Standing session turned on: the account's session works every project under it {NOTE}",
              verb=MOVED_VERB, entity_id=source["id"] if moved else None)


def rollback(conn) -> None:
    """Undo migrate() — only the move its marker records, once. Data only; the caller commits."""
    account = _row(conn, ACCOUNT)
    if not account:
        return
    marker = _marker(conn, account["id"])
    if marker is None:
        return  # this migration moved nothing (or it was already rolled back): leave every session alone
    source = conn.execute("SELECT id, session_roles FROM projects WHERE id = %s",
                          (marker["entity_id"],)).fetchone() if marker["entity_id"] else None
    if source is not None and source["session_roles"]:
        return  # mallen-erp has a session again (set by the owner since): not ours to overwrite
    if source is not None:
        conn.execute("UPDATE projects SET session_roles = %s WHERE id = %s",
                     (list(account["session_roles"] or ["all"]), source["id"]))
        _activity(conn, source["id"], f"Standing session moved back from the account '{account['name']}' "
                                      f"(rollback of task #253)", entity_id=source["id"])
    conn.execute("UPDATE projects SET session_roles = '{}' WHERE id = %s", (account["id"],))
    _activity(conn, account["id"], f"Standing session moved back to {FROM_PROJECT} (rollback of task #253)"
              if source is not None else "Standing session turned off again (rollback of task #253)",
              verb=BACK_VERB, entity_id=marker["entity_id"])
