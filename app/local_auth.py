"""Local sign-in (task #706; #976 slice A): Crewboss's own sign-in, the default way into the pages.

Owner decision q826 (2026-11-27, reversing #809's "no password"): in EVERY mode the normal sign-in is a password plus a 6-digit code
from an authenticator app (TOTP), with recovery codes for a lost phone. Passkeys are an OPTIONAL "passwordless sign-in" upgrade, offered
only where passkeys_available(request) is true (a padlocked public address, or localhost; never the LAN self-signed address). Someone
who already has a passkey and no password + authenticator still signs in by passkey, and is nudged to add a backup.

A password an admin (or the server console) hands over is still ONE-TIME and TEMPORARY (server-generated, hashed, 24 hours, single
use): it opens a restricted ("enrolment-only") session whose stages are password -> totp -> codes. The person chooses a password, scans
the authenticator QR code, types a 6-digit code from it, saves the recovery codes, and only then has a normal session. A recovery code
opens the same restricted session (new password or keep the current one, then a NEW authenticator, then new codes).

This module holds the credential logic (no HTTP): passwords, TOTP, recovery codes, rate limiting, WebAuthn ceremonies. Sessions and the
gate are app/web_auth.py; the pages are app/routers/local_auth_pages.py."""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
import time
import urllib.parse
from functools import lru_cache
from pathlib import Path

from app import config, instance
from app.db import get_cursor

log = logging.getLogger("crewboss.local_auth")

# --- context: https / passkeys -------------------------------------------------------------------------------------

_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})


def public_host() -> str:
    """The WebAuthn RP ID: the public URL's hostname (no port)."""
    return (urllib.parse.urlsplit(config.PUBLIC_URL).hostname or "").lower()


def public_origin() -> str:
    parts = urllib.parse.urlsplit(config.PUBLIC_URL)
    return f"{parts.scheme}://{parts.netloc}".lower()


def cookie_secure() -> bool:
    return urllib.parse.urlsplit(config.PUBLIC_URL).scheme == "https"


def passkeys_enabled() -> bool:
    """Can passkeys work at the configured public address at all (https, or a loopback host)? Per-request availability, which also
    honours the front door and `auth_passkeys: off`, is passkeys_available(request)."""
    return cookie_secure() or public_host() in _LOOPBACK


# --- the front-door contract (slice B, app/front_door.py) ------------------------------------------------------------
# Slice B publishes app/front_door.py with passkeys_available(request), rp_id_for(request) and note_signin_ip(ip, ok). Until it is on
# this branch these thin fallbacks stand in; the moment the module exists they hand over to it, with no change here.

def _fd():
    try:
        from app import front_door
        return front_door
    except ImportError:
        return None


def passkeys_available(request) -> bool:
    """May this request's page offer passkeys? (A padlocked public address or localhost, and auth_passkeys is not off.)"""
    fd = _fd()
    if fd is not None:
        return bool(fd.passkeys_available(request))
    return getattr(config, "AUTH_PASSKEYS", "auto") != "off" and passkeys_enabled()


def rp_id_for(request) -> str:
    """The WebAuthn RP ID of THIS request's own origin."""
    fd = _fd()
    if fd is not None:
        return fd.rp_id_for(request)
    return public_host()


def note_signin_ip(ip: str, ok: bool) -> None:
    """Tell the front door about a sign-in outcome from an address (it only warns about public-internet sign-ins)."""
    fd = _fd()
    if fd is not None:
        try:
            fd.note_signin_ip(ip, ok)
        except Exception:  # noqa: BLE001 — a warning aid must never break sign-in
            log.exception("note_signin_ip failed")


def origin_for(request, rp_id: str) -> str:
    """The origin the browser will report for this RP ID: the public origin for the public host, else the request's own
    (http for localhost, https otherwise) with the host's port."""
    if rp_id == public_host():
        return public_origin()
    host = (request.headers.get("host") or "").strip().lower()
    scheme = "http" if rp_id in _LOOPBACK else "https"
    name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    if name.strip("[]") != rp_id:
        return f"{scheme}://{rp_id}"
    return f"{scheme}://{host}"


def startup_warning() -> str:
    """The warning to log at start-up in local mode ('' = none). Passwords + authenticator work anywhere, so nothing is switched off."""
    return ""


def clear_stale_totp() -> int:
    """A pre-#809 authenticator secret on someone with NO password does not count as set up: the backup flow must confirm a NEW one, and the
    last-passkey guard (has_mfa) must never lean on it. Run at start-up (data, so not in a migration). Safe to repeat. It only touches a
    secret confirmed BEFORE migration 0118 was applied: anything confirmed since (#976) is live and is never cleared, and the account page
    now refuses to start an authenticator before a password is chosen."""
    with get_cursor() as cur:
        cur.execute("UPDATE users SET totp_secret_enc = NULL, totp_pending_enc = NULL, totp_confirmed_at = NULL, totp_last_step = NULL "
                    "WHERE password_hash IS NULL AND (totp_secret_enc IS NOT NULL OR totp_confirmed_at IS NOT NULL) "
                    "AND (totp_confirmed_at IS NULL OR totp_confirmed_at < COALESCE("
                    "(SELECT applied_at FROM schema_migrations WHERE filename = '0118_mfa_default.sql'), '-infinity'::timestamptz))")
        return cur.rowcount


def backfill_rp_id() -> int:
    """Every passkey made before #976 was made under the public host: stamp those with it. Safe to repeat."""
    host = public_host()
    if not host:
        return 0
    with get_cursor() as cur:
        cur.execute("UPDATE webauthn_credentials SET rp_id = %s WHERE rp_id IS NULL", (host,))
        return cur.rowcount


# --- the temporary password -----------------------------------------------------------------------------------------

