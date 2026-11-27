"""Platform keys page (#562; #577): ONE page on the Dashboard project, /<dashboard>/keys (platform_keys.home_slug), with
a section per key store in config.PLATFORM_KEY_STORES (one store per project). Platform
keys are global (the owner's #547), so they live with the Dashboard, not with the project whose server holds a store:
the old /<store-slug>/keys answers with a redirect to /<dashboard>/keys#<store-slug>.

Each section shows its store's STATUS (set or not, age, grace, expiry and rotation, the stage) and sets, rotates, ends
grace, clears and edits a key's dates — each a signed call through app/platform_keys.py to THAT store, named in the
form's path (/<dashboard>/keys/<store-slug>/<NAME>/…). It never reads a value back.

THE VALUE goes from the Set/Rotate form's password field into platform_keys.set_value and nowhere else: never stored,
logged, put in a cookie or URL, re-rendered or returned. Every POST answers with a redirect (303) to the page, so a
reload can't resend it, and the page's responses are no-store.

Gates: Microsoft sign-in (app/web_auth.py: every page needs a signed-in super admin) and the same-origin guard
(app/origin_guard.py), which refuses any cross-site POST here as it does for every other page form. The store records
who did it: actor = the signed-in person (platform_keys.actor_for)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import access
from app import platform_keys as pk
from app.repo import NotFound
from app.routers.pages import project_from_path, render

project_router = APIRouter(prefix="/{slug}", dependencies=[Depends(access.owner_only)])   # platform keys: the owner only (#713)

NOTE_COOKIE = "crewboss_keys_note"          # the result of the last action: {"tone", "text", "store", "key"} — never a value
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


def _home(request: Request, slug: str) -> dict:
    """The Dashboard project, when the URL names it (else 404): the only project these pages live on."""
    home = pk.home_slug()
    if not home or slug != home:
        raise NotFound("Platform keys live on the Dashboard project")
    return project_from_path(request, slug)


def _store(store_slug: str) -> dict:
    s = pk.store(store_slug)
    if not s:
        raise NotFound(f"no platform key store {store_slug!r}")
    return s


def _actor(request: Request) -> str:
    return pk.actor_for(getattr(request.state, "web_user", None))


def anchor(s: dict, name: str) -> str:
    """The id of a key's row on the page: per store, so two stores can hold the same NAME."""
    return f"key-{s['slug']}-{name}"


def _back(s: dict, name: str, tone: str, text: str) -> RedirectResponse:
    page = pk.page_path()
    frag = anchor(s, name) if pk.NAME_RE.fullmatch(name or "") else s["slug"]
    resp = RedirectResponse(f"{page}#{frag}", status_code=303, headers=NO_STORE)
    resp.set_cookie(NOTE_COOKIE, json.dumps({"tone": tone, "text": text[:400], "store": s["slug"],
                                             "key": name if frag != s["slug"] else ""}),
                    max_age=60, samesite="lax", httponly=True, path=page)
    return resp


def _failed(s: dict, name: str, exc: pk.KeysError, what: str) -> RedirectResponse:
    """The note for a refused or failed action. When the store may have acted (no answer after the request went out)
    it never says the key wasn't changed."""
    if exc.code == "uncertain":
        return _back(s, name, "error", f"{name}: {exc}")
    return _back(s, name, "error", f"{what}: {exc}")


def _note(request: Request) -> dict | None:
    raw = request.cookies.get(NOTE_COOKIE)
    if not raw:
        return None
    try:
        note = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(note, dict) or note.get("tone") not in ("ok", "error") or not isinstance(note.get("text"), str):
        return None
    return {"tone": note["tone"], "text": note["text"][:400], "store": str(note.get("store") or "")[:60],
            "key": str(note.get("key") or "")[:82]}


PROPAGATION = {
    "all": "in use on every server now",
    "pending": "some servers haven't fetched it yet (they check every few minutes)",
    "none": "no server fetches keys from this store",
    "legacy": "also written to the ERP's rollback table",
}
ACTION = {"set": "set", "rotate": "replaced (the previous value is still accepted during its grace)",
          "rotate_no_grace": "replaced, and the old value revoked at once"}


