"""Local sign-in pages (task #706; #976 slice A: password + authenticator app by default, passkeys optional): /auth/login (the email or
username), /auth/login/id (the password and the 6-digit code together, "Lost your phone? Use a recovery code", and the browser's passkey
prompt where passkeys are offered), /auth/enrol (the restricted session: choose a password, set up the authenticator app, save the
recovery codes) and /account/security. The rules are app/local_auth.py (credentials) and app/web_auth.py (sessions, the gate). Every
route 404s unless the instance is in local mode.

Every POST here is covered by app/origin_guard.py (they are page routes, not /api): a cross-site browser request is refused before it
reaches a handler. Answers never say whether an email or username exists: an unknown, a disabled and a known one all get the same
pages and the same generic failures (a wrong password, a wrong code, no password at all, a passkey-only person: all one answer)."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Depends, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from app import access, config, local_auth as la, task_summary, web_auth
from app.templating import templates

log = logging.getLogger("crewboss.web_auth")

_NO_STORE = {"Cache-Control": "no-store"}
FLOW_COOKIE = "__Host-crewboss_login"        # the identified-person step / a WebAuthn challenge
NOTICES = {"confirmed": "Confirmed.", "passkey-removed": "Passkey removed.", "passkey-added": "Passkey added.",
           "password-saved": "Password saved.", "offer-passkey": "You're in. Want to skip the password and code next time? Add a passkey below.",
           "add-backup": "You sign in with a passkey. Add a password and authenticator app below as a backup, so a lost device can't lock you out.",
           "email-saved": "Sign-in email saved.", "email-cleared": "Sign-in email removed.",
           "session-ended": "That session is signed out.", "prefs-saved": "Preference saved."}      # fixed texts: ?notice= is a key, never shown raw
GENERIC_FAIL = "Those sign-in details didn't work."
CODE_FAIL = "That code didn't work."
PASSWORD_FAIL = "That password didn't match."


def _local_only() -> None:
    if web_auth.mode() != "local":
        raise HTTPException(404)


router = APIRouter(include_in_schema=False, dependencies=[Depends(_local_only)])


# --- helpers --------------------------------------------------------------------------------------------------------

def _flow_cookie() -> str:
    return web_auth.cookie_name(FLOW_COOKIE)


def _page(request: Request, template: str, status: int = 200, **ctx) -> Response:
    ctx.setdefault("passkeys_enabled", la.passkeys_available(request))
    return templates.TemplateResponse(request, template, ctx, status_code=status, headers=_NO_STORE)


def _account_page(request: Request, status: int = 200, **ctx) -> Response:
    """Account security in the NORMAL chrome (header, menu, phone bar): it is a signed-in page, not a sign-in step (owner 11-16)."""
    from app.routers.pages import render
    ctx.setdefault("passkeys_enabled", la.passkeys_available(request))
    resp = render(request, "pages/account_security.html", "home", **ctx)
    resp.status_code = status
    resp.headers.update(_NO_STORE)
    return resp


def _session(request: Request) -> dict | None:
    return web_auth.resolve_session(request.cookies.get(web_auth.session_cookie_name()))


def _issue(resp: Response, request: Request, token: str, max_age: int | None = None) -> None:
    web_auth.set_cookie(resp, web_auth.session_cookie_name(), token, max_age or web_auth.session_minutes() * 60)


def _meta(request: Request) -> dict:
    return {"ip": la.client_ip(request), "user_agent": request.headers.get("user-agent", "")}


def _locked_page(request: Request, template: str, secs: int, **ctx) -> Response:
    mins = max(1, (secs + 59) // 60)
    resp = _page(request, template, 429, error=f"Too many attempts. Try again in about {mins} minute{'s' if mins != 1 else ''}.", **ctx)
    resp.headers["Retry-After"] = str(secs)
    return resp


def _locked_json(secs: int) -> Response:
    return JSONResponse({"error": "Too many attempts. Try again later."}, status_code=429, headers={"Retry-After": str(secs), **_NO_STORE})


def _limits(request: Request, name: str | None = None) -> list[tuple[str, int]]:
    """The keys a sign-in attempt is counted and limited by: the address, the RESOLVED username from that address, and the username
    alone (a hard ceiling). `name` is the user's username when the identifier matched someone (so an email and a username for one person
    share one counter), else the text typed (so an unknown name is counted the same way)."""
    ip = la.client_ip(request)
    keys = [(la.ip_key(ip), la.IP_THRESHOLD)]
    if name:
        keys.append((la.user_ip_key(name, ip), la.USER_THRESHOLD))
        keys.append((la.user_key(name), la.USER_CEILING))
    return keys


def _full_session(request: Request) -> dict:
    s = _session(request)
    if not s or s["enrolment_only"] or not s["user_id"]:
        raise HTTPException(401, "Sign in first")
    return s


def _usable(user: dict | None) -> dict | None:
    return user if user and not user["disabled_at"] else None


def _identified(flow: dict) -> tuple[dict | None, str]:
    """(the identified user or None, the name to count attempts under). A disabled user is returned as None for every purpose but the
    counter name, which is their username (so the counter is the same as before they were disabled)."""
    user = la.get_user(flow["user_id"]) if flow["user_id"] else None
    return _usable(user), (user["username"] if user else flow["identifier"])


# --- sign-in: identifier, then the passkey prompt or "another way" --------------------------------------------------------

def login_form(request: Request, next_path: str) -> Response:
    """GET /auth/login in local mode (auth_pages.login hands over here): ONE field, the email or username."""
    s = _session(request)
    if s and not s["enrolment_only"]:
        return RedirectResponse(web_auth.safe_next(next_path), status_code=303, headers=_NO_STORE)
    return _page(request, "pages/auth_login.html", step="identifier", next=web_auth.safe_next(next_path), error="",
                 no_owner=not la.owner_exists())


@router.post("/auth/login/identify")
def identify(request: Request, identifier: str = Form(""), next: str = Form("/")):  # noqa: A002
    """The identifier step. A known user, an unknown name and a disabled account all get a login_id flow and the same next page; the
    flow differs only in a user_id the browser never sees. Every submission is counted against the address."""
    nxt = web_auth.safe_next(next)
    ident = la.normalise_username(identifier)[:320]
    if not ident:
        return _page(request, "pages/auth_login.html", 400, step="identifier", next=nxt, error="Enter your email or username.",
                     no_owner=False)
    user = la.get_user_by_identifier(ident)
    ip_step = la.idstep_key(la.client_ip(request))
    # The address limits only: the per-person counters are enforced at the credential steps. Checking them here answered 429 for
    # an email that belongs to a locked username, so the step leaked which emails and usernames go together (Fable review #809).
    wait = la.lockout_seconds(_limits(request, None) + [(ip_step, la.ID_STEP_THRESHOLD)])
    if wait:
        return _locked_page(request, "pages/auth_login.html", wait, step="identifier", next=nxt, no_owner=False)
    la.record_failure(ip_step)
    old = request.cookies.get(_flow_cookie())
    if old:
        la.drop_flow(old)
    token = la.start_flow("login_id", user_id=user["id"] if user else None, next_path=nxt, identifier=ident)
    resp = RedirectResponse("/auth/login/id", status_code=303, headers=_NO_STORE)
    web_auth.set_cookie(resp, _flow_cookie(), token, la.FLOW_MINUTES * 60)
    return resp


def _id_ctx(flow: dict, **extra) -> dict:
    return dict(step="identified", next=flow["next_path"], identifier=flow["identifier"], no_owner=False, **extra)


@router.get("/auth/login/id")
def identified_form(request: Request):
    flow = la.peek_flow(request.cookies.get(_flow_cookie()), "login_id")
    if not flow:
        return RedirectResponse("/auth/login", status_code=303, headers=_NO_STORE)
    return _page(request, "pages/auth_login.html", error="", **_id_ctx(flow))


def _open_enrolment(request: Request, user: dict, method: str, kind: str, to: str = "/auth/enrol") -> Response:
    """A restricted session: it reaches nothing but the enrolment page until a passkey is added and the recovery codes are saved."""
    web_auth.end_session(request.cookies.get(web_auth.session_cookie_name()))
    token = web_auth.create_local_session(user, method, enrolment_only=True, enrol_kind=kind, **_meta(request))
    log.info("sign-in: %s opened a restricted enrolment session (%s)", user["username"], method)
    resp = RedirectResponse(to, status_code=303, headers=_NO_STORE)
    _issue(resp, request, token, config.ENROLMENT_SESSION_MINUTES * 60)
    web_auth.clear_cookie(resp, _flow_cookie())
    return resp


@router.post("/auth/login/id/password")
def identified_password(request: Request, password: str = Form(""), code: str = Form("")):
    """The normal sign-in: the password AND the 6-digit code from the authenticator app, in one step. Every way of failing (no such
    person, disabled, a passkey-only person with no password, a wrong password, a wrong or reused code) ends the same way: the same
    page, the same words, about the same time. A one-time TEMPORARY password an admin gave (code left empty) opens the restricted
    enrolment session instead. The code is spent only when the password was right too, and a code used here can't be used again at
    step-up (one shared last-used step)."""
    ftoken = request.cookies.get(_flow_cookie())
    flow = la.peek_flow(ftoken, "login_id")
    if not flow:
        return RedirectResponse("/auth/login", status_code=303, headers=_NO_STORE)
    user, name = _identified(flow)
    limits = _limits(request, name)
    wait = la.lockout_seconds(limits)
    if wait:
        return _locked_page(request, "pages/auth_login.html", wait, **_id_ctx(flow))
    ip = la.client_ip(request)
    stored = user["password_hash"] if user else None
    temp = bool(user and user["password_temp"])
    typed = password[:1024]
    pw_ok, new_hash = la.verify_password(stored, la._normal_code(typed) if temp else typed)    # a dummy hash when there is no hash
    step = la.match_totp_step(user, code[:64])        # spends nothing; a dummy secret when there is no usable user
    outcome = None
    if pw_ok and user:
        if temp:
            if la.consume_temp_password(user["id"], stored):
                la.drop_flow(ftoken)
                outcome = _open_enrolment(request, user, "temp", "password")
        elif la.has_mfa(user) and (locked := la.totp_lock_message(user)):
            # the password was right but too many codes in a row were wrong: say so (only here, so a stranger learns nothing)
            la.note_signin_ip(ip, False)
            return _page(request, "pages/auth_login.html", 401, error=locked, **_id_ctx(flow))
        elif la.has_mfa(user) and step is not None and la.consume_totp_step(user["id"], step):
            la.totp_reset(user["id"])
            la.drop_flow(ftoken)
            if new_hash:
                la.replace_hash_if_current(user["id"], stored, new_hash)
            web_auth.end_session(request.cookies.get(web_auth.session_cookie_name()))
            token = web_auth.create_local_session(user, "mfa", **_meta(request))
            log.info("sign-in: %s (password + authenticator)", user["username"])
            outcome = RedirectResponse(web_auth.safe_next(flow["next_path"]), status_code=303, headers=_NO_STORE)
            _issue(outcome, request, token)
            web_auth.clear_cookie(outcome, _flow_cookie())
    if outcome is None:
        if pw_ok and user and not temp and la.has_mfa(user):
            la.totp_failed(user["id"])          # right password, wrong or reused code: one more in the no-window streak
        la.record_failure(*[k for k, _ in limits])
        la.flow_failed(ftoken)
        la.note_signin_ip(ip, False)
        return _page(request, "pages/auth_login.html", 401, error=GENERIC_FAIL, **_id_ctx(flow))
    la.clear_failures(la.user_ip_key(name, ip))
    la.note_signin_ip(ip, True)
    return outcome


@router.post("/auth/login/id/recovery")
def identified_recovery(request: Request, code: str = Form("")):
    """"Use another way" > a recovery code. It is spent now and opens a restricted session that goes straight to adding a new passkey."""
    ftoken = request.cookies.get(_flow_cookie())
    flow = la.peek_flow(ftoken, "login_id")
    if not flow:
        return RedirectResponse("/auth/login", status_code=303, headers=_NO_STORE)
    user, name = _identified(flow)
    limits = _limits(request, name)
    wait = la.lockout_seconds(limits)
    if wait:
        return _locked_page(request, "pages/auth_login.html", wait, show="recovery", **_id_ctx(flow))
    if not la.use_recovery_code(user["id"] if user else None, code[:64]) or not user:
        la.record_failure(*[k for k, _ in limits])
        la.flow_failed(ftoken)
        return _page(request, "pages/auth_login.html", 401, error=CODE_FAIL, show="recovery", **_id_ctx(flow))
    la.drop_flow(ftoken)
    la.totp_reset(user["id"])         # a recovery code is a second factor: it clears the guess count (and the enrolment still needs the password)
    la.clear_failures(la.user_ip_key(name, la.client_ip(request)))
    return _open_enrolment(request, user, "recovery", "recovery")


def _need_passkeys(request: Request) -> None:
    if not la.passkeys_available(request):
        raise HTTPException(403, "Passkeys aren't available at this address")


@router.post("/auth/login/id/passkey/options")
def identified_passkey_options(request: Request):
    """Options for the browser's passkey prompt. allowCredentials is ALWAYS empty (discoverable credentials), whoever the email was, so
    the browser shows its own chooser, including "Use a phone or tablet" (the QR code), and the options say nothing about the account."""
    _need_passkeys(request)
    ftoken = request.cookies.get(_flow_cookie())
    flow = la.peek_flow(ftoken, "login_id")
    if not flow:
        return JSONResponse({"error": "Start again from the sign-in page."}, status_code=401, headers=_NO_STORE)
    _user, name = _identified(flow)
    wait = la.lockout_seconds(_limits(request, name))
    if wait:
        return _locked_json(wait)
    options_json, challenge = la.authentication_options(None, rp_id=la.rp_id_for(request))
    if not la.set_flow_challenge(ftoken, "login_id", challenge):
        return JSONResponse({"error": "Start again from the sign-in page."}, status_code=401, headers=_NO_STORE)
    return Response(options_json, media_type="application/json", headers=_NO_STORE)


@router.post("/auth/login/id/passkey/verify")
def identified_passkey_verify(request: Request, credential: dict = Body(...)):
    """A passkey assertion is a sign-in only if it verifies, with user verification, for THE identified user."""
    _need_passkeys(request)
    ftoken = request.cookies.get(_flow_cookie())
    flow = la.peek_flow(ftoken, "login_id")
    if not flow:
        return JSONResponse({"error": "Start again from the sign-in page."}, status_code=401, headers=_NO_STORE)
    user, name = _identified(flow)
    limits = _limits(request, name)
    wait = la.lockout_seconds(limits)
    if wait:
        return _locked_json(wait)
    challenge = la.take_flow_challenge(ftoken, "login_id")
    rp = la.rp_id_for(request)
    ok = bool(challenge and user and la.verify_authentication(credential, challenge, only_user_id=user["id"], rp_id=rp,
                                                              origin=la.origin_for(request, rp)) == user["id"])
    if not ok:
        la.note_signin_ip(la.client_ip(request), False)
        la.record_failure(*[k for k, _ in limits])
        la.flow_failed(ftoken)
        return JSONResponse({"error": "That passkey didn't work."}, status_code=401, headers=_NO_STORE)
    la.drop_flow(ftoken)
    la.clear_failures(la.user_ip_key(name, la.client_ip(request)))
    la.note_signin_ip(la.client_ip(request), True)
    web_auth.end_session(request.cookies.get(web_auth.session_cookie_name()))
    token = web_auth.create_local_session(user, "passkey", **_meta(request))
    log.info("sign-in: %s (passkey)", user["username"])
    target = "/account/security?notice=add-backup" if la.take_backup_nudge(user) else web_auth.safe_next(flow["next_path"])
    resp = JSONResponse({"redirect": target}, headers=_NO_STORE)
    _issue(resp, request, token)
    web_auth.clear_cookie(resp, _flow_cookie())
    return resp


# --- enrolment: the restricted session chooses a password, sets up the authenticator app, saves the recovery codes ---------------

def _enrol_session(request: Request) -> dict:
    s = _session(request)
    if not s or not s["user_id"]:
        raise HTTPException(401, "Sign in first")
    return s


def _stage(s: dict) -> str:
    """The restricted session's stage: 'password' -> 'totp' -> 'codes'. A session begun under #809 ('passkey') starts at 'password'."""
    return "password" if s["enrol_stage"] == "passkey" else s["enrol_stage"]


