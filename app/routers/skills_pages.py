"""The Skills pages (task #718): Assets -> Skills (/assets/skills) is the GLOBAL layer; a project's own Skills list is /<slug>/build-skills
(inherited + its own, each marked with its layer). Both list, create, import (GitHub URL or an uploaded SKILL.md / zip), view,
edit (every save is a new immutable version), compare versions, confirm a licence, archive and copy; a project can also pin a version.

Rights are asked in app/asset_skills.py through app/asset_access.py (global = the owner; account and project = whoever may change that
project; a licence confirm = an admin). A skill nobody may see is the same 404 a missing one gets. Import and licence confirmation need
a fresh sign-in (access.require_fresh): they bring outside text into every job's instructions."""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from app import access, asset_access, asset_resolve, asset_skills, asset_store, config, repo, skill_import, skill_scan, tree
from app.repo import BadRequest, Conflict, NotFound
from app.routers.pages import FLASH_COOKIE, ProjectDep, render

router = APIRouter(prefix="/assets/skills", include_in_schema=False)           # the global layer
project_router = APIRouter(prefix="/{slug}/build-skills", include_in_schema=False)

MAX_UPLOAD = skill_import.MAX_ZIP
MAX_FILE_FORM = config.ASSET_ATTACHMENT_MAX_BYTES
TEXT_PEEK = 20_000


# --- helpers ----------------------------------------------------------------------------------------------------------------

def _num(text) -> int | None:
    """A whole number from a form or query value, or None. ASCII digits only (str.isdigit also accepts '²', which int() refuses)."""
    text = (text or "").strip()
    return int(text) if text.isascii() and text.isdigit() and len(text) < 10 else None


def _prefix(project: dict | None) -> str:
    return "/assets/skills" if project is None else f"/{project['slug']}/build-skills"


def _nav(project: dict | None) -> str:
    return "assets_skills" if project is None else "build_skills"


def _go(project: dict | None, tail: str, message: str = ""):
    resp = RedirectResponse(_prefix(project) + tail, status_code=303)
    if message:
        resp.set_cookie(FLASH_COOKIE, message, max_age=60, samesite="lax", httponly=True)     # fixed text only: never echo input into a header
    return resp


def _targets(p, project: dict | None) -> list[dict]:
    """The 'Install to' choices this person has: [{value, label}]. On a project page that project comes first."""
    out: list[dict] = []
    if project is not None and access.can_edit(p, project["id"]):
        what = "this account (every project in it)" if project["kind"] == "account" else "this project"
        out.append({"value": project["slug"], "label": f"{project['name']}: {what}"})
    if asset_access.can_install_global(p):
        out.append({"value": "global", "label": "Global: every project"})
    if project is None:
        idx = access.filtered_index(p)
        ids = access.editable_ids(p)
        for row in tree.project_tree(include_archived=False, idx=idx):
            if ids is not None and row["id"] not in ids:
                continue
            kind = "account (every project in it)" if row["kind"] == "account" else "project"
            out.append({"value": row["slug"], "label": f"{row['name']}: {kind}"})
            if len(out) >= 300:
                break
    return out


def _install_to(p, value: str, project: dict | None) -> dict | None:
    """The 'Install to' value -> a project dict (None = global). The right to change it is checked when the skill is created."""
    value = (value or "").strip()
    if value in ("", "global"):
        if value == "" and project is not None:
            return project
        return None
    try:
        row = repo.get_project_by_slug(value)
    except NotFound:
        raise BadRequest("choose where to install the skill") from None
    if not access.can_view(p, row["id"]):
        raise BadRequest("choose where to install the skill")
    return row


def _bases(p, project: dict | None, skip_key: str = "") -> list[dict]:
    """Skills a new or edited skill may be built on, as {key, name}: those reaching its scope."""
    if project is None:
        rows = asset_skills.list_global(p)
    else:
        rows = asset_skills.list_for_project(p, project)["entries"]
    return [{"key": r["key"], "name": r["name"]} for r in rows if r["key"] != skip_key and r["usable"]]