# Minimum 12, not 15: NIST SP 800-63B asks 15 for a password used ALONE; here a password is always paired with a second factor
# (the authenticator code), for which 8 would be the floor. Max 128 stops a hashing-cost abuse. No composition rules and no forced
# rotation (800-63B). A bundled list of common passwords is checked.
PASSWORD_MIN = 12
PASSWORD_MAX = 128
_WORDLIST = Path(__file__).with_name("wordlists") / "common-passwords.txt"

# argon2id with argon2-cffi's defaults (RFC 9106 "low memory" profile: t=3, m=64 MiB, p=4), above OWASP's minimum. When the
# library's defaults move, check_needs_rehash() is true and the hash is replaced at the next successful sign-in.
TEMP_PASSWORD_HOURS = 24


@lru_cache(maxsize=1)
def _hasher():
    from argon2 import PasswordHasher
    return PasswordHasher()


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    return _hasher().hash(secrets.token_urlsafe(16))


def hash_password(password: str) -> str:
    return _hasher().hash(password)


def verify_password(stored_hash: str | None, password: str) -> tuple[bool, str | None]:
    """(matches, a replacement hash if the stored one uses outdated parameters). With no stored hash (no such user) a dummy
    hash is verified, so the response takes about as long and a username can't be told from the timing."""
    from argon2 import exceptions as ax
    h = _hasher()
    if not stored_hash:
        try:
            h.verify(_dummy_hash(), password)
        except ax.VerifyMismatchError:
            pass
        return False, None
    try:
        h.verify(stored_hash, password)
    except (ax.VerifyMismatchError, ax.VerificationError, ax.InvalidHashError):
        return False, None
    return True, (h.hash(password) if h.check_needs_rehash(stored_hash) else None)


@lru_cache(maxsize=1)
def _common() -> frozenset[str]:
    # Common-password list: SecLists "Passwords/Common-Credentials/10k-most-common.txt" (github.com/danielmiessler/SecLists),
    # MIT licence, (c) 2018 Daniel Miessler. Bundled; never fetched at runtime.
    try:
        return frozenset(w.strip().lower() for w in _WORDLIST.read_text(encoding="utf-8", errors="ignore").splitlines() if w.strip())
    except OSError:
        log.error("common-password list %s is missing; the check is skipped", _WORDLIST)
        return frozenset()


def _stem(pw: str) -> str:
    return re.sub(r"[^a-z]+$", "", pw.lower())          # "Password123456!" -> "password"


def password_problem(password: str, username: str = "") -> str:
    """Why a new password is refused ('' = fine). Length and the common list only; nothing about character classes."""
    if len(password) < PASSWORD_MIN:
        return f"Use at least {PASSWORD_MIN} characters."
    if len(password) > PASSWORD_MAX:
        return f"Use at most {PASSWORD_MAX} characters."
    low = password.lower()
    common = _common()
    if low in common or _stem(password) in common or re.sub(r"[^a-z0-9]", "", low) in common:
        return "That password is too common. Pick something longer and less guessable, such as a few unrelated words."
    if len(set(low)) < 5:
        return "That password repeats too few characters."
    if username and len(username) >= 3 and username.lower() in low:
        return "The password shouldn't contain your username."
    return ""


def new_temp_password() -> str:
    """A strong one-time password for a person to type once: 20 characters from a 31-letter alphabet (~99 bits), shown in groups of 4."""
    return _make_code(20, 4)


def set_temp_password(cur, user_id: int) -> str:
    """Give the user a NEW temporary password (replacing any password) inside the caller's transaction; returns the plain text, shown
    once. Only the argon2id hash is stored, with its expiry."""
    pw = new_temp_password()
    cur.execute("UPDATE users SET password_hash = %s, password_temp = true, password_expires_at = now() + make_interval(hours => %s) "
                "WHERE id = %s", (hash_password(_normal_code(pw)), TEMP_PASSWORD_HOURS, user_id))
    return pw


def temp_password_live(user: dict | None) -> bool:
    """Is this user's password a temporary one that has not expired? (The caller compares the clock in SQL when it spends it.)"""
    return bool(user and user.get("password_hash") and user.get("password_temp"))


def consume_temp_password(user_id: int, stored_hash: str) -> bool:
    """SPEND the temporary password: the hash is deleted in the same statement that checks it is still the live, unexpired one. Of two
    concurrent sign-ins with it, exactly one wins."""
    with get_cursor() as cur:
        cur.execute("UPDATE users SET password_hash = NULL, password_temp = false, password_expires_at = NULL "
                    "WHERE id = %s AND password_temp AND password_hash = %s AND password_expires_at > now() RETURNING id",
                    (user_id, stored_hash))
        return cur.fetchone() is not None


def clear_password(user_id: int) -> None:
    with get_cursor() as cur:
        cur.execute("UPDATE users SET password_hash = NULL, password_temp = false, password_expires_at = NULL WHERE id = %s", (user_id,))


def has_mfa(user: dict | None) -> bool:
    """A chosen (not temporary) password AND a confirmed authenticator app: the default way in, with the 6-digit code."""
    return bool(user and user.get("password_hash") and not user.get("password_temp") and user.get("totp_confirmed_at")
                and user.get("totp_secret_enc"))


def set_chosen_password(user_id: int, password: str) -> None:
    """Store a password the person chose (argon2id), replacing a temporary one. The caller has run password_problem()."""
    with get_cursor() as cur:
        cur.execute("UPDATE users SET password_hash = %s, password_temp = false, password_expires_at = NULL WHERE id = %s",
                    (hash_password(password), user_id))


def replace_hash_if_current(user_id: int, old_hash: str, new_hash: str) -> None:
    """Swap in an upgraded hash (argon2 parameters moved) only if the stored one is still the one that was checked."""
    with get_cursor() as cur:
        cur.execute("UPDATE users SET password_hash = %s WHERE id = %s AND password_hash = %s AND NOT password_temp",
                    (new_hash, user_id, old_hash))


# --- users ---------------------------------------------------------------------------------------------------------

_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,62}[a-z0-9]$")


def normalise_username(value: str) -> str:
    return (value or "").strip().lower()