def _restricted(request: Request, want: str) -> dict | Response:
    """The restricted session, when it is at stage `want`; otherwise a redirect back to /auth/enrol (which shows the stage it is
    really at). A stage can only be moved forward by the handler that earns it, so none can be skipped."""
    s = _enrol_session(request)
    if not s["enrolment_only"]:
        return RedirectResponse("/account/security", status_code=303, headers=_NO_STORE)
    if _stage(s) != want:
        return RedirectResponse("/auth/enrol", status_code=303, headers=_NO_STORE)
    return s


def _enrol_ctx(s: dict, **extra) -> dict:
    user = la.get_user(s["user_id"])
    return dict(user=user, stage=_stage(s), kind=s["enrol_kind"], codes=extra.pop("codes", None), error=extra.pop("error", ""),
                totp=extra.pop("totp", None), **extra)


def _totp_ctx(user: dict) -> dict:
    """The QR code and typed-in key for the pending authenticator secret (a reload shows the same one, so a scanned code stays valid)."""
    secret = la.pending_totp(user["id"])
    if not secret:
        secret, _uri, qr = la.begin_totp(user)
    else:
        secret, _uri, qr = la.totp_view(secret, user)
    return {"secret": " ".join(secret[i:i + 4] for i in range(0, len(secret), 4)), "qr": qr}


