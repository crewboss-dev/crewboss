"""Phone notifications (#487): the service worker at /sw.js and the avatar menu's Enable / Test this device / Disable
(js/push.js). Owner browser writes like every page route: no app login (NetBird is the gate, #394 adds one), and the
same-origin guard refuses a POST from any other site. The sending and the rule are app/push.py."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from app import access, push

router = APIRouter()


@router.get("/sw.js", include_in_schema=False)
def service_worker():
    """From the ROOT so its scope is the whole dashboard (a worker under /static/ could only control /static/), never
    cached so an update reaches the phone on its next visit."""
    return FileResponse("app/static/sw.js", media_type="application/javascript",
                        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})


@router.get("/push/status", dependencies=[Depends(access.owner_only)])
def push_status(endpoint: str = ""):
    """?endpoint= is the browser's current subscription, so the menu can tell "on for this device" from a subscription
    the server has since pruned."""
    return push.status(endpoint)


@router.get("/push/key", response_class=PlainTextResponse, dependencies=[Depends(access.owner_only)])
def push_key():
    """The VAPID public key a browser subscribes with (applicationServerKey)."""
    key = push.public_key()
    if key is None:
        return PlainTextResponse(f"Phone notifications are off: {push.status()['reason']}.", status_code=503)
    return key


async def _json(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        body = None
    return body if isinstance(body, dict) else {}


@router.post("/push/subscribe", dependencies=[Depends(access.owner_only)])
async def push_subscribe(request: Request):
    try:
        devices = push.subscribe(await _json(request), request.headers.get("user-agent", ""))
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    return {"ok": True, "devices": devices}


@router.post("/push/unsubscribe", dependencies=[Depends(access.owner_only)])
async def push_unsubscribe(request: Request):
    removed = push.unsubscribe((await _json(request)).get("endpoint"))
    return {"ok": True, "removed": removed, "devices": push.status()["devices"]}


@router.post("/push/test", dependencies=[Depends(access.owner_only)])
def push_test():
    """A test notification to every subscribed device; the menu shows "sent N, failed M, pruned K"."""
    st = push.status()
    if not st["enabled"]:
        return JSONResponse({"ok": False, "message": f"Phone notifications are off: {st['reason']}."}, status_code=503)
    if not st["devices"]:
        return {"ok": False, "sent": 0, "failed": 0, "pruned": 0, "message": "No device is subscribed: Enable first."}
    r = push.deliver(push.test_payload())
    return {"ok": r["sent"] > 0, **r, "message": push.summary_line(r).capitalize() + "."}
