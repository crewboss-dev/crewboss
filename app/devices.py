"""Runner devices and enrolment (task #627, change #50, phase P1). A runner is a native process on a user's PC that
connects OUT to this dashboard. It is enrolled once, by a one-time code, and from then on holds a device token (cbr_...)
bound to ONE user and ONE account, plus a keypair whose PRIVATE half never leaves the PC.

The code: `host:port#<fp8>-<random>`, made on the account's Machines page by a signed-in person and shown ONCE. `fp8` is
the dashboard's runner identity key fingerprint (app/runner_identity.py), so the runner pins WHICH server it is talking
to before it sends anything. The secret is 24 random bytes (urlsafe); only its SHA-256 is stored and the row is found BY
that hash, so a wrong guess matches nothing and can't be pinned to, or lock, anyone's live code. A code lives at most 5
minutes (a CHECK in the table too) and works once. What stops guessing is the entropy plus a per-IP rate limit in front of
everything. Every attempt, good or bad, is logged: to the activity log of the code's account when a code was found, else
to the server log with the IP. A refusal never says WHY.

enrol() stores the runner's PUBLIC key only (base64 of the raw 32-byte ed25519 key; anything else is refused) and returns
{device_id, device_token}; of the token only its hash is stored. verify_signature() checks a message signed by the runner's
private key (used from P3). Scope: a device of another account is NotFound, exactly like a missing one."""
from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from app import config, jobs, repo, runner_identity, tree
from app.db import get_cursor
from app.repo import BadRequest, NotFound

log = logging.getLogger("crewboss.devices")

MODES = ("subscription", "api")
CODE_MINUTES = 5
RATE_LIMIT_ATTEMPTS = 10    # enrol attempts per client address per RATE_LIMIT_WINDOW seconds
RATE_LIMIT_WINDOW = 60
from app.auth import DEVICE_PREFIX   # cbr_ (the old mdr_ is still accepted: app/auth.py)
ACTOR = "runner:enrol"
REFUSED = "that enrolment code isn't valid (it may be wrong, already used, expired or locked)"
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
# ASCII only: str.isdigit() and \w accept other scripts.
_SECRET_RE = re.compile(r"[A-Za-z0-9_-]{20,64}")


class EnrolRefused(Exception):
    """An enrolment that must not make a device. The message never says why (REFUSED)."""


class RateLimited(Exception):
    """Too many enrolment attempts from one address."""


_attempts: dict[str, deque] = defaultdict(deque)
_attempts_lock = threading.Lock()


def _rate_check(ip: str) -> None:
    """A sliding window per client address; every attempt counts, good or bad. (The per-code lockout is the real
    guard; this just keeps one address from hammering many codes.)"""
    now = time.monotonic()
    with _attempts_lock:
        q = _attempts[ip or "?"]
        while q and now - q[0] > RATE_LIMIT_WINDOW:
            q.popleft()
        if len(q) >= RATE_LIMIT_ATTEMPTS:
            log.warning("runner enrol: rate limit refused %s", ip or "?")
            raise RateLimited("too many enrolment attempts; wait a minute and try again")
        q.append(now)
        if len(_attempts) > 5000:   # forget idle addresses
            for k in [k for k, v in _attempts.items() if not v or now - v[-1] > RATE_LIMIT_WINDOW]:
                del _attempts[k]


def reset_rate_limit() -> None:
    with _attempts_lock:
        _attempts.clear()


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _clean(value: str, limit: int) -> str:
    return _CTRL.sub("", value if isinstance(value, str) else "").strip()[:limit]


def parse_pubkey(pubkey: str) -> bytes:
    """The raw 32 bytes of a runner's PUBLIC ed25519 key given as base64, or BadRequest. Anything else (a 64-byte
    seed+public pair, a PEM, ...) is refused, so a private key is never stored."""
    try:
        raw = base64.b64decode(pubkey.strip() if isinstance(pubkey, str) else "", validate=True)
        if len(raw) != 32:
            raise ValueError
        Ed25519PublicKey.from_public_bytes(raw)
    except (binascii.Error, ValueError, TypeError):
        raise BadRequest("pubkey must be the base64 of a raw 32-byte ed25519 public key") from None
    return raw


def _log(cur, account_id: int | None, verb: str, entity_id: int | None, summary: str) -> None:
    """One activity row. A refused attempt with no code behind it has no account: it goes to the overseer project when
    there is one, else only to the server log."""
    if account_id is None and config.OVERSEER_PROJECT:
        try:
            account_id = repo.get_project_by_slug(config.OVERSEER_PROJECT)["id"]
        except NotFound:
            account_id = None
    if account_id is None:
        log.warning("runner enrol: %s: %s", verb, summary)
        return
    repo.record_activity(cur, account_id, ACTOR, verb, "device", entity_id, summary)


