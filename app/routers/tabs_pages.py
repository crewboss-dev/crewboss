"""Edits to a record's tab bar (#773): POST /<slug>/tabs/<action> (add, remove, left, right, rename, default). Behind ProjectDep (a project you can't
see is a 404, a viewer can't write), then app/tabs.require_manage: only the project's owner or an admin. The browser (js/tabs.js) posts with
HX-Request and reloads on success, so a refusal is a plain 4xx with a toast, never a redirect."""
from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse

from app import access, tabs
from app.repo import BadRequest
from app.routers.pages import ProjectDep, tab_keys

project_router = APIRouter(prefix="/{slug}")

ACTIONS = ("add", "remove", "left", "right", "rename", "default")


@project_router.post("/tabs/{action}", include_in_schema=False)
def edit_tab(request: Request, action: str, key: str = Form(""), value: str = Form(""), project: dict = ProjectDep):
    if action not in ACTIONS:
        raise BadRequest("unknown action")
    who = access.of_request(request)
    tabs.require_manage(who, project)
    level = "account" if project["kind"] == "account" else "project"
    wu = getattr(request.state, "web_user", None) or {}
    lay = tabs.edit(project, level, tab_keys(who, project), action, key.strip(), value, wu.get("user_id"), tab_keys(None, project))
    return JSONResponse({"ok": True, **lay}, headers={"Cache-Control": "no-store"})