@router.get("/auth/enrol", name="enrol")
def enrol_page(request: Request):
    s = _session(request)
    if not s or not s["user_id"]:
        return RedirectResponse("/auth/login", status_code=303, headers=_NO_STORE)
    if not s["enrolment_only"]:
        return RedirectResponse("/account/security", status_code=303, headers=_NO_STORE)
    ctx = _enrol_ctx(s)
    if ctx["stage"] == "totp":
        if problem := la.key_problem():
            return _page(request, "pages/auth_enrol.html", 503, **{**ctx, "error": problem})
        ctx["totp"] = _totp_ctx(ctx["user"])
    return _page(request, "pages/auth_enrol.html", **ctx)


@router.post("/auth/enrol/password")
def enrol_password(request: Request, password: str = Form(""), confirm: str = Form(""), current: str = Form("")):
    """Stage 1. After a temporary password: choose a password. After a RECOVERY code (owner q828, "phone only; password still needed"): type
    the CURRENT password, which is kept; someone who lost both goes to an admin's Reset sign-in (or reset-owner). Then the authenticator app."""
    s = _restricted(request, "password")
    if isinstance(s, Response):
        return s
    user = la.get_user(s["user_id"])
    if problem := la.key_problem():
        return _page(request, "pages/auth_enrol.html", 503, **_enrol_ctx(s, error=problem))
    if s["enrol_kind"] == "recovery":
        limits = _limits(request, user["username"])
        if wait := la.lockout_seconds(limits):
            return _locked_page(request, "pages/auth_enrol.html", wait, **_enrol_ctx(s))
        stored = user["password_hash"] if not user["password_temp"] else None
        ok, _new = la.verify_password(stored, current[:1024])
        if not ok:
            la.record_failure(*[k for k, _ in limits])
            return _page(request, "pages/auth_enrol.html", 400, **_enrol_ctx(s, error=PASSWORD_FAIL))
    else:
        if password != confirm:
            return _page(request, "pages/auth_enrol.html", 400, **_enrol_ctx(s, error="The two passwords don't match."))
        if problem := la.password_problem(password, user["username"] or ""):
            return _page(request, "pages/auth_enrol.html", 400, **_enrol_ctx(s, error=problem))
        la.set_chosen_password(user["id"], password)
    la.begin_totp(user)                       # a NEW authenticator secret, pending until a correct code confirms it
    web_auth.set_enrol_stage(s["id"], "totp")
    return RedirectResponse("/auth/enrol", status_code=303, headers=_NO_STORE)


