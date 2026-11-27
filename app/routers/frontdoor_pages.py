"""The Front door card (#976): /admin/front-door. OWNER ONLY, local sign-in only. It shows how people reach Crewboss and walks the owner
through getting a padlock (their own web address, no browser warning) in three steps: the address, point it here and open the port, check
and switch on. The work is done by the front door service (app/front_door.py client); this page only asks.

Every action that changes anything needs a fresh confirmation from the last 5 minutes (FRESH_SHORT), and is audited. A failed switch
changes nothing. Plain words only on the page: no certificate jargon."""
from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse

from app import access, config, front_door, runner_ws, web_auth
from app.db import get_cursor
from app.routers.pages import render
from app.templating import templates

log = logging.getLogger("crewboss.frontdoor")


def _local_only() -> None:
    if web_auth.mode() != "local":
        raise HTTPException(404)


router = APIRouter(prefix="/admin/front-door", include_in_schema=False,
                   dependencies=[Depends(access.owner_only), Depends(_local_only)])
FRESH_SHORT = Depends(access.require_fresh(access.STEPUP_SHORT_SECONDS))
_NO_STORE = {"Cache-Control": "no-store"}


def _me(request: Request) -> int | None:
    wu = getattr(request.state, "web_user", None)
    return wu.get("user_id") if wu else None


def _audit(verb: str, summary: str) -> None:
    try:
        with get_cursor() as cur:
            access.audit(cur, verb, summary)
    except Exception:  # noqa: BLE001 — an audit hiccup must not hide the result
        log.exception("front door audit failed")


def _mode(request: Request) -> str:
    """Which card to draw: "lan" (LAN install, can upgrade), "public" (has a padlock now), "proxy" (own proxy: read-only), "local" (this
    computer only: read-only)."""
    if front_door.public_host():
        return "public"
    if front_door.lan_hosts():
        return "lan"
    if front_door.origin_of(request) == "public":
        return "proxy"      # an https address that is not ours to change (their own reverse proxy)
    return "local"


def _model(request: Request, **extra) -> dict:
    mode = _mode(request)
    status: dict = {}
    problem = ""
    if mode in ("lan", "public") and front_door.available():
        try:
            status = front_door.client().status()
        except front_door.FrontDoorError as exc:
            problem = str(exc)
    elif mode in ("lan", "public"):
        problem = "The front door service is not set up on this install, so this page can only show the state."
    managed = status.get("managed", True) is not False
    if not managed:
        problem = "The web server settings on this computer were edited by hand, so Crewboss won't change them. Nothing to do here."
    no_passkey = 0
    if mode == "public":
        no_passkey = _people_without_passkey()
    return {"mode": mode, "lan_url": front_door.lan_url(), "lan_hosts": front_door.lan_hosts(), "fingerprint": front_door.lan_fingerprint(),
            "public_host": front_door.public_host(), "public_url": front_door.public_url(), "warning": front_door.LAN_WARNING,
            "svc_problem": problem, "rotation": (status.get("rotation") or {}), "busy": bool(status.get("busy")),
            "can_change": mode in ("lan", "public") and front_door.available() and managed, "no_passkey": no_passkey,
            "passkeys_setting": config.AUTH_PASSKEYS, "this_computer": _lan_target(), **extra}


def _people_without_passkey() -> int:
    try:
        with get_cursor(commit=False) as cur:
            cur.execute("SELECT count(*) AS n FROM users u WHERE u.disabled_at IS NULL "
                        "AND NOT EXISTS (SELECT 1 FROM webauthn_credentials w WHERE w.user_id = u.id)")
            return int(cur.fetchone()["n"])
    except Exception:  # noqa: BLE001 — a count must not break the page
        return 0


def _lan_target() -> str:
    hosts = front_door.lan_hosts()
    return hosts[0] if hosts else "this computer"


@router.get("", response_class=HTMLResponse)
def page(request: Request):
    resp = render(request, "pages/admin_front_door.html", "front_door", **_model(request))
    resp.headers.update(_NO_STORE)
    return resp


def _partial(request: Request, name: str, status: int = 200, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, f"partials/{name}", ctx, status_code=status, headers=_NO_STORE)


def _bad_input(domain: str, email: str = "") -> str:
    if not front_door.valid_domain(domain):
        return "That doesn't look like a web address. Use a name like crew.example.com (not a number, and without https://)."
    if email and not front_door.valid_email(email):
        return "That email address doesn't look right. Leave it empty if you would rather not give one."
    return ""


@router.post("/check", response_class=HTMLResponse, dependencies=[FRESH_SHORT])
def start_check(request: Request, domain: str = Form("")):
    domain = domain.strip().lower()
    err = _bad_input(domain)
    if err:
        return _partial(request, "front_door_check.html", 400, state="error", problem=err, domain=domain)
    try:
        started = front_door.client().check(domain)
    except front_door.FrontDoorError as exc:
        return _partial(request, "front_door_check.html", 200, state="error", problem=str(exc), domain=domain)
    _audit("front_door.check", f"Checked {domain} for the front door")
    return _partial(request, "front_door_check.html", state="running", domain=domain, check_id=str(started.get("check_id") or ""),
                    result=started)