async def _uploads(files: list[UploadFile] | None) -> list[dict]:
    out = []
    for f in files or []:
        if not f or not (f.filename or "").strip():
            continue
        raw = await f.read(MAX_FILE_FORM + 1)
        if len(raw) > MAX_FILE_FORM:
            raise BadRequest(f"{f.filename} is over {MAX_FILE_FORM // 1024} KB")
        out.append({"path": (f.filename or "").rsplit("/", 1)[-1], "raw": raw})
    return out


def _scope_asset(request: Request, project: dict | None, uid: str, n: int | None = None) -> dict:
    """The skill for a page: this layer's own, or (on a project page) one that reaches the project."""
    p = access.of_request(request)
    a = asset_skills.get(p, uid, n=n, via_project=project)
    if project is None and a["owner_scope"] != "instance":
        raise NotFound("no such skill")
    return a


def _peek_files(files: list[dict]) -> list[dict]:
    out = []
    for f in files:
        item = {"path": f["path"], "bytes": f["bytes"], "text": None}
        if (f["content_type"] or "").startswith("text/") or f["content_type"] in ("application/json",):
            try:
                item["text"] = asset_store.read_rel(f["rel_path"])[:TEXT_PEEK].decode("utf-8", "replace")
            except NotFound:
                item["text"] = None
        out.append(item)
    return out


# --- list -------------------------------------------------------------------------------------------------------------------

def _list(request: Request, project: dict | None):
    p = access.of_request(request)
    if project is None:
        entries = asset_skills.list_global(p)
        can_add = asset_access.can_install_global(p)
        extra = {"inherited": [], "mine": entries, "conflicts": []}
    else:
        got = asset_skills.list_for_project(p, project)
        entries = got["entries"]
        can_add = access.can_edit(p, project["id"])
        own = [e for e in entries if e["owner"]["project_id"] == project["id"]]
        extra = {"mine": own, "inherited": [e for e in entries if e not in own], "conflicts": got["conflicts"]}
    return render(request, "pages/skills_list.html", _nav(project), skills_base=_prefix(project), scope_project=project,
                  can_add=can_add, entries=entries, **extra)


@router.get("", response_class=HTMLResponse)
def global_list(request: Request):
    return _list(request, None)


@project_router.get("", response_class=HTMLResponse)
def project_list(request: Request, project: dict = ProjectDep):
    return _list(request, project)


# --- new --------------------------------------------------------------------------------------------------------------------

