"""Materials pages (task #295): /<slug>/materials lists what a project or account keeps and inherits, with an upload
form; /media/materials/<id>/<filename> serves a stored file. Owner browser writes, like every page route (actor
'owner', same-origin guard). Files are dev-time resources: never executed in the dashboard (CSP sandbox, nosniff)."""
from __future__ import annotations

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app import access, config, tree
from app import materials as mat
from app.repo import BadRequest
from app.routers.pages import ProjectDep, render

router = APIRouter()                           # global: /media/materials/…
project_router = APIRouter(prefix="/{slug}")   # /<slug>/materials


def _page(request: Request, project: dict, error: str | None = None, form: dict | None = None, status: int = 200):
    idx = access.filtered_index(access.of_request(request))    # inheritance never reaches a parent the person can't see (#713)
    q = request.query_params
    effective = mat.effective(project, idx)
    history = [r for r in mat.own(project["id"], include_old=True) if r["replaced_at"] is not None]
    resp = render(request, "pages/materials.html", "materials", effective=effective,
                  overridden=mat.overridden(project, idx), history=history, kinds=mat.KINDS,
                  kind_label=mat.KIND_LABEL, error=error, form=form or {},
                  prefill_slot=q.get("slot", ""), added=q.get("added", ""),
                  max_mb=config.MATERIALS_MAX_BYTES // (1024 * 1024))
    resp.status_code = status
    return resp


@project_router.get("/materials", response_class=HTMLResponse)
def materials_page(request: Request, project: dict = ProjectDep):
    return _page(request, project)


@project_router.post("/materials", response_class=HTMLResponse)
async def upload_material(request: Request, project: dict = ProjectDep, file: UploadFile = File(...),
                          slot: str = Form(""), kind: str = Form("other"), title: str = Form(""),
                          purpose: str = Form(""), usage_notes: str = Form("")):
    raw = await file.read(config.MATERIALS_MAX_BYTES + 1)
    form = {"slot": slot, "kind": kind, "title": title, "purpose": purpose, "usage_notes": usage_notes}
    try:
        row = mat.add(project, slot, kind, title, purpose, file.filename or "", raw, "owner", usage_notes)
    except BadRequest as exc:
        return _page(request, project, error=str(exc), form=form, status=400)
    return RedirectResponse(f"/{project['slug']}/materials?added={row['id']}#m-{row['slot']}", status_code=303)


@router.get("/media/materials/{material_id:int}/{filename}")
def material_file(request: Request, material_id: int, filename: str, download: int = 0):
    row = mat.get(material_id)
    if filename != row["filename"] or not access.can_view(access.of_request(request), row["project_id"]):
        return Response(status_code=404)
    inline = row["is_image"] and not download
    return Response(
        content=mat.read_bytes(row),
        media_type=row["content_type"],
        headers={
            "X-Content-Type-Options": "nosniff",
            # Never a document that runs here (an SVG or HTML template is data, not a page of this site).
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:; sandbox",
            "Cross-Origin-Resource-Policy": "same-origin",
            "Content-Disposition": f"{'inline' if inline else 'attachment'}; filename=\"{row['filename']}\"",
            "Cache-Control": "private, max-age=31536000, immutable",  # a new version is a new id
        },
    )