# --- the code ------------------------------------------------------------------------------------------------------

def _host_port() -> str:
    parts = urlsplit(config.RUNNER_BASE_URL)
    host = parts.hostname or "localhost"
    port = parts.port or (443 if parts.scheme == "https" else 80)
    return f"{host}:{port}"


WHERE_CHOICES = ("auto", "lan", "public")


def _target(where: str) -> tuple[str, str | None]:
    """(host:port, pin26 | None) for the code. A LAN address has a self-signed certificate, so its code carries the certificate's pin
    (`fp8.pin26`); a public (Let's Encrypt) address keeps the old format. `auto` follows RUNNER_BASE_URL: a LAN host there means a pinned code."""
    from app import front_door
    if where not in WHERE_CHOICES:
        raise BadRequest("where must be one of " + ", ".join(WHERE_CHOICES))
    lan = front_door.state()["lan"]
    parts = urlsplit(config.RUNNER_BASE_URL)
    base_host = (parts.hostname or "").lower()
    base_is_lan = bool(lan) and base_host in [h.lower() for h in lan["hosts"]]
    if where == "public" or (where == "auto" and not base_is_lan):
        if where == "public" and not front_door.public_host():
            raise BadRequest("this installation has no public address yet")
        if where == "public" and base_is_lan:   # RUNNER_BASE_URL is the LAN address: the public one is the padlocked name
            pub = front_door.state()["public"]
            return f"{pub['domain']}:{pub['port']}", None
        return _host_port(), None
    if not lan or not lan.get("spki_pin26"):
        raise BadRequest("the local-network certificate is not set up, so a pinned code cannot be made")
    host = base_host if base_is_lan else lan["hosts"][0]
    return f"{host}:{lan['https_port']}", lan["spki_pin26"]


def issue_code(user: dict, account: dict, mode: str, where: str = "auto") -> dict:
    """A new enrolment code for `user` (a users row) on `account`. Returns {code, expires_at}: `code` is
    `host:port#<fp8>-<secret>` (or `host:port#<fp8>.<pin26>-<secret>` for a LAN address) and is shown ONCE; only the secret's hash is stored.
    `where`: "lan" (On this network), "public" (Anywhere) or "auto" (what RUNNER_BASE_URL says)."""
    if mode not in MODES:
        raise BadRequest(f"mode must be one of {', '.join(MODES)}")
    if not tree.is_runner_home(account):
        raise BadRequest("a runner belongs to an account (or to your own top-level project)")
    fp = runner_identity.fp8()   # before any row: no identity key, no code
    hostport, pin = _target(where)
    with get_cursor() as cur:
        cur.execute("SELECT disabled_at FROM users WHERE id = %s", (user["id"],))
        row = cur.fetchone()
        if not row or row["disabled_at"]:
            raise NotFound("no such user")
        secret = secrets.token_urlsafe(24)
        cur.execute(
            "INSERT INTO enrolment_codes (secret_hash, user_id, account_id, mode, created_at, expires_at) "
            "VALUES (%s, %s, %s, %s, now(), now() + make_interval(mins => %s)) RETURNING id, expires_at",
            (_hash(secret), user["id"], account["id"], mode, CODE_MINUTES))
        made = cur.fetchone()
        code_id, expires = made["id"], made["expires_at"]
        repo.record_activity(cur, account["id"], f"user:{user['id']}", "issued", "enrolment_code", code_id,
                             f"Issued a {mode} runner enrolment code")
    return {"code": f"{hostport}#{fp}{'.' + pin if pin else ''}-{secret}", "expires_at": expires, "pinned": bool(pin)}


