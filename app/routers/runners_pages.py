"""Runners (task #627, phases P1, P2). Since #810 the page is the Machines page (app/routers/machines_pages.py) and GET /<account>/runners
redirects to /machines?tab=runners; the POSTs below stay (the forms are the same) and send you back there. The account's enrolled runners
(devices), its jobs (state, device, profile, fence) and locks (an orphaned one has a Clear button), a New enrolment code
form whose code is shown ONCE with its 5-minute expiry, and Revoke. Signed-in web session only: a person's users row
comes from the sign-in (web_auth.upsert_user), so with sign-in off there is nobody to issue a code for and the page
refuses. Accounts only, and a device id of another account is a 404 like a missing one (app/devices.py)."""
from __future__ import annotations

from fastapi import Depends, APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import access, devices, jobs, runner_identity, tree, web_auth
from app.db import get_cursor
from app.repo import BadRequest, NotFound
from app.routers.pages import FLASH_COOKIE, ProjectDep

project_router = APIRouter(prefix="/{slug}")


def _account(project: dict) -> dict:
    if not tree.is_runner_home(project):
        raise NotFound("runners belong to an account")
    return project


def _only_mine(request: Request, user: dict) -> int | None:
    """None for the owner (every runner of the account); a person's own id for anyone else (#713)."""
    return None if access.of_request(request).is_owner else user["id"]


def _user(request: Request) -> dict:
    """The signed-in person's users row (made if their sign-in predates the table). 403 without a web session."""
    try:
        return web_auth.signed_in_user_row(request)
    except PermissionError as exc:
        raise HTTPException(403, f"runners are managed by a signed-in person ({exc})") from None


def _page(request: Request, account: dict, code: dict | None = None, error: str = "", status: int = 200):
    from app.routers import machines_pages        # lazy: machines_pages imports this module's helpers
    return machines_pages.render_page(request, account, "jobs", status=status, code=code, error=error)


@project_router.get("/runners", response_class=HTMLResponse)
def runners_page(request: Request, project: dict = ProjectDep):
    """Old links keep working: the runners are the Machines page's "Runs jobs" role (#810)."""
    account = _account(project)
    _user(request)
    return RedirectResponse(f"/{account['slug']}/machines?tab=runners", status_code=302)


@project_router.post("/runners/codes", response_class=HTMLResponse, dependencies=[Depends(access.require_fresh())])  # a runner code enrols a machine that runs this account's jobs (Fable #809)
def new_code(request: Request, project: dict = ProjectDep, mode: str = Form(""), where: str = Form("auto")):
    account = _account(project)
    user = _user(request)
    try:
        code = devices.issue_code(user, account, mode, where)
    except BadRequest as exc:
        return _page(request, account, error=str(exc), status=400)
    except runner_identity.IdentityUnavailable as exc:
        return _page(request, account, error=str(exc), status=503)
    return _page(request, account, code=code)   # rendered here, not redirected: the code is shown once


@project_router.post("/runners/{device_id:int}/revoke")
def revoke_device(request: Request, device_id: int, project: dict = ProjectDep):
    account = _account(project)
    user = _user(request)
    devices.revoke(account["id"], device_id, f"user:{user['id']}", user_id=_only_mine(request, user))
    resp = RedirectResponse(f"/{account['slug']}/machines?tab=runners", status_code=303)
    # A fixed ASCII flash: a runner's name (any script) in a cookie header would break the response after the revoke committed.
    resp.set_cookie(FLASH_COOKIE, "Runner revoked.", max_age=60, samesite="lax", httponly=True)
    return resp


@project_router.post("/runners/{device_id:int}/labels")
def set_device_labels(request: Request, device_id: int, project: dict = ProjectDep, labels: str = Form(""), add: str = Form("")):
    """The signed-in owner sets a device's labels (e.g. android-usb). The runner itself can never set them. `add` is the tag box's
    new label: it is appended to `labels` (the current set), so each chip's x and the box both save at once, no Save button (#810)."""
    labels = ", ".join(x for x in (labels.strip(), add.strip()) if x)
    account = _account(project)
    user = _user(request)
    try:
        saved = devices.set_labels(account["id"], device_id, labels, f"user:{user['id']}", user_id=_only_mine(request, user))
    except BadRequest as exc:
        return _page(request, account, error=str(exc), status=400)
    resp = RedirectResponse(f"/{account['slug']}/machines?tab=runners", status_code=303)
    resp.set_cookie(FLASH_COOKIE, "Labels saved." if saved else "Labels cleared.", max_age=60, samesite="lax", httponly=True)
    return resp


@project_router.post("/runners/locks/clear")
def clear_lock(request: Request, project: dict = ProjectDep, kind: str = Form(""), resource: str = Form("")):
    """A person clears an ORPHANED deploy/migrate lock (a job died holding it; nothing retries it)."""
    account = _account(project)
    user = _user(request)
    access.require_owner(access.of_request(request))      # a lock may guard a deploy: the owner clears it (#713)
    jobs.clear_orphan(account["id"], kind, resource, f"user:{user['id']}")
    resp = RedirectResponse(f"/{account['slug']}/machines?tab=runners", status_code=303)
    resp.set_cookie(FLASH_COOKIE, "Lock cleared.", max_age=60, samesite="lax", httponly=True)
    return resp
