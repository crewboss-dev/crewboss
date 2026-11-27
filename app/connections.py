"""PROJECT CONNECTIONS (#784, P1): how one project relates to another, as rows in `project_connections`.

  part_of          the subject lives in the other project's code. One host per project; a host is never itself part_of.
  depends_on       the subject needs the other (directed, informational).
  integrates_with  the two talk to each other: SYMMETRIC, stored once with the lower id first, read both ways.

The reverse of part_of / depends_on is shown on the other project as "contains" / "needed by".

RULES (design 1.2 / section 2 / security rows 5-7, 9)
  - Both ends sit in the same account partition (tree.account_root_id today) and neither is an account: this module checks first for a
    clear message, and the trigger in db/migrations/0075 is the backstop.
  - A write needs MANAGE on the subject project (tabs.can_manage: the instance owner, or an admin of it) and VIEW of the other end;
    part_of additionally needs MANAGE on the host. A project you cannot see is the same 404 text as a missing one.
  - A connection is shown only when the viewer can see BOTH ends; otherwise the row is omitted (no placeholder, no count).
  - Notes are one line, at most NOTE_MAX characters.

P1 changes no repo, deploy or agent behaviour for part_of: it is recorded and displayed only.
TODO(P2): effective_repo(project) / effective_environments(pid) for a part_of child (the host's repo, remote and deploy targets,
          put into a job payload only after a member-on-host check), `can_start_agents` / `can_deploy` against host_of(pid), and the
          logic-map edges (the workflow map is not app/knowledge_graph.py, which only links knowledge entries; add connected
          projects there as edge nodes labelled by kind once the map takes a project-level layer).
"""
from __future__ import annotations

import logging

import psycopg

from app import access, tabs
from app.db import get_cursor
from app.repo import BadRequest, Conflict, Forbidden, NotFound

log = logging.getLogger("crewboss.connections")

KINDS = ("part_of", "depends_on", "integrates_with")
KIND_LABELS = {"part_of": "Part of", "contains": "Contains", "depends_on": "Depends on", "needed_by": "Needed by",
               "integrates_with": "Integrates with"}
SECTIONS = ("part_of", "contains", "depends_on", "needed_by", "integrates_with")
NOTE_MAX = 300


# --- reading -----------------------------------------------------------------------------------------------------------------

def _index(cur) -> list[dict]:
    cur.execute("SELECT c.id, c.project_id, c.other_id, c.kind, c.note, "
                "p.slug AS project_slug, p.name AS project_name, p.archived_at AS project_archived_at, "
                "o.slug AS other_slug, o.name AS other_name, o.archived_at AS other_archived_at "
                "FROM project_connections c JOIN projects p ON p.id = c.project_id JOIN projects o ON o.id = c.other_id "
                "ORDER BY lower(o.name), c.id")
    return [dict(r) for r in cur.fetchall()]


def index(request=None) -> list[dict]:
    """Every connection (the table is tiny): ONE query per request when `request` is given (memoised on request.state; a write
    through add / set_note / remove drops the memo)."""
    state = getattr(request, "state", None)
    cached = getattr(state, "connections_index", None) if state is not None else None
    if cached is not None:
        return cached
    with get_cursor(commit=False) as cur:
        rows = _index(cur)
    if state is not None:
        state.connections_index = rows
    return rows


def _forget(request) -> None:
    state = getattr(request, "state", None)
    if state is not None and getattr(state, "connections_index", None) is not None:
        state.connections_index = None


def _end(row: dict, pid: int, as_other: bool) -> dict:
    side = "project" if as_other else "other"
    return {"id": row["id"], "project_id": row[f"{side}_id"], "slug": row[f"{side}_slug"], "name": row[f"{side}_name"],
            "archived": bool(row[f"{side}_archived_at"]), "note": row["note"], "kind": row["kind"],
            "editable_from": row["project_id"] == pid or row["kind"] == "integrates_with"}


def for_project(pid: int, visible: set[int] | None, rows: list[dict] | None = None) -> dict[str, list[dict]]:
    """{part_of, contains, depends_on, needed_by, integrates_with}: each a list of {id, project_id, slug, name, archived, note, kind,
    editable_from}. `visible` = the project ids the viewer may see (None = all); a row whose OTHER end they cannot see is omitted."""
    out: dict[str, list[dict]] = {k: [] for k in SECTIONS}
    for r in rows if rows is not None else index():
        if r["project_id"] == pid:
            other = r["other_id"]
            key = r["kind"]
            entry = _end(r, pid, False)
        elif r["other_id"] == pid:
            other = r["project_id"]
            key = {"part_of": "contains", "depends_on": "needed_by"}.get(r["kind"], r["kind"])
            entry = _end(r, pid, True)
        else:
            continue
        if visible is not None and other not in visible:
            continue
        out[key].append(entry)
    return out


