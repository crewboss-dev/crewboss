"""WHO MAY SEE AND CHANGE WHICH PROJECT: the one place multi-user access is decided (task #713, change #41).

Every page, list, search, SSE stream, MCP tool, runner/job API, export and health view that touches a project-scoped row
goes through this module (or through app/scope.py, which calls it for session tokens). Do not write a second rule.

PRINCIPALS
  owner     the instance owner: everything (users, servers, every project, the overseer's global views). Also what every
            Microsoft (OIDC) super admin is, and what the pages are with sign-in switched off.
  designer  the projects they CREATED (projects.owner_user_id) plus the projects SHARED with them (project_members); they
            write in a project they own or were given as 'admin' or 'member'. They deploy only to servers the owner assigned them.
PROJECT ROLES (change #77), per project: owner (the creator: adds people, sets roles, transfers ownership), admin (approves merges
to main and production deploys; deploys dev and production), member (works, runs agents, deploys dev, REQUESTS production),
viewer (read only). The instance owner is above all projects. Release rules: app/deploys.py, app/release_gates.py.
  viewer    read only, in the projects shared with them. A viewer role caps every share at 'viewer' and never writes.
  none      a request that should have a signed-in person and has none (fail closed): sees nothing.

THE RULES
  - `visible_ids(p)`     the project ids p may READ; None means all (the owner). `editable_ids(p)` the ones p may CHANGE.
  - A project p cannot see is a 404, exactly like one that does not exist (`require_view`); a project p can see but not
    change is a 403 (`require_edit`) - they already know it exists.
  - Sharing is per project. Sharing a PARENT never shares its children (a child someone else made may be unrelated work),
    and sharing a child never shares its parent. A parent's page rolls up only the children the viewer can see; a visible
    child whose parent is hidden shows as a top-level project (`filtered_index`).
  - projects.owner_user_id NULL = the instance owner's own project: only an owner sees it. A session token acts for the
    owner of its project (`of_session`); a token on an owner-less project acts as the owner, as before multi-user.
  - Owner-only things (users, servers, platform keys, health, the global plan, tree moves) are `require_owner` (a 404 to
    anyone else).

AUDIT: `current_user_id()` is the person behind the request (set by the web gate, or from a session token); record_activity
and the approval tables store it beside the old text actor, so 'owner' stays readable in old rows and new rows name a user."""
from __future__ import annotations

import contextvars

from fastapi import Request
from dataclasses import dataclass

from app.db import get_cursor
from app.repo import Forbidden, NotFound

OWNER, DESIGNER, VIEWER, NONE = "owner", "designer", "viewer", "none"
ROLES = (OWNER, DESIGNER, VIEWER)
INVITE_ROLES = (DESIGNER, VIEWER)          # the owner is made on the console, never by an invite
SHARE_ROLES = ("admin", "member", "viewer")      # per-project roles in project_members (change #77)
PROJECT_OWNER = "owner"                              # the creator: projects.owner_user_id

_user_ctx: contextvars.ContextVar[int | None] = contextvars.ContextVar("crewboss_acting_user", default=None)


def current_user_id() -> int | None:
    """The users.id behind the running request / tool call, or None (sign-in off, a machine, the instance owner under OIDC)."""
    return _user_ctx.get()


def set_current_user(user_id: int | None):
    """Returns the reset token. Called by the web gate and by the token resolvers; harmless if never reset (per-request task)."""
    return _user_ctx.set(user_id)


@dataclass(frozen=True)
class Principal:
    user_id: int | None
    role: str
    name: str = ""

    @property
    def is_owner(self) -> bool:
        return self.role == OWNER

    @property
    def can_write(self) -> bool:
        return self.role in (OWNER, DESIGNER)


OWNER_PRINCIPAL = Principal(None, OWNER)
ANONYMOUS = Principal(None, NONE)


