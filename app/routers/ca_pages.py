"""The CA drawer's endpoints (task #702): /ca/… JSON and one SSE stream, for the SIGNED-IN person only (a web session cookie; a
runner, job or turn token can't act here, app/web_auth.signed_in_user_row). Every conversation, card and notice is found BY
(id, user), so another person's id is a 404. Browser writes are same-origin-checked like every page POST (app/origin_guard.py).
The runner side is not here: it speaks over the runner WebSocket (app/runner_ws.py)."""
from __future__ import annotations

import asyncio
import json
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from app import ca, ca_files, ca_notify, ca_tools, config, speech, web_auth
from app.repo import BadRequest, Conflict, NotFound
from app.routers.pages import render

router = APIRouter(prefix="/ca")

HEARTBEAT_SECONDS = 20


def _user(request: Request) -> dict:
    try:
        return web_auth.signed_in_user_row(request)
    except PermissionError as exc:
        raise HTTPException(403, f"CA needs a signed-in person ({exc})") from None


async def _body(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "send JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(400, "send a JSON object")
    return body


def _jsonable(value):
    return json.loads(json.dumps(value, default=str))


def _guard(fn, *args, **kwargs):
    """Run a ca call; a repo error becomes the matching HTTP status (NotFound -> 404, so another person's id looks missing)."""
    try:
        return fn(*args, **kwargs)
    except NotFound as exc:
        raise HTTPException(404, str(exc)) from None
    except ca.Forbidden as exc:
        raise HTTPException(403, str(exc)) from None
    except Conflict as exc:
        raise HTTPException(409, str(exc)) from None
    except BadRequest as exc:
        raise HTTPException(400, str(exc)) from None


@router.get("/state")
def state(request: Request):
    """What the drawer needs on open: the person's runners (online or not), the model, conversations, unread notices."""
    user = _user(request)
    devs = ca.live_devices(user["id"])
    return _jsonable({"model": ca.model(), "runners": [{"id": d["id"], "name": d["name"], "connected": d["connected"]} for d in devs],
                      "online": any(d["connected"] for d in devs), "conversations": ca.list_conversations(user["id"]),
                      "speech": speech.configured(), "user_id": user["id"], "first_name": ((user.get("display_name") or "").split(" ") or [""])[0],
                      "uploads": {"max_per_message": ca_files.MAX_PER_MESSAGE, "max_image_pdf": ca_files.MAX_BYTES["image"],
                                  "max_text": ca_files.MAX_BYTES["text"], "task_closed_days": config.CA_UPLOAD_TASK_CLOSED_DAYS,
                                  "idle_days": config.CA_UPLOAD_IDLE_DAYS},
                      **ca_notify.list_notices(user["id"], 10)})


@router.post("/conversations")
async def new_conversation(request: Request):
    user = _user(request)
    body = await _body(request)
    task_id = body.get("task_id")
    entity = body.get("entity")
    conv = _guard(ca.create_conversation, user["id"], str(body.get("path") or ""), body.get("project") or None,
                  task_id if isinstance(task_id, int) and not isinstance(task_id, bool) else None,
                  entity=entity if isinstance(entity, dict) else None)
    return _jsonable({"id": conv["id"], "title": conv["title"], "context": conv["context"]})


@router.get("/tasks/{task_id}/discussion")
def task_discussion(request: Request, task_id: int):
    """The Discussion panel of a task window: that task's conversations (newest first) and whether this person may post. 404 without
    view access to the task. Staff session only: a client token or client view never reaches /ca."""
    user = _user(request)
    return _jsonable(_guard(ca.task_discussion, user["id"], task_id))


@router.get("/conversations/{conversation_id}")
def one_conversation(request: Request, conversation_id: int):
    user = _user(request)
    return _jsonable(_guard(ca.get_conversation, user["id"], conversation_id))


@router.post("/conversations/{conversation_id}/messages")
async def post_message(request: Request, conversation_id: int):
    user = _user(request)
    body = await _body(request)
    try:
        out = await asyncio.to_thread(ca.send_message, user["id"], conversation_id, str(body.get("text") or ""),
                                     body.get("attachments") if isinstance(body.get("attachments"), list) else None)
    except ca.Offline as exc:   # nothing was stored: the browser keeps the text in the box
        return JSONResponse({"offline": True, "detail": str(exc)}, status_code=409)
    except NotFound as exc:
        raise HTTPException(404, str(exc)) from None
    except ca.Forbidden as exc:       # view-only on the task: read, never post
        raise HTTPException(403, str(exc)) from None
    except Conflict as exc:
        raise HTTPException(409, str(exc)) from None
    except BadRequest as exc:
        raise HTTPException(400, str(exc)) from None
    return out


@router.patch("/conversations/{conversation_id}")
async def link_entity(request: Request, conversation_id: int):
    """Link the person's own conversation to an item they can see, or unlink it: {entity: {kind, id}} or {entity: null}."""
    user = _user(request)
    body = await _body(request)
    if "entity" not in body:
        raise HTTPException(400, "send {entity: {kind, id}} or {entity: null}")
    ctx = _guard(ca.set_entity, user["id"], conversation_id, body["entity"])
    return _jsonable({"id": conversation_id, "context": ctx})


# --- uploads (task #727) -------------------------------------------------------------------------------------------------

@router.post("/conversations/{conversation_id}/uploads")
async def upload(request: Request, conversation_id: int, name: str = ""):
    """One file per call, as the raw request body (?name=<filename>); -> its metadata. The type is decided by the content."""
    user = _user(request)
    cap = ca_files.MAX_UPLOAD
    try:
        if int(request.headers.get("content-length") or 0) > cap:
            raise HTTPException(413, f"a file is at most {cap // (1024 * 1024)} MB")
    except ValueError:
        raise HTTPException(400, "bad Content-Length") from None
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > cap:
            raise HTTPException(413, f"a file is at most {cap // (1024 * 1024)} MB")
        chunks.append(chunk)
    meta = await asyncio.to_thread(_guard, ca_files.store, user["id"], conversation_id, name, b"".join(chunks))
    return _jsonable(meta)


_IMAGE_OK = {"image/png", "image/jpeg", "image/gif", "image/webp"}


@router.get("/uploads/{attachment_id}")
def get_upload(request: Request, attachment_id: int):
    """The file, only for the person who owns it (another person's id is a 404). Images show inline; everything else downloads.
    Served with nosniff and a sandboxing CSP so an uploaded file can never run as a page."""
    user = _user(request)
    f = _guard(ca_files.fetch, user["id"], attachment_id)
    inline = f["mime"] in _IMAGE_OK
    safe = f["filename"].replace('"', "").replace("\r", "").replace("\n", "")
    ctype = f["mime"] if inline or f["kind"] == "pdf" else "text/plain; charset=utf-8"
    return Response(f["data"], media_type=ctype, headers={
        "Content-Disposition": f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(safe)}",
        "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox; default-src 'none'; img-src 'self'; style-src 'unsafe-inline'",
        "Cache-Control": "private, max-age=300"})