@router.post("/auth/enrol/totp")
def enrol_totp(request: Request, code: str = Form("")):
    """Stage 2: a correct 6-digit code from the new authenticator makes it live (and replaces any old one). Then the recovery codes."""
    s = _restricted(request, "totp")
    if isinstance(s, Response):
        return s
    user = la.get_user(s["user_id"])
    limits = _limits(request, user["username"])
    if wait := la.lockout_seconds(limits):
        return _locked_page(request, "pages/auth_enrol.html", wait, **_enrol_ctx(s, totp=_totp_ctx(user)))
    if not la.confirm_totp(user["id"], code[:32]):
        la.record_failure(*[k for k, _ in limits])
        return _page(request, "pages/auth_enrol.html", 400, **_enrol_ctx(s, error=CODE_FAIL, totp=_totp_ctx(user)))
    la.end_other_sessions(user["id"], s["id"])      # whoever held the old authenticator is out the moment the new one is live (this session stays)
    web_auth.set_enrol_stage(s["id"], "codes")
    return RedirectResponse("/auth/enrol", status_code=303, headers=_NO_STORE)


def _codes_session(request: Request) -> dict:
    """Stage 3 needs the stage, a password AND an authenticator confirmed by the stages before it."""
    s = _enrol_session(request)
    if not s["enrolment_only"] or _stage(s) != "codes" or not la.has_mfa(la.get_user(s["user_id"])):
        raise HTTPException(409, "Finish the earlier steps first")
    return s