def owner_user_ids() -> frozenset:
    """Every users row with the owner role: the INSTANCE OWNER is one principal even when they have several rows (the legacy
    Microsoft row and a local account). Used wherever 'the same person' matters (routing, ownership)."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT id FROM users WHERE role = 'owner'")
        return frozenset(r["id"] for r in cur.fetchall())


def canonical_owner_id() -> int | None:
    """The users row new owner-made projects are recorded under: the local owner account if one exists, else the Microsoft row.
    Migration 0043 backfills existing projects to the same row."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT id FROM users WHERE role = 'owner' AND disabled_at IS NULL "
                    "ORDER BY (username IS NOT NULL) DESC, is_owner DESC, id LIMIT 1")
        row = cur.fetchone()
    return row["id"] if row else None


def of_web_user(web_user: dict | None) -> Principal:
    """The principal for a resolved web session (web_auth.resolve_session). An OIDC session has no user_id: its people are the
    super admins, i.e. owners. No session at all: the owner only while sign-in is switched off, else nobody."""
    if not web_user:
        from app import web_auth
        return ANONYMOUS if web_auth.required() else OWNER_PRINCIPAL
    role = web_user.get("role") or (OWNER if web_user.get("user_id") is None or web_user.get("is_owner") else NONE)
    if role not in ROLES:
        return ANONYMOUS
    return Principal(web_user.get("user_id") or web_user.get("oidc_user_id"), role,
                     web_user.get("name") or web_user.get("username") or "")


def of_request(request) -> Principal:
    state = getattr(request, "state", None)
    cached = getattr(state, "principal", None)
    if cached is not None:
        return cached
    p = of_web_user(getattr(state, "web_user", None))
    if state is not None:
        state.principal = p
    return p


def principal_for_user_id(user_id: int | None) -> Principal:
    """The principal of a users row (a runner's user, a job's starter); ANONYMOUS when there is none or it is disabled."""
    if user_id is None:
        return ANONYMOUS
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT id, role, display_name, disabled_at FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
    if row is None or row["disabled_at"] or row["role"] not in ROLES:
        return ANONYMOUS
    return Principal(row["id"], row["role"], row["display_name"] or "")


def of_session(session: dict) -> Principal:
    """The principal a SESSION TOKEN acts for: the owner of its project. No owner recorded (or a legacy project): the instance
    owner, which keeps every pre-multi-user token working. A disabled user's tokens do not resolve (auth.resolve_token)."""
    cached = session.get("_principal")
    if cached is not None:
        return cached
    uid = (session.get("project") or {}).get("owner_user_id")
    p = OWNER_PRINCIPAL
    if uid:
        with get_cursor(commit=False) as cur:
            cur.execute("SELECT id, role, display_name, disabled_at FROM users WHERE id = %s", (uid,))
            row = cur.fetchone()
        if row is None or row["disabled_at"]:
            p = ANONYMOUS
        else:
            p = Principal(row["id"], row["role"], row["display_name"] or "")
    session["_principal"] = p
    return p


# --- what a principal may see / change -----------------------------------------------------------------------------

def _owned_and_shared(user_id: int) -> tuple[set[int], dict[int, str]]:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT id FROM projects WHERE owner_user_id = %s", (user_id,))
        owned = {r["id"] for r in cur.fetchall()}
        cur.execute("SELECT project_id, role FROM project_members WHERE user_id = %s", (user_id,))
        shared = {r["project_id"]: r["role"] for r in cur.fetchall()}
    return owned, shared


def visible_ids(p: Principal) -> set[int] | None:
    """The project ids p may read; None = all of them (the owner)."""
    if p.is_owner:
        return None
    if p.user_id is None or p.role not in (DESIGNER, VIEWER):
        return set()
    owned, shared = _owned_and_shared(p.user_id)
    return owned | set(shared)


def editable_ids(p: Principal) -> set[int] | None:
    if p.is_owner:
        return None
    if p.user_id is None or p.role != DESIGNER:
        return set()
    owned, shared = _owned_and_shared(p.user_id)
    return owned | {pid for pid, role in shared.items() if role in ("admin", "member")}


