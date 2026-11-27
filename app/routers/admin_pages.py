"""The owner's Users page (task #713; #809): /admin/users. Add a person (they get a one-time temporary password), see who is here and their
sign-in email, change a role, disable, Reset sign-in, sign someone out everywhere, share a project, assign servers, and set how long a
sign-in lasts. OWNER ONLY (a 404 to anyone else: app/access.owner_only) and local sign-in only (Microsoft mode has one super-admin list).

Anything that grants reach or changes a person's sign-in needs a recent PASSKEY tap (access.require_fresh: 12 h by default; Reset sign-in and
the sign-in length ask for 5 minutes). A temporary password appears on the response that makes it, never again. POSTs are page forms
behind the same-origin guard."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import access, config, repo, server_access, tree, users, web_auth
from app.db import get_cursor
from app.repo import BadRequest, Conflict, NotFound
from app.routers.pages import FLASH_COOKIE, render

log = logging.getLogger("crewboss.admin")


def _local_only() -> None:
    if web_auth.mode() != "local":
        raise HTTPException(404)


router = APIRouter(prefix="/admin", include_in_schema=False,
                   dependencies=[Depends(access.owner_only), Depends(_local_only)])


def _me(request: Request) -> int | None:
    wu = getattr(request.state, "web_user", None)
    return wu.get("user_id") if wu else None


FRESH = Depends(access.require_fresh())                                   # a passkey tap in the step-up window (12 h)
FRESH_SHORT = Depends(access.require_fresh(access.STEPUP_SHORT_SECONDS))  # ... the last 5 minutes


def _model() -> dict:
    people = users.list_users()
    by_id = {u["id"]: u for u in people}
    for u in people:
        u["shares"], u["servers"], u["owned"] = [], [], 0
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT m.user_id, m.role, p.slug, p.name FROM project_members m JOIN projects p ON p.id = m.project_id "
                    "ORDER BY p.name")
        for r in cur.fetchall():
            if r["user_id"] in by_id:
                by_id[r["user_id"]]["shares"].append(dict(r))
        cur.execute("SELECT a.user_id, s.id AS server_id, s.name, p.slug AS account, pr.slug AS pinned, pr.id AS project_id "
                    "FROM server_assignments a JOIN commissioned_servers s ON s.id = a.server_id "
                    "JOIN projects p ON p.id = s.account_id JOIN projects pr ON pr.id = a.project_id ORDER BY s.name")
        for r in cur.fetchall():
            if r["user_id"] in by_id:
                by_id[r["user_id"]]["servers"].append(dict(r))
        cur.execute("SELECT owner_user_id, count(*) AS n FROM projects WHERE owner_user_id IS NOT NULL GROUP BY owner_user_id")
        for r in cur.fetchall():
            if r["owner_user_id"] in by_id:
                by_id[r["owner_user_id"]]["owned"] = r["n"]
    accounts = [p["id"] for p in tree.project_tree() if tree.is_account(p)]
    slug_of = {p["id"]: p["slug"] for p in tree.index().values()}
    servers = [dict(s, account=slug_of.get(s["account_id"], "")) for s in server_access.list_for(accounts)] if accounts else []
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT id, slug, name, owner_user_id FROM projects WHERE owner_user_id IS NOT NULL AND archived_at IS NULL "
                    "ORDER BY name")
        owned = [dict(r) for r in cur.fetchall()]
    for u in people:
        u["owned_projects"] = [p for p in owned if p["owner_user_id"] == u["id"]]
    return {"people": people, "session_choices": web_auth.SESSION_CHOICES, "session_minutes": web_auth.session_minutes(),
            "projects": repo.list_projects(include_archived=False),
            "servers": servers, "roles": access.INVITE_ROLES, "share_roles": access.SHARE_ROLES,
            "audit": access.recent_audit()}


def _page(request: Request, status: int = 200, error: str = "", temp_password: str = "", temp_for: str = "", temp_reset: bool = False):
    resp = render(request, "pages/admin_users.html", "admin", error=error, temp_password=temp_password, temp_for=temp_for,
                  temp_reset=temp_reset, **_model())
    resp.status_code = status
    resp.headers["Cache-Control"] = "no-store"        # a temporary password is on this response only
    return resp


def _back(message: str):
    resp = RedirectResponse("/admin/users", status_code=303)
    resp.set_cookie(FLASH_COOKIE, message, max_age=60, samesite="lax", httponly=True)   # fixed ASCII text only
    return resp


@router.get("/users", response_class=HTMLResponse)
def users_page(request: Request):
    return _page(request)


@router.post("/users/create", response_class=HTMLResponse, dependencies=[FRESH])
def create_user(request: Request, username: str = Form(""), role: str = Form("designer"), email: str = Form("")):
    try:
        user, pw = users.create_user(username, role, email)
    except BadRequest as exc:
        return _page(request, 400, error=str(exc))
    return _page(request, temp_password=pw, temp_for=user["username"])


@router.post("/settings/session-length", dependencies=[FRESH_SHORT])
def set_session_length(request: Request, minutes: int = Form(0)):
    try:
        web_auth.set_session_minutes(minutes, _me(request))
    except ValueError as exc:
        return _page(request, 400, error=str(exc))
    return _back("Sign-in length changed.")


def _act(request: Request, fn, *args, done: str):
    try:
        fn(*args)
    except (BadRequest, NotFound, Conflict) as exc:
        return _page(request, 400, error=str(exc))
    return _back(done)


@router.post("/users/{user_id:int}/role", dependencies=[FRESH])
def change_role(request: Request, user_id: int, role: str = Form(...)):
    return _act(request, users.set_role, user_id, role, done="Role changed.")


@router.post("/users/{user_id:int}/disable", dependencies=[FRESH])
def disable_user(request: Request, user_id: int):
    return _act(request, users.set_disabled, user_id, True, done="User disabled and signed out.")


@router.post("/users/{user_id:int}/enable", dependencies=[FRESH])
def enable_user(request: Request, user_id: int):
    return _act(request, users.set_disabled, user_id, False, done="User enabled.")


@router.post("/users/{user_id:int}/revoke-sessions", dependencies=[FRESH])
def revoke_sessions(request: Request, user_id: int):
    return _act(request, users.revoke_sessions, user_id, done="Signed out everywhere.")


@router.post("/users/{user_id:int}/clear-email", dependencies=[FRESH])
def clear_email(request: Request, user_id: int):
    return _act(request, users.clear_email, user_id, done="Sign-in email removed.")


@router.post("/users/{user_id:int}/reset-signin", response_class=HTMLResponse, dependencies=[FRESH_SHORT])
def reset_signin(request: Request, user_id: int):
    """Needs the owner's passkey tap from the last 5 minutes. The owner account itself is refused (its way back is a recovery code or the server console)."""
    try:
        pw = users.reset_signin(user_id)
        name = next((u["username"] for u in users.list_users() if u["id"] == user_id), "")
    except (BadRequest, NotFound, Conflict) as exc:
        return _page(request, 400, error=str(exc))
    return _page(request, temp_password=pw, temp_for=name, temp_reset=True)


