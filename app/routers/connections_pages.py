"""Edits to a project's connections (#784): POST /<slug>/connections/<add|note|remove>, from the Settings page's Connections section.
Behind ProjectDep (a project you can't see is a 404; a write needs the right to change it) and the `manage` policy (pages.SLUG_MANAGE:
the owner or the project's creator); app/connections.py then needs can_manage on the subject, view of the other end, and manage of the
host for part_of. A refusal is a flash (pages.handle_repo_error), never a 500."""
from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from app import access, connections
from app.routers.pages import FLASH_COOKIE, ProjectDep

project_router = APIRouter(prefix="/{slug}")


def _back(project: dict, message: str) -> RedirectResponse:
    resp = RedirectResponse(f"/{project['slug']}/settings#connections", status_code=303)
    resp.set_cookie(FLASH_COOKIE, message, max_age=60, samesite="lax", httponly=True)
    return resp


@project_router.post("/connections/add", include_in_schema=False)
def add_connection(request: Request, kind: str = Form(""), other: str = Form(""), note: str = Form(""), project: dict = ProjectDep):
    connections.add(access.of_request(request), project, kind.strip(), other, note, request=request)
    return _back(project, "Connection added.")


@project_router.post("/connections/note", include_in_schema=False)
def note_connection(request: Request, id: str = Form(""), note: str = Form(""), project: dict = ProjectDep):
    connections.set_note(access.of_request(request), project, id, note, request=request)
    return _back(project, "Note saved.")


@project_router.post("/connections/remove", include_in_schema=False)
def remove_connection(request: Request, id: str = Form(""), project: dict = ProjectDep):
    connections.remove(access.of_request(request), project, id, request=request)
    return _back(project, "Connection removed.")