def enrol(secret: str, pubkey: str, name: str, platform: str, ip: str = "") -> dict:
    """Swap a live enrolment code's `secret` (the part after `#<fp8>-`) and the runner's PUBLIC key for a device.
    Returns {device_id, device_token, expires_at}. Raises RateLimited, BadRequest (a bad public key) or EnrolRefused."""
    _rate_check(ip)
    secret = secret.strip() if isinstance(secret, str) else ""
    if not _SECRET_RE.fullmatch(secret):
        with get_cursor() as cur:
            _log(cur, None, "enrol_refused", None, f"Malformed enrolment code from {ip or '?'}")
        raise EnrolRefused(REFUSED)
    name, platform = _clean(name, 100), _clean(platform, 60)
    try:
        raw = parse_pubkey(pubkey)
    except BadRequest:
        with get_cursor() as cur:
            _log(cur, None, "enrol_refused", None, f"Enrolment refused from {ip or '?'}: bad public key")
        raise
    with get_cursor() as cur:
        cur.execute("SELECT *, (expires_at <= now()) AS expired FROM enrolment_codes WHERE secret_hash = %s FOR UPDATE",
                    (_hash(secret),))
        code = cur.fetchone()
        why = ("unknown code" if not code else "code already used" if code["used_at"] is not None
               else "code expired" if code["expired"] else None)
        if why:
            _log(cur, code["account_id"] if code else None, "enrol_refused", code["id"] if code else None,
                 f"Enrolment refused from {ip or '?'}: {why}")
        else:
            token = DEVICE_PREFIX + secrets.token_urlsafe(32)
            cur.execute("UPDATE enrolment_codes SET used_at = now() WHERE id = %s", (code["id"],))
            cur.execute(
                "INSERT INTO device_tokens (user_id, account_id, name, mode, token_hash, pubkey, pubkey_fp, platform, "
                "expires_at, last_ip) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now() + make_interval(days => %s), %s) "
                "RETURNING id, expires_at",
                (code["user_id"], code["account_id"], name or "runner", code["mode"], _hash(token),
                 base64.b64encode(raw).decode("ascii"), hashlib.sha256(raw).hexdigest(), platform,
                 config.RUNNER_DEVICE_TOKEN_DAYS, ip[:100] or None))
            row = cur.fetchone()
            _log(cur, code["account_id"], "enrolled", row["id"],
                 f"Enrolled runner '{name or 'runner'}' ({code['mode']}, {platform or 'unknown platform'}) from {ip or '?'}")
            return {"device_id": row["id"], "device_token": token, "expires_at": row["expires_at"], "mode": code["mode"]}
    raise EnrolRefused(REFUSED)


# --- devices -------------------------------------------------------------------------------------------------------

def list_for(account_id: int, user_id: int | None = None) -> list[dict]:
    """The account's devices, newest first, each with `status` (active / revoked / expired) and the owner's name.
    `user_id`: only that person's runners (a non-owner sees nobody else's, #713)."""
    with get_cursor(commit=False) as cur:
        cur.execute(
            "SELECT d.id, d.name, d.mode, d.platform, d.pubkey_fp, d.created_at, d.expires_at, d.revoked_at, "
            "d.revoked_by, d.last_seen, d.last_ip, d.labels, d.server_id, d.user_id, d.gemini_key_last4, d.gemini_key_fp, d.gemini_key_set_at, d.gemini_key_state, "
            "u.display_name AS user_name, "
            "CASE WHEN d.revoked_at IS NOT NULL THEN 'revoked' WHEN d.expires_at <= now() THEN 'expired' "
            "ELSE 'active' END AS status "
            "FROM device_tokens d JOIN users u ON u.id = d.user_id WHERE d.account_id = %s "
            "AND (%s::int IS NULL OR d.user_id = %s) ORDER BY d.id DESC",
            (account_id, user_id, user_id))
        return [dict(r) for r in cur.fetchall()]


def rename(account_id: int, device_id: int, name: str, by: str, user_id: int | None = None) -> str:
    """Rename a runner (a person's own, or any for the owner). The name is only a label: nothing keys on it."""
    name = _clean(name, 100)
    if not name:
        raise BadRequest("give the runner a name")
    with get_cursor() as cur:
        cur.execute("UPDATE device_tokens SET name = %s WHERE id = %s AND account_id = %s AND (%s::int IS NULL OR user_id = %s) "
                    "RETURNING id", (name, device_id, account_id, user_id, user_id))
        if not cur.fetchone():
            raise NotFound(f"device {device_id} not found")
        repo.record_activity(cur, account_id, by[:100], "renamed", "device", device_id, f"Runner {device_id} renamed to {name!r}")
    return name


def suggest_server(device: dict, servers: list[dict]) -> dict | None:
    """The server this runner may be the same machine as, or None: the runner's name equals the server's name or host name
    (case-blind), or the address the runner last connected from IS the server's address. Only a SUGGESTION: the card asks, and a
    person says yes (link_server). A runner already joined suggests nothing."""
    if device.get("server_id"):
        return None
    name, ip = (device.get("name") or "").strip().lower(), (device.get("last_ip") or "").strip()
    for srv in servers:
        names = {(srv.get("name") or "").lower(), (srv.get("hostname") or "").lower()} - {""}
        if (name and name in names) or (ip and ip == srv.get("address")):
            return srv
    return None