@router.post("/auth/enrol/codes")
def enrol_codes(request: Request):
    """Make the recovery codes (they replace any earlier set, which is void from now) and show them ONCE."""
    s = _codes_session(request)
    if problem := la.key_problem():
        return _page(request, "pages/auth_enrol.html", 503, **_enrol_ctx(s, error=problem))
    return _page(request, "pages/auth_enrol.html", **_enrol_ctx(s, codes=la.new_recovery_codes(s["user_id"])))


@router.post("/auth/enrol/complete")
def enrol_complete(request: Request, saved: str = Form("")):
    """"I've saved them": the restricted session ends and a normal one starts (the password stays). Where passkeys are on offer the
    person lands on the account page's optional passkey upgrade."""
    s = _codes_session(request)
    if saved not in ("on", "1", "true") or not la.fresh_recovery_codes_since(s["user_id"], s["id"]):
        return _page(request, "pages/auth_enrol.html", 400, **_enrol_ctx(s, error="Make your recovery codes and tick that you've saved them first."))
    user = la.get_user(s["user_id"])
    web_auth.end_user_sessions(user["id"])          # a finished enrolment (a recovery code, a new authenticator) ends EVERY earlier sign-in
    token = web_auth.create_local_session(user, "mfa", **_meta(request))
    log.info("sign-in: %s finished enrolment (%s)", user["username"], s["enrol_kind"])
    resp = RedirectResponse("/account/security?notice=offer-passkey" if la.passkeys_available(request) else "/", status_code=303,
                            headers=_NO_STORE)
    _issue(resp, request, token)
    return resp


