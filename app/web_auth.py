"""Dashboard sign-in (task #394): Microsoft (Entra) OIDC for the owner's web pages.

The pages used to take no login (NetBird was the only gate). Now every page needs a signed-in SUPER ADMIN: an Entra
account in the owner's tenant whose object id (`oid`) is in config.SUPERADMIN_OIDS, a server setting that no page can
change (#503). Tenant membership or Microsoft groups grant nothing. /api and /mcp are unchanged: they authenticate with
Bearer tokens (app/auth.py, #368) and never look at a browser session.

Flow: authorization code + PKCE (S256) against the single tenant, with a confidential-client secret read from a file.
  /auth/login     makes a one-time flow (state, nonce, PKCE verifier) server-side, puts the state in a short-lived
                  __Host- cookie, and sends the browser to Microsoft.
  /auth/callback  requires the returned state to equal that cookie's, uses the flow ONCE, swaps the code for tokens,
                  checks the ID token (RS256 against the tenant's keys, issuer, audience, tenant, nonce, expiry), checks
                  the oid against the allowlist and only then makes a session.
  /auth/logout    (POST, same-origin by origin_guard) ends the session.
The session is a random token in a __Host- cookie (Secure, HttpOnly, SameSite=Lax, host-only); only its hash is
stored. An OIDC session ends 120 minutes after sign-in however busy it is (owner q296; local sessions last 30 days, #809), and every request re-checks the allowlist,
so taking an oid out of the setting ends that person's sessions at once.

Everything runs on ONE host, config.PUBLIC_URL (the redirect URI registered in Entra): a request on another dashboard
name is sent there to sign in, so the cookies and the callback always share a host.

Fail closed: with sign-in required (the default) but not configured, pages answer 503, never open.

Task #706 adds a second mode, LOCAL sign-in (app/local_auth.py): Crewboss's own username + password + second factor or
passkey. mode() picks between them (instance.yaml `auth:`; see there); everything below that is about sessions and the gate
serves both. A local session carries its user_id and the user's session epoch (a stale epoch ends it), idles out, and a first-run
`enrolment_only` session reaches nothing but /auth/*."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import urllib.error
import urllib.parse
import urllib.request
from functools import lru_cache
from pathlib import Path

import jwt
from starlette.concurrency import run_in_threadpool
from starlette.responses import PlainTextResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from app import access, config
from app.db import get_cursor

log = logging.getLogger("crewboss.web_auth")

SESSION_COOKIE = "__Host-crewboss_session"
FLOW_COOKIE = "__Host-crewboss_flow"
SCOPES = "openid profile email"
LEEWAY_SECONDS = 120
HTTP_TIMEOUT = 10

# Paths that never need a browser session: Bearer-token APIs, the sign-in itself, and what a browser or installer
# fetches before anyone is signed in (static files, icons, the manifest, the service worker) or a monitor polls.
# /runner/ (task #627) authenticates by enrolment code or device signature, not a browser session (a runner has none).
_OPEN_PREFIXES = ("/api/", "/mcp/", "/static/", "/auth/", "/runner/")
_OPEN_EXACT = frozenset({"/api", "/mcp", "/healthz", "/trust", "/favicon.ico", "/manifest.webmanifest", "/sw.js",
                         # root-convention icon + manifest paths an installer finds without signing in (#750)
                         "/manifest.json", "/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"})
# ...except FastAPI's own docs page, which lives under /api but is a page: owner only.
_GATED_UNDER_API = ("/api/docs",)


def _authority() -> str:
    return f"https://login.microsoftonline.com/{config.OIDC_TENANT_ID}"


def issuer() -> str:
    return f"{_authority()}/v2.0"


def redirect_uri() -> str:
    return f"{config.PUBLIC_URL}/auth/callback"


def public_host() -> str:
    return (urllib.parse.urlsplit(config.PUBLIC_URL).netloc or "").lower()


def client_secret() -> str:
    try:
        return Path(config.OIDC_CLIENT_SECRET_FILE).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def missing_config() -> list[str]:
    """What sign-in still needs ([] = ready). Names only, never values."""
    missing = []
    if not config.OIDC_TENANT_ID:
        missing.append("tenant id (CREWBOSS_OIDC_TENANT_ID)")
    if not config.OIDC_CLIENT_ID:
        missing.append("client id (CREWBOSS_OIDC_CLIENT_ID)")
    if not client_secret():
        missing.append("client secret file")
    # An empty super admin allowlist is NOT missing config: sign-in runs and refuses everyone, showing each person
    # their own object id, which is how the first super admin finds the value to put in CREWBOSS_SUPERADMIN_OIDS.
    return missing


def startup_check() -> None:
    """Start-up notes for the operator (task #706): which sign-in is active, and what is missing for it."""
    if not required():
        log.warning("sign-in is OFF (CREWBOSS_WEB_AUTH=off): every page is open")
        return
    m = mode()
    log.info("sign-in mode: %s", m)
    if m == "local":
        from app import local_auth
        warning = local_auth.startup_warning()
        if warning:
            log.warning(warning)
        try:
            n = local_auth.backfill_rp_id()
            if n:
                log.info("local sign-in: stamped %d existing passkey(s) with the RP ID %s", n, local_auth.public_host())
        except Exception:  # noqa: BLE001 — the column arrives with migration 0118; a missing one must not stop start-up
            log.exception("local sign-in: could not backfill passkey RP IDs")
        try:
            if local_auth.clear_stale_totp():
                log.info("local sign-in: cleared stale authenticator secrets on people with no password")
        except Exception:  # noqa: BLE001
            log.exception("local sign-in: could not clear stale authenticator secrets")
        if not local_auth.owner_exists():
            log.warning("local sign-in: no owner account yet. Create it on the server: python -m app.cli owner create")


def required() -> bool:
    return config.WEB_AUTH != "off"


def mode() -> str:
    """'local' or 'oidc': how people sign in. An explicit `auth:` setting wins. Unset: OIDC when it is already configured
    (a client id; a missing secret file then fails closed, as before), so an install that has Microsoft sign-in today keeps it when it gains
    this code; otherwise local, the default (owner q506). Read live (a stat), not at import."""
    if config.AUTH_SETTING:
        return config.AUTH_SETTING
    if config.OIDC_CLIENT_ID:
        return "oidc"
    return "local"


def signin_label(user: dict | None) -> str:
    """The second line of the menu's account block: how this person signed in. A local session carries a users.id; an OIDC one
    does not. No user (sign-in off) is a neutral 'Sign-in off'."""
    if not user:
        return "Sign-in off"
    return "Local sign-in" if user.get("user_id") else "Microsoft sign-in"


def cookie_secure() -> bool:
    """OIDC sessions are always Secure (__Host-). Local sessions are Secure when the public URL is https; over plain http
    (a LAN box, localhost) the browser would drop a Secure cookie, so it is sent without."""
    from app import local_auth
    return mode() == "oidc" or local_auth.cookie_secure()


def cookie_name(base: str) -> str:
    """'__Host-crewboss_session' when Secure (the __Host- prefix demands it); the bare name otherwise."""
    return base if cookie_secure() else base.removeprefix("__Host-")


def session_cookie_name() -> str:
    return cookie_name(SESSION_COOKIE)


def set_cookie(resp, name: str, value: str, max_age: int) -> None:
    # HttpOnly: no script reads it. Lax: sent on a top-level GET from elsewhere, never on another site's POST.
    resp.set_cookie(name, value, max_age=max_age, path="/", secure=cookie_secure(), httponly=True, samesite="lax")


def clear_cookie(resp, name: str) -> None:
    resp.delete_cookie(name, path="/", secure=cookie_secure(), httponly=True, samesite="lax")


def is_open_path(path: str) -> bool:
    if any(path == p or path.startswith(p + "/") for p in _GATED_UNDER_API):
        return False
    return path in _OPEN_EXACT or path.startswith(_OPEN_PREFIXES)


def safe_next(value: str | None) -> str:
    """A same-site path to return to after sign-in; anything else (another host, //host, a backslash trick, the
    sign-in pages themselves) becomes '/'."""
    v = (value or "").strip()
    if (not v.startswith("/") or v.startswith("//") or "\\" in v or len(v) > 2000
            or any(ord(c) < 32 for c in v) or v.startswith("/auth/")):
        return "/"
    return v


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


# --- sessions ----------------------------------------------------------------------------------------------------

def create_session(claims: dict, ip: str = "", user_agent: str = "") -> str:
    token = secrets.token_urlsafe(32)
    amr = claims.get("amr") if isinstance(claims.get("amr"), list) else []
    auth_time = claims.get("auth_time") or claims.get("iat")
    with get_cursor() as cur:
        cur.execute("DELETE FROM web_sessions WHERE expires_at < now()")
        cur.execute(
            "INSERT INTO web_sessions (token_hash, oid, name, username, amr, auth_time, expires_at, ip, user_agent) "
            "VALUES (%s, %s, %s, %s, %s, to_timestamp(%s), now() + make_interval(mins => %s), %s, %s)",
            (_hash(token), claims["oid"], str(claims.get("name") or "")[:200],
             str(claims.get("preferred_username") or "")[:200], [str(a)[:40] for a in amr][:10],
             float(auth_time), config.OIDC_SESSION_MINUTES, ip[:100], user_agent[:300]),
        )
    return token


# --- how long a local sign-in lasts (the owner's global setting, #809) ------------------------------------------------------

SESSION_CHOICES = ((60, "1 hour"), (480, "8 hours"), (1440, "1 day"), (10080, "7 days"), (43200, "30 days"), (129600, "90 days"))
SESSION_SETTING = "session_minutes"


def session_minutes() -> int:
    """How long a local sign-in lasts: the instance setting if the owner made one, else config.WEB_SESSION_MINUTES (30 days). Read
    live, so a change applies to every session at once (resolve_session checks created_at + this)."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT value FROM instance_settings WHERE key = %s", (SESSION_SETTING,))
        row = cur.fetchone()
    try:
        v = int(row["value"]) if row else config.WEB_SESSION_MINUTES
    except (TypeError, ValueError):
        v = config.WEB_SESSION_MINUTES
    return v


def idle_minutes() -> int:
    """The idle limit follows the session length: min(session length, WEB_IDLE_MINUTES), so a 1-hour setting is really 1 hour."""
    return min(session_minutes(), config.WEB_IDLE_MINUTES)


def set_session_minutes(minutes: int, by_user_id: int | None) -> tuple[int, int]:
    """Set it (one of SESSION_CHOICES only; ValueError otherwise), log who changed what, and end every session that is already past
    its new length. Returns (old, new). Lengthening never extends a session already issued (its own expires_at still caps it)."""
    if minutes not in dict(SESSION_CHOICES):
        raise ValueError("choose one of the listed lengths")
    old = session_minutes()
    labels = dict(SESSION_CHOICES)
    with get_cursor() as cur:
        cur.execute("INSERT INTO instance_settings (key, value, updated_by) VALUES (%s, %s, %s) ON CONFLICT (key) DO UPDATE "
                    "SET value = EXCLUDED.value, updated_at = now(), updated_by = EXCLUDED.updated_by",
                    (SESSION_SETTING, str(minutes), by_user_id))
        cur.execute("DELETE FROM web_sessions WHERE user_id IS NOT NULL AND NOT enrolment_only "
                    "AND created_at + make_interval(mins => %s) <= now()", (minutes,))
        access.audit(cur, "sign-in length", f"Sign-in lasts {labels.get(minutes)} (was {labels.get(old, str(old) + ' minutes')})")
    return old, minutes


def create_local_session(user: dict, method: str, ip: str = "", user_agent: str = "", enrolment_only: bool = False,
                         enrol_kind: str | None = None) -> str:
    """A session for a local user (task #706). `method` is how they proved themselves: 'mfa' (password + authenticator code) or 'passkey'
    (a normal session; either sign-in IS the second factor that makes step-up fresh), or, for an enrolment-only session, 'temp' (the
    one-time temporary password) or 'recovery' (a recovery code); `enrol_kind` says which of 'password' / 'recovery' it is. An
    enrolment-only session starts at enrol_stage 'password' (then 'totp', then 'codes'). A fresh
    random token every time, so any earlier session id is dead at sign-in."""
    token = secrets.token_urlsafe(32)
    amr = {"passkey": ["hwk", "user"], "mfa": ["pwd", "otp"], "temp": ["pwd"], "recovery": ["rcv"]}.get(method, [])
    if enrolment_only and enrol_kind not in ("password", "recovery"):
        raise ValueError("an enrolment-only session needs an enrol_kind")
    minutes = config.ENROLMENT_SESSION_MINUTES if enrolment_only else session_minutes()
    with get_cursor() as cur:
        cur.execute("DELETE FROM web_sessions WHERE expires_at < now()")
        cur.execute(
            "INSERT INTO web_sessions (token_hash, oid, name, username, amr, auth_time, expires_at, ip, user_agent, "
            "user_id, epoch, enrolment_only, second_factor_at, enrol_kind) "
            "VALUES (%s, %s, %s, %s, %s, now(), now() + make_interval(mins => %s), %s, %s, %s, %s, %s, "
            "CASE WHEN %s THEN NULL ELSE now() END, %s)",
            (_hash(token), user["oid"], (user.get("display_name") or "")[:200], (user.get("username") or "")[:200], amr,
             minutes, ip[:100], user_agent[:300], user["id"], user["session_epoch"], enrolment_only, enrolment_only, enrol_kind),
        )
    return token


def resolve_session(token: str | None) -> dict | None:
    """The signed-in user for a session cookie, or None (unknown, expired, no longer allowed, or - for a local session -
    idle too long, or issued under an older session epoch)."""
    if not token or len(token) > 200:
        return None
    idle, length = idle_minutes(), session_minutes()
    with get_cursor() as cur:
        cur.execute("SELECT s.id, s.oid, s.name, s.username, s.amr, s.auth_time, s.expires_at, s.user_id, s.enrolment_only, "
                    "s.epoch, s.second_factor_at, s.enrol_kind, s.enrol_stage, "
                    "(s.second_factor_at IS NOT NULL AND s.second_factor_at > now() - interval '5 minutes') AS factor_fresh, "
                    "extract(epoch from now() - s.second_factor_at) AS tap_age, extract(epoch from now() - s.auth_time) AS auth_age, "
                    "(s.last_seen_at > now() - make_interval(mins => %s)) AS not_idle, "
                    "u.session_epoch, u.disabled_at, u.is_owner, u.role "
                    "FROM web_sessions s LEFT JOIN users u ON u.id = s.user_id "
                    "WHERE s.token_hash = %s AND s.expires_at > now() "
                    "AND (s.user_id IS NULL OR s.enrolment_only OR s.created_at + make_interval(mins => %s) > now())",
                    (idle, _hash(token), length))
        row = cur.fetchone()
        if not row:
            return None
        oidc_user_id = None
        if row["user_id"] is None:                      # an OIDC session
            if mode() != "oidc" or row["oid"] not in config.SUPERADMIN_OIDS:
                return None
            cur.execute("SELECT id FROM users WHERE oid = %s", (row["oid"],))      # its users row: the audit user (#713)
            u = cur.fetchone()
            oidc_user_id = u["id"] if u else None
        else:                                           # a local session
            # Any enrolled local user may hold a session (task #713: owner, designer, viewer); what they can reach is
            # app/access.py. A disabled user, a session from before "sign out everywhere", or an idle one is dead.
            if mode() != "local" or row["disabled_at"] or row["epoch"] != row["session_epoch"] or not row["not_idle"]:
                return None
        cur.execute("UPDATE web_sessions SET last_seen_at = now() WHERE id = %s "
                    "AND last_seen_at < now() - interval '1 minute'", (row["id"],))
    out = dict(row)
    out["oidc_user_id"] = oidc_user_id
    return out


def set_enrol_stage(session_id: int, stage: str) -> None:
    with get_cursor() as cur:
        cur.execute("UPDATE web_sessions SET enrol_stage = %s WHERE id = %s AND enrolment_only", (stage, session_id))


def mark_factor_fresh(session_id: int) -> None:
    """A passkey tap just happened for this session: step-up windows count from now."""
    with get_cursor() as cur:
        cur.execute("UPDATE web_sessions SET second_factor_at = now() WHERE id = %s", (session_id,))


def auth_age_seconds(session: dict) -> int:
    """How long ago this session's holder last proved themselves (sign-in), in seconds. The seam for a later step-up (#809: ask
    again before something sensitive when the sign-in is old); nothing uses it yet. A session with no auth_time counts as ancient."""
    t = session.get("auth_time")
    if t is None:
        return 10 ** 9
    from datetime import datetime, timezone
    if isinstance(t, (int, float)):
        t = datetime.fromtimestamp(t, tz=timezone.utc)
    elif t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - t).total_seconds()))


def describe_user_agent(ua: str) -> str:
    """A short 'Chrome on Windows' for the sessions list. A guess from the User-Agent text, for the person's eyes only."""
    u = ua or ""
    browser = next((name for key, name in (("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"), ("Chrome/", "Chrome"),
                                           ("CriOS/", "Chrome"), ("Safari/", "Safari")) if key in u), "")
    system = next((name for key, name in (("Windows", "Windows"), ("Android", "Android"), ("iPhone", "iPhone"), ("iPad", "iPad"),
                                           ("Mac OS X", "macOS"), ("Macintosh", "macOS"), ("CrOS", "ChromeOS"), ("Linux", "Linux"))
                    if key in u), "")
    if browser and system:
        return f"{browser} on {system}"
    return browser or system or "Unknown browser"


def list_user_sessions(user_id: int, current_id: int | None = None) -> list[dict]:
    """The user's live full sessions for the account page: id, browser, address, signed-in and last-seen times. Never the token hash."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT id, user_agent, ip, created_at, last_seen_at, least(expires_at, created_at + make_interval(mins => %s)) AS expires_at "
                    "FROM web_sessions WHERE user_id = %s AND NOT enrolment_only AND expires_at > now() "
                    "AND created_at + make_interval(mins => %s) > now() ORDER BY last_seen_at DESC", (session_minutes(), user_id, session_minutes()))
        rows = cur.fetchall()
    return [{"id": r["id"], "browser": describe_user_agent(r["user_agent"]), "ip": r["ip"], "signed_in": r["created_at"],
             "last_seen": r["last_seen_at"], "expires": r["expires_at"], "current": r["id"] == current_id} for r in rows]


def end_user_session(user_id: int, session_id: int) -> bool:
    """End ONE of this user's sessions (never another user's). True when a session was ended."""
    with get_cursor() as cur:
        cur.execute("DELETE FROM web_sessions WHERE id = %s AND user_id = %s", (session_id, user_id))
        return cur.rowcount == 1


def end_user_sessions(user_id: int) -> None:
    with get_cursor() as cur:
        cur.execute("DELETE FROM web_sessions WHERE user_id = %s", (user_id,))


def upsert_user(claims: dict) -> int:
    """The `users` row for a signed-in person (task #627): created at their first sign-in, display name kept current.
    Runner devices and enrolment codes belong to this row. Returns its id."""
    with get_cursor() as cur:
        cur.execute("INSERT INTO users (oid, display_name, role) VALUES (%s, %s, 'owner') ON CONFLICT (oid) DO UPDATE "
                    "SET display_name = CASE WHEN EXCLUDED.display_name <> '' THEN EXCLUDED.display_name "
                    "ELSE users.display_name END RETURNING id",
                    (str(claims["oid"]).lower(), str(claims.get("name") or claims.get("preferred_username") or "")[:200]))
        return cur.fetchone()["id"]


def end_session(token: str | None) -> None:
    if token:
        with get_cursor() as cur:
            cur.execute("DELETE FROM web_sessions WHERE token_hash = %s", (_hash(token),))


# --- the OIDC flow -----------------------------------------------------------------------------------------------

def start_flow(next_path: str) -> tuple[str, str]:
    """(state, authorize URL). The flow row holds the nonce and PKCE verifier; the state goes in the flow cookie."""
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    with get_cursor() as cur:
        cur.execute("DELETE FROM oidc_flows WHERE created_at < now() - make_interval(mins => %s)",
                    (config.OIDC_FLOW_MINUTES,))
        cur.execute("INSERT INTO oidc_flows (state_hash, nonce, code_verifier, next_path) VALUES (%s, %s, %s, %s)",
                    (_hash(state), nonce, verifier, safe_next(next_path)))
    query = urllib.parse.urlencode({
        "client_id": config.OIDC_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": redirect_uri(),
        "response_mode": "query",
        "scope": SCOPES,
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
    return state, f"{_authority()}/oauth2/v2.0/authorize?{query}"


def take_flow(state: str) -> dict | None:
    """Use a flow ONCE: deleted as it's read, and only while it is fresh."""
    with get_cursor() as cur:
        cur.execute("DELETE FROM oidc_flows WHERE state_hash = %s "
                    "AND created_at > now() - make_interval(mins => %s) RETURNING nonce, code_verifier, next_path",
                    (_hash(state), config.OIDC_FLOW_MINUTES))
        row = cur.fetchone()
    return dict(row) if row else None


def exchange_code(code: str, verifier: str) -> dict:
    """POST the code to the tenant's token endpoint (server to Microsoft). Returns the token response."""
    body = urllib.parse.urlencode({
        "client_id": config.OIDC_CLIENT_ID,
        "client_secret": client_secret(),
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri(),
        "code_verifier": verifier,
        "scope": SCOPES,
    }).encode("ascii")
    req = urllib.request.Request(f"{_authority()}/oauth2/v2.0/token", data=body, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:  # noqa: S310 — fixed https URL
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read()).get("error", "")
        except Exception:  # noqa: BLE001
            detail = ""
        raise SignInError(f"Microsoft refused the sign-in code ({detail or exc.code}).") from None
    except (urllib.error.URLError, TimeoutError, ValueError):
        raise SignInError("Couldn't reach Microsoft to finish signing in. Try again.") from None


@lru_cache(maxsize=1)
def _jwks_client() -> jwt.PyJWKClient:
    return jwt.PyJWKClient(f"{_authority()}/discovery/v2.0/keys", cache_keys=True, lifespan=3600,
                           timeout=HTTP_TIMEOUT)


def signing_key(id_token: str):
    return _jwks_client().get_signing_key_from_jwt(id_token).key


class SignInError(Exception):
    """A sign-in that must not make a session. The message is safe to show."""


def verify_id_token(id_token: str, nonce: str) -> dict:
    try:
        claims = jwt.decode(
            id_token, signing_key(id_token), algorithms=["RS256"], audience=config.OIDC_CLIENT_ID,
            issuer=issuer(), leeway=LEEWAY_SECONDS,
            options={"require": ["exp", "iat", "aud", "iss", "sub", "nonce", "tid", "oid"]},
        )
    except (jwt.PyJWTError, ValueError) as exc:
        log.warning("sign-in refused: ID token failed verification (%s)", type(exc).__name__)
        raise SignInError("Microsoft's answer didn't check out. Try again.") from None
    claims["oid"] = str(claims["oid"]).lower()
    if str(claims.get("tid")).lower() != config.OIDC_TENANT_ID.lower():
        raise SignInError("That account isn't in this install's Microsoft tenant.")
    if not hmac.compare_digest(str(claims.get("nonce")), nonce):
        raise SignInError("Microsoft's answer didn't match this sign-in. Try again.")
    return claims


# --- the gate ----------------------------------------------------------------------------------------------------

def cookie_from_headers(raw_headers: list[tuple[bytes, bytes]], name: str) -> str | None:
    for k, v in raw_headers:
        if k.lower() == b"cookie":
            for part in v.decode("latin-1").split(";"):
                key, _, val = part.strip().partition("=")
                if key == name:
                    return val
    return None


def _login_url(next_path: str) -> str:
    return "/auth/login?" + urllib.parse.urlencode({"next": safe_next(next_path)})


class WebAuthGate:
    """Pure ASGI middleware: every request for a page needs a signed-in super admin. Signed in = the user goes in
    scope['state']['web_user'] (request.state.web_user in the pages)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not required() or is_open_path(scope["path"]):
            await self.app(scope, receive, send)
            return
        user = await run_in_threadpool(self._user, scope)
        if user and user.get("enrolment_only"):
            # First-run enrolment: nothing but /auth/* (open paths never reach here) until a factor is confirmed.
            refusal = self._enrol_only(scope)
            await refusal(scope, receive, send)
            return
        if user:
            scope.setdefault("state", {})["web_user"] = user
            reset = access.set_current_user(user.get("user_id") or user.get("oidc_user_id"))   # the audit actor of every write this request makes (#713)
            try:
                await self.app(scope, receive, send)
            finally:
                access.set_current_user(None)
                del reset
            return
        refusal = await run_in_threadpool(self._refuse, scope)
        await refusal(scope, receive, send)

    @staticmethod
    def _user(scope: Scope) -> dict | None:
        return resolve_session(cookie_from_headers(scope["headers"], session_cookie_name()))

    @staticmethod
    def _enrol_only(scope: Scope) -> Response:
        if scope["method"] in ("GET", "HEAD") and scope["path"] != "/all/events":
            return RedirectResponse("/auth/enrol", status_code=302)
        if any(k.lower() == b"hx-request" for k, _ in scope["headers"]):
            return PlainTextResponse("Finish setting up sign-in first", status_code=403, headers={"HX-Redirect": "/auth/enrol"})
        return PlainTextResponse("Finish setting up sign-in first", status_code=403)

    @staticmethod
    def _refuse(scope: Scope) -> Response:
        if mode() == "oidc" and missing_config():  # reads the secret file
            return PlainTextResponse("Dashboard sign-in isn't set up yet (task #394).", status_code=503)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        path = scope["path"] + (("?" + scope["query_string"].decode("latin-1")) if scope.get("query_string") else "")
        if headers.get("hx-request"):
            # htmx follows HX-Redirect whatever the status; send the whole page (not the fragment) back after sign-in.
            current = urllib.parse.urlsplit(headers.get("hx-current-url", ""))
            back = (current.path or "/") + (("?" + current.query) if current.query else "")
            return PlainTextResponse("Signed out", status_code=401, headers={"HX-Redirect": _login_url(back)})
        if scope["method"] in ("GET", "HEAD") and scope["path"] != "/all/events":
            return RedirectResponse(_login_url(path), status_code=302)
        return PlainTextResponse("Sign in first", status_code=401)


def signed_in_user_row(request) -> dict:
    """The signed-in person's `users` row (made if their sign-in predates the table) for a page write that acts AS that person
    (runners, starting a job). Only a real web session counts: a request that carries a runner device/job token (cbr_/cbj_, or the old mdr_/mdj_)
    is refused even alongside a cookie, and with sign-in off there is nobody to act as. Raises PermissionError otherwise."""
    auth_header = (request.headers.get("authorization") or "").strip().lower()
    from app.auth import RUNNER_PREFIXES
    if any(auth_header.startswith("bearer " + p) for p in RUNNER_PREFIXES):
        raise PermissionError("a runner or job token can't act for a person")
    web_user = getattr(request.state, "web_user", None)
    if not web_user:
        raise PermissionError("this needs a signed-in person")
    user_id = web_user.get("user_id") or upsert_user({"oid": web_user["oid"], "name": web_user.get("name") or web_user.get("username")})
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT id, oid, display_name, disabled_at FROM users WHERE id = %s", (user_id,))
        user = cur.fetchone()
    if user["disabled_at"]:
        raise PermissionError("this user is disabled")
    return dict(user)
