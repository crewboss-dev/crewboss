"""Runner endpoints (task #627): /runner/identity and /runner/enrol (P1), and (P2) /runner/jobs/claim and
/runner/jobs/<id>/heartbeat, which P4 moved onto the verified socket (REST now refuses them), and WS /runner/ws (P3; a device token, app/runner_ws.py). The job MCP is /runner/mcp (app/job_mcp.py; a job token). They are open
to the web sign-in gate and the same-origin guard (web_auth._OPEN_PREFIXES, origin_guard.EXEMPT_PREFIXES) because a runner
is not a browser: it authenticates by enrolment code, device token or job token, and by device signature in later phases.
The rules are app/devices.py and app/jobs.py."""
from __future__ import annotations

import re
import logging

from fastapi import APIRouter, Request, WebSocket
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app import devices, jobs, runner_identity, runner_ws
from app.repo import BadRequest

log = logging.getLogger("crewboss.devices")

router = APIRouter(prefix="/runner", include_in_schema=False)

_NO_STORE = {"Cache-Control": "no-store"}


class EnrolBody(BaseModel):
    code: str = Field(max_length=200)    # the whole `host:port#<fp8>-<secret>` code, or just the secret
    pubkey: str = Field(max_length=200)  # base64 of the runner's raw 32-byte ed25519 PUBLIC key
    name: str = Field(default="", max_length=200)
    platform: str = Field(default="", max_length=200)


_client_ip = runner_ws.client_ip   # shared with the WebSocket (X-Forwarded-For is believed only from a trusted proxy)


def _identity_unavailable(exc: Exception) -> JSONResponse:
    return JSONResponse({"detail": str(exc)}, status_code=503, headers=_NO_STORE)


@router.get("/identity")
def identity():
    """The dashboard's runner identity: its public key and fp8 (what an enrolment code carries)."""
    try:
        return JSONResponse({"public_key": runner_identity.public_key(), "fp8": runner_identity.fp8()},
                            headers=_NO_STORE)
    except runner_identity.IdentityUnavailable as exc:
        return _identity_unavailable(exc)


@router.post("/enrol")
def enrol(body: EnrolBody, request: Request):
    try:   # no identity key = no enrolment, whether or not the code carries the fingerprint
        mine = runner_identity.fp8()
    except runner_identity.IdentityUnavailable as exc:
        return _identity_unavailable(exc)
    code = body.code.strip()
    if "#" in code:   # the whole code: it must name THIS server's key, or it was meant for another dashboard
        fp, _, secret = code.partition("#")[2].partition("-")
        fp, dot, pin = fp.partition(".")   # a LAN code is `fp8.pin26`: the pin matters to the RUNNER (it checks it before sending anything)
        if fp != mine or (dot and not re.fullmatch(r"[a-z2-7]{26}", pin)):
            log.warning("runner enrol: refused %s: code names another server's key (fp8 mismatch)",
                        _client_ip(request) or "?")
            return JSONResponse({"detail": devices.REFUSED}, status_code=403, headers=_NO_STORE)
    else:
        secret = code
    try:
        dev = devices.enrol(secret, body.pubkey, body.name, body.platform, ip=_client_ip(request))
    except devices.RateLimited as exc:
        return JSONResponse({"detail": str(exc)}, status_code=429, headers={**_NO_STORE, "Retry-After": "60"})
    except devices.EnrolRefused as exc:
        return JSONResponse({"detail": str(exc)}, status_code=403, headers=_NO_STORE)
    except BadRequest as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400, headers=_NO_STORE)
    return JSONResponse({"device_id": dev["device_id"], "device_token": dev["device_token"],
                         "expires_at": dev["expires_at"].isoformat(), "mode": dev["mode"]}, headers=_NO_STORE)


_SOCKET_ONLY = ("claim, heartbeat and finish are messages on the verified WS /runner/ws (the device key must prove "
                "itself); a device token alone can't do them")


@router.post("/jobs/claim")
@router.post("/jobs/{job_id:int}/heartbeat")
def socket_only(request: Request):
    """Refused for every caller (review 2280): a stolen device token must not be enough to take or hold a job."""
    log.warning("runner REST %s refused from %s: socket only", request.url.path.rsplit("/", 1)[-1],
                _client_ip(request) or "?")
    return JSONResponse({"detail": _SOCKET_ONLY}, status_code=403, headers=_NO_STORE)


@router.websocket("/ws")
async def runner_socket(ws: WebSocket):
    """The runner's outbound socket (P3): host allowlist, no Origin, device token, mutual signed challenge. See runner_ws."""
    await runner_ws.serve(ws)