@router.post("/users/{user_id:int}/share", dependencies=[FRESH])
def share_project(request: Request, user_id: int, project: str = Form(...), role: str = Form("member")):
    def go():
        access.share(repo.get_project_by_slug(project.strip())["id"], user_id, role, _me(request))
    return _act(request, go, done="Shared. Only that project; its sub-projects are not included.")


@router.post("/users/{user_id:int}/unshare", dependencies=[FRESH])
def unshare_project(request: Request, user_id: int, project: str = Form(...)):
    def go():
        access.unshare(repo.get_project_by_slug(project.strip())["id"], user_id)
    return _act(request, go, done="No longer shared.")


@router.post("/users/{user_id:int}/servers", dependencies=[FRESH])
def assign_server(request: Request, user_id: int, server_id: int = Form(...), project_id: int = Form(...)):
    return _act(request, access.assign_server, server_id, user_id, _me(request), project_id, done="Server pinned to the project.")


@router.post("/users/{user_id:int}/servers/remove", dependencies=[FRESH])
def unassign_server(request: Request, user_id: int, server_id: int = Form(...), project_id: int = Form(0)):
    return _act(request, access.unassign_server, server_id, user_id, project_id or None, done="Server unassigned.")


# THE ADMINISTRATION HOME (#778): /admin, an overview linking to each owner page with a count. Owner only (a 404 to anyone else); unlike the Users
# pages above it does not need local sign-in (Runners, Servers and Platform keys exist in every sign-in mode).
home_router = APIRouter(prefix="/admin", include_in_schema=False, dependencies=[Depends(access.owner_only)])


def _overview(request: Request) -> list[dict]:
    from app import devices, platform_keys
    rows = []
    has_account = bool(_me(request))
    if has_account and web_auth.mode() == "local":
        n = len(users.list_users())
        rows.append({"key": "users", "label": "Users", "href": "/admin/users", "count": n, "noun": "person" if n == 1 else "people",
                     "blurb": "Invite people, set roles, share projects, assign servers."})
    homes = [p for p in tree.project_tree() if tree.is_runner_home(p)]
    n_run = sum(len(devices.list_for(p["id"])) for p in homes)
    accounts = [p["id"] for p in tree.project_tree() if tree.is_account(p)]
    n_srv = len(server_access.list_for(accounts)) if accounts else 0
    n_mach = n_run + n_srv
    rows.append({"key": "machines", "label": "Machines", "href": "/machines", "count": n_mach, "noun": "machine" if n_mach == 1 else "machines",
                 "blurb": "PCs that run jobs and servers that receive deploys, one card each (#810)."})
    if config.PLATFORM_KEY_STORES and platform_keys.home_slug():
        n = len(config.PLATFORM_KEY_STORES)
        rows.append({"key": "keys", "label": "Platform keys", "href": platform_keys.page_path(), "count": n, "noun": "store" if n == 1 else "stores",
                     "blurb": "The keys each connected store holds."})
    return rows


@home_router.get("", response_class=HTMLResponse)
def admin_home(request: Request):
    resp = render(request, "pages/admin_home.html", "admin_home", cards=_overview(request))
    resp.headers["Cache-Control"] = "no-store"
    return resp
