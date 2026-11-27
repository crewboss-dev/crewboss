"""Servers page (task #300, reshaped by #648 / change #52): /<account>/servers. The page is the account's DEPLOY TARGETS:
each commissioned server with its deploy targets under it, tagged dev/prod (target) and internal/external (server).
The old estate stub (backups, reboots, Zabbix: app/servers.py) is no longer drawn here: monitoring belongs to Tech
Assist, and this page covers access and deploying code only (#810). Accounts only — a server can host many projects.

Deploy targets belong to the PROJECT (#810): they are added and edited on its Releases tab, and a machine here only lists them
read-only ("Used by").

Server access (task #367): the owner adds a server here (account fixed to the page's), pastes the one line it shows,
and runs Check access; Remove forgets it (app/server_access.py). Owner browser writes like every page route (actor
'owner', same-origin guard); a server id of another account is a 404 like a missing one."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from app import access, deploys, server_access, tree
from app.repo import BadRequest, Conflict, NotFound
from app.fold import flash_header, is_htmx
from app.routers.pages import FLASH_COOKIE, ProjectDep, fold_or_redirect, render

project_router = APIRouter(prefix="/{slug}")


def _account(project: dict) -> dict:
    if not tree.is_account(project):
        raise NotFound("servers belong to an account")
    return project


def _page(request: Request, project: dict, error: str = "", form: dict | None = None, status: int = 200):
    from app.routers import machines_pages        # lazy: machines_pages imports runners_pages
    return machines_pages.render_page(request, project, "deploys", status=status, add_error=error, add_form=form)


@project_router.get("/servers", response_class=HTMLResponse)
def servers_page(request: Request, project: dict = ProjectDep):
    """Old links keep working: the servers are the Machines page's "Receives deploys" role (#810)."""
    return RedirectResponse(f"/{_account(project)['slug']}/machines?tab=servers", status_code=302)


@project_router.post("/servers", response_class=HTMLResponse, dependencies=[Depends(access.require_fresh())])
def add_server(request: Request, project: dict = ProjectDep, name: str = Form(""), address: str = Form(""),
               port: str = Form("22"), login_user: str = Form(""), via_server_id: str = Form("")):
    account = _account(project)
    form = {"name": name, "address": address, "port": port, "login_user": login_user,
            "via_server_id": via_server_id}
    try:
        row = server_access.add(account, name, address, port, login_user, "owner", via_server_id)
    except (BadRequest, Conflict) as exc:
        return _page(request, account, error=str(exc), form=form, status=400)
    return RedirectResponse(f"/{account['slug']}/machines?tab=servers&added={row['id']}#cs-{row['id']}", status_code=303)


@project_router.post("/servers/{server_id:int}/check")
def check_server(server_id: int, project: dict = ProjectDep):
    account = _account(project)
    row = server_access.check(account, server_id)
    return RedirectResponse(f"/{account['slug']}/machines?tab=servers&checked={row['id']}#cs-{row['id']}", status_code=303)


@project_router.post("/servers/{server_id:int}/via", response_class=HTMLResponse, dependencies=[Depends(access.require_fresh())])
def set_server_via(request: Request, server_id: int, project: dict = ProjectDep, via_server_id: str = Form("")):
    """Set or clear the server's "Connect via" jump host (owner-only like the rest of this page)."""
    account = _account(project)
    try:
        server_access.set_via(account["id"], server_id, via_server_id)
    except BadRequest as exc:
        return _page(request, account, error=str(exc), status=400)
    return RedirectResponse(f"/{account['slug']}/machines?tab=servers&checked={server_id}#cs-{server_id}", status_code=303)


@project_router.post("/servers/{server_id:int}/session-passes")
def set_server_session_passes(request: Request, server_id: int, project: dict = ProjectDep, allow: str = Form("0")):
    """The per-server switch "AI sessions may open a shell here" (OFF by default). Owner-only like the rest of this page (the first
    path segment is `servers`). Turning it ON needs a fresh passkey tap; turning it OFF never does."""
    account = _account(project)
    on = allow == "1"
    if on:
        access.require_fresh(access.STEPUP_SHORT_SECONDS)(request)
    row = server_access.set_session_passes(account["id"], server_id, on)
    return _back(account, f"AI sessions can now open a one-hour shell on {row['name']} without asking you." if on
                 else f"AI sessions can no longer open a shell on {row['name']} without you.", server_id)


def _back(account: dict, message: str, server_id: int | None = None):
    resp = RedirectResponse(f"/{account['slug']}/machines?tab=servers" + (f"#cs-{server_id}" if server_id else ""), status_code=303)
    resp.set_cookie(FLASH_COOKIE, message, max_age=60, samesite="lax", httponly=True)   # fixed ASCII text only
    return resp