def valid_username(value: str) -> bool:
    return bool(_USERNAME_RE.match(value))


def get_user(user_id: int) -> dict | None:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
    return dict(row) if row else None


def get_user_by_username(username: str) -> dict | None:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT * FROM users WHERE lower(username) = %s", (normalise_username(username),))
        row = cur.fetchone()
    return dict(row) if row else None


def get_user_by_identifier(identifier: str) -> dict | None:
    """The user whose username OR sign-in email matches, case-insensitively (#809). A username can't contain '@', so the two never
    collide."""
    ident = normalise_username(identifier)
    if not ident or len(ident) > 320:
        return None
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT * FROM users WHERE lower(username) = %s OR lower(email) = %s ORDER BY (lower(username) = %s) DESC LIMIT 1",
                    (ident, ident, ident))
        row = cur.fetchone()
    return dict(row) if row else None


_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s.]{2,63}$")


def valid_email(value: str) -> bool:
    return bool(_EMAIL_RE.match(value or "")) and len(value) <= 320


class EmailTaken(Exception):
    pass


def set_email(user_id: int, email: str) -> None:
    """Set or clear (empty) the sign-in email. An identifier only, nothing is ever sent. Raises ValueError for a bad address and
    EmailTaken when another user has it (case-insensitively)."""
    email = (email or "").strip()
    if email and not valid_email(email):
        raise ValueError("That doesn't look like an email address.")
    import psycopg.errors
    try:
        with get_cursor() as cur:
            cur.execute("UPDATE users SET email = %s WHERE id = %s", (email or None, user_id))
    except psycopg.errors.UniqueViolation:
        raise EmailTaken() from None


def owner_exists() -> bool:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT 1 FROM users WHERE is_owner")
        return cur.fetchone() is not None


class OwnerExists(Exception):
    pass


def reset_signin_in(cur, user_id: int) -> str:
    """Wipe how this person signs in (password, passkeys, recovery codes, authenticator, every session and flow), end their sessions and give
    them a NEW temporary password; returns it (shown once). In the caller's transaction."""
    cur.execute("UPDATE users SET totp_secret_enc = NULL, totp_pending_enc = NULL, totp_confirmed_at = NULL, totp_last_step = NULL, "
                "totp_fail_streak = 0, totp_fail_at = NULL, session_epoch = session_epoch + 1 WHERE id = %s", (user_id,))
    for table in ("webauthn_credentials", "recovery_codes", "setup_codes", "web_sessions", "auth_flows"):
        cur.execute(f"DELETE FROM {table} WHERE user_id = %s", (user_id,))  # noqa: S608 — fixed table names
    return set_temp_password(cur, user_id)


def create_owner(username: str, reset: bool = False) -> tuple[int, str]:
    """Make the single owner and return (user id, one-time temporary password). Refuses when an owner exists unless `reset`, which
    keeps the row (so runners and devices stay linked), wipes every way in and issues a new temporary password. Console only
    (python -m app.cli); no web route calls this."""
    username = normalise_username(username)
    with get_cursor() as cur:
        cur.execute("SELECT id FROM users WHERE is_owner FOR UPDATE")
        existing = cur.fetchone()
        if existing and not reset:
            raise OwnerExists()
        if existing:
            uid = existing["id"]
            cur.execute("UPDATE users SET username = %s, oid = %s, disabled_at = NULL, display_name = %s WHERE id = %s",
                        (username, "local:" + username, username, uid))
        else:
            cur.execute("INSERT INTO users (oid, display_name, username, is_owner, role) "
                        "VALUES (%s, %s, %s, true, 'owner') RETURNING id", ("local:" + username, username, username))
            uid = cur.fetchone()["id"]
        pw = reset_signin_in(cur, uid)
    return uid, pw


def reset_owner() -> tuple[str, str]:
    """The owner's way back from the server shell (`python -m app.cli reset-owner`): same wipe as an admin's Reset sign-in, for the
    owner row, whom no admin can reset. Returns (username, temporary password). OwnerExists is not raised; LookupError when there is none."""
    with get_cursor() as cur:
        cur.execute("SELECT id, username FROM users WHERE is_owner FOR UPDATE")
        row = cur.fetchone()
        if not row:
            raise LookupError("no owner account exists yet")
        cur.execute("UPDATE users SET disabled_at = NULL WHERE id = %s", (row["id"],))
        pw = reset_signin_in(cur, row["id"])
    return row["username"], pw


# --- one-time codes: recovery -------------------------------------------------------------------------------

_CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"      # no 0/o/1/l/i
RECOVERY_CODE_COUNT = 10


def _code_hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _recovery_hash(normal_code: str) -> str:
    """HMAC-SHA256 under a key derived from the local-auth key file, so a copy of the database alone can't be used to
    test guesses offline. The key file should be backed up separately from the database."""
    key = hmac.new(_key_bytes(config.LOCAL_AUTH_KEY_FILE), b"crewboss-recovery-codes-v1", hashlib.sha256).digest()
    return hmac.new(key, normal_code.encode("utf-8"), hashlib.sha256).hexdigest()


def _normal_code(code: str) -> str:
    return re.sub(r"[\s-]+", "", code or "").lower()


def _make_code(length: int, group: int) -> str:
    raw = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(length))
    return "-".join(raw[i:i + group] for i in range(0, length, group))


def new_recovery_codes(user_id: int) -> list[str]:
    """Replace the user's recovery codes with 10 new ones; the plain codes are returned ONCE and only hashes are kept."""
    codes = [_make_code(16, 4) for _ in range(RECOVERY_CODE_COUNT)]        # ~80 bits each
    hashes = [_recovery_hash(_normal_code(c)) for c in codes]
    with get_cursor() as cur:
        cur.execute("DELETE FROM recovery_codes WHERE user_id = %s", (user_id,))
        for h in hashes:
            cur.execute("INSERT INTO recovery_codes (user_id, code_hash) VALUES (%s, %s)", (user_id, h))
    return codes