def any_shown(conns: dict[str, list[dict]]) -> bool:
    return any(conns.get(k) for k in SECTIONS)


def host_of(pid: int, rows: list[dict] | None = None) -> int | None:
    """The project this one is part_of, or None."""
    for r in rows if rows is not None else index():
        if r["project_id"] == pid and r["kind"] == "part_of":
            return r["other_id"]
    return None


def picker(who: "access.Principal", project: dict, rows: list[dict] | None = None) -> list[dict]:
    """The projects this person may connect `project` to: the same account partition, visible to them, live, never an account or itself."""
    from app import tree
    idx = tree.index()
    mine = tree.account_root_id(project["id"], idx)
    ids = access.visible_ids(who)
    out = [p for p in idx.values()
           if p["id"] != project["id"] and p["kind"] != "account" and not p["archived_at"]
           and (ids is None or p["id"] in ids) and tree.account_root_id(p["id"], idx) == mine]
    return sorted(out, key=lambda p: p["name"].lower())


# --- writing -----------------------------------------------------------------------------------------------------------------

def clean_note(note: str | None) -> str:
    one = " ".join((note or "").split())
    if len(one) > NOTE_MAX:
        raise BadRequest(f"a note is at most {NOTE_MAX} characters")
    return one


def require_manage(who: "access.Principal", project: dict) -> None:
    if not tabs.can_manage(who, project):
        raise Forbidden("only the project's owner or an admin changes its connections")


def _other(who: "access.Principal", slug: str) -> dict:
    """The other end by slug. Missing and unseen share ONE message, so a slug cannot be probed."""
    from app import repo
    slug = (slug or "").strip()
    msg = f"project {slug!r} not found"
    if not repo.SLUG_RE.match(slug):
        raise NotFound(msg)
    try:
        other = repo.get_project_by_slug(slug)
    except NotFound:
        raise NotFound(msg) from None
    return access.require_view(who, other, msg)


def _normalise(subject_id: int, other_id: int, kind: str) -> tuple[int, int]:
    return (min(subject_id, other_id), max(subject_id, other_id)) if kind == "integrates_with" else (subject_id, other_id)


def _check_pair(subject: dict, other: dict, kind: str, rows: list[dict]) -> None:
    """The structural rules, with a clear message (the trigger is the backstop)."""
    from app import tree
    if kind not in KINDS:
        raise BadRequest("unknown kind of connection")
    if subject["id"] == other["id"]:
        raise BadRequest("a project cannot be connected to itself")
    if subject["kind"] == "account" or other["kind"] == "account":
        raise BadRequest("a connection joins two projects, not an account")
    idx = tree.index()
    if not tree.same_account(subject["id"], other["id"], idx):
        raise BadRequest("both projects must be in the same account")
    a, b = _normalise(subject["id"], other["id"], kind)
    if any(r["kind"] == kind and r["project_id"] == a and r["other_id"] == b for r in rows):
        raise BadRequest("those two projects are already connected that way")
    if kind == "part_of":
        host = host_of(subject["id"], rows)
        # One generic message: which of these holds may depend on rows the person cannot see, so none is named.
        if host is not None or host_of(other["id"], rows) is not None \
                or any(r["kind"] == "part_of" and r["other_id"] == subject["id"] for r in rows):
            raise BadRequest("a project has at most one host, and a host is never itself part of another project (no chains)")


def add(who: "access.Principal", project: dict, kind: str, other_slug: str, note: str = "", request=None) -> dict:
    """Connect `project` (the subject) to the project named by `other_slug`. Needs MANAGE on the subject and VIEW of the other;
    part_of also needs MANAGE on the host."""
    require_manage(who, project)
    other = _other(who, other_slug)
    note = clean_note(note)
    if kind == "part_of":
        require_manage(who, other)
    _check_pair(project, other, kind, index())
    a, b = _normalise(project["id"], other["id"], kind)
    try:
        with get_cursor() as cur:
            cur.execute("INSERT INTO project_connections (project_id, other_id, kind, note, created_by) VALUES (%s, %s, %s, %s, %s) RETURNING id",
                        (a, b, kind, note, who.user_id))
            cid = cur.fetchone()["id"]
            cur.execute("DELETE FROM connection_seed_tombstones WHERE project_id = %s AND other_id = %s AND kind = %s", (a, b, kind))
    except psycopg.errors.CheckViolation as exc:
        raise BadRequest(str(exc).splitlines()[0]) from None
    except psycopg.errors.UniqueViolation:
        raise BadRequest("those two projects are already connected that way") from None
    _forget(request)
    return {"id": cid}