@router.get("/check/{check_id}", response_class=HTMLResponse)
def poll_check(request: Request, check_id: str, domain: str = ""):
    if not check_id.replace("-", "").isalnum() or len(check_id) > 64:
        raise HTTPException(404)
    try:
        res = front_door.client().check_result(check_id)
    except front_door.FrontDoorError as exc:
        return _partial(request, "front_door_check.html", state="error", problem=str(exc), domain=domain)
    done = bool(res.get("done", all(k in res for k in ("dns", "port80", "port443"))))
    dns, p80, p443 = (res.get("dns") or {}), (res.get("port80") or {}), (res.get("port443") or {})
    passed = bool(dns.get("ok") and p80.get("ok"))
    state = "running" if not done else ("passed" if passed else "failed")
    return _partial(request, "front_door_check.html", state=state, domain=domain, check_id=check_id, result=res, dns=dns, p80=p80,
                    p443=p443, no_443=bool(done and passed and not p443.get("ok")))


@router.post("/switch", response_class=HTMLResponse, dependencies=[FRESH_SHORT])
def switch_on(request: Request, domain: str = Form(""), email: str = Form(""), check_id: str = Form(""),
              allow_no_443: str = Form("")):
    domain, email = domain.strip().lower(), email.strip()
    err = _bad_input(domain, email)
    if err:
        return _partial(request, "front_door_switch.html", 400, ok=False, problem=err, domain=domain)
    try:
        out = front_door.client().switch(domain, email, check_id, allow_no_443 in ("1", "on", "true", "yes"))
    except front_door.FrontDoorError as exc:
        return _partial(request, "front_door_switch.html", ok=False, problem=str(exc), domain=domain)
    if not out.get("ok"):
        _audit("front_door.switch_failed", f"Switching the front door to {domain} failed; nothing changed")
        return _partial(request, "front_door_switch.html", ok=False, problem=str(out.get("problem") or "It didn't work."), domain=domain)
    _audit("front_door.switch", f"Front door switched to {domain} by user {_me(request)}")
    front_door.restart_app_soon()
    return _partial(request, "front_door_switch.html", ok=True, domain=domain, new_url=f"https://{domain}")


@router.post("/remove-public", response_class=HTMLResponse, dependencies=[FRESH_SHORT])
def remove_public(request: Request):
    try:
        out = front_door.client().remove_public()
    except front_door.FrontDoorError as exc:
        return _partial(request, "front_door_switch.html", ok=False, problem=str(exc), domain=front_door.public_host())
    if not out.get("ok"):      # the service answers 200 with {"ok": false, "problem": ...}: nothing changed, so no audit and no restart
        return _partial(request, "front_door_switch.html", ok=False, problem=str(out.get("problem") or "It didn't work."), domain=front_door.public_host())
    _audit("front_door.remove", f"Stopped using {front_door.public_host()} (user {_me(request)})")
    front_door.restart_app_soon()
    return _partial(request, "front_door_switch.html", ok=True, removed=True, domain="", new_url=front_door.lan_url() or "/")


_PIN_RE = re.compile(r"^[a-z2-7]{26}$")


def _ready_partial(request: Request, pin: str, sent: int | None = None) -> HTMLResponse:
    try:
        r = runner_ws.tls_ready(pin)
    except Exception:  # noqa: BLE001 — a count must not break the page
        log.exception("tls_ready failed")
        r = {"ready": 0, "total": 0}
    return _partial(request, "front_door_renewal.html", ok=True, pin=pin, ready=r["ready"], total=r["total"], sent=sent)


@router.post("/lan-cert/prepare", response_class=HTMLResponse, dependencies=[FRESH_SHORT])
def lan_cert_prepare(request: Request):
    """Step one of a renewal: the service makes the new certificate, every connected runner is told it (signed), then the card shows how many
    computers that run agents are ready. 'Switch now' is offered only after this."""
    try:
        prepared = front_door.client().rotate_prepare()
        sent = runner_ws.rotate_tls_from_prepare(prepared)
        pin = runner_ws.pin26(runner_ws._pem_der(prepared["next_cert_pem"]))
    except front_door.FrontDoorError as exc:
        return _partial(request, "front_door_renewal.html", ok=False, problem=str(exc))
    except (ValueError, KeyError, TypeError):
        log.exception("lan cert prepare answer was not usable")
        return _partial(request, "front_door_renewal.html", ok=False, problem="The front door service gave an answer Crewboss could not use. Nothing was changed.")
    _audit("front_door.lan_cert_prepare", f"New network certificate prepared; {sent} computers told (user {_me(request)})")
    return _ready_partial(request, pin, sent)


@router.get("/lan-cert/ready", response_class=HTMLResponse)
def lan_cert_ready(request: Request, pin: str = ""):
    if not _PIN_RE.match(pin):
        raise HTTPException(400)
    return _ready_partial(request, pin)


@router.post("/lan-cert/switch", response_class=HTMLResponse, dependencies=[FRESH_SHORT])
def lan_cert_switch(request: Request):
    try:
        out = front_door.client().rotate_switch()
    except front_door.FrontDoorError as exc:
        return _partial(request, "front_door_switch.html", ok=False, problem=str(exc), domain="")
    if not out.get("ok"):
        return _partial(request, "front_door_switch.html", ok=False, problem=str(out.get("problem") or "It didn't work."), domain="")
    _audit("front_door.lan_cert", f"LAN certificate renewed (user {_me(request)})")
    return _partial(request, "front_door_switch.html", ok=True, renewed=True, domain="", new_url=front_door.lan_url() or "/")


@router.post("/dismiss", response_class=HTMLResponse)
def dismiss(request: Request):
    """Hide the 'someone reached sign-in from the internet' banner (D9). Owner only; it changes nothing but the banner."""
    front_door.dismiss_public_seen()
    return HTMLResponse("", headers=_NO_STORE)
