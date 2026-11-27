"""Knowledge + Objectives pages (knowledge-v2) and the knowledge image media route.

Owner browser writes, like every page route: no Authorization header (NetBird gates the network), actor
is always "owner", and the same-origin guard (app/origin_guard.py) refuses cross-site POSTs — including
the image upload. Every write goes through app.repo, so the outbox rule fires exactly once: an
"Ask AI to change…" request reaches its session as 'Owner assigned task #N to AI', never as the words
the owner typed.

Every page and write lives under its project, /<slug>/knowledge and /<slug>/objectives (project_router,
ProjectDep: the project comes from the URL, an entry of another project is a 404). Templates build every URL
from `base`. Only the image media route, /media/knowledge/<id>.<ext>, is global (router).
"""
from __future__ import annotations

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app import access, knowledge_graph, repo
from app import shared as shared_mod
from app import tree
from app.repo import BadRequest, NotFound
from app.routers.pages import ProjectDep, render
from app.templating import templates

router = APIRouter()                           # global: /media/knowledge/…
project_router = APIRouter(prefix="/{slug}")   # /<slug>/knowledge, /<slug>/objectives

KNOWLEDGE_KINDS = ("decision", "responsibility", "fact", "note", "link")  # objectives have their own page
# An account's shared rules and AI skills (task #148) are knowledge rows too, shown on its Rules / AI Skills pages.
SHARED_KINDS = repo.SHARED_ONLY_KINDS
NOT_ON_KNOWLEDGE_PAGE = ("objective", *SHARED_KINDS)
_STREAMS = ("tweak", "feature", "bug", "review")


def _entry_in_project(project: dict, knowledge_id: int) -> tuple[dict, dict]:
    entry = repo.get_knowledge(knowledge_id)
    if entry["project_id"] != project["id"]:
        raise NotFound(f"knowledge entry {knowledge_id} is not in {project['name']}")
    return project, entry


def _home(project: dict, entry_or_kind) -> str:
    kind = entry_or_kind if isinstance(entry_or_kind, str) else entry_or_kind["kind"]
    return f"/{project['slug']}" + ({"objective": "/objectives", "rule": "/rules", "skill": "/ai-features"}.get(kind)
                                     or "/knowledge")


def _kinds_for(project: dict) -> tuple:
    """The kinds a NEW entry may take here: an account also authors shared rules and AI skills."""
    return (*KNOWLEDGE_KINDS, "objective", *(SHARED_KINDS if tree.is_account(project) else ()))


def _open_url(project: dict, entry: dict, **extra) -> str:
    from urllib.parse import urlencode

    return f"{_home(project, entry)}?{urlencode({'open': entry['id'], **extra})}#k-{entry['id']}"


def _float_or_none(value: str | None):
    if value is None or not str(value).strip():
        return None
    try:
        return float(str(value).replace(",", "").replace("$", "").strip())
    except ValueError as exc:
        raise BadRequest("target value must be a number") from exc


# =================================================================================================
# list pages
# =================================================================================================

@project_router.get("/knowledge", response_class=HTMLResponse)
def knowledge_page(request: Request, project: dict = ProjectDep):
    q = request.query_params
    kind = q.get("kind") if q.get("kind") in KNOWLEDGE_KINDS else None
    tag = q.get("tag") or q.get("subproject") or None
    search = (q.get("q") or "").strip() or None
    needs_review = q.get("needs_review") == "1"
    show_archived = q.get("archived") == "1"
    open_id = int(q["open"]) if (q.get("open") or "").isdigit() else None
    # ?view=graph (task #550): the same page with the link graph + housekeeping lens in place of the card list.
    view = "graph" if q.get("view") == "graph" else "list"
    graph = knowledge_graph.build(project) if (view == "graph" or open_id) else None
    entries = repo.list_knowledge(project["id"], kind=kind, subproject=tag,
                                   needs_review=True if needs_review else None, q=search,
                                   include_archived=show_archived, exclude_kinds=NOT_ON_KNOWLEDGE_PAGE)
    # A project inherits its account's shared knowledge (read-only here); an account's own entries ARE shared.
    inherited = [] if tree.is_account(project) else shared_mod.inherited(project, access.filtered_index(access.of_request(request)))
    return render(request, "pages/knowledge.html", "knowledge", entries=entries, inherited=inherited,
                  counts=repo.knowledge_counts(project["id"], exclude_kinds=NOT_ON_KNOWLEDGE_PAGE),
                  tags=repo.list_subprojects(project["id"]), kind=kind or "", tag=tag or "",
                  search=search or "", needs_review=needs_review, show_archived=show_archived,
                  open_id=open_id, kinds=KNOWLEDGE_KINDS, requests=repo.open_knowledge_requests(project["id"]),
                  asked=_asked(q), view=view, graph=graph if view == "graph" else None,
                  housekeeping=knowledge_graph.housekeeping_rows(graph) if view == "graph" else None,
                  linked_from=knowledge_graph.linked_from(graph, open_id) if open_id else None)