def _row_for(project: dict, connection_id) -> dict:
    """The connection, only if it involves `project` (its id in the WHERE); anything else is the same 404 as a missing id."""
    try:
        cid = int(connection_id)
    except (TypeError, ValueError):
        raise NotFound("connection not found") from None
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT * FROM project_connections WHERE id = %s AND (project_id = %s OR other_id = %s)",
                    (cid, project["id"], project["id"]))
        row = cur.fetchone()
    if row is None:
        raise NotFound("connection not found")
    return dict(row)


def _authorise_edit(who, project: dict, row: dict) -> None:
    """VIEW of the other end FIRST (a hidden end is the same 404 as a missing id: no walking ids to count hidden edges), then the direction
    rule, then MANAGE on the project."""
    other_id = row["other_id"] if row["project_id"] == project["id"] else row["project_id"]
    if not access.can_view(who, other_id):
        raise NotFound("connection not found")
    if row["project_id"] != project["id"] and row["kind"] != "integrates_with":
        raise Forbidden("a connection is changed from the project it starts at")
    require_manage(who, project)


def set_note(who: "access.Principal", project: dict, connection_id, note: str, request=None) -> None:
    row = _row_for(project, connection_id)
    _authorise_edit(who, project, row)
    note = clean_note(note)
    try:
        with get_cursor() as cur:
            cur.execute("UPDATE project_connections SET note = %s, updated_at = now() WHERE id = %s", (note, row["id"]))
    except psycopg.errors.CheckViolation as exc:
        raise BadRequest(str(exc).splitlines()[0]) from None
    _forget(request)


def remove(who: "access.Principal", project: dict, connection_id, request=None) -> None:
    row = _row_for(project, connection_id)
    _authorise_edit(who, project, row)
    with get_cursor() as cur:
        cur.execute("DELETE FROM project_connections WHERE id = %s", (row["id"],))
        # remembered, so instance.yaml's seed does not put a pair someone removed back at the next start
        cur.execute("INSERT INTO connection_seed_tombstones (project_id, other_id, kind) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                    (row["project_id"], row["other_id"], row["kind"]))
    _forget(request)


# --- instance config (instance.yaml `connections:`) ---------------------------------------------------------------------------

def seed_from_config(specs: list[dict] | None = None) -> dict:
    """Insert the connections an install lists in instance.yaml that are not there yet. IDEMPOTENT and ADDITIVE: it never deletes or
    edits a row (a note a person changed stays), skips a slug that is not a project (logged) and a pair the rules refuse (logged).
    A pair a person removed (connection_seed_tombstones) is not re-added. Returns {"added": n, "present": n, "skipped": n}."""
    from app import config, tree
    specs = config.CONNECTIONS if specs is None else specs
    idx = tree.index()
    by_slug = {p["slug"]: p for p in idx.values()}
    rows = index()
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT project_id, other_id, kind FROM connection_seed_tombstones")
        removed = {(r["project_id"], r["other_id"], r["kind"]) for r in cur.fetchall()}
    counts = {"added": 0, "present": 0, "skipped": 0}
    for s in specs:
        subject, other = by_slug.get(s["project"]), by_slug.get(s["other"])
        if subject is None or other is None:
            log.warning("connections: skipped %s %s %s: unknown project %s", s["project"], s["kind"], s["other"],
                        s["project"] if subject is None else s["other"])
            counts["skipped"] += 1
            continue
        a, b = _normalise(subject["id"], other["id"], s["kind"])
        if any(r["kind"] == s["kind"] and r["project_id"] == a and r["other_id"] == b for r in rows):
            counts["present"] += 1
            continue
        if (a, b, s["kind"]) in removed:
            log.info("connections: %s %s %s was removed by a person: not re-added", s["project"], s["kind"], s["other"])
            counts["skipped"] += 1
            continue
        try:
            _check_pair(subject, other, s["kind"], rows)
            note = clean_note(s.get("note"))
            with get_cursor() as cur:
                cur.execute("INSERT INTO project_connections (project_id, other_id, kind, note) VALUES (%s, %s, %s, %s) "
                            "ON CONFLICT DO NOTHING", (a, b, s["kind"], note))
        except (BadRequest, psycopg.Error) as exc:
            log.warning("connections: skipped %s %s %s: %s", s["project"], s["kind"], s["other"], exc)
            counts["skipped"] += 1
            continue
        counts["added"] += 1
        rows = index()
    if counts["added"]:
        log.info("connections: added %d from instance.yaml", counts["added"])
    return counts