@router.delete("/uploads/{attachment_id}")
def delete_upload(request: Request, attachment_id: int):
    """Take back a file that was attached but not yet sent."""
    user = _user(request)
    _guard(ca_files.discard, user["id"], attachment_id)
    return {"deleted": attachment_id}


# --- speech (task #727) ---------------------------------------------------------------------------------------------------

@router.get("/speech/url")
async def speech_url(request: Request, lang: str = ""):
    """A short-lived signed address for the configured speech provider. 404 when none is configured (the drawer hides the mic),
    429 + Retry-After past 6 a minute."""
    user = _user(request)
    if not speech.provider():
        raise HTTPException(404, "speech is not configured")
    wait = speech.allow(user["id"])
    if wait:
        return JSONResponse({"detail": "too many voice starts: wait a moment"}, status_code=429,
                            headers={"Retry-After": str(max(1, int(wait + 0.999))), "Cache-Control": "no-store"})
    try:
        out = await asyncio.to_thread(speech.listen_url, lang)
    except speech.NotConfigured:
        raise HTTPException(404, "speech is not configured") from None
    return JSONResponse(out, headers={"Cache-Control": "no-store"})


@router.get("/items/{kind}/{ident:path}/discussions", response_class=HTMLResponse)
def item_discussions(request: Request, kind: str, ident: str):
    """The Discussions section of an item's page (a fragment, loaded by htmx): the conversations linked to it, the person's own with
    their title and an open link, everyone else's as a soft link with no title; and the item's agent jobs. 404 if the person can't
    see the item. Content never leaves its owner."""
    user = _user(request)
    if not ca.resolve_entity(user["id"], None, kind, ident):
        raise HTTPException(404, "not found")
    data = ca.discussions_of(user["id"], kind, ident)
    from app.templating import templates
    return templates.TemplateResponse(request, "partials/ca_discussions.html",
                                      {"d": data, "kind": kind, "ident": ident, "item": ca.resolve_entity(user["id"], None, kind, ident)})


