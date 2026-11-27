"""Saved filters' endpoints (#772): /all/filters. The caller is always the signed-in person (app/access.py); a project-scoped page's project
must be one they may see (the same 404 a missing one gets). The ids are the person's own: another person's id is a 404."""
from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse

from app import access, repo, saved_filters
from app.repo import NotFound

router = APIRouter()


def _project_id(request: Request, page: str, slug: str) -> int | None:
    if page in saved_filters.GLOBAL_PAGES or not slug:
        return None
    project = repo.get_project_by_slug(slug)
    access.require_view(access.of_request(request), project, f"project {slug!r} not found")
    return project["id"]


def _json(payload, status: int = 200):
    return JSONResponse(payload, status_code=status, headers={"Cache-Control": "no-store"})


@router.get("/all/filters", include_in_schema=False)
def list_filters(request: Request, page: str = "list", project: str = ""):
    who = access.of_request(request)
    return _json({"filters": saved_filters.list_for(who, page, _project_id(request, page, project))})


@router.post("/all/filters", include_in_schema=False)
def save_filter(request: Request, page: str = Form(...), name: str = Form(...), data: str = Form("{}"), project: str = Form(""),
                is_default: str = Form("")):
    who = access.of_request(request)
    row = saved_filters.save(who, page, _project_id(request, page, project), name, data, make_default=is_default == "1")
    return _json({"filter": row}, 201)


@router.post("/all/filters/{filter_id}/rename", include_in_schema=False)
def rename_filter(request: Request, filter_id: int, name: str = Form(...)):
    return _json({"filter": saved_filters.rename(access.of_request(request), filter_id, name)})


@router.post("/all/filters/{filter_id}/default", include_in_schema=False)
def default_filter(request: Request, filter_id: int, on: str = Form("1")):
    return _json({"filter": saved_filters.set_default(access.of_request(request), filter_id, on == "1")})


@router.post("/all/filters/{filter_id}/delete", include_in_schema=False)
def delete_filter(request: Request, filter_id: int):
    saved_filters.delete(access.of_request(request), filter_id)
    return _json({"deleted": filter_id})