# --- the account page --------------------------------------------------------------------------------------------------

def _account_ctx(request: Request, s: dict, **extra) -> dict:
    user = la.get_user(s["user_id"])
    passkeys = la.list_passkeys(user["id"])
    return dict(user=user, passkeys=passkeys, has_mfa=la.has_mfa(user), has_totp=bool(user["totp_confirmed_at"]),
                has_password=bool(user["password_hash"] and not user["password_temp"]),
                recovery_left=la.recovery_codes_left(user["id"]), factor_fresh=s["factor_fresh"], error=extra.pop("error", ""),
                notice=extra.pop("notice", ""), codes=extra.pop("codes", None), totp=extra.pop("totp", None),
                sessions=web_auth.list_user_sessions(user["id"], s["id"]), **extra)


@router.get("/account/security")
def account_page(request: Request):
    s = _full_session(request)
    return _account_page(request, **_account_ctx(request, s, notice=NOTICES.get(request.query_params.get("notice", ""), "")))


prefs_router = APIRouter(include_in_schema=False)      # every sign-in mode (the rest of this module is local-only)


def _pref_user(request: Request) -> dict:
    """Whoever is signed in, local account or Microsoft (OIDC) sign-in: preferences are the person's own and need no fresh factor."""
    try:
        return web_auth.signed_in_user_row(request)
    except PermissionError:
        raise HTTPException(401, "Sign in first") from None


@prefs_router.get("/account/preferences")
def preferences_page(request: Request):
    from app.routers.pages import render
    user = _pref_user(request)
    resp = render(request, "pages/account_preferences.html", "home", runner_summaries=task_summary.runner_summaries_on(user["id"]),
                  notice=NOTICES.get(request.query_params.get("notice", ""), ""))
    resp.headers.update(_NO_STORE)
    return resp


@prefs_router.post("/account/preferences/runner-summaries")
def set_runner_summaries(request: Request, on: str = Form("")):
    """Your own preference "Use my runner for task summaries" (users.use_runner_summaries)."""
    task_summary.set_runner_summaries(_pref_user(request)["id"], on == "1")
    return RedirectResponse("/account/preferences?notice=prefs-saved", status_code=303, headers=_NO_STORE)


@router.get("/account/security/stepup-info")
def stepup_info(request: Request):
    """For the step-up dialog: may it offer 'Use my passkey instead'? (Passkeys available here AND this person has one.)"""
    s = _full_session(request)
    return JSONResponse({"passkey": bool(la.passkeys_available(request) and la.passkey_count(s["user_id"]))}, headers=_NO_STORE)


@router.post("/account/security/reauth")
def reauth(request: Request, code: str = Form("")):
    """Confirm it's you again with a fresh 6-digit authenticator code (never a recovery code: those aren't burned on step-ups).
    Sensitive actions then work for 5 minutes. The code shares its replay guard with sign-in, so a code used there can't be reused."""
    s = _full_session(request)
    user = la.get_user(s["user_id"])
    wants_json = "application/json" in (request.headers.get("accept") or "")
    limits = _limits(request, user["username"])
    wait = la.lockout_seconds(limits)
    if wait:
        return _locked_json(wait) if wants_json else _locked_page(request, "pages/account_security.html", wait, **_account_ctx(request, s))
    if msg := la.totp_lock_message(user):
        if wants_json:
            return JSONResponse({"error": msg}, status_code=429, headers=_NO_STORE)
        return _account_page(request, 429, **_account_ctx(request, s, error=msg))
    if not la.verify_totp(user, code[:32], count_failure=False):
        la.record_failure(*[k for k, _ in limits])
        if la.step_up_failed(s["id"], user["id"]):           # 10 wrong codes in this session: it is signed out (the sign-in lock is untouched)
            if wants_json:
                resp = JSONResponse({"error": "Too many wrong codes. You have been signed out."}, status_code=401, headers=_NO_STORE)
            else:
                resp = RedirectResponse("/auth/login", status_code=303, headers=_NO_STORE)
            web_auth.clear_cookie(resp, web_auth.session_cookie_name())
            return resp
        if wants_json:
            return JSONResponse({"error": "That code didn't work. If you just signed in, wait for the next 6-digit code."},
                                status_code=401, headers=_NO_STORE)
        return _account_page(request, 400, **_account_ctx(request, s, error=CODE_FAIL))
    web_auth.mark_factor_fresh(s["id"])
    if wants_json:
        return JSONResponse({"ok": True}, headers=_NO_STORE)
    return RedirectResponse("/account/security?notice=confirmed", status_code=303, headers=_NO_STORE)