@project_router.get("/objectives", response_class=HTMLResponse)
def objectives_page(request: Request, project: dict = ProjectDep):
    q = request.query_params
    show_archived = q.get("archived") == "1"
    open_id = int(q["open"]) if (q.get("open") or "").isdigit() else None
    objectives = repo.list_knowledge(project["id"], kind="objective", include_archived=show_archived)
    for o in objectives:
        o["progress"] = repo.objective_progress(o)
    archived_count = repo.list_knowledge(project["id"], kind="objective", archived_only=True)
    return render(request, "pages/objectives.html", "objectives", objectives=objectives,
                  show_archived=show_archived, open_id=open_id, archived_count=len(archived_count),
                  requests=repo.open_knowledge_requests(project["id"]), asked=_asked(q))


@project_router.get("/knowledge/{knowledge_id:int}/body", response_class=HTMLResponse)
def knowledge_body(request: Request, knowledge_id: int, project: dict = ProjectDep):
    """The expanded part of a card (body + actions), fetched the first time the card opens. It ends with the card's
    backlinks ("Linked from", task #550), read from the cached link graph."""
    _, entry = _entry_in_project(project, knowledge_id)
    linked = knowledge_graph.linked_from(knowledge_graph.build(project), knowledge_id)
    return templates.TemplateResponse(request, "partials/knowledge_body.html", {"e": entry, "linked_from": linked})


@project_router.get("/knowledge/{knowledge_id:int}", response_class=HTMLResponse)
def knowledge_entry_page(request: Request, knowledge_id: int, project: dict = ProjectDep):
    """A knowledge entry's FULL PAGE (#816): its own URL, so a link (the assistant's) opens it without the list's expanding card."""
    _, entry = _entry_in_project(project, knowledge_id)
    linked = knowledge_graph.linked_from(knowledge_graph.build(project), knowledge_id)
    return render(request, "pages/knowledge_page.html", "objectives" if entry["kind"] == "objective" else "knowledge",
                  e=entry, linked_from=linked, back=_home(project, entry))


def _asked(q) -> int | None:
    """?asked=<task id> after an Ask AI request: the opened card confirms it with a link to the task."""
    return int(q["asked"]) if (q.get("asked") or "").isdigit() else None


# =================================================================================================
# editor (new + edit)
# =================================================================================================

def _editor(request: Request, project: dict, entry: dict | None, kind: str, draft: dict | None = None,
            error: str | None = None):
    """The editor page. On a refused save it is re-rendered with what the owner typed (`draft`) and the
    reason, so a long body is never lost to a validation error."""
    nav = {"objective": "objectives", "rule": "rules", "skill": "ai_features"}.get(kind, "knowledge")
    resp = render(request, "pages/knowledge_edit.html", nav,
                  e=entry, kind=kind, kinds=KNOWLEDGE_KINDS, tags=repo.list_subprojects(project["id"]),
                  back=(_open_url(project, entry) if entry else _home(project, kind)), draft=draft or {}, error=error)
    if error:
        resp.status_code = 400
    return resp


@project_router.get("/knowledge/new", response_class=HTMLResponse)
def new_knowledge_page(request: Request, project: dict = ProjectDep):
    kind = request.query_params.get("kind") or "note"
    if kind not in _kinds_for(project):
        kind = "note"
    return _editor(request, project, None, kind)


@project_router.get("/knowledge/{knowledge_id:int}/edit", response_class=HTMLResponse)
def edit_knowledge_page(request: Request, knowledge_id: int, project: dict = ProjectDep):
    _, entry = _entry_in_project(project, knowledge_id)
    return _editor(request, project, entry, entry["kind"])


def _form_fields(form) -> dict:
    return {
        "title": form.get("title") or "",
        "summary": form.get("summary") or "",
        "body": (form.get("body") or "").replace("\r\n", "\n"),
        "source": (form.get("source") or "").strip()[:300],
        "subprojects": [t for t in form.getlist("tags") if t],
        "target_value": form.get("target_value") or "",
        "target_period": form.get("target_period") or "",
        "target_unit": form.get("target_unit") or "",
        "kind": form.get("kind") or "",
    }


@project_router.post("/knowledge")
async def create_knowledge_form(request: Request, project: dict = ProjectDep):
    form = await request.form()
    kind = form.get("kind") if form.get("kind") in _kinds_for(project) else "note"
    fields = _form_fields(form)
    try:
        target = {}
        if kind == "objective":
            target = {"target_value": _float_or_none(fields["target_value"]),
                      "target_period": fields["target_period"] or None,
                      "target_unit": fields["target_unit"] or None}
        entry = repo.create_knowledge(project["id"], kind, fields["title"], fields["body"],
                                      fields["subprojects"], fields["source"], False, "owner",
                                      summary=fields["summary"], **target)
    except BadRequest as exc:
        return _editor(request, project, None, kind, draft=fields, error=str(exc))
    return RedirectResponse(_open_url(project, entry), status_code=303)


