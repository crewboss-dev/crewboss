"""Approvals page (task #627, P5): /<account>/approvals. Deploy requests that running jobs made (project, target, commit,
which job and device asked) with Approve and Refuse. Deploy targets are managed on each project's Releases tab (#810). A real Entra web session
only (the same gate as /runners: a device token or a job token is not a session and never reaches here, and is refused if it
does); the POSTs are page forms, so the same-origin guard (app/origin_guard.py) covers them like every other page write.
Approving records who and when; the executor (app/deploys.py) does the work."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import access, deploys, server_access
from app.repo import BadRequest, Conflict, NotFound
from app.routers.pages import FLASH_COOKIE, ProjectDep, render
from app.routers.runners_pages import _account, _user

project_router = APIRouter(prefix="/{slug}")


def _page(request: Request, account: dict, error: str = "", status: int = 200):
    resp = render(request, "pages/approvals.html", "approvals", requests=deploys.list_for(account["id"]),
                  targets=deploys.list_targets(account["id"]), servers=server_access.list_for([account["id"]]),
                  error=error)
    resp.status_code = status
    return resp


def _back(account: dict, message: str):
    resp = RedirectResponse(f"/{account['slug']}/approvals", status_code=303)
    resp.set_cookie(FLASH_COOKIE, message, max_age=60, samesite="lax", httponly=True)   # fixed ASCII text only
    return resp


@project_router.get("/approvals", response_class=HTMLResponse)
def approvals_page(request: Request, project: dict = ProjectDep):
    account = _account(project)
    _user(request)
    return _page(request, account)


@project_router.post("/approvals/{request_id:int}/approve", dependencies=[Depends(access.require_fresh())])
def approve(request: Request, request_id: int, project: dict = ProjectDep):
    account = _account(project)
    user = _user(request)
    try:
        deploys.approve_for_account(access.of_request(request), account["id"], request_id, True, user["id"])
    except Conflict:
        return _back(account, "That request was already decided.")
    return _back(account, "Approved. The dashboard will deploy it shortly.")


@project_router.post("/approvals/{request_id:int}/refuse")
def refuse(request: Request, request_id: int, project: dict = ProjectDep):
    account = _account(project)
    user = _user(request)
    try:
        deploys.refuse_for_account(access.of_request(request), account["id"], request_id, user["id"])
    except Conflict:
        return _back(account, "That request was already decided.")
    return _back(account, "Refused.")


@project_router.post("/approvals/{request_id:int}/again")
def ask_again(request: Request, request_id: int, project: dict = ProjectDep):
    """Ask again on a failed, refused or orphaned card: a new request, asked by this person."""
    account = _account(project)
    user = _user(request)
    try:
        deploys.ask_again(account["id"], request_id, user["id"])
    except (Conflict, NotFound) as exc:
        return _back(account, "Could not ask again: that request is not failed or refused, or its target is gone.")
    return _back(account, "Asked again. Approve the new request below.")


@project_router.post("/approvals/request", response_class=HTMLResponse)
def person_request(request: Request, project: dict = ProjectDep, target_id: int = Form(0), sha: str = Form("")):
    """A person asks for a deploy of a commit that is already on the target's branch (e.g. after merging a job's work)."""
    account = _account(project)
    user = _user(request)
    try:
        deploys.request_by_person(account["id"], user["id"], target_id, sha)
    except (BadRequest, NotFound) as exc:
        return _page(request, account, error=str(exc), status=400)
    return _back(account, "Deploy requested. Approve it below.")
