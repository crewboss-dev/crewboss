"""The fixed menu's pages (#733, change #80; #771): Dashboard, Recent, the header search's jump endpoint, the profile menu's Runners and
Servers entry points, and the old Projects / Accounts list URLs. The lists themselves are gone (the live records are the left menu's
Projects and Accounts sections, archived ones behind each section's "Show archived"); /projects and /accounts redirect.

Who sees what: every query goes through app/access.py (filtered_index / visible_ids), as the rest of the pages do."""
from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app import access, favourites, nav, repo, snapshot, tour, tree
from app import recent as recent_mod
from app import search as search_mod
from app.repo import NotFound
from app.routers.pages import _current_project, _saved_default_redirect, partial, render

router = APIRouter()


def _list_url(request: Request, kind: str):
    """The old list URLs (#771; owner 11-12: no list pages, the only list is the left menu). `?open=<slug>` (the old master-detail
    link) goes to that record; anything else goes to the Dashboard. Archived records are the menu section's "Show archived" toggle."""
    slug = request.query_params.get("open", "")
    if slug:
        who = access.of_request(request)
        try:
            project = repo.get_project_by_slug(slug)
        except NotFound:
            project = None
        if project is not None and access.can_view(who, project["id"]):
            return RedirectResponse(nav.landing(project), status_code=302)
    return RedirectResponse("/dashboard", status_code=302)


@router.get("/projects", include_in_schema=False)
def projects_list(request: Request):
    return _list_url(request, "projects")


@router.get("/accounts", include_in_schema=False)
def accounts_list(request: Request):
    return _list_url(request, "accounts")


@router.get("/projects/{slug}/hub", include_in_schema=False)
@router.get("/accounts/{slug}/hub", include_in_schema=False)
def old_hub(request: Request, slug: str):
    """The list's right-hand card is gone (#771): its old fragment URLs go to the record's page (or 404 like a missing one)."""
    project = repo.get_project_by_slug(slug)
    access.require_view(access.of_request(request), project, f"project {slug!r} not found")
    return RedirectResponse(nav.landing(project), status_code=302)


@router.get("/recent", response_class=HTMLResponse)
def recent_page(request: Request, project: str = "", kind: str = "", before: int | None = None):
    """RECENT (#771): what happened across every project the person can see, newest first. `before` pages it (the cursor is the
    last id shown); a request with HX-Request gets only the next rows, which the page appends."""
    who = access.of_request(request)
    if not request.headers.get("HX-Request"):
        redirect = _saved_default_redirect(request, None, "recent", "/recent")
        if redirect:
            return redirect
    kind = kind if kind in recent_mod.KINDS else ""
    idx = access.filtered_index(who)
    slugs = {r["slug"] for r in idx.values()}
    project = project if project in slugs else ""
    page = recent_mod.feed(who, project, kind, before)
    qs = urlencode({k: v for k, v in (("project", project), ("kind", kind)) if v})
    ctx = dict(entries=page["entries"], next_id=page["next"], f_project=project, f_kind=kind, qs=qs)
    if request.headers.get("HX-Request") and before:
        return partial(request, "partials/recent_rows.html", ctx)
    rows = [r for r in tree.project_tree(include_archived=True, idx=idx) if not tree.is_global_root(r)]
    return render(request, "pages/recent.html", "recent", kinds=recent_mod.KINDS, filter_projects=rows, **ctx)


@router.get("/all/jump", include_in_schema=False)
def jump(request: Request, q: str = "", section: str = ""):
    """The header search's results as JSON (#771), scoped to what the person may see (app/search.py). `section` = admin | assets (#778): that
    section's own pages instead of records and tasks."""
    who = access.of_request(request)
    if section in ("admin", "assets"):
        has_account = bool((getattr(request.state, "web_user", None) or {}).get("user_id"))
        return JSONResponse({"results": search_mod.jump_section(who, section, q, has_account)}, headers={"Cache-Control": "no-store"})
    return JSONResponse({"results": search_mod.jump(who, q)}, headers={"Cache-Control": "no-store"})


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_page(request: Request):
    who = access.of_request(request)
    return render(request, "pages/snapshot.html", "dashboard", d=snapshot.snapshot(who), is_owner=who.is_owner)


def _profile_target(request: Request, role: str):
    """Machines are an account's page (/<account>/machines). From the Administration menu there is no account in the URL, so go to the one
    the person was last in (its partition's root), else the first they may use."""
    who = access.of_request(request)
    idx = access.filtered_index(who)
    candidates = []
    cur = _current_project(request)
    if cur is not None:
        root = idx.get(tree.account_root_id(cur["id"], idx))
        if root:
            candidates.append(root)
    tree_rows = tree.project_tree(include_archived=False, idx=idx)
    candidates += [p for p in tree_rows if tree.is_account(p)] + [p for p in tree_rows if not tree.is_account(p)]
    for p in candidates:
        if tree.is_runner_home(p, idx):
            return RedirectResponse(f"/{p['slug']}/machines" + (f"?role={role}" if role else ""), status_code=302)
    raise NotFound("there is no account yet: make one from Accounts")


@router.get("/machines", include_in_schema=False)
def machines_entry(request: Request):
    return _profile_target(request, "")


@router.get("/runners", include_in_schema=False)
def runners_entry(request: Request):
    return _profile_target(request, "jobs")


@router.get("/servers", include_in_schema=False)
def servers_entry(request: Request):
    access.require_owner(access.of_request(request))
    return _profile_target(request, "deploys")


@router.post("/all/favourites", include_in_schema=False)
def toggle_favourite(request: Request, slug: str = Form("")):
    """Star or unstar a project for the signed-in person (#778). Any person who can SEE the project may star it (a viewer too: it is their own
    list, not a change to the project), so this is not behind ProjectDep; an unseen project is a 404."""
    project = repo.get_project_by_slug(slug)
    who = access.of_request(request)
    starred = favourites.toggle(who, project, slug)
    return JSONResponse({"starred": starred, "slug": project["slug"], "name": project["name"], "href": nav.landing(project)}, headers={"Cache-Control": "no-store"})


@router.get("/all/tour", include_in_schema=False)
def tour_state(request: Request):
    """The first-run how-to (#938): this person's progress and the steps, for js/tour.js. Per person, so any signed-in person may read their own."""
    return JSONResponse(tour.state(access.signed_in_person(request)), headers={"Cache-Control": "no-store"})


@router.post("/all/tour", include_in_schema=False)
def tour_save(request: Request, action: str = Form(""), step: str = Form("")):
    """Save the person's progress: action = step | finish | skip | restart (a step id with `step`). Only ever their own row."""
    who = access.signed_in_person(request)
    try:
        return JSONResponse(tour.save(who, action, step), headers={"Cache-Control": "no-store"})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