def _section(s: dict) -> dict:
    """One store's section: its status, read live (else the last read), with the same failure clock and stage
    tracking as the reminder loop."""
    doc, as_of, error, live = None, None, "", False
    credential = pk.credential_problem(s)
    if not credential:
        try:
            read_at = datetime.now(timezone.utc)   # before the GET: observe() orders reads by when they started
            doc = pk.get_status(s)
            live = True
        except pk.KeysError as exc:
            error = str(exc)
            pk.record_failure(s, error)     # the same failure clock as the loop (the "can't read" alarm after 30 min)
    if live:
        try:
            pk.observe(s, doc, read_at)     # the same stage tracking as the loop: a change seen here notifies too
        except Exception:  # noqa: BLE001 — showing the page matters more than recording the read
            pk.log.exception("platform keys: recording the page's read")
    else:
        doc, as_of, _last_error = pk.last_snapshot(s["slug"])
    return {"store": s, "v": pk.view(doc), "live": live, "as_of": as_of, "error": error or credential}


@project_router.get("/keys", response_class=HTMLResponse)
def keys_page(request: Request, slug: str):
    if slug != pk.home_slug() and pk.store(slug):
        # The old per-project page (#562): the store is a section of the Dashboard's page now (#577).
        return RedirectResponse(pk.page_path(slug), status_code=307, headers=NO_STORE)
    _home(request, slug)
    note = _note(request)
    sections = [_section(s) for s in pk.stores()]
    resp = render(request, "pages/platform_keys.html", "keys", sections=sections, note=note,
                  STAGE_META=pk.STAGE_META, anchor=anchor)
    resp.headers.update(NO_STORE)
    if note:
        resp.delete_cookie(NOTE_COOKIE, path=pk.page_path())
    return resp


@project_router.post("/keys/{store_slug}/{name}/set", dependencies=[Depends(access.require_fresh())])
def set_key(request: Request, slug: str, store_slug: str, name: str, value: str = Form(""),
            no_grace: str = Form("")):
    _home(request, slug)
    s = _store(store_slug)
    try:
        out = pk.set_value(s, name, value, no_grace=no_grace == "on", actor=_actor(request))
    except pk.KeysError as exc:
        return _failed(s, name, exc, f"{name} wasn't changed")
    finally:
        del value
    text = f"{name} {ACTION.get(out.get('action'), 'saved')}"
    if out.get("key_version") is not None:
        text += f" · version {out['key_version']}"
    if out.get("propagation") in PROPAGATION:
        text += f" · {PROPAGATION[out['propagation']]}"
    return _back(s, name, "ok", text + ".")


@project_router.post("/keys/{store_slug}/{name}/meta", dependencies=[Depends(access.require_fresh())])
def set_key_meta(request: Request, slug: str, store_slug: str, name: str, expires_mode: str = Form("keep"),
                 expires_on: str = Form(""), rotate_mode: str = Form("keep"), rotate_months: str = Form(""),
                 last_rotated: str = Form("")):
    _home(request, slug)
    s = _store(store_slug)
    try:
        meta = pk.meta_from_form(expires_mode, expires_on, rotate_mode, rotate_months, last_rotated)
        out = pk.set_meta(s, name, meta, actor=_actor(request))
    except pk.KeysError as exc:
        return _failed(s, name, exc, f"{name}'s dates weren't changed")
    m = out.get("meta") if isinstance(out.get("meta"), dict) else {}
    stage = pk.STAGE_META.get(m.get("stage") or "")
    text = f"{name}'s dates saved"
    if m.get("next_due"):
        text += f" · next due {m['next_due']}"
    if stage:
        text += f" · {stage['label'].lower()}"
    return _back(s, name, "ok", text + ".")


@project_router.post("/keys/{store_slug}/{name}/end-grace", dependencies=[Depends(access.require_fresh())])
def end_grace(request: Request, slug: str, store_slug: str, name: str):
    _home(request, slug)
    s = _store(store_slug)
    try:
        out = pk.end_grace(s, name, actor=_actor(request))
    except pk.KeysError as exc:
        return _failed(s, name, exc, f"{name}: grace not ended")
    if not out.get("changed"):
        return _back(s, name, "ok", f"{name} had no previous value in grace: nothing to end.")
    return _back(s, name, "ok", f"{name}: the previous value is no longer accepted.")


@project_router.post("/keys/{store_slug}/{name}/clear", dependencies=[Depends(access.require_fresh())])
def clear_key(request: Request, slug: str, store_slug: str, name: str, confirm: str = Form("")):
    _home(request, slug)
    s = _store(store_slug)
    if confirm.strip() != name:
        return _back(s, name, "error", f"{name} wasn't cleared: type its name to confirm.")
    try:
        out = pk.clear(s, name, actor=_actor(request))
    except pk.KeysError as exc:
        return _failed(s, name, exc, f"{name} wasn't cleared")
    if not out.get("changed"):
        return _back(s, name, "ok", f"{name} had no stored value: nothing to clear.")
    return _back(s, name, "ok", f"{name} cleared: the server's .env value (if it has one) is in use again.")