def use_recovery_code(user_id: int | None, code: str) -> bool:
    """Spend one code of this user (atomically: of two uses of one code, one wins). With no user (an unknown identifier) the same
    hashing and the same query run against nobody, so the answer takes the same time."""
    n = _normal_code(code)
    if len(n) != 16:
        return False
    try:
        h = _recovery_hash(n)
    except KeyUnavailable:
        return False
    with get_cursor() as cur:
        cur.execute("UPDATE recovery_codes SET used_at = now() WHERE user_id = %s AND code_hash = %s AND used_at IS NULL RETURNING id",
                    (user_id if user_id is not None else 0, h))
        return cur.fetchone() is not None


def fresh_recovery_codes_since(user_id: int, session_id: int) -> int:
    """Unused codes made AFTER this (enrolment) session began: proof the "I've saved them" step really followed a new set, so a
    recovery-code sign-in can't keep the old codes alive by skipping /auth/enrol/codes (Fable review #809)."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT count(*) AS n FROM recovery_codes r JOIN web_sessions s ON s.id = %s "
                    "WHERE r.user_id = %s AND r.used_at IS NULL AND r.created_at >= s.created_at", (session_id, user_id))
        return cur.fetchone()["n"]


def recovery_codes_left(user_id: int) -> int:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT count(*) AS n FROM recovery_codes WHERE user_id = %s AND used_at IS NULL", (user_id,))
        return cur.fetchone()["n"]


# --- TOTP ----------------------------------------------------------------------------------------------------------

TOTP_STEP = 30


def _now() -> float:
    return time.time()          # a seam for the tests


class KeyUnavailable(Exception):
    """The key file can't be read or made. The message is safe to show."""


def _key_bytes(path: str) -> bytes:
    from cryptography.fernet import Fernet
    p = Path(path)
    try:
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(Fernet.generate_key())
        return p.read_bytes().strip()
    except OSError:
        raise KeyUnavailable(f"The sign-in key file ({path}) can't be created or read. Create it on the server with "
                             "`python -m app.cli owner create`, and make sure its folder is writable "
                             "(CREWBOSS_LOCAL_AUTH_KEY_FILE).") from None


def key_problem() -> str:
    """'' when the key file is usable (made on the way if it can be), else why not."""
    try:
        _key_bytes(config.LOCAL_AUTH_KEY_FILE)
        return ""
    except KeyUnavailable as exc:
        return str(exc)


@lru_cache(maxsize=4)
def _fernet(path: str):
    from cryptography.fernet import Fernet
    return Fernet(_key_bytes(path))


def encrypt_secret(plain: str) -> str:
    return _fernet(config.LOCAL_AUTH_KEY_FILE).encrypt(plain.encode("ascii")).decode("ascii")


def decrypt_secret(token: str) -> str:
    return _fernet(config.LOCAL_AUTH_KEY_FILE).decrypt(token.encode("ascii")).decode("ascii")