@router.post("/account/security/passkey/options")
def reauth_passkey_options(request: Request):
    _need_passkeys(request)
    s = _full_session(request)
    options_json, challenge = la.authentication_options(s["user_id"], rp_id=la.rp_id_for(request))
    token = la.start_flow("passkey_auth", user_id=s["user_id"], challenge=challenge, session_id=s["id"])
    resp = Response(options_json, media_type="application/json", headers=_NO_STORE)
    web_auth.set_cookie(resp, _flow_cookie(), token, la.FLOW_MINUTES * 60)
    return resp


@router.post("/account/security/passkey/verify")
def reauth_passkey_verify(request: Request, credential: dict = Body(...)):
    _need_passkeys(request)
    s = _full_session(request)
    flow = la.take_flow(request.cookies.get(_flow_cookie()), "passkey_auth")
    rp = la.rp_id_for(request)
    if (not flow or flow["session_id"] != s["id"] or flow["user_id"] != s["user_id"]
            or la.verify_authentication(credential, bytes(flow["challenge"]), only_user_id=s["user_id"], rp_id=rp,
                                        origin=la.origin_for(request, rp)) != s["user_id"]):
        la.record_failure(la.ip_key(la.client_ip(request)))
        return JSONResponse({"error": "That passkey didn't work."}, status_code=401, headers=_NO_STORE)
    web_auth.mark_factor_fresh(s["id"])           # the 5-minute account actions AND the step-up window count from now
    return JSONResponse({"redirect": "/account/security?notice=confirmed"}, headers=_NO_STORE)


def _account_fresh(request: Request) -> tuple[dict, Response | None]:
    s = _full_session(request)
    if s["factor_fresh"]:
        return s, None
    return s, _account_page(request, 403, **_account_ctx(request, s, error="Confirm it's you first (the box at the top)."))


# --- adding passkeys (optional) and the password + authenticator backup, from a signed-in session ---------------------------

@router.post("/auth/enrol/passkey/options")
def passkey_reg_options(request: Request):
    _need_passkeys(request)
    s, refuse = _account_fresh(request)
    if refuse:
        return JSONResponse({"error": "Confirm it's you first."}, status_code=403, headers=_NO_STORE)
    options_json, challenge = la.registration_options(la.get_user(s["user_id"]), rp_id=la.rp_id_for(request))
    token = la.start_flow("passkey_reg", user_id=s["user_id"], challenge=challenge, session_id=s["id"])
    resp = Response(options_json, media_type="application/json", headers=_NO_STORE)
    web_auth.set_cookie(resp, _flow_cookie(), token, la.FLOW_MINUTES * 60)
    return resp


@router.post("/auth/enrol/passkey/verify")
def passkey_reg_verify(request: Request, payload: dict = Body(...)):
    _need_passkeys(request)
    s, refuse = _account_fresh(request)
    if refuse:
        return JSONResponse({"error": "Confirm it's you first."}, status_code=403, headers=_NO_STORE)
    flow = la.take_flow(request.cookies.get(_flow_cookie()), "passkey_reg")
    credential = payload.get("credential")
    rp = la.rp_id_for(request)
    if (not flow or flow["user_id"] != s["user_id"] or flow["session_id"] != s["id"] or not isinstance(credential, dict)
            or not la.verify_registration(s["user_id"], credential, bytes(flow["challenge"]), str(payload.get("name") or ""),
                                          rp_id=rp, origin=la.origin_for(request, rp))):
        return JSONResponse({"error": "That passkey couldn't be added. Try again."}, status_code=400, headers=_NO_STORE)
    resp = JSONResponse({"ok": True, "redirect": "/account/security?notice=passkey-added"}, headers=_NO_STORE)
    web_auth.clear_cookie(resp, _flow_cookie())
    return resp


@router.post("/account/security/password")
def set_password(request: Request, password: str = Form(""), confirm: str = Form("")):
    """Choose or change the password (needs the fresh confirmation). It signs nobody in until an authenticator app is set up too."""
    s, refuse = _account_fresh(request)
    if refuse:
        return refuse
    user = la.get_user(s["user_id"])
    problem = ("The two passwords don't match." if password != confirm else la.password_problem(password, user["username"] or ""))
    if problem:
        return _account_page(request, 400, **_account_ctx(request, s, error=problem))
    la.set_chosen_password(user["id"], password)
    la.end_other_sessions(user["id"], s["id"])
    return RedirectResponse("/account/security?notice=password-saved", status_code=303, headers=_NO_STORE)