# --- history (task #727) --------------------------------------------------------------------------------------------------

@router.get("/history", response_class=HTMLResponse)
def history_page(request: Request):
    """Chat history: the signed-in person's own conversations, searchable, newest first, paged. Rows open in the drawer."""
    user = _user(request)
    ctx = _history_ctx(request, user, jsonable=False)
    return render(request, "pages/ca_history.html", "home", **ctx)


def _history_ctx(request: Request, user: dict, jsonable: bool = True) -> dict:
    q = request.query_params
    try:
        page = int(q.get("page") or 1)
    except ValueError:
        page = 1
    archived = q.get("archived") == "1"
    data = ca.history(user["id"], q.get("q") or "", page, 20, archived)
    return {"hist": _jsonable(data) if jsonable else data, "show_archived": archived}


@router.get("/history.json")
def history_json(request: Request):
    user = _user(request)
    return _jsonable(_history_ctx(request, user)["hist"])


@router.post("/conversations/{conversation_id}/share")
def share(request: Request, conversation_id: int):
    """The starter shares their own task-linked conversation with the task (everyone who can see the task then reads it)."""
    user = _user(request)
    return _guard(ca.share_conversation, user["id"], conversation_id)


@router.post("/conversations/{conversation_id}/stop")
def stop(request: Request, conversation_id: int):
    user = _user(request)
    return {"stopped": _guard(ca.cancel_turn, user["id"], conversation_id)}


@router.post("/conversations/{conversation_id}/archive")
def archive(request: Request, conversation_id: int):
    user = _user(request)
    _guard(ca.archive_conversation, user["id"], conversation_id)
    return {"archived": conversation_id}


@router.post("/cards/{card_id}")
async def card(request: Request, card_id: int):
    """A tap on a card: {action: confirm | dismiss | answer, answer?}. The effect runs here, on the person's own session."""
    user = _user(request)
    body = await _body(request)
    return _jsonable(await asyncio.to_thread(_guard, ca_tools.resolve_card, user["id"], card_id, str(body.get("action") or ""),
                                              str(body.get("answer") or "")))


@router.get("/notices")
def notices(request: Request):
    return _jsonable(ca_notify.list_notices(_user(request)["id"]))


@router.post("/notices/read")
def notices_read(request: Request):
    return {"marked": ca_notify.mark_read(_user(request)["id"])}


def _frame(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, default=str, separators=(',', ':'))}\n\n".encode()


async def _stream(request: Request, user_id: int):
    sub = ca.subscribe(user_id)
    try:
        yield b"retry: 3000\n\n"
        yield b": connected\n\n"
        while True:
            if await request.is_disconnected():
                break
            try:
                event, data = await asyncio.wait_for(sub.queue.get(), HEARTBEAT_SECONDS)
            except asyncio.TimeoutError:
                yield b": ping\n\n"
                continue
            yield _frame(event, data)
    finally:
        ca.unsubscribe(user_id, sub)


@router.get("/stream")
async def stream(request: Request):
    """The person's live feed (SSE): `delta` (answer text as it is written), `tool`, `message`, `turn_done`, `card`, `card_resolved`
    and `notice`. One per open tab. Like /all/events it must not be buffered by a proxy."""
    user = _user(request)
    return StreamingResponse(_stream(request, user["id"]), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})