@project_router.post("/servers/{server_id:int}/remove", dependencies=[Depends(access.require_fresh())])
def remove_server(request: Request, server_id: int, project: dict = ProjectDep):
    account = _account(project)
    try:
        row = server_access.remove(account["id"], server_id)
    except Conflict as exc:
        if is_htmx(request):   # refused: the card stays, the reason is a calm toast
            return PlainTextResponse(str(exc), status_code=409, headers={**flash_header(str(exc)), "HX-Reswap": "none"})
        resp = RedirectResponse(f"/{account['slug']}/machines?tab=servers#cs-{server_id}", status_code=303)
        resp.set_cookie(FLASH_COOKIE, str(exc), max_age=60, samesite="lax", httponly=True)
        return resp
    # The card folds away in place (#582); without JS: the 303 back with the same flash.
    return fold_or_redirect(request, f"Removed {row['name']}. To revoke the dashboard's access, delete the "
                            f"crewboss line (or the mallen-dash line, if it was pasted before the rename) for srv-{row['id']} from ~{row['login_user']}/.ssh/authorized_keys "
                            "on the server.", f"/{account['slug']}/machines?tab=servers")


# --- Manifest & backups (#726) --------------------------------------------------------------------------------------
# What is deployed on the server and what must be backed up, generated from each app's recipe (app/manifest.py). Owner-only like the rest of
# this page (the first path segment is `servers`). A read token for the API is shown ONCE, here, when it is made.

def _manifest_page(request: Request, account: dict, server_id: int, new_token: str = "", status: int = 200):
    from app import manifest
    row = server_access.get(account["id"], server_id)
    st = manifest.status(server_id)
    shown = st["stored"] or {"manifest": st["fresh"], "skill": manifest.skill_md(st["fresh"]), "content_hash": "", "generated_at": None,
                             "pushed_at": None, "push_error": ""}
    resp = render(request, "pages/manifest.html", "machines", server=row, st=st, m=shown["manifest"], skill=shown["skill"], stored=st["stored"],
                  tokens=manifest.list_tokens(server_id), new_token=new_token, webhook=manifest.webhook_configured())
    resp.status_code = status
    if new_token:
        resp.headers["Cache-Control"] = "no-store"        # the token is on this response only
    return resp


@project_router.get("/servers/{server_id:int}/manifest", response_class=HTMLResponse)
def manifest_page(request: Request, server_id: int, project: dict = ProjectDep):
    return _manifest_page(request, _account(project), server_id)


@project_router.get("/servers/{server_id:int}/manifest/file")
def manifest_file(server_id: int, kind: str = "json", project: dict = ProjectDep):
    """Download the STORED manifest (generated first if none exists): kind yaml, json or skill (Markdown)."""
    import json
    from app import manifest
    account = _account(project)
    name = server_access.get(account["id"], server_id)["name"]
    cur = manifest.current(server_id)
    if kind == "yaml":
        body, mt, fn = manifest.to_yaml(cur["manifest"]), "application/yaml", f"{name}-manifest.yaml"
    elif kind == "skill":
        body, mt, fn = cur["skill"], "text/markdown", f"server-{name}.md"
    elif kind == "json":
        body, mt, fn = json.dumps(cur["manifest"], indent=2, default=str), "application/json", f"{name}-manifest.json"
    else:
        raise BadRequest("kind: yaml, json or skill")
    return PlainTextResponse(body, media_type=mt, headers={"Content-Disposition": f'attachment; filename="{fn}"'})


@project_router.post("/servers/{server_id:int}/manifest/regenerate", dependencies=[Depends(access.require_fresh())])
def manifest_regenerate(server_id: int, project: dict = ProjectDep):
    from app import manifest
    account = _account(project)
    server_access.get(account["id"], server_id)
    manifest.generate(server_id, "on demand")
    return RedirectResponse(f"/{account['slug']}/servers/{server_id}/manifest", status_code=303)


@project_router.post("/servers/{server_id:int}/manifest/tokens", response_class=HTMLResponse, dependencies=[Depends(access.require_fresh())])
def manifest_token_create(request: Request, server_id: int, project: dict = ProjectDep, label: str = Form("")):
    from app import manifest
    account = _account(project)
    server_access.get(account["id"], server_id)
    token = manifest.create_token(server_id, label, "owner")
    return _manifest_page(request, account, server_id, new_token=token)


@project_router.post("/servers/{server_id:int}/manifest/tokens/{token_id:int}/revoke", dependencies=[Depends(access.require_fresh())])
def manifest_token_revoke(server_id: int, token_id: int, project: dict = ProjectDep):
    from app import manifest
    account = _account(project)
    server_access.get(account["id"], server_id)
    manifest.revoke_token(server_id, token_id)
    return RedirectResponse(f"/{account['slug']}/servers/{server_id}/manifest", status_code=303)
