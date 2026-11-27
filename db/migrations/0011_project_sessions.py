"""Per-project Claude sessions (owner 2026-09-30).

- projects.short_name: unique, 2-12 chars [a-z0-9-], not a reserved word. It names the project's sessions
  (tmux + `claude --remote-control`): <short>-<role>, e.g. erp-bugs, erp-review; a one-session project is just
  <short> ("dash"). Backfill: mallen-erp -> erp, mallen-dashboard -> dash, any other project -> derived from
  its slug (made unique).
- projects.session_roles (text[]): which standing sessions the project runs. 'all' = ONE session for every
  stream. mallen-erp = {features,reviewer,bugs,tweaks,errors}; mallen-dashboard = {all}; any OTHER existing
  project = {} (no sessions until the owner turns them on); new projects default to {all} (app code).
- session_tokens.project_id: a session token is bound to (project, stream|all). Existing session tokens (the
  live flat token files) are bound to mallen-erp, so the running sessions keep working. The 'scanner' role
  stays project-less (catalog import only).
- sessions (presence) is keyed by (project_id, stream); existing rows belong to mallen-erp.
"""
import re

SHORT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,10}[a-z0-9]$")
KNOWN = {"mallen-erp": "erp", "mallen-dashboard": "dash"}
ROLES_ERP = ["features", "reviewer", "bugs", "tweaks", "errors"]
ALL_ROLES = ("all", "features", "reviewer", "bugs", "tweaks", "errors")


def _reserved() -> set:
    try:
        from app.repo import RESERVED_SLUGS
        return set(RESERVED_SLUGS)
    except Exception:  # noqa: BLE001 — the migration must not depend on app code being importable
        return {"api", "mcp", "static", "healthz", "media", "projects", "tasks", "all"}


def _derive(slug: str, taken: set, reserved: set) -> str:
    base = re.sub(r"^mallen-", "", slug)[:12].strip("-") or "p"
    if len(base) < 2:
        base = (base + "0")[:2]
    candidate, n = base, 2
    while candidate in taken or candidate in reserved or not SHORT_RE.match(candidate):
        suffix = str(n)
        candidate = (base[: 12 - len(suffix)].rstrip("-") + suffix)
        n += 1
    return candidate


def migrate(conn) -> None:
    conn.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS short_name TEXT")
    conn.execute("ALTER TABLE projects ADD COLUMN IF NOT EXISTS session_roles TEXT[] NOT NULL DEFAULT '{}'")
    reserved = _reserved()
    rows = conn.execute("SELECT id, slug, short_name FROM projects ORDER BY id").fetchall()
    taken = {r["short_name"] for r in rows if r["short_name"]}
    for r in rows:
        if r["short_name"]:
            continue
        want = KNOWN.get(r["slug"])
        short = want if want and want not in taken else _derive(r["slug"], taken, reserved)
        taken.add(short)
        conn.execute("UPDATE projects SET short_name = %s WHERE id = %s", (short, r["id"]))
    conn.execute("ALTER TABLE projects ALTER COLUMN short_name SET NOT NULL")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS projects_short_name_key ON projects (short_name)")
    conn.execute("ALTER TABLE projects DROP CONSTRAINT IF EXISTS projects_short_name_check")
    conn.execute("ALTER TABLE projects ADD CONSTRAINT projects_short_name_check "
                 "CHECK (short_name ~ '^[a-z0-9][a-z0-9-]{0,10}[a-z0-9]$')")
    conn.execute("UPDATE projects SET session_roles = %s WHERE slug = 'mallen-erp'", (ROLES_ERP,))
    conn.execute("UPDATE projects SET session_roles = '{all}' WHERE slug = 'mallen-dashboard'")
    conn.execute("ALTER TABLE projects DROP CONSTRAINT IF EXISTS projects_session_roles_check")
    conn.execute("ALTER TABLE projects ADD CONSTRAINT projects_session_roles_check "
                 "CHECK (session_roles <@ ARRAY['all','features','reviewer','bugs','tweaks','errors']::text[])")

    erp = conn.execute("SELECT id FROM projects WHERE slug = 'mallen-erp'").fetchone()
    erp_id = erp["id"] if erp else None

    # tokens: bound to (project, stream|all)
    conn.execute("ALTER TABLE session_tokens ADD COLUMN IF NOT EXISTS project_id INTEGER "
                 "REFERENCES projects(id) ON DELETE CASCADE")
    conn.execute("ALTER TABLE session_tokens DROP CONSTRAINT IF EXISTS session_tokens_stream_check")
    conn.execute("ALTER TABLE session_tokens ADD CONSTRAINT session_tokens_stream_check "
                 "CHECK (stream IN ('features', 'bugs', 'tweaks', 'reviewer', 'errors', 'all', 'scanner'))")
    if erp_id is not None:
        conn.execute("UPDATE session_tokens SET project_id = %s WHERE project_id IS NULL AND stream <> 'scanner'",
                     (erp_id,))

    # an 'all' session grounds too (its checkpoints are per (all, project))
    conn.execute("ALTER TABLE ground_checkpoints DROP CONSTRAINT IF EXISTS ground_checkpoints_stream_check")
    conn.execute("ALTER TABLE ground_checkpoints ADD CONSTRAINT ground_checkpoints_stream_check "
                 "CHECK (stream IN ('features', 'bugs', 'tweaks', 'reviewer', 'errors', 'all'))")

    # presence: (project, stream)
    conn.execute("ALTER TABLE sessions ADD COLUMN IF NOT EXISTS project_id INTEGER "
                 "REFERENCES projects(id) ON DELETE CASCADE")
    if erp_id is not None:
        conn.execute("UPDATE sessions SET project_id = %s WHERE project_id IS NULL", (erp_id,))
    conn.execute("DELETE FROM sessions WHERE project_id IS NULL")  # presence is ephemeral: nothing to keep
    conn.execute("ALTER TABLE sessions ALTER COLUMN project_id SET NOT NULL")
    conn.execute("ALTER TABLE sessions DROP CONSTRAINT IF EXISTS sessions_pkey")
    conn.execute("ALTER TABLE sessions ADD PRIMARY KEY (project_id, stream)")
    conn.execute("ALTER TABLE sessions DROP CONSTRAINT IF EXISTS sessions_stream_check")
    conn.execute("ALTER TABLE sessions ADD CONSTRAINT sessions_stream_check "
                 "CHECK (stream IN ('features', 'bugs', 'tweaks', 'reviewer', 'errors', 'all'))")
