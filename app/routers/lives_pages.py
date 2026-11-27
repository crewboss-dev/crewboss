"""The project's Where it lives page (#839, #726): /<slug>/where. Per stage the server, folder, URL and the shared services on that server (read
from the server manifests), the project's test mode (Browser / Installed / No UI), and the recipe of what the project keeps that must be backed up
(per project, with an override per deploy target). Reads: anyone who may see the project. Writes need the right to change the project (project_from_path
refuses a viewer; the modules check access.can_edit again). Rules: app/lives.py, app/stateful.py."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import access, external_models, lives, server_reads, stateful, web_auth
from app.repo import BadRequest, Conflict, NotFound
from app.routers.pages import FLASH_COOKIE, ProjectDep, render

project_router = APIRouter(prefix="/{slug}")


def _user_id(request: Request) -> int | None:
    if not getattr(request.state, "web_user", None):
        return None
    try:
        return web_auth.signed_in_user_row(request)["id"]
    except PermissionError:
        return None


def _page(request: Request, project: dict, error: str = "", forms: dict | None = None, status: int = 200, open_scope: str = "", read_form: dict | None = None):
    who = access.of_request(request)
    data = lives.view(who, project, _user_id(request))
    recipes = stateful.all_for(project["id"])
    scopes = [{"key": "project", "target_id": None, "label": "All stages (the project's default)", "recipe": recipes.get(None),
               "form": stateful.to_form(recipes.get(None))}]
    for stage in data["stages"]:
        for t in stage["targets"]:
            r = recipes.get(t["id"])
            scopes.append({"key": f"t{t['id']}", "target_id": t["id"], "label": f"{stage['label']}: {t['name']} on {t['server_name']}",
                           "recipe": r, "form": stateful.to_form(r), "override": r is not None})
    for s in scopes:
        if forms and s["key"] in forms:
            s["form"] = forms[s["key"]]
    resp = render(request, "pages/where.html", "where", can_edit=access.can_edit(who, project["id"]), can_manage=stateful.can_manage(who, project), error=error, modes=lives.MODES,
                  mode_labels=lives.MODE_LABELS, mode_help=lives.MODE_HELP, scopes=scopes, engines=stateful.ENGINES, open_scope=open_scope,
                  parts=stateful.parts, require_state=bool(project.get("require_state_declared")),
                  reads=server_reads.servers_view(project), reads_prod=bool(project.get("server_reads_prod")), read_kinds=server_reads.KINDS,
                  read_labels=server_reads.KIND_LABELS, read_help=server_reads.KIND_HELP, read_form=read_form or {},
                  gemini_on=bool(project.get("gemini_allowed")), gemini_usage=external_models.usage(project["id"]), **data)
    resp.status_code = status
    return resp


def _back(project: dict, message: str):
    resp = RedirectResponse(f"/{project['slug']}/where", status_code=303)
    resp.set_cookie(FLASH_COOKIE, message, max_age=60, samesite="lax", httponly=True)    # fixed text only: never echo input into a header
    return resp


@project_router.get("/where", response_class=HTMLResponse)
def where_page(request: Request, project: dict = ProjectDep):
    return _page(request, project)


@project_router.post("/where/test-mode", dependencies=[Depends(access.require_fresh())])
def set_test_mode(request: Request, project: dict = ProjectDep, mode: str = Form(""), channel: str = Form("")):
    try:
        lives.set_test_mode(access.of_request(request), project, mode, channel)
    except BadRequest as exc:
        return _page(request, project, error=str(exc), status=400)
    return _back(project, "Test mode saved.")


@project_router.post("/where/require-state", dependencies=[Depends(access.require_fresh())])
def require_state(request: Request, project: dict = ProjectDep, on: str = Form("")):
    lives.set_require_state(access.of_request(request), project, on == "1")
    return _back(project, "Release check saved.")


@project_router.post("/where/recipe", dependencies=[Depends(access.require_fresh())])
async def save_recipe(request: Request, project: dict = ProjectDep):
    form = await request.form()
    raw_target = str(form.get("target_id") or "").strip()
    target_id = int(raw_target) if raw_target.isdigit() else None
    scope_key = f"t{target_id}" if target_id else "project"
    try:
        stateful.save(access.of_request(request), project, stateful.from_form(form), target_id)
    except NotFound:
        raise
    except BadRequest as exc:
        keep = {k: str(form.get(k) or "") for k in ("databases", "volumes", "upload_folders", "config_files", "secrets", "ports", "domains",
                                                    "health_url", "log_endpoint", "rpo_minutes", "rto_minutes", "notes")}
        keep = {k: ("" if stateful.looks_like_secret(v) else v) for k, v in keep.items()}       # a refused secret is never echoed back
        return _page(request, project, error=str(exc), status=400, forms={scope_key: keep | {"stateless": str(form.get("stateless") or "") == "1"}},
                     open_scope=scope_key)
    return _back(project, "Saved. The server manifest shows it as changed until it is regenerated.")


@project_router.post("/where/recipe/{target_id:int}/remove", dependencies=[Depends(access.require_fresh())])
def remove_override(request: Request, target_id: int, project: dict = ProjectDep):
    stateful.remove_override(access.of_request(request), project, target_id)
    return _back(project, "Override removed: this stage uses the project's default.")


@project_router.post("/where/reads/add", dependencies=[Depends(access.require_fresh())])
def add_read(request: Request, project: dict = ProjectDep, target_id: str = Form(""), name: str = Form(""), kind: str = Form(""),
             spec: str = Form("")):
    try:
        server_reads.add_read(access.of_request(request), project, int(target_id) if target_id.isdigit() else 0, name, kind, spec)
    except (BadRequest, Conflict) as exc:
        return _page(request, project, error=str(exc), status=400, read_form={"target_id": target_id, "name": name, "kind": kind, "spec": spec})
    return _back(project, "Server read added. Install the generated file on the server for it to work.")


@project_router.post("/where/reads/{read_id:int}/remove", dependencies=[Depends(access.require_fresh())])
def remove_read(request: Request, read_id: int, project: dict = ProjectDep):
    server_reads.remove_read(access.of_request(request), project, read_id)
    return _back(project, "Server read removed.")


@project_router.post("/where/reads/prod", dependencies=[Depends(access.require_fresh())])
def reads_prod(request: Request, project: dict = ProjectDep, on: str = Form("")):
    server_reads.set_prod(access.of_request(request), project, on == "1")
    return _back(project, "Production reads saved.")


@project_router.post("/where/gemini", dependencies=[Depends(access.require_fresh())])
def gemini_switch(request: Request, project: dict = ProjectDep, on: str = Form("")):
    external_models.set_allowed(access.of_request(request), project, on == "1")
    return _back(project, "Gemini setting saved.")