def role_in(p: Principal, project_id: int | None) -> str | None:
    """The person's role in ONE project: 'owner' (the instance owner, or the project's creator), 'admin', 'member', 'viewer', or
    None (cannot see it). A viewer-role account is capped at viewer."""
    if project_id is None:
        return None
    if p.is_owner:
        return PROJECT_OWNER
    if p.user_id is None or p.role not in (DESIGNER, VIEWER):
        return None
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT owner_user_id FROM projects WHERE id = %s", (project_id,))
        row = cur.fetchone()
        cur.execute("SELECT role FROM project_members WHERE project_id = %s AND user_id = %s", (project_id, p.user_id))
        member = cur.fetchone()
    if row is None:
        return None
    role = PROJECT_OWNER if row["owner_user_id"] == p.user_id else (member["role"] if member else None)
    if role is not None and p.role == VIEWER:
        return "viewer"
    return role


def is_admin(p: Principal, project_id: int | None) -> bool:
    """May p approve merges to main and production deploys of this project? Owner (creator) and Admin; the instance owner."""
    return role_in(p, project_id) in (PROJECT_OWNER, "admin")


def admin_user_ids(project_id: int) -> set[int]:
    """The people who are Admin of a project: its creator and its admin members (the two-person rule counts them)."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT p.owner_user_id FROM projects p JOIN users u ON u.id = p.owner_user_id "
                    "WHERE p.id = %s AND u.disabled_at IS NULL", (project_id,))      # a disabled creator is not an Admin
        row = cur.fetchone()
        cur.execute("SELECT m.user_id FROM project_members m JOIN users u ON u.id = m.user_id "
                    "WHERE m.project_id = %s AND m.role = 'admin' AND u.disabled_at IS NULL", (project_id,))
        ids = {r["user_id"] for r in cur.fetchall()}
    if row and row["owner_user_id"]:
        ids.add(row["owner_user_id"])
    return ids


STEPUP_SHORT_SECONDS = 300           # the window a caller asks for when the action is the most sensitive (a secret; admin sign-in reset)


class StepUpRequired(Exception):
    """A route needs a recent confirmation (a fresh authenticator code, or a passkey tap) the session doesn't have (or can never have: `token`). main.py answers it: an htmx caller gets
    403 + HX-Trigger "stepup-required" (the shared modal asks for a code or passkey and re-sends the request); other callers get a
    403 page or JSON."""

    def __init__(self, within: int, token: bool = False, oidc: bool = False):
        super().__init__("a recent confirmation is needed")
        self.within, self.token, self.oidc = within, token, oidc


def stepup_seconds() -> int:
    """The default step-up window: WEB_STEPUP_HOURS (default 12 h)."""
    from app import config
    return max(60, int(config.WEB_STEPUP_HOURS * 3600))


def is_fresh(request, within: int | None = None) -> bool:
    """Has this person confirmed it's them (sign-in, a code, or a passkey tap) in the last `within` seconds (default the step-up window)?
      - sign-in off (nobody to ask: the owner at the console): yes;
      - a request carrying a Bearer token: NEVER (a token is not a person; it can't tap);
      - a local session: its last passkey tap (sign-in by passkey counts, so does a re-confirm);
      - a Microsoft (OIDC) session: a passkey re-confirm if it has one, else the OIDC auth_time (when Microsoft authenticated them);
      - no web session at all: no."""
    from app import web_auth
    within = within if within is not None else stepup_seconds()
    if (request.headers.get("authorization") or "").strip().lower().startswith("bearer "):
        return False
    if not web_auth.required():
        return True
    wu = getattr(request.state, "web_user", None)
    if not wu or wu.get("enrolment_only"):
        return False
    tap = wu.get("tap_age")
    if tap is not None and float(tap) <= within:
        return True
    if wu.get("user_id") is None:                      # OIDC: no local passkey to tap, so the sign-in time counts
        age = wu.get("auth_age")
        return age is not None and float(age) <= within
    return False


def factor_fresh(request) -> bool:
    """The short (5 minute) window: a passkey tap in the last 5 minutes."""
    return is_fresh(request, STEPUP_SHORT_SECONDS)


def require_fresh(within: int | None = None):
    """FastAPI dependency factory: `Depends(access.require_fresh())` (12 h default, WEB_STEPUP_HOURS) or
    `Depends(access.require_fresh(access.STEPUP_SHORT_SECONDS))`. Raises StepUpRequired unless is_fresh."""
    def _dep(request: Request) -> None:
        if not is_fresh(request, within):
            from app import web_auth
            bearer = (request.headers.get("authorization") or "").strip().lower().startswith("bearer ")
            wu = getattr(request.state, "web_user", None)
            raise StepUpRequired(within if within is not None else stepup_seconds(), token=bearer or not wu,
                                 oidc=bool(wu and wu.get("user_id") is None and web_auth.required()))
    return _dep


def can_view(p: Principal, project_id: int | None) -> bool:
    ids = visible_ids(p)
    return project_id is not None and (ids is None or project_id in ids)


def can_edit(p: Principal, project_id: int | None) -> bool:
    ids = editable_ids(p)
    return project_id is not None and (ids is None or project_id in ids)


def owns(p: Principal, project: dict | None) -> bool:
    """Created by p (or p is the owner): may rename/archive it and set its details. Sharing never grants this."""
    if p.is_owner:
        return True
    return bool(project) and p.user_id is not None and project.get("owner_user_id") == p.user_id


def require_view(p: Principal, project: dict | None, what: str | None = None) -> dict:
    """The project, or NotFound with the SAME words a missing project gets: its existence never leaks."""
    if project is None or not can_view(p, project["id"]):
        raise NotFound(what or f"project {(project or {}).get('slug')!r} not found")
    return project


def require_edit(p: Principal, project: dict) -> dict:
    require_view(p, project)
    if not can_edit(p, project["id"]):
        raise Forbidden("you can look at this project but not change it")
    return project


def require_owner(p: Principal) -> None:
    """Owner-only surface: 404 to anyone else, so what the owner has stays unlisted."""
    if not p.is_owner:
        raise NotFound("not found")


def filter_projects(p: Principal, rows: list[dict]) -> list[dict]:
    ids = visible_ids(p)
    return list(rows) if ids is None else [r for r in rows if r["id"] in ids]


def filtered_index(p: Principal, idx: dict[int, dict] | None = None) -> dict[int, dict]:
    """tree.index() restricted to what p may see. A visible project whose parent is hidden becomes a top-level one in
    tree.project_tree (it only follows a parent that is in the index)."""
    from app import tree
    idx = idx if idx is not None else tree.index()
    ids = visible_ids(p)
    return dict(idx) if ids is None else {pid: row for pid, row in idx.items() if pid in ids}


def visible_list(p: Principal) -> list[int] | None:
    """visible_ids as a list for SQL `= ANY(%s)`; None = no filter."""
    ids = visible_ids(p)
    return None if ids is None else sorted(ids)


# --- creating and placing ------------------------------------------------------------------------------------------

def check_new_project(p: Principal, parent: dict | None, kind: str, sessions: list[str]) -> None:
    """What a non-owner may create: a plain project (never an account), at the top or under a project they can change, with
    NO standing tmux session (that would start Claude on the owner's dev box: designers run their own runner)."""
    if p.is_owner:
        return
    if not p.can_write:
        raise Forbidden("a viewer can't create projects")
    if kind != "project":
        raise Forbidden("only the owner creates accounts")
    if sessions:
        raise Forbidden("only the owner starts standing sessions; run your agents on your own runner")
    if parent is not None and not can_edit(p, parent["id"]):
        raise NotFound("no such parent project")


# --- sharing, assignment (owner-run; the admin page calls these) ----------------------------------------------------

def members(project_id: int) -> list[dict]:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT m.user_id, m.role, u.display_name, u.username, u.role AS user_role FROM project_members m "
                    "JOIN users u ON u.id = m.user_id WHERE m.project_id = %s ORDER BY lower(u.username)", (project_id,))
        return [dict(r) for r in cur.fetchall()]


def _no_pending_release(cur, project_id: int, user_id: int) -> None:
    """Refuse to demote or remove an Admin who has a production deploy or merge request still waiting: the two-person rule is
    counted when the request is made, and an Admin can't be removed from under it (then self-approve as the last Admin)."""
    from app import repo
    cur.execute("SELECT role FROM project_members WHERE project_id = %s AND user_id = %s", (project_id, user_id))
    row = cur.fetchone()
    if not row or row["role"] != "admin":
        return
    cur.execute("SELECT (SELECT count(*) FROM server_jobs WHERE project_id = %s AND env = 'prod' AND state = 'requested' "
                "AND requested_by = %s) + (SELECT count(*) FROM merge_requests WHERE project_id = %s AND state = 'requested' "
                "AND requested_by = %s) AS n", (project_id, user_id, project_id, user_id))
    if cur.fetchone()["n"]:
        raise repo.Conflict("that Admin has a production deploy or merge request waiting: decide it first")


def share(project_id: int, user_id: int, role: str, by: int | None) -> None:
    from app import repo
    if role not in SHARE_ROLES:
        raise repo.BadRequest("role: admin, member or viewer")
    with get_cursor() as cur:
        cur.execute("SELECT 1 FROM projects WHERE id = %s", (project_id,))
        if not cur.fetchone():
            raise NotFound("project not found")
        cur.execute("SELECT role, disabled_at FROM users WHERE id = %s", (user_id,))
        u = cur.fetchone()
        if not u or u["disabled_at"]:
            raise NotFound("no such user")
        if u["role"] == OWNER:
            raise repo.BadRequest("the owner already sees everything")
        cur.execute("SELECT owner_user_id FROM projects WHERE id = %s", (project_id,))
        if cur.fetchone()["owner_user_id"] == user_id:
            raise repo.BadRequest("that person owns the project already")
        if role != "admin":
            _no_pending_release(cur, project_id, user_id)      # no demoting an Admin out of a pending production request
        cur.execute("INSERT INTO project_members (project_id, user_id, role, added_by) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (project_id, user_id) DO UPDATE SET role = EXCLUDED.role, added_by = EXCLUDED.added_by",
                    (project_id, user_id, role, by))
        repo.record_activity(cur, project_id, "owner", "shared", "project", project_id,
                             f"Shared with user {user_id} as {role}")


def transfer_ownership(project_id: int, new_owner_id: int) -> None:
    """The project's creator-role moves to another (enabled, non-viewer) person; the old Owner stays on as Admin. Callers have
    checked that the actor is the current Owner or the instance owner."""
    from app import repo
    with get_cursor() as cur:
        cur.execute("SELECT owner_user_id FROM projects WHERE id = %s FOR UPDATE", (project_id,))
        proj = cur.fetchone()
        if not proj:
            raise NotFound("project not found")
        cur.execute("SELECT role, disabled_at FROM users WHERE id = %s", (new_owner_id,))
        u = cur.fetchone()
        if not u or u["disabled_at"] or u["role"] != DESIGNER:
            raise repo.BadRequest("ownership goes to an active designer")
        old = proj["owner_user_id"]
        cur.execute("UPDATE projects SET owner_user_id = %s WHERE id = %s", (new_owner_id, project_id))
        cur.execute("DELETE FROM project_members WHERE project_id = %s AND user_id = %s", (project_id, new_owner_id))
        if old and old != new_owner_id:
            cur.execute("INSERT INTO project_members (project_id, user_id, role, added_by) VALUES (%s, %s, 'admin', %s) "
                        "ON CONFLICT (project_id, user_id) DO UPDATE SET role = 'admin'", (project_id, old, new_owner_id))
        repo.record_activity(cur, project_id, "owner", "transferred", "project", project_id,
                             f"Ownership moved to user {new_owner_id}")


def unshare(project_id: int, user_id: int) -> None:
    from app import repo
    with get_cursor() as cur:
        _no_pending_release(cur, project_id, user_id)
        cur.execute("DELETE FROM project_members WHERE project_id = %s AND user_id = %s", (project_id, user_id))
        repo.record_activity(cur, project_id, "owner", "unshared", "project", project_id,
                             f"Stopped sharing with user {user_id}")


def server_assigned_to_project(project_id: int, server_id: int) -> bool:
    """Did the instance owner pin this server to this project? (An assignment is per project, never per person.)"""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT 1 FROM server_assignments WHERE project_id = %s AND server_id = %s", (project_id, server_id))
        return cur.fetchone() is not None


def audit(cur, verb: str, summary: str) -> None:
    """One row in the INSTANCE audit (invites, roles, MFA resets, server assignments): only the instance owner reads it."""
    cur.execute("INSERT INTO instance_audit (actor_user_id, verb, summary) VALUES (%s, %s, %s)",
                (current_user_id(), verb, summary[:500]))


def recent_audit(limit: int = 40) -> list[dict]:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT a.at, a.verb, a.summary, u.display_name AS actor FROM instance_audit a "
                    "LEFT JOIN users u ON u.id = a.actor_user_id ORDER BY a.id DESC LIMIT %s", (limit,))
        return [dict(r) for r in cur.fetchall()]


