"""The project's Environment tab (#810 step 6): /<slug>/environment. What a machine needs installed to build this project (rows: kind, name, minimum
version, OS, install hint), "Suggest from the repo", and the per-runner check results with "Check again". The rules are app/environment.py; the
checks are run by the runner's own built-in probes (this page never sends a command). Reads: anyone who may see the project. Writes (rows,
suggest): need a project ADMIN or Owner, not a Member, because the rows are what runs on other people's PCs (a file path a Windows runner
touches, a program it looks for). "Check again" needs the signed-in person's own runner."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import access, environment, runner_ws, runs_on, tree, web_auth
from app.repo import BadRequest, Forbidden
from app.routers.pages import FLASH_COOKIE, ProjectDep, render

project_router = APIRouter(prefix="/{slug}")


def _user_id(request: Request) -> int | None:
    if not getattr(request.state, "web_user", None):
        return None
    try:
        return web_auth.signed_in_user_row(request)["id"]
    except PermissionError:
        return None


def _page(request: Request, project: dict, error: str = "", form: dict | None = None, status: int = 200):
    me = _user_id(request)
    rows = environment.list_rows(project["id"])
    runners = []
    if me is not None and not tree.is_account(project):
        account_id = tree.account_root_id(project["id"])
        pref = runs_on.get_pref(project["id"], me)
        for d in runs_on.my_devices(account_id, me):
            runners.append({**d, "connected": runner_ws.connected(d["id"]), "default": d["id"] == pref,
                            "env": environment.latest(project["id"], d["id"])})
    resp = render(request, "pages/environment.html", "environment", rows=rows, runners=runners, kinds=environment.KINDS,
                  kind_labels=environment.KIND_LABELS, oses=environment.OSES, status_words=environment.STATUS_WORDS,
                  can_edit=access.is_admin(access.of_request(request), project["id"]), error=error, form=form or {},
                  repo_here=environment.repo_dir(project) is not None, max_rows=environment.MAX_ROWS)
    resp.status_code = status
    return resp


def _admin(request: Request, project: dict) -> None:
    if not access.is_admin(access.of_request(request), project["id"]):
        raise Forbidden("only a project Admin or Owner changes the environment: it is what runs on other people's machines")


def _back(project: dict, message: str):
    resp = RedirectResponse(f"/{project['slug']}/environment", status_code=303)
    resp.set_cookie(FLASH_COOKIE, message, max_age=60, samesite="lax", httponly=True)    # fixed text only: never echo input into a header
    return resp


@project_router.get("/environment", response_class=HTMLResponse)
def environment_page(request: Request, project: dict = ProjectDep):
    return _page(request, project)


@project_router.post("/environment/rows", dependencies=[Depends(access.require_fresh())])
def add_row(request: Request, project: dict = ProjectDep, kind: str = Form(""), name: str = Form(""), min_version: str = Form(""),
            os: str = Form("any"), install_hint: str = Form(""), fingerprint: str = Form("")):
    _admin(request, project)
    try:
        environment.add_row(project, kind, name, min_version, os, install_hint, fingerprint, f"user:{_user_id(request) or 'owner'}")
    except BadRequest as exc:
        return _page(request, project, error=str(exc), status=400, form={"kind": kind, "name": name, "min_version": min_version, "os": os,
                                                                           "install_hint": install_hint, "fingerprint": fingerprint})
    return _back(project, "Requirement added.")


@project_router.post("/environment/rows/{row_id:int}", dependencies=[Depends(access.require_fresh())])
def update_row(request: Request, row_id: int, project: dict = ProjectDep, kind: str = Form(""), name: str = Form(""),
               min_version: str = Form(""), os: str = Form("any"), install_hint: str = Form(""), fingerprint: str = Form("")):
    _admin(request, project)
    try:
        environment.update_row(project, row_id, kind, name, min_version, os, install_hint, fingerprint, f"user:{_user_id(request) or 'owner'}")
    except BadRequest as exc:
        return _page(request, project, error=str(exc), status=400)
    return _back(project, "Requirement saved.")


@project_router.post("/environment/rows/{row_id:int}/delete", dependencies=[Depends(access.require_fresh())])
def delete_row(request: Request, row_id: int, project: dict = ProjectDep):
    _admin(request, project)
    environment.delete_row(project, row_id, f"user:{_user_id(request) or 'owner'}")
    return _back(project, "Requirement removed.")


@project_router.post("/environment/suggest", dependencies=[Depends(access.require_fresh())])
def suggest(request: Request, project: dict = ProjectDep):
    _admin(request, project)
    got = environment.suggest(project, f"user:{_user_id(request) or 'owner'}")
    if not got["found"]:
        return _back(project, "This project's repo is not on this server, so nothing could be read. Add the rows by hand.")
    return _back(project, f"Added {got['added']} suggested requirement{'s' if got['added'] != 1 else ''}." if got["added"]
                 else "Nothing new to suggest from the repo.")


@project_router.post("/environment/check")
def check(request: Request, project: dict = ProjectDep, device_id: str = Form("")):
    """"Check again": queue a check on one of the signed-in person's runners (or all of them). The runner does it when it is online."""
    me = _user_id(request)
    if me is None:
        raise BadRequest("sign in as a person to check a runner")
    ids = [int(device_id)] if device_id.strip().isdigit() else None
    queued = environment.request_for_user(project["id"], me, ids)
    return _back(project, "Checking your runner." if queued else "Nothing to check: this project lists no requirements yet, or you have no runner.")