def _totp_codes(secret: str):
    """(step, code) for the previous, current and next 30 s step: ±1 step of clock drift."""
    import pyotp
    totp = pyotp.TOTP(secret)
    now_step = int(_now() // TOTP_STEP)
    return [(s, totp.generate_otp(s)) for s in (now_step - 1, now_step, now_step + 1)]


def _match_step(secret: str, code: str) -> int | None:
    code = re.sub(r"\s+", "", code or "")
    if not re.fullmatch(r"\d{6}", code):
        return None
    hit = None
    for step, expect in _totp_codes(secret):       # no early exit: constant work
        if hmac.compare_digest(expect, code):
            hit = step
    return hit


def begin_totp(user: dict) -> tuple[str, str, str]:
    """A fresh pending secret for the user (the confirmed one, if any, keeps working until this is confirmed):
    (secret, otpauth URI, QR as an inline SVG). Pending, so only a correct code makes it live."""
    import pyotp
    secret = pyotp.random_base32()
    with get_cursor() as cur:
        cur.execute("UPDATE users SET totp_pending_enc = %s WHERE id = %s", (encrypt_secret(secret), user["id"]))
    return totp_view(secret, user)


def totp_view(secret: str, user: dict) -> tuple[str, str, str]:
    import pyotp
    import segno
    uri = pyotp.TOTP(secret).provisioning_uri(name=user.get("username") or "owner", issuer_name=instance.get_str("name", "Crewboss"))
    svg = segno.make(uri, error="m").svg_inline(scale=5, dark="#000000", light="#ffffff", border=2,
                                                 title="QR code for your authenticator app")
    from markupsafe import Markup
    return secret, uri, Markup(svg)   # segno output: only paths and numbers, no input text


def pending_totp(user_id: int) -> str | None:
    u = get_user(user_id)
    if not u or not u["totp_pending_enc"]:
        return None
    try:
        return decrypt_secret(u["totp_pending_enc"])
    except Exception:  # noqa: BLE001 — a key that no longer decrypts: start again
        return None


def clear_pending_totp(user_id: int) -> None:
    with get_cursor() as cur:
        cur.execute("UPDATE users SET totp_pending_enc = NULL WHERE id = %s", (user_id,))


def confirm_totp(user_id: int, code: str) -> bool:
    """Make the pending secret live, only after a correct code from it."""
    secret = pending_totp(user_id)
    if not secret:
        return False
    step = _match_step(secret, code)
    if step is None:
        return False
    with get_cursor() as cur:
        cur.execute("UPDATE users SET totp_secret_enc = totp_pending_enc, totp_pending_enc = NULL, totp_confirmed_at = now(), "
                    "totp_last_step = %s WHERE id = %s AND totp_pending_enc IS NOT NULL", (step, user_id))
        return cur.rowcount == 1


_DUMMY_TOTP_SECRET = "JBSWY3DPEHPK3PXP"


def match_totp_step(user: dict | None, code: str) -> int | None:
    """The 30 s step a typed code matches for this user's CONFIRMED secret (±1 step), or None. It spends nothing: a caller that
    also checks something else (the password) spends the step with consume_totp_step only when everything held. With no usable
    user the same work runs against a dummy secret, so the answer takes the same time."""
    secret = _DUMMY_TOTP_SECRET
    usable = bool(user and user.get("totp_confirmed_at") and user.get("totp_secret_enc"))
    if usable:
        try:
            secret = decrypt_secret(user["totp_secret_enc"])
        except Exception:  # noqa: BLE001
            log.error("a TOTP secret could not be decrypted (is %s the key it was made with?)", config.LOCAL_AUTH_KEY_FILE)
            usable = False
    step = _match_step(secret, code)
    return step if usable else None


def consume_totp_step(user_id: int, step: int) -> bool:
    """Spend a step: only a step LATER than the last one used (by sign-in or step-up alike) is accepted, atomically, so a code
    can't be replayed and of two concurrent uses only one wins."""
    with get_cursor() as cur:
        cur.execute("UPDATE users SET totp_last_step = %s WHERE id = %s AND (totp_last_step IS NULL OR totp_last_step < %s)",
                    (step, user_id, step))
        return cur.rowcount == 1


def verify_totp(user: dict, code: str, count_failure: bool = True) -> bool:
    """A code from the confirmed secret, ±1 step, and never the same (or an earlier) step twice, across sign-in and step-up. Counts
    consecutive failures (totp_failed) and refuses while locked (totp_lock); a success resets the count. A step-up passes
    count_failure=False: a signed-in session's wrong guesses are counted on that session (step_up_failed), never on the account-wide
    sign-in streak, so a stolen cookie cannot lock the owner's sign-in."""
    if totp_lock(user)[0] != "ok":
        return False
    step = match_totp_step(user, code)
    if step is not None and consume_totp_step(user["id"], step):
        totp_reset(user["id"])
        return True
    if count_failure and user.get("totp_confirmed_at") and user.get("totp_secret_enc"):
        totp_failed(user["id"])
    return False


STEPUP_SESSION_LIMIT = 10      # wrong step-up codes in ONE session, then that session is ended


def step_up_failed(session_id: int, user_id: int) -> bool:
    """One more wrong step-up code on this session. At STEPUP_SESSION_LIMIT the session is deleted (signed out) and an audit line is
    written; the account-wide sign-in lock is not touched. Returns True when the session was ended."""
    with get_cursor() as cur:
        cur.execute("UPDATE web_sessions SET stepup_failed = stepup_failed + 1 WHERE id = %s RETURNING stepup_failed AS n", (session_id,))
        row = cur.fetchone()
        if not row or row["n"] < STEPUP_SESSION_LIMIT:
            return False
        cur.execute("DELETE FROM web_sessions WHERE id = %s", (session_id,))
        cur.execute("SELECT username FROM users WHERE id = %s", (user_id,))
        who = (cur.fetchone() or {}).get("username", "")
        from app import access
        access.audit(cur, "signin.stepup_lock", f"{who}: {row['n']} wrong step-up codes in one signed-in session; that session was signed out. "
                                                "Sign-in itself is not locked.")
    log.warning("step-up guessing on %s: %s wrong codes in one session; session ended", who, row["n"])
    return True


# --- the second-factor guess limit: consecutive failures, NO time window ------------------------------------------------
# The hourly ceiling below cannot stop a slow guess from many addresses (99 an hour, forever). This count is per person, never ages
# out, and only a successful second factor (or an admin's Reset sign-in) clears it. It is only ever raised by someone who already had
# the right PASSWORD, so a stranger who merely knows the name cannot lock the owner out.

TOTP_SOFT_LOCK_AFTER = 10      # then a delay that doubles per further failure
TOTP_HARD_LOCK_AT = 100        # then only a recovery code or an admin's Reset sign-in
TOTP_DELAY_BASE_SECONDS = 60
TOTP_DELAY_CAP_SECONDS = 3600


def totp_lock_state(streak: int, since_seconds: float) -> tuple[str, int]:
    """("ok" | "wait" | "hard", seconds to wait) for a streak of consecutive failures, the last one `since_seconds` ago."""
    if streak >= TOTP_HARD_LOCK_AT:
        return "hard", 0
    if streak >= TOTP_SOFT_LOCK_AFTER:
        delay = min(TOTP_DELAY_BASE_SECONDS * 2 ** (streak - TOTP_SOFT_LOCK_AFTER), TOTP_DELAY_CAP_SECONDS)
        left = int(delay - since_seconds) + 1
        if left > 0:
            return "wait", left
    return "ok", 0


def totp_lock(user: dict | None) -> tuple[str, int]:
    if not user:
        return "ok", 0
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT totp_fail_streak AS n, extract(epoch from now() - totp_fail_at) AS since FROM users WHERE id = %s", (user["id"],))
        row = cur.fetchone()
    if not row:
        return "ok", 0
    return totp_lock_state(row["n"], float(row["since"] or 0))


def totp_failed(user_id: int) -> int:
    """One more consecutive wrong code. Writes a line in the instance audit (the owner's Users page) when the soft and the hard lock begin."""
    with get_cursor() as cur:
        cur.execute("UPDATE users SET totp_fail_streak = totp_fail_streak + 1, totp_fail_at = now() WHERE id = %s "
                    "RETURNING totp_fail_streak AS n, username", (user_id,))
        row = cur.fetchone()
        if not row:
            return 0
        if row["n"] in (TOTP_SOFT_LOCK_AFTER, TOTP_HARD_LOCK_AT):
            from app import access
            hard = row["n"] >= TOTP_HARD_LOCK_AT
            access.audit(cur, "signin.code_lock", (f"{row['username']}: {row['n']} wrong authenticator codes in a row after the right password. "
                                                  + ("Locked: a recovery code or Reset sign-in is needed." if hard else "Sign-in is slowed down until a code is right.")))
            log.warning("second-factor guessing on %s: %s wrong codes in a row", row["username"], row["n"])
        return row["n"]


def totp_reset(user_id: int) -> None:
    with get_cursor() as cur:
        cur.execute("UPDATE users SET totp_fail_streak = 0, totp_fail_at = NULL WHERE id = %s AND totp_fail_streak <> 0", (user_id,))


LOCK_WAIT_TEXT = "Too many wrong codes in a row. Wait {mins} minute(s) and try the next code, or use a recovery code."
LOCK_HARD_TEXT = "Too many wrong codes in a row. Use a recovery code, or ask an administrator to Reset sign-in for you."


def totp_lock_message(user: dict | None) -> str:
    state, secs = totp_lock(user)
    if state == "hard":
        return LOCK_HARD_TEXT
    if state == "wait":
        return LOCK_WAIT_TEXT.format(mins=max(1, (secs + 59) // 60))
    return ""


def end_other_sessions(user_id: int, keep_session_id: int | None) -> None:
    """A new password or authenticator ends every OTHER sign-in of this person (whoever held the old one is out), keeping the current one."""
    with get_cursor() as cur:
        cur.execute("DELETE FROM web_sessions WHERE user_id = %s AND id <> %s", (user_id, keep_session_id if keep_session_id is not None else 0))


# --- rate limiting -------------------------------------------------------------------------------------------------

USER_THRESHOLD = 5          # failures per (username, address) before the backoff starts
USER_CEILING = 100          # failures per username across ALL addresses in an hour: a hard stop against a distributed guess
IP_THRESHOLD = 20           # per address (higher: several people can share one)
BACKOFF_BASE_SECONDS = 30   # then 30 s, 60 s, 120 s ... doubling per further failure
BACKOFF_CAP_SECONDS = 3600
_WINDOW = "1 hour"


def _trusted_peer(peer: str) -> bool:
    import ipaddress
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        return False
    for net in config.TRUSTED_PROXIES:
        try:
            if addr in ipaddress.ip_network(net, strict=False):
                return True
        except ValueError:
            log.warning("trusted_proxies: %r is not a CIDR range", net)
    return False


def client_ip(request) -> str:
    """The address to rate-limit. X-Forwarded-For is believed ONLY when the socket peer is a trusted proxy
    (config.TRUSTED_PROXIES); then it is the LAST entry (what that proxy appended; earlier ones are client-supplied) and must
    be a valid address. Otherwise the peer itself."""
    import ipaddress
    peer = request.client.host if request.client else ""
    if peer and _trusted_peer(peer):
        last = request.headers.get("x-forwarded-for", "").split(",")[-1].strip()
        try:
            return str(ipaddress.ip_address(last))
        except ValueError:
            pass
    return peer[:100]


def ip_key(ip: str) -> str:
    return "ip:" + ip


def user_key(username: str) -> str:
    return "user:" + hashlib.sha256(normalise_username(username).encode("utf-8")).hexdigest()[:32]


ID_STEP_THRESHOLD = 40      # identifier-step submissions per address per hour (every one is counted, known name or not)


def idstep_key(ip: str) -> str:
    return "idstep:" + ip


def user_ip_key(username: str, ip: str) -> str:
    """The backoff key: one username FROM one address, so someone elsewhere guessing the owner's name can't lock the owner
    out of their own address."""
    return "userip:" + hashlib.sha256((normalise_username(username) + "|" + ip).encode("utf-8")).hexdigest()[:32]


def lockout_seconds(keys_with_thresholds: list[tuple[str, int]]) -> int:
    """Seconds until the next attempt is allowed (0 = go ahead): after `threshold` failures inside the last hour, each
    further failure doubles the wait."""
    worst = 0
    with get_cursor(commit=False) as cur:
        for key, threshold in keys_with_thresholds:
            cur.execute(f"SELECT count(*) AS n, extract(epoch from now() - max(at)) AS since FROM login_attempts "  # noqa: S608
                        f"WHERE key = %s AND at > now() - interval '{_WINDOW}'", (key,))
            row = cur.fetchone()
            if row["n"] >= threshold:
                delay = min(BACKOFF_BASE_SECONDS * 2 ** (row["n"] - threshold), BACKOFF_CAP_SECONDS)
                worst = max(worst, int(delay - float(row["since"] or 0)) + 1)
    return max(worst, 0)


def record_failure(*keys: str) -> None:
    with get_cursor() as cur:
        cur.execute("DELETE FROM login_attempts WHERE at < now() - interval '1 day'")
        for k in keys:
            cur.execute("INSERT INTO login_attempts (key) VALUES (%s)", (k,))


def clear_failures(key: str) -> None:
    with get_cursor() as cur:
        cur.execute("DELETE FROM login_attempts WHERE key = %s", (key,))


# --- sign-in steps in progress (the 'password accepted, second factor awaited' state and WebAuthn challenges) -----------

FLOW_MINUTES = 5
FLOW_MAX_ATTEMPTS = 5


def start_flow(kind: str, user_id: int | None = None, challenge: bytes | None = None, next_path: str = "/",
               session_id: int | None = None, identifier: str = "") -> str:
    token = secrets.token_urlsafe(32)
    with get_cursor() as cur:
        cur.execute("DELETE FROM auth_flows WHERE expires_at < now()")
        cur.execute("INSERT INTO auth_flows (token_hash, kind, user_id, session_id, challenge, next_path, identifier, expires_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, now() + make_interval(mins => %s))",
                    (_flow_hash(token), kind, user_id, session_id, challenge, next_path, identifier[:320], FLOW_MINUTES))
    return token


def _flow_hash(token: str) -> str:
    return hashlib.sha256(("flow:" + token).encode("utf-8")).hexdigest()


def peek_flow(token: str | None, kind: str) -> dict | None:
    if not token or len(token) > 200:
        return None
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT * FROM auth_flows WHERE token_hash = %s AND kind = %s AND expires_at > now()", (_flow_hash(token), kind))
        row = cur.fetchone()
    return dict(row) if row else None


def drop_flow(token: str | None) -> None:
    if token:
        with get_cursor() as cur:
            cur.execute("DELETE FROM auth_flows WHERE token_hash = %s", (_flow_hash(token),))


def take_flow(token: str | None, kind: str) -> dict | None:
    """Use a flow once: deleted as it is read, and only while fresh."""
    if not token or len(token) > 200:
        return None
    with get_cursor() as cur:
        cur.execute("DELETE FROM auth_flows WHERE token_hash = %s AND kind = %s AND expires_at > now() RETURNING *",
                    (_flow_hash(token), kind))
        row = cur.fetchone()
    return dict(row) if row else None


def set_flow_challenge(token: str | None, kind: str, challenge: bytes) -> bool:
    """Put a fresh WebAuthn challenge on a flow that is still live (the second step's passkey). False when it isn't."""
    if not token or len(token) > 200:
        return False
    with get_cursor() as cur:
        cur.execute("UPDATE auth_flows SET challenge = %s WHERE token_hash = %s AND kind = %s AND expires_at > now()",
                    (challenge, _flow_hash(token), kind))
        return cur.rowcount == 1


def take_flow_challenge(token: str | None, kind: str) -> bytes | None:
    """The challenge stored on a live flow, cleared as it is read so it answers once. The flow itself stays."""
    if not token or len(token) > 200:
        return None
    with get_cursor() as cur:
        cur.execute("SELECT challenge FROM auth_flows WHERE token_hash = %s AND kind = %s AND expires_at > now() FOR UPDATE",
                    (_flow_hash(token), kind))
        row = cur.fetchone()
        if not row or row["challenge"] is None:
            return None
        cur.execute("UPDATE auth_flows SET challenge = NULL WHERE token_hash = %s", (_flow_hash(token),))
    return bytes(row["challenge"])


def flow_failed(token: str) -> None:
    """One wrong second-factor answer: count it; the flow dies after FLOW_MAX_ATTEMPTS."""
    with get_cursor() as cur:
        cur.execute("UPDATE auth_flows SET attempts = attempts + 1 WHERE token_hash = %s", (_flow_hash(token),))
        cur.execute("DELETE FROM auth_flows WHERE token_hash = %s AND attempts >= %s", (_flow_hash(token), FLOW_MAX_ATTEMPTS))


# --- passkeys (WebAuthn, py_webauthn) --------------------------------------------------------------------------------

def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def user_handle(user_id: int) -> bytes:
    return f"crewboss-user-{user_id}".encode("ascii")


def passkey_count(user_id: int) -> int:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT count(*) AS n FROM webauthn_credentials WHERE user_id = %s", (user_id,))
        return cur.fetchone()["n"]


def list_passkeys(user_id: int) -> list[dict]:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT id, name, created_at, last_used_at FROM webauthn_credentials WHERE user_id = %s ORDER BY id", (user_id,))
        return [dict(r) for r in cur.fetchall()]


# WebAuthn L3 `hints`: steers browsers to this computer's own passkeys, a phone (the QR code) and password managers, and away from
# security keys. Browsers that don't know it ignore it. authenticatorAttachment is deliberately NOT set (every kind of passkey stays
# possible), and no attestation is demanded, so a security key is never refused.
PASSKEY_HINTS = ["client-device", "hybrid"]

# Common authenticator AAGUIDs -> the name shown on the account page. Unknown (or all-zero, which Apple and others send) = "Passkey".
KNOWN_AAGUIDS = {
    "08987058-cadc-4b81-b6e1-30de50dcbe96": "Windows Hello",
    "9ddd1817-af5a-4672-a2b9-3e3dd95000a9": "Windows Hello",
    "6028b017-b1d4-4c02-b4b3-afcdafc96bb2": "Windows Hello",
    "fbfc3007-154e-4ecc-8c0b-6e020557d7bd": "iCloud Keychain",
    "dd4ec289-e01d-41c9-bb89-70fa845d4bf2": "iCloud Keychain",
    "ea9b8d66-4d01-1d21-3ce4-b6b48cb575d4": "Google Password Manager",
    "adce0002-35bc-c60a-648b-0b25f1f05503": "Google Password Manager",
    "d548826e-79b4-db40-a3d8-11116f7e8349": "Bitwarden",
    "bada5566-a7aa-401f-bd96-45619a55120d": "1Password",
    "90a3ccdf-635c-4729-a248-9b709135078f": "Microsoft Authenticator",
    "de1e552d-db1d-4423-a619-566b625cdbad": "Microsoft Authenticator",
}


def passkey_label(aaguid: str | None) -> str:
    return KNOWN_AAGUIDS.get((aaguid or "").lower(), "Passkey")


def _with_hints(options_json: str) -> str:
    import json
    o = json.loads(options_json)
    o["hints"] = list(PASSKEY_HINTS)
    return json.dumps(o)


def registration_options(user: dict, rp_id: str | None = None) -> tuple[str, bytes]:
    """(options JSON for navigator.credentials.create, challenge). A DISCOVERABLE (resident) credential is REQUIRED, because sign-in
    sends an empty allowCredentials and the browser lists what it holds; user verification is REQUIRED too (PIN/biometric)."""
    from webauthn import generate_registration_options, options_to_json
    from webauthn.helpers import base64url_to_bytes
    from webauthn.helpers.structs import (AuthenticatorSelectionCriteria, PublicKeyCredentialDescriptor,
                                          ResidentKeyRequirement, UserVerificationRequirement)
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT credential_id FROM webauthn_credentials WHERE user_id = %s", (user["id"],))
        existing = [PublicKeyCredentialDescriptor(id=base64url_to_bytes(r["credential_id"])) for r in cur.fetchall()]
    opts = generate_registration_options(
        rp_id=rp_id or public_host(), rp_name=instance.get_str("name", "Crewboss"), user_id=user_handle(user["id"]),
        user_name=user.get("username") or "owner", user_display_name=user.get("display_name") or user.get("username") or "owner",
        exclude_credentials=existing,
        authenticator_selection=AuthenticatorSelectionCriteria(resident_key=ResidentKeyRequirement.REQUIRED,
                                                               user_verification=UserVerificationRequirement.REQUIRED))
    return _with_hints(options_to_json(opts)), opts.challenge


def verify_registration(user_id: int, credential: dict, challenge: bytes, name: str, rp_id: str | None = None,
                        origin: str | None = None) -> bool:
    """Check a new passkey for THIS request's RP ID and store it with that RP ID."""
    rp_id = rp_id or public_host()
    from webauthn import verify_registration_response
    try:
        v = verify_registration_response(credential=credential, expected_challenge=challenge, expected_rp_id=rp_id,
                                         expected_origin=origin or public_origin(), require_user_verification=True)
    except Exception as exc:  # noqa: BLE001 — py_webauthn raises its own InvalidRegistrationResponse etc.
        log.warning("passkey registration refused (%s)", type(exc).__name__)
        return False
    transports = [t for t in (credential.get("response", {}).get("transports") or []) if isinstance(t, str)][:8]
    name = passkey_label(getattr(v, "aaguid", "")) if passkey_label(getattr(v, "aaguid", "")) != "Passkey" else ((name or "").strip()[:80] or "Passkey")
    with get_cursor() as cur:
        cur.execute("INSERT INTO webauthn_credentials (user_id, credential_id, public_key, sign_count, transports, name, rp_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (credential_id) DO NOTHING",
                    (user_id, _b64u(v.credential_id), v.credential_public_key, v.sign_count, transports, name, rp_id))
        return cur.rowcount == 1


def authentication_options(user_id: int | None = None, rp_id: str | None = None) -> tuple[str, bytes]:
    """Options for navigator.credentials.get. Sign-in passes NO user, whoever the email was: allowCredentials is empty, so the browser
    shows its own chooser (including "Use a phone or tablet") and nothing about the account is revealed. Only re-confirming a signed-in
    session passes the user, which limits it to that user's passkeys."""
    from webauthn import generate_authentication_options, options_to_json
    from webauthn.helpers import base64url_to_bytes
    from webauthn.helpers.structs import PublicKeyCredentialDescriptor, UserVerificationRequirement
    allow = None
    if user_id is not None:
        with get_cursor(commit=False) as cur:
            cur.execute("SELECT credential_id FROM webauthn_credentials WHERE user_id = %s", (user_id,))
            allow = [PublicKeyCredentialDescriptor(id=base64url_to_bytes(r["credential_id"])) for r in cur.fetchall()]
    opts = generate_authentication_options(rp_id=rp_id or public_host(), allow_credentials=allow,
                                           user_verification=UserVerificationRequirement.REQUIRED)
    return _with_hints(options_to_json(opts)), opts.challenge


def verify_authentication(credential: dict, challenge: bytes, only_user_id: int | None = None, rp_id: str | None = None,
                          origin: str | None = None) -> int | None:
    """The user id a good assertion belongs to, else None. The sign counter moves forward (py_webauthn refuses a counter that
    doesn't, which is how a cloned authenticator shows). A credential made for another RP ID than this request's is refused."""
    from webauthn import verify_authentication_response
    rp_id = rp_id or public_host()
    cid = str(credential.get("rawId") or credential.get("id") or "")
    if not cid or len(cid) > 1400:
        return None
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT c.id, c.user_id, c.public_key, c.sign_count, c.rp_id, u.disabled_at FROM webauthn_credentials c "
                    "JOIN users u ON u.id = c.user_id WHERE c.credential_id = %s", (cid,))
        row = cur.fetchone()
    if not row or row["disabled_at"] or (only_user_id is not None and row["user_id"] != only_user_id):
        return None
    if (row["rp_id"] or public_host()) != rp_id:          # a NULL (not yet backfilled) one was made under the public host
        log.warning("passkey sign-in refused (credential belongs to another RP ID)")
        return None
    handle = (credential.get("response") or {}).get("userHandle")
    if handle:
        try:
            pad = "=" * (-len(handle) % 4)
            if base64.urlsafe_b64decode(handle + pad) != user_handle(row["user_id"]):
                return None
        except Exception:  # noqa: BLE001
            return None
    try:
        v = verify_authentication_response(credential=credential, expected_challenge=challenge, expected_rp_id=rp_id,
                                           expected_origin=origin or public_origin(), credential_public_key=bytes(row["public_key"]),
                                           credential_current_sign_count=row["sign_count"], require_user_verification=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("passkey sign-in refused (%s)", type(exc).__name__)
        return None
    with get_cursor() as cur:
        cur.execute("UPDATE webauthn_credentials SET sign_count = %s, last_used_at = now() WHERE id = %s", (v.new_sign_count, row["id"]))
    return row["user_id"]


def remove_passkey(user_id: int, cred_id: int) -> bool:
    """Delete a passkey. The LAST one goes only for someone who has a password + authenticator to sign in with instead (otherwise
    they would be locked out): refused when no other would remain and they have no such way in."""
    with get_cursor() as cur:
        cur.execute("SELECT * FROM users WHERE id = %s FOR UPDATE", (user_id,))
        user = cur.fetchone()
        if not user:
            return False
        cur.execute("SELECT count(*) AS n FROM webauthn_credentials WHERE user_id = %s AND id <> %s", (user_id, cred_id))
        if not cur.fetchone()["n"] and not has_mfa(dict(user)):
            return False
        cur.execute("DELETE FROM webauthn_credentials WHERE id = %s AND user_id = %s", (cred_id, user_id))
        return cur.rowcount == 1


def take_backup_nudge(user: dict) -> bool:
    """True ONCE for a person who signs in with a passkey but has no password + authenticator backup: the caller shows the
    "add a backup" prompt."""
    if has_mfa(user):
        return False
    with get_cursor() as cur:
        cur.execute("UPDATE users SET mfa_nudged_at = now() WHERE id = %s AND mfa_nudged_at IS NULL RETURNING id", (user["id"],))
        return cur.fetchone() is not None