def _password_first(s: dict) -> str:
    """The authenticator app is step 2 on the account page: it is refused until a password has been chosen (a temporary one does not count)."""
    user = la.get_user(s["user_id"])
    return "" if (user["password_hash"] and not user["password_temp"]) else "Choose a password first (step 1), then set up the authenticator app."


@router.post("/account/security/authenticator/start")
def authenticator_start(request: Request):
    s, refuse = _account_fresh(request)
    if refuse:
        return refuse
    if msg := _password_first(s):
        return _account_page(request, 400, **_account_ctx(request, s, error=msg))
    if problem := la.key_problem():
        return _account_page(request, 503, **_account_ctx(request, s, error=problem))
    user = la.get_user(s["user_id"])
    _secret, _uri, qr = la.begin_totp(user)
    return _account_page(request, **_account_ctx(request, s, totp=_totp_ctx(user)))


@router.post("/account/security/authenticator/confirm")
def authenticator_confirm(request: Request, code: str = Form("")):
    """A correct code from the new authenticator makes it live (replacing any old one) and gives a fresh set of recovery codes."""
    s, refuse = _account_fresh(request)
    if refuse:
        return refuse
    if msg := _password_first(s):
        return _account_page(request, 400, **_account_ctx(request, s, error=msg))
    user = la.get_user(s["user_id"])
    limits = _limits(request, user["username"])
    if wait := la.lockout_seconds(limits):
        return _locked_page(request, "pages/account_security.html", wait, **_account_ctx(request, s))
    if not la.confirm_totp(user["id"], code[:32]):
        la.record_failure(*[k for k, _ in limits])
        return _account_page(request, 400, **_account_ctx(request, s, error=CODE_FAIL, totp=_totp_ctx(user)))
    la.end_other_sessions(user["id"], s["id"])
    return _account_page(request, **_account_ctx(request, s, codes=la.new_recovery_codes(user["id"]), notice="Authenticator app set up."))


@router.post("/account/security/passkeys/{cred_id}/remove")
def passkey_remove(request: Request, cred_id: int):
    s, refuse = _account_fresh(request)
    if refuse:
        return refuse
    if not la.remove_passkey(s["user_id"], cred_id):
        return _account_page(request, 400,
                     **_account_ctx(request, s, error="That passkey can't be removed: it is your only way in. Set up a password and authenticator app, or add another passkey, first."))
    return RedirectResponse("/account/security?notice=passkey-removed", status_code=303, headers=_NO_STORE)


@router.post("/account/security/recovery/regenerate")
def recovery_regenerate(request: Request):
    s, refuse = _account_fresh(request)
    if refuse:
        return refuse
    codes = la.new_recovery_codes(s["user_id"])
    la.end_other_sessions(s["user_id"], s["id"])    # the old codes are void, and so is every other sign-in made with them
    return _account_page(request, **_account_ctx(request, s, codes=codes))


@router.post("/account/security/email")
def set_email(request: Request, email: str = Form("")):
    """The sign-in email: an identifier only, never mailed. Changing it needs the same fresh second factor as other sign-in settings."""
    s, refuse = _account_fresh(request)
    if refuse:
        return refuse
    try:
        la.set_email(s["user_id"], email)
    except ValueError as exc:
        return _account_page(request, 400, **_account_ctx(request, s, error=str(exc)))
    except la.EmailTaken:
        return _account_page(request, 400, **_account_ctx(request, s, error="That email is already used for another sign-in."))
    return RedirectResponse("/account/security?notice=" + ("email-saved" if email.strip() else "email-cleared"), status_code=303,
                            headers=_NO_STORE)


@router.post("/account/security/sessions/{session_id}/end")
def session_end(request: Request, session_id: int):
    """Sign ONE of this user's sessions out (another browser, or this one). Not a sensitive change, so no fresh factor needed."""
    s = _full_session(request)
    web_auth.end_user_session(s["user_id"], session_id)
    if session_id == s["id"]:
        resp = RedirectResponse("/auth/signed-out", status_code=303, headers=_NO_STORE)
        web_auth.clear_cookie(resp, web_auth.session_cookie_name())
        return resp
    return RedirectResponse("/account/security?notice=session-ended", status_code=303, headers=_NO_STORE)


@router.post("/account/security/signout-everywhere")
def signout_everywhere(request: Request):
    s, refuse = _account_fresh(request)
    if refuse:
        return refuse
    from app.db import get_cursor
    with get_cursor() as cur:
        cur.execute("UPDATE users SET session_epoch = session_epoch + 1 WHERE id = %s", (s["user_id"],))
    web_auth.end_user_sessions(s["user_id"])
    resp = RedirectResponse("/auth/signed-out", status_code=303, headers=_NO_STORE)
    web_auth.clear_cookie(resp, web_auth.session_cookie_name())
    return resp