def same_person(a: int | None, b: int | None) -> bool:
    """The same human, counting the instance owner's several users rows as one."""
    if a is None or b is None:
        return False
    group = owner_user_ids()
    return a == b or (a in group and b in group)


def assign_server(server_id: int, user_id: int, by: int | None, project_id: int) -> None:
    """Pin a commissioned server to ONE project owned by `user_id` (a designer): that project's Owner may define deploy targets on
    it, and that project (only) deploys to it."""
    from app import repo
    with get_cursor() as cur:
        cur.execute("SELECT account_id FROM commissioned_servers WHERE id = %s", (server_id,))
        s = cur.fetchone()
        if not s:
            raise NotFound("server not found")
        cur.execute("SELECT role, disabled_at FROM users WHERE id = %s", (user_id,))
        u = cur.fetchone()
        if not u or u["disabled_at"]:
            raise NotFound("no such user")
        if u["role"] != DESIGNER:
            raise repo.BadRequest("only a designer is assigned servers")
        cur.execute("SELECT owner_user_id FROM projects WHERE id = %s", (project_id,))
        pr = cur.fetchone()
        if not pr or pr["owner_user_id"] != user_id:
            raise repo.BadRequest("a server is assigned to one of that person's own projects")
        cur.execute("INSERT INTO server_assignments (server_id, project_id, user_id, assigned_by) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (server_id, project_id) DO UPDATE SET user_id = EXCLUDED.user_id", (server_id, project_id, user_id, by))
        audit(cur, "assigned", f"Server {server_id} pinned to project {project_id} (owner user {user_id})")