def _new_page(request: Request, project: dict | None, mode: str, error: str = "", form: dict | None = None, status: int = 200):
    p = access.of_request(request)
    resp = render(request, "pages/skill_new.html", _nav(project), skills_base=_prefix(project), scope_project=project, mode=mode,
                  targets=_targets(p, project), bases=_bases(p, project), error=error, form=form or {},
                  max_zip_mb=MAX_UPLOAD // (1024 * 1024), allowed_hosts=skill_import.ALLOWED_HOSTS)
    resp.status_code = status
    return resp


@router.get("/new", response_class=HTMLResponse)
def global_new(request: Request):
    return _new_page(request, None, "new")


@project_router.get("/new", response_class=HTMLResponse)
def project_new(request: Request, project: dict = ProjectDep):
    return _new_page(request, project, "new")


@router.get("/import", response_class=HTMLResponse)
def global_import_form(request: Request):
    return _new_page(request, None, "import")


@project_router.get("/import", response_class=HTMLResponse)
def project_import_form(request: Request, project: dict = ProjectDep):
    return _new_page(request, project, "import")


async def _do_new(request: Request, project: dict | None, name, key, description, body, base, install_to, note, files):
    p = access.of_request(request)
    form = {"name": name, "key": key, "description": description, "body": body, "base": base, "install_to": install_to, "note": note}
    try:
        target = _install_to(p, install_to, project)
        a = asset_skills.create(p, name=name, key=key, description=description, body=body, install_to=target, base=base, note=note,
                                files=await _uploads(files))
    except (BadRequest, Conflict) as exc:
        return _new_page(request, project, "new", error=str(exc), form=form, status=400)
    return RedirectResponse(_url_of(a, project), status_code=303)


def _url_of(a: dict, project: dict | None) -> str:
    """Where a skill lives: its own layer's page, so the link works for everyone who can see it."""
    if a["owner_scope"] == "instance":
        return f"/assets/skills/{a['uid']}"
    owner = repo.get_project(a["owner_project_id"])
    return f"/{owner['slug']}/build-skills/{a['uid']}"


@router.post("/new")
async def global_create(request: Request, name: str = Form(""), key: str = Form(""), description: str = Form(""), body: str = Form(""),
                        base: str = Form(""), install_to: str = Form("global"), note: str = Form(""),
                        files: list[UploadFile] = File(default=[])):
    return await _do_new(request, None, name, key, description, body, base, install_to, note, files)


@project_router.post("/new")
async def project_create(request: Request, project: dict = ProjectDep, name: str = Form(""), key: str = Form(""),
                         description: str = Form(""), body: str = Form(""), base: str = Form(""), install_to: str = Form(""),
                         note: str = Form(""), files: list[UploadFile] = File(default=[])):
    return await _do_new(request, project, name, key, description, body, base, install_to, note, files)


# --- import -----------------------------------------------------------------------------------------------------------------

async def _do_import(request: Request, project: dict | None, url: str, install_to: str, file: UploadFile | None):
    p = access.of_request(request)
    form = {"url": url, "install_to": install_to}
    try:
        target = _install_to(p, install_to, project)
        asset_skills.check_install_rights(p, target)            # before anything is fetched
        upload = file if file is not None and (file.filename or "").strip() else None
        if bool(url.strip()) == (upload is not None):
            raise BadRequest("give either a GitHub address or a file, not both and not neither")
        if upload is not None:
            raw = await upload.read(MAX_UPLOAD + 1)
            if len(raw) > MAX_UPLOAD:
                raise BadRequest(f"the file is over {MAX_UPLOAD // (1024 * 1024)} MB")
            parsed = await run_in_threadpool(skill_import.prepare_upload, upload.filename or "", raw)
        else:
            parsed = await run_in_threadpool(skill_import.prepare_github, url)      # network and DNS: never on the event loop
        a = asset_skills.import_parsed(p, parsed, target)
    except (BadRequest, Conflict) as exc:
        return _new_page(request, project, "import", error=str(exc), form=form, status=400)
    return RedirectResponse(_url_of(a, project), status_code=303)


@router.post("/import", dependencies=[Depends(access.require_fresh())])
async def global_import(request: Request, url: str = Form(""), install_to: str = Form("global"), file: UploadFile | None = File(default=None)):
    return await _do_import(request, None, url, install_to, file)


@project_router.post("/import", dependencies=[Depends(access.require_fresh())])
async def project_import(request: Request, project: dict = ProjectDep, url: str = Form(""), install_to: str = Form(""),
                         file: UploadFile | None = File(default=None)):
    return await _do_import(request, project, url, install_to, file)


# --- one skill --------------------------------------------------------------------------------------------------------------

def _detail(request: Request, project: dict | None, uid: str, error: str = "", form: dict | None = None, status: int = 200):
    p = access.of_request(request)
    q = request.query_params
    v = _num(q.get("v"))
    a = _scope_asset(request, project, uid, v)
    live = asset_store.get(a["id"])["version"]
    vers = asset_skills.versions(p, uid)
    shown = a["version"]
    frm, to = q.get("from", ""), q.get("to", "")
    diff = None
    if _num(frm) and _num(to):
        diff = {"from": _num(frm), "to": _num(to), "lines": asset_skills.diff(p, uid, _num(frm), _num(to))}
    pin = None
    if project is not None:
        with_pin = asset_skills.pin_of(project["id"], a["id"])
        pin = with_pin
    can_edit = asset_access.can_edit(p, a) and a["origin"] != "builtin" and a["state"] == "active"
    fields = shown["fields"] or {}
    peek = _peek_files(shown["files"])
    imported = asset_skills.is_imported(fields)
    warnings = skill_scan.scan(shown["body"], peek) if imported else []
    since = None
    if imported and not fields.get("review"):
        prev = asset_skills.last_reviewed_n(p, uid)
        if prev and prev != shown["n"]:
            since = {"from": prev, "to": shown["n"], "lines": asset_skills.diff(p, uid, prev, shown["n"])}
    resp = render(request, "pages/skill_detail.html", _nav(project), skills_base=_prefix(project), scope_project=project, a=a,
                  shown=shown, live=live, old=shown["id"] != live["id"], versions=vers, diff=diff, files=peek, warnings=warnings, since=since,
                  imported=imported, hold=asset_skills.hold_reason(fields),
                  fields=fields, lic=fields.get("license") or {}, source=fields.get("source") or {}, blocked=asset_skills.licence_state(fields) == "blocked",
                  can_edit=can_edit, can_confirm=asset_access.can_confirm_licence(p, a), pin=pin,
                  can_pin=project is not None and access.can_edit(p, project["id"]) and a["owner_project_id"] != project["id"]
                  and a["locked"] is False, bases=_bases(p, project, a["key"]), targets=_targets(p, project),
                  layer=asset_skills.LAYER_LABEL[0 if a["origin"] == "builtin" else 1 if a["owner_scope"] == "instance"
                                                 else 2 if a["owner_scope"] == "account" else 4],
                  error=error, form=form or {})
    resp.status_code = status
    return resp


@router.get("/{uid}", response_class=HTMLResponse)
def global_detail(request: Request, uid: str):
    return _detail(request, None, uid)


@project_router.get("/{uid}", response_class=HTMLResponse)
def project_detail(request: Request, uid: str, project: dict = ProjectDep):
    return _detail(request, project, uid)


async def _do_edit(request: Request, project: dict | None, uid: str, description, body, base, note, base_version_id, remove_files, files):
    p = access.of_request(request)
    form = {"description": description, "body": body, "base": base, "note": note}
    try:
        out = asset_skills.edit(p, uid, description=description, body=body, base=base, add_files=await _uploads(files),
                                remove_files=remove_files, note=note, base_version_id=_num(base_version_id))
    except asset_store.StaleVersion as exc:
        return _detail(request, project, uid, error=f"{exc}: open it again to see the new text, then redo your change.", form=form, status=409)
    except (BadRequest, Conflict) as exc:
        return _detail(request, project, uid, error=str(exc), form=form, status=400)
    return _go(project, f"/{uid}", f"Saved as version {out['version']['n']}." if out.get("created") else "Nothing changed.")


@router.post("/{uid}/edit")
async def global_edit(request: Request, uid: str, description: str = Form(""), body: str = Form(""), base: str = Form(""), note: str = Form(""),
                      base_version_id: str = Form(""), remove_files: list[str] = Form(default=[]), files: list[UploadFile] = File(default=[])):
    return await _do_edit(request, None, uid, description, body, base, note, base_version_id, remove_files, files)


@project_router.post("/{uid}/edit")
async def project_edit(request: Request, uid: str, project: dict = ProjectDep, description: str = Form(""), body: str = Form(""),
                       base: str = Form(""), note: str = Form(""), base_version_id: str = Form(""),
                       remove_files: list[str] = Form(default=[]), files: list[UploadFile] = File(default=[])):
    return await _do_edit(request, project, uid, description, body, base, note, base_version_id, remove_files, files)


def _simple(request: Request, project: dict | None, uid: str, fn, message: str):
    p = access.of_request(request)
    _scope_asset(request, project, uid)
    try:
        fn(p)
    except (BadRequest, Conflict) as exc:
        return _detail(request, project, uid, error=str(exc), status=400)
    return _go(project, f"/{uid}", message)


@router.post("/{uid}/licence", dependencies=[Depends(access.require_fresh())])
def global_licence(request: Request, uid: str, note: str = Form("")):
    return _simple(request, None, uid, lambda p: asset_skills.confirm_licence(p, uid, note), "Licence confirmed: the skill is in use.")


@project_router.post("/{uid}/licence", dependencies=[Depends(access.require_fresh())])
def project_licence(request: Request, uid: str, project: dict = ProjectDep, note: str = Form("")):
    return _simple(request, project, uid, lambda p: asset_skills.confirm_licence(p, uid, note), "Licence confirmed: the skill is in use.")


@router.post("/{uid}/review", dependencies=[Depends(access.require_fresh())])
def global_review(request: Request, uid: str, note: str = Form("")):
    return _simple(request, None, uid, lambda p: asset_skills.review(p, uid, note), "Reviewed: jobs may now use this version.")


@project_router.post("/{uid}/review", dependencies=[Depends(access.require_fresh())])
def project_review(request: Request, uid: str, project: dict = ProjectDep, note: str = Form("")):
    return _simple(request, project, uid, lambda p: asset_skills.review(p, uid, note), "Reviewed: jobs may now use this version.")


@router.post("/{uid}/archive")
def global_archive(request: Request, uid: str, reason: str = Form("")):
    p = access.of_request(request)
    _scope_asset(request, None, uid)
    try:
        asset_skills.archive(p, uid, reason)
    except (BadRequest, Conflict) as exc:
        return _detail(request, None, uid, error=str(exc), status=400)
    return _go(None, "", "Skill archived.")


@project_router.post("/{uid}/archive")
def project_archive(request: Request, uid: str, project: dict = ProjectDep, reason: str = Form("")):
    p = access.of_request(request)
    _scope_asset(request, project, uid)
    try:
        asset_skills.archive(p, uid, reason)
    except (BadRequest, Conflict) as exc:
        return _detail(request, project, uid, error=str(exc), status=400)
    return _go(project, "", "Skill archived.")


def _copy(request: Request, project: dict | None, uid: str, install_to: str, key: str):
    p = access.of_request(request)
    a = _scope_asset(request, project, uid)
    try:
        out = asset_skills.copy_to(p, a["uid"], _install_to(p, install_to, project), key)
    except (BadRequest, Conflict) as exc:
        return _detail(request, project, uid, error=str(exc), status=400)
    return RedirectResponse(_url_of(out, project), status_code=303)


@router.post("/{uid}/copy")
def global_copy(request: Request, uid: str, install_to: str = Form("global"), key: str = Form("")):
    return _copy(request, None, uid, install_to, key)


@project_router.post("/{uid}/copy")
def project_copy(request: Request, uid: str, project: dict = ProjectDep, install_to: str = Form(""), key: str = Form("")):
    return _copy(request, project, uid, install_to, key)


@project_router.post("/{uid}/pin")
def project_pin(request: Request, uid: str, project: dict = ProjectDep, n: str = Form(""), reason: str = Form("")):
    p = access.of_request(request)
    _scope_asset(request, project, uid)
    try:
        asset_skills.pin(p, project, uid, _num(n) or 0, reason)
    except (BadRequest, Conflict) as exc:
        return _detail(request, project, uid, error=str(exc), status=400)
    return _go(project, f"/{uid}", f"Held at version {_num(n)} in this project.")


@project_router.post("/{uid}/unpin")
def project_unpin(request: Request, uid: str, project: dict = ProjectDep):
    p = access.of_request(request)
    _scope_asset(request, project, uid)
    asset_skills.unpin(p, project, uid)
    return _go(project, f"/{uid}", "Following the live version again.")
