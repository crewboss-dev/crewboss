"""Dashboard sign-in pages (task #394): /auth/login, /auth/callback, /auth/logout, /auth/signed-out.
The rules and the gate are app/web_auth.py."""
from __future__ import annotations

import hmac
import logging
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app import config, web_auth
from app.routers import local_auth_pages
from app.templating import templates

log = logging.getLogger("crewboss.web_auth")

router = APIRouter(prefix="/auth", include_in_schema=False)


def _oidc_only() -> None:
    if web_auth.mode() != "oidc":
        raise HTTPException(404)

_NO_STORE = {"Cache-Control": "no-store"}


def _message(request: Request, title: str, message: str, status: int, detail: str = "",
             action_url: str = "/auth/login", action_label: str = "") -> HTMLResponse:
    return templates.TemplateResponse(
        request, "pages/auth_message.html",
        {"title": title, "message": message, "detail": detail, "action_url": action_url,
         "action_label": action_label or ("Sign in with Microsoft" if web_auth.mode() == "oidc" else "Sign in")},
        status_code=status, headers=_NO_STORE,
    )


def _set_cookie(resp: Response, name: str, value: str, max_age: int) -> None:
    # __Host- cookies: Secure, Path=/, no Domain (host-only). HttpOnly: no script reads them. Lax: sent on the
    # top-level GET back from Microsoft, never on another site's POST.
    resp.set_cookie(name, value, max_age=max_age, path="/", secure=True, httponly=True, samesite="lax")


def _clear_cookie(resp: Response, name: str) -> None:
    resp.delete_cookie(name, path="/", secure=True, httponly=True, samesite="lax")


@router.get("/login")
def login(request: Request, next: str = "/"):  # noqa: A002 — the query parameter's name
    if web_auth.mode() == "local":                      # task #706: the default; OIDC below is the optional mode
        return local_auth_pages.login_form(request, next)
    missing = web_auth.missing_config()
    if missing:
        return _message(request, "Sign-in isn't set up yet",
                        "The dashboard's Microsoft sign-in still needs: " + ", ".join(missing) + ".", 503,
                        action_url="", action_label="")
    host = (request.headers.get("host") or "").lower()
    if host != web_auth.public_host():
        # Cookies are per host and Microsoft only returns to the registered address: sign in there.
        return RedirectResponse(config.PUBLIC_URL + "/auth/login?" + urlencode({"next": web_auth.safe_next(next)}),
                                status_code=302, headers=_NO_STORE)
    state, url = web_auth.start_flow(next)
    resp = RedirectResponse(url, status_code=302, headers=_NO_STORE)
    _set_cookie(resp, web_auth.FLOW_COOKIE, state, config.OIDC_FLOW_MINUTES * 60)
    return resp


@router.get("/callback", dependencies=[Depends(_oidc_only)])
def callback(request: Request, code: str = "", state: str = "", error: str = "", error_description: str = ""):
    cookie_state = request.cookies.get(web_auth.FLOW_COOKIE) or ""
    if not state or not cookie_state or not hmac.compare_digest(state, cookie_state):
        return _message(request, "Sign-in expired", "That sign-in didn't start here, or took too long. Start again.",
                        400)
    flow = web_auth.take_flow(state)
    if not flow:
        return _message(request, "Sign-in expired", "That sign-in was already used or took too long. Start again.",
                        400)
    if error:
        log.warning("sign-in: Microsoft returned %s: %s", error[:80], error_description[:300])
        resp = _message(request, "Microsoft didn't sign you in", "Microsoft stopped the sign-in. Try again.", 400,
                        detail=error[:80])
        _clear_cookie(resp, web_auth.FLOW_COOKIE)
        return resp
    try:
        tokens = web_auth.exchange_code(code, flow["code_verifier"])
        claims = web_auth.verify_id_token(str(tokens.get("id_token") or ""), flow["nonce"])
    except web_auth.SignInError as exc:
        resp = _message(request, "Couldn't sign you in", str(exc), 400)
        _clear_cookie(resp, web_auth.FLOW_COOKIE)
        return resp
    if claims["oid"] not in config.SUPERADMIN_OIDS:
        log.warning("sign-in refused: oid %s (%s) is not a super admin", claims["oid"],
                    str(claims.get("preferred_username") or "")[:120])
        resp = _message(
            request, "Not allowed here",
            "You signed in with Microsoft, but this account isn't allowed into Crewboss. "
            "Only a super admin, set on the server, can use it.", 403,
            detail=f"{claims.get('preferred_username') or ''} · object id {claims['oid']}",
            action_url="", action_label="")
        _clear_cookie(resp, web_auth.FLOW_COOKIE)
        return resp
    web_auth.upsert_user(claims)  # the runners' users row (task #627)
    token = web_auth.create_session(claims, ip=request.headers.get("x-forwarded-for", "") or
                                    (request.client.host if request.client else ""),
                                    user_agent=request.headers.get("user-agent", ""))
    log.info("sign-in: %s", str(claims.get("preferred_username") or claims["oid"])[:120])
    resp = RedirectResponse(web_auth.safe_next(flow["next_path"]), status_code=303, headers=_NO_STORE)
    _set_cookie(resp, web_auth.SESSION_COOKIE, token, config.OIDC_SESSION_MINUTES * 60)
    _clear_cookie(resp, web_auth.FLOW_COOKIE)
    return resp


@router.post("/logout")
def logout(request: Request):
    web_auth.end_session(request.cookies.get(web_auth.session_cookie_name()))
    resp = RedirectResponse("/auth/signed-out", status_code=303, headers=_NO_STORE)
    web_auth.clear_cookie(resp, web_auth.session_cookie_name())
    return resp


@router.get("/signed-out")
def signed_out(request: Request):
    return _message(request, "Signed out", "You've signed out of Crewboss.", 200)