def unassign_server(server_id: int, user_id: int, project_id: int | None = None) -> None:
    with get_cursor() as cur:
        cur.execute("DELETE FROM server_assignments WHERE server_id = %s AND user_id = %s "
                    "AND (%s::int IS NULL OR project_id = %s)", (server_id, user_id, project_id, project_id))
        audit(cur, "unassigned", f"Server {server_id} unpinned from user {user_id}")


def may_deploy_to(p: Principal, project_id: int, server_id: int) -> bool:
    """May p deploy this project to this commissioned server without anyone approving it? The owner always may; a designer
    only for a project they can change and a server the owner assigned them."""
    if p.is_owner:
        return True
    return role_in(p, project_id) in (PROJECT_OWNER, "admin", "member") and server_assigned_to_project(project_id, server_id)


# --- the ambient filter: defence in depth for the few "everything" reads in the data layer --------------------------

def ambient_ids() -> list[int] | None:
    """The project ids the person behind THIS request/tool call may read, for repo functions whose unscoped form means
    "every active project" (the global overview, Focus, open questions, the change log, the activity feed, the activity
    cursor, the task finder, the owner's WIP count). None = no filter: the owner, a machine, or sign-in off. A caller that
    already passes explicit ids does not need this; a caller that forgets to still cannot read past the person's reach."""
    uid = current_user_id()
    if uid is None:
        return None
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT role, disabled_at FROM users WHERE id = %s", (uid,))
        row = cur.fetchone()
    if row is None or row["disabled_at"]:
        return []
    if row["role"] == OWNER:
        return None
    ids = visible_ids(Principal(uid, row["role"]))
    return None if ids is None else sorted(ids)


# --- FastAPI dependencies (the route-coverage test in tests/test_isolation.py recognises these) ----------------------

def owner_only(request: Request) -> None: 
    """Dependency for an owner-only page route: 404 to anyone else."""
    require_owner(of_request(request))


def signed_in_person(request: Request) -> Principal: 
    """Dependency for a route that needs a real person (not "nobody"); the handler then scopes what it touches."""
    p = of_request(request)
    if p.role == NONE:
        raise NotFound("not found")
    return p