@project_router.post("/objectives")
async def create_objective_form(request: Request, project: dict = ProjectDep):
    """Kept for old bookmarks/forms: the editor posts to /knowledge with kind=objective. A refused value
    comes back as the usual flash (pages.handle_repo_error)."""
    form = await request.form()
    fields = _form_fields(form)
    entry = repo.create_knowledge(project["id"], "objective", fields["title"], fields["body"], [], "", False,
                                  "owner", target_value=_float_or_none(fields["target_value"]),
                                  target_period=fields["target_period"] or None, summary=fields["summary"],
                                  target_unit=fields["target_unit"] or None)
    return RedirectResponse(_open_url(project, entry), status_code=303)


@project_router.post("/knowledge/{knowledge_id:int}")
async def update_knowledge_form(request: Request, knowledge_id: int, project: dict = ProjectDep):
    """The owner's own edit from the editor. Key-presence gated: a field the form didn't send is left
    alone (absent is not empty)."""
    _, entry = _entry_in_project(project, knowledge_id)
    form = await request.form()
    patch: dict = {}
    for key in ("title", "summary"):
        if key in form:
            patch[key] = form.get(key) or ""
    if "body" in form:
        patch["body"] = (form.get("body") or "").replace("\r\n", "\n")
    if "source" in form:
        patch["source"] = (form.get("source") or "").strip()[:300]
    if "tags_present" in form:
        patch["subprojects"] = [t for t in form.getlist("tags") if t]
    if "kind" in form and entry["kind"] != "objective" and form.get("kind") in KNOWLEDGE_KINDS:
        patch["kind"] = form.get("kind")
    try:
        if entry["kind"] == "objective":
            if "target_value" in form:
                patch["target_value"] = _float_or_none(form.get("target_value"))
            for key in ("target_period", "target_unit"):
                if key in form:
                    patch[key] = form.get(key) or None
        repo.update_knowledge(knowledge_id, patch, "owner")
    except BadRequest as exc:
        return _editor(request, project, entry, entry["kind"], draft=_form_fields(form), error=str(exc))
    return RedirectResponse(_open_url(project, entry), status_code=303)


@project_router.post("/knowledge/{knowledge_id:int}/reviewed")
def mark_knowledge_reviewed(request: Request, knowledge_id: int, project: dict = ProjectDep):
    _, entry = _entry_in_project(project, knowledge_id)
    repo.update_knowledge(knowledge_id, {"needs_review": False}, "owner")
    return RedirectResponse(_open_url(project, entry), status_code=303)


@project_router.post("/knowledge/{knowledge_id:int}/restore")
def restore_knowledge_form(request: Request, knowledge_id: int, project: dict = ProjectDep):
    _, entry = _entry_in_project(project, knowledge_id)
    repo.restore_knowledge(knowledge_id, "owner")
    return RedirectResponse(_open_url(project, entry), status_code=303)


# =================================================================================================
# Ask AI to change… / Ask AI to remove -> a request task for the session
# =================================================================================================

@project_router.post("/knowledge/{knowledge_id:int}/ask")
def ask_ai_about_knowledge(request: Request, knowledge_id: int, mode: str = Form(...), text: str = Form(""),
                           stream: str = Form("tweak"), project: dict = ProjectDep):
    _, entry = _entry_in_project(project, knowledge_id)
    if stream not in _STREAMS:
        raise BadRequest(f"stream must be one of {_STREAMS}")
    task = repo.request_knowledge_change(knowledge_id, mode, text, "owner", stream=stream)
    return RedirectResponse(_open_url(project, entry, asked=task["id"]), status_code=303)


# =================================================================================================
# images: upload (editor) + same-origin media
# =================================================================================================

@project_router.post("/knowledge/images")
async def upload_knowledge_image(request: Request, file: UploadFile = File(...), project: dict = ProjectDep):
    """Editor upload (toolbar button, paste or drop). Returns the image's /media URL + size for the
    editor to insert. Errors come back as JSON {detail} with a 4xx the editor shows as a toast."""
    raw = await file.read(repo.IMAGE_MAX_UPLOAD_BYTES + 1)
    try:
        image = repo.save_knowledge_image(project["id"], raw, "owner")
    except BadRequest as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    return JSONResponse({"id": image["id"], "url": image["url"], "width": image["width"],
                         "height": image["height"]}, status_code=201)


_EXT_TYPE = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}


@router.get("/media/knowledge/{image_id:int}.{ext}")
def knowledge_image(request: Request, image_id: int, ext: str):
    image = repo.get_knowledge_image(image_id)
    if _EXT_TYPE.get(ext) != image["content_type"] or not access.can_view(access.of_request(request), image["project_id"]):
        raise NotFound("no such image")          # another project's image is the same 404 as a missing one (#713)
    return Response(
        content=bytes(image["data"]),
        media_type=image["content_type"],
        headers={
            # An image, only ever an image: never sniffed as anything else, never a document that runs.
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Cross-Origin-Resource-Policy": "same-origin",
            # Content never changes under an id (a new upload is a new row), so cache hard.
            "Cache-Control": "private, max-age=31536000, immutable",
        },
    )
