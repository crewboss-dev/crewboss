"""The project's Logic tab (task #715, change #67): /<slug>/logic. The app's workflows and AI processes as a picture you can open step by
step, what the code does that the map does not cover (UNCOVERED), what the map names that is gone (STALE), the release gate and its
one-release override, and /<slug>/logic/diff, what a commit changed in the logic. The map comes from the commit's report
(app/logic.py); the rules are app/logic_map.py. Reads: anyone who may see the project. The gate switch and the override: a project Admin
(fresh second factor)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import access, logic, logic_map
from app.repo import BadRequest, Conflict, Forbidden, NotFound
from app.routers.pages import FLASH_COOKIE, ProjectDep, render

project_router = APIRouter(prefix="/{slug}")
UNCOVERED_SHOWN = 60


def _back(project: dict, message: str, path: str = "logic"):
    resp = RedirectResponse(f"/{project['slug']}/{path}", status_code=303)
    resp.set_cookie(FLASH_COOKIE, message, max_age=60, samesite="lax", httponly=True)    # fixed text only
    return resp


def _by_kind(items: list[dict]) -> dict:
    out: dict = {}
    for u in items:
        out[u["kind"]] = out.get(u["kind"], 0) + 1
    return out


def _page(request: Request, project: dict, error: str = "", status: int = 200):
    who = access.of_request(request)
    rep = logic.latest_report(project["id"])
    q = request.query_params
    step = q.get("step") or ""
    unreadable = False
    try:
        workflows = rep["workflows"] if rep else []
        wf_id = q.get("wf") or (workflows[0]["id"] if workflows else "")
        wf = next((w for w in workflows if w["id"] == wf_id), None)
        picture = logic_map.layout(wf) if wf else None
        steps = {s["id"]: s for s in wf["steps"]} if wf else {}
        uncovered = list(rep["uncovered"]) if rep else []
        _ = [u["key"] for u in uncovered[:UNCOVERED_SHOWN]]
        _ = [(w["id"], w["name"], w["trigger"]["type"], len(w["steps"])) for w in workflows]
    except (KeyError, TypeError, AttributeError, ValueError, IndexError):
        unreadable, workflows, wf, picture, steps, uncovered = True, [], None, None, {}, []
    gate = project.get("logic_gate") or "off"
    verdict = None
    open_override = None
    if rep:
        from app.db import get_cursor
        with get_cursor(commit=False) as cur:
            verdict = logic.evaluate(cur, project["id"], rep["sha"])
            open_override = logic.open_override(cur, project["id"], rep["sha"])
    resp = render(request, "pages/logic.html", "logic", rep=rep, workflows=workflows, wf=wf, picture=picture, steps=steps, step_sel=step,
                  gate=gate, verdict=verdict, open_override=open_override, overrides=logic.list_overrides(project["id"]),
                  uncovered=uncovered[:UNCOVERED_SHOWN], uncovered_total=len(uncovered), uncovered_kinds=_by_kind(uncovered),
                  reports=logic.list_reports(project["id"], 8), is_admin=access.is_admin(who, project["id"]), error=error, unreadable=unreadable, reporter=logic.reporter_label(rep['reported_by']) if rep else '',
                  new_keys={u["key"] for u in (verdict["new_uncovered"] if verdict else [])})
    resp.status_code = status
    return resp


@project_router.get("/logic", response_class=HTMLResponse)
def logic_page(request: Request, project: dict = ProjectDep):
    return _page(request, project)


@project_router.get("/logic/diff", response_class=HTMLResponse)
def logic_diff_page(request: Request, sha: str = "", project: dict = ProjectDep):
    sha = (sha or "").strip().lower()
    if not sha:
        rep = logic.latest_report(project["id"])
        sha = rep["sha"] if rep else ""
    res = logic.release_diff(project["id"], sha) if sha else None
    return render(request, "pages/logic_diff.html", "logic", res=res, sha=sha)


@project_router.post("/logic/gate", dependencies=[Depends(access.require_fresh())])
def set_gate(request: Request, mode: str = Form(...), project: dict = ProjectDep):
    try:
        logic.set_gate(access.of_request(request), project, mode)
    except (BadRequest, Forbidden) as exc:
        return _page(request, project, error=str(exc), status=400 if isinstance(exc, BadRequest) else 403)
    return _back(project, "Logic map gate is enforced." if mode == "enforce" else "Logic map gate is off.")


@project_router.post("/logic/override", dependencies=[Depends(access.require_fresh())])
def grant_override(request: Request, sha: str = Form(...), reason: str = Form(""), project: dict = ProjectDep):
    try:
        logic.grant_override(access.of_request(request), project, sha, reason)
    except (BadRequest, Conflict) as exc:
        return _page(request, project, error=str(exc), status=400)
    return _back(project, "Override recorded for one release.")


@project_router.post("/logic/override/{override_id:int}/revoke", dependencies=[Depends(access.require_fresh())])
def revoke_override(request: Request, override_id: int, project: dict = ProjectDep):
    try:
        logic.revoke_override(access.of_request(request), project, override_id)
    except NotFound as exc:
        return _page(request, project, error=str(exc), status=404)
    return _back(project, "Override withdrawn.")