def link_server(account_id: int, device_id: int, server_id: int | None, by: str, user_id: int | None = None) -> None:
    """Say a runner and a server are one machine (or, with server_id None, that they are not). The server must be this account's;
    the runner this person's own (the owner: any). A runner is joined to at most one server."""
    from app import server_access
    if server_id is not None:
        server_access.get(account_id, int(server_id))          # NotFound for another account's, like a missing one
    with get_cursor() as cur:
        cur.execute("UPDATE device_tokens SET server_id = %s WHERE id = %s AND account_id = %s AND (%s::int IS NULL OR user_id = %s) "
                    "RETURNING name", (server_id, device_id, account_id, user_id, user_id))
        row = cur.fetchone()
        if not row:
            raise NotFound(f"device {device_id} not found")
        repo.record_activity(cur, account_id, by[:100], "joined" if server_id else "unjoined", "device", device_id,
                             f"Runner {row['name']!r} " + (f"joined to server {server_id}" if server_id else "is no longer joined to a server"))


def set_labels(account_id: int, device_id: int, raw, by: str, user_id: int | None = None) -> list[str]:
    """The owner sets a device's labels (jobs that require labels go only to a device holding all of them). Only this
    function writes them: the runner can't set its own. Another account's device is NotFound. Returns the labels."""
    labels = jobs.parse_labels(raw)
    with get_cursor() as cur:
        cur.execute("UPDATE device_tokens SET labels = %s WHERE id = %s AND account_id = %s "
                    "AND (%s::int IS NULL OR user_id = %s) RETURNING name",
                    (labels, device_id, account_id, user_id, user_id))
        row = cur.fetchone()
        if not row:
            raise NotFound(f"device {device_id} not found")
        repo.record_activity(cur, account_id, by[:100], "labelled", "device", device_id,
                             f"Labels of runner {row['name']!r} set to: {', '.join(labels) or 'none'}")
    jobs.nudge_queued(account_id)   # a newly labelled device may now be able to take a waiting job
    return labels


def revoke(account_id: int, device_id: int, by: str, user_id: int | None = None) -> dict:
    """Revoke one device of `account_id` (its token stops resolving at once). Another account's device, or a missing
    one, is NotFound. Revoking twice is harmless. Its running jobs end now, and queued jobs pinned to it ("Runs on") are unpinned so
    another of the person's runners can take them. Returns {id, name}."""
    requeued: list[int] = []
    with get_cursor() as cur:
        cur.execute("SELECT id, name, revoked_at FROM device_tokens WHERE id = %s AND account_id = %s "
                    "AND (%s::int IS NULL OR user_id = %s) FOR UPDATE",
                    (device_id, account_id, user_id, user_id))
        row = cur.fetchone()
        if not row:
            raise NotFound(f"device {device_id} not found")
        if not row["revoked_at"]:
            cur.execute("UPDATE device_tokens SET revoked_at = now(), revoked_by = %s WHERE id = %s",
                        (by[:200], device_id))
            repo.record_activity(cur, account_id, by[:100], "revoked", "device", device_id,
                                 f"Revoked runner '{row['name']}'")
            # Its running jobs end now, in this transaction: locks orphaned, job failed or requeued with a new fence,
            # job token cleared (the same handling as an expired lease).
            jobs.end_for_device(cur, device_id, "its runner was revoked", requeued)
            from app import ca   # lazy: ca imports the runner registry lazily too
            ca.end_for_device(cur, device_id)   # its running CA chat turns fail now (task #702)
    # After the commit: its live socket closes (4401), and its requeued jobs are offered to the user's other runners.
    from app import runner_ws   # lazy: runner_ws imports jobs, which would loop back here
    runner_ws.close_device(device_id, runner_ws.CLOSE_AUTH, "revoked")
    jobs.nudge(requeued)
    return {"id": row["id"], "name": row["name"]}


def verify_signature(device_id: int, message: bytes, signature: bytes | str) -> bool:
    """True only if `signature` (raw bytes, or base64) is the ed25519 signature of `message` by this device's private
    key AND the device is live (not revoked, not expired, its user not disabled). Never raises on a bad signature.
    For P3."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT d.pubkey FROM device_tokens d JOIN users u ON u.id = d.user_id WHERE d.id = %s "
                    "AND d.revoked_at IS NULL AND d.expires_at > now() AND u.disabled_at IS NULL", (device_id,))
        row = cur.fetchone()
    if not row:
        return False
    try:
        sig = base64.b64decode(signature, validate=True) if isinstance(signature, str) else bytes(signature)
        Ed25519PublicKey.from_public_bytes(base64.b64decode(row["pubkey"])).verify(sig, bytes(message))
    except (InvalidSignature, binascii.Error, ValueError, TypeError):
        return False
    return True
