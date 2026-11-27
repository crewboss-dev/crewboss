"""Platform keys (#562; Dashboard side of the ERP's #547/#563 contract, the key store's integration doc).

The keys that run the service for every tenant (AI, sending, IoT, storage, the multi-tenant app registrations, the keys
this install's own systems share) are SET from Crewboss and written ONE WAY into each project's own key store,
through its signed door:

    PUT  <url>/platform/keys/{NAME}             {"value"?, "no_grace"?, "expires_on"?, "rotate_every_months"?,
                                                 "last_rotated"?, "actor"}   set / rotate, and/or the key's clock
    POST <url>/platform/keys/{NAME}/end-grace   {"actor"}                   stop accepting the previous value now
    POST <url>/platform/keys/{NAME}/clear       {"actor"}                   remove the stored value
    GET  <url>/platform/keys                                                status: every platform key, NO values

THE SIGNATURE is the store's shared scheme (signedapi v1.1), copied byte for byte from the reference client
(the reference client): X-Dash-Key = the key id, X-Dash-Ts = unix seconds (60 s skew allowed), X-Dash-Nonce = a fresh
32-hex nonce per call (a replay is refused), X-Dash-Sig = hex HMAC-SHA256 over
"METHOD\\nPATH\\nCANONICAL_QUERY\\nts\\nnonce\\n" + the raw body, keyed with the writer credential's UTF-8 bytes. No query
string is ever sent, so CANONICAL_QUERY is "". The door allows 30 calls a minute across every route (429 beyond).

THE CREDENTIAL is a 0600 file (config.PLATFORM_KEY_STORES[slug]["key_file"]), read at CALL time and refused when anyone
but its owner could read it. NOTHING here prints, logs, stores or returns the credential or a key value: a value goes
from the page's POST into the request body and nowhere else; errors and log lines name the key and the status only.
The status document the store sends never carries a value either (the contract), so it may be kept and shown.

STAGE REMINDERS (1143): sync() reads each store's status every config.PLATFORM_KEYS_POLL_SECONDS (and observe() runs
on every page view's read too). The last stage seen per key lives in platform_key_stages; when a key's stage goes UP
(ok -> due_30 -> due_7 -> overdue, or first seen already due) the Dashboard raises a Health reminder (health_problems,
source 'keys': warning at 30 days, average at 7, high when overdue) and sends ONE phone notification for everything that
went up in that read (app/push.py deliver). Going back down (a rotation, new dates) updates or withdraws the reminder
quietly. The rows are marked pushed as they are written, so the estate sweep (app/push.py sweep) never pushes them
twice."""
from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import logging
import os
import re
import secrets
import ssl
import stat
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit

from psycopg.types.json import Json

from app import config, roles
from app.db import get_cursor

log = logging.getLogger("crewboss.platform_keys")

PREFIX = "/platform/keys"
MIN_SECRET_LEN = 32
TIMEOUT = 20
USER_AGENT = "crewboss/platform-keys"
NAME_RE = re.compile(r"[A-Z][A-Z0-9_]{1,80}")     # used with fullmatch: no trailing newline gets through
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_CTRL = re.compile(r"[\x00-\x1f\x7f]+")

SOURCE = "keys"                                    # health_problems.source of the reminders
STAGES = ("ok", "due_30", "due_7", "overdue")
RANK = {"ok": 0, "due_30": 1, "due_7": 2, "overdue": 3}
LEVEL = {"due_30": 2, "due_7": 3, "overdue": 4}    # Zabbix's scale, as every Health source: warning, average, high
DOWN_LEVEL = 4         # "can't read the key store": high, so the estate sweep pushes it and home_slot counts it
DOWN_MINUTES = 30      # a read failing this long (or no usable credential at all) raises that alarm
FRESH_MINUTES = 60     # a good read this recent = the Dashboard's reminders are live (erp_health then skips the ERP's)
STAGE_META = {
    "ok": {"label": "On schedule", "classes": "bg-status-ok/10 text-status-ok"},
    "due_30": {"label": "Due within 30 days", "classes": "bg-status-warn/15 text-charcoal dark:text-status-warn"},
    "due_7": {"label": "Due within 7 days", "classes": "bg-status-high/15 text-status-high font-semibold"},
    "overdue": {"label": "Overdue", "classes": "bg-status-disaster/15 text-status-disaster font-semibold"},
}


class KeysError(Exception):
    """A refusal or failure to show the person. The message never contains a key value or the credential."""

    def __init__(self, message: str, code: str = "error", status: int | None = None):
        super().__init__(message)
        self.code = code
        self.status = status


# ---------------------------------------------------------------------------------------------
# stores
# ---------------------------------------------------------------------------------------------

def store(slug: str) -> dict | None:
    """A key store by its slug (config.PLATFORM_KEY_STORES), with `slug`, a display `name` and its `host`; None when
    there is no such store."""
    s = config.PLATFORM_KEY_STORES.get(slug or "")
    if not s:
        return None
    return dict(s, slug=slug, name=s.get("name") or slug, host=urlsplit(s.get("url") or "").hostname or "")


def stores() -> list[dict]:
    return [store(slug) for slug in config.PLATFORM_KEY_STORES]


def home_slug() -> str:
    """The project whose menu carries Platform keys and whose page lists EVERY store (#577; the owner's #547: platform
    keys are global, set from the Dashboard): the Dashboard project, i.e. the overseer root (the overseer role),
    or the dashboard role on a box with no overseer. '' = none."""
    return roles.slug("overseer") or roles.slug("dashboard") or ""


def page_path(slug: str | None = None) -> str:
    """The Platform keys page, at a store's section when one is named: /<dashboard>/keys#<store-slug>."""
    return f"/{home_slug()}/keys" + (f"#{slug}" if slug else "")


# ---------------------------------------------------------------------------------------------
# the signature — byte for byte the reference client's (the reference client) and the store's (app/signedapi.py)
# ---------------------------------------------------------------------------------------------

def message(method: str, path: str, query: str, ts, nonce: str, body: bytes) -> bytes:
    """What is signed: METHOD, PATH, CANONICAL_QUERY, ts, nonce (each + "\\n"), then the raw body."""
    return f"{method.upper()}\n{path}\n{query}\n{ts}\n{nonce}\n".encode("utf-8") + (body or b"")


def signed_headers(key_id: str, secret: str, method: str, path: str, body: bytes, *, ts: int | None = None,
                   nonce: str | None = None) -> dict:
    """The four X-Dash-* headers for one call. A fresh nonce and the current time unless given (the tests)."""
    ts = int(time.time()) if ts is None else int(ts)
    nonce = nonce or secrets.token_hex(16)
    sig = hmac.new(secret.encode("utf-8"), message(method, path, "", ts, nonce, body), hashlib.sha256).hexdigest()
    return {"X-Dash-Key": key_id, "X-Dash-Ts": str(ts), "X-Dash-Nonce": nonce, "X-Dash-Sig": sig}


def read_secret(path: str) -> str:
    """The writer credential, read now. Refused (KeysError 'credential') when it is missing, not a plain file (docker
    made a directory because it was missing at `compose up`), readable by anyone but its owner, or too short. The
    message names the file, never its content."""
    try:
        st = os.stat(path)
    except OSError:
        raise KeysError("The Dashboard has no writer credential for this key store yet (the credential file isn't "
                        "mounted).", "credential") from None
    if not stat.S_ISREG(st.st_mode):
        raise KeysError("The writer credential's path is not a file (it was missing when the Dashboard started).",
                        "credential")
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise KeysError("The writer credential file is readable by others: chmod 600 it first.", "credential")
    try:
        with open(path, encoding="utf-8") as fh:
            secret = fh.read().strip()
    except (OSError, UnicodeDecodeError):
        raise KeysError("The writer credential file can't be read.", "credential") from None
    if len(secret) < MIN_SECRET_LEN:
        raise KeysError("The writer credential file holds no usable credential (32+ characters).", "credential")
    return secret


def credential_problem(s: dict) -> str:
    """'' when the store's credential is usable, else why not (no content)."""
    try:
        read_secret(s["key_file"])
    except KeysError as exc:
        return str(exc)
    return ""


# ---------------------------------------------------------------------------------------------
# the calls
# ---------------------------------------------------------------------------------------------

_urlopen = urllib.request.urlopen   # the tests replace this; nothing else does

MAYBE_WRITTEN = ("The key store didn't answer after the change was sent, so it may have been written. Check the "
                 "key's version and last change on this page before trying again.")


def _refusal(status: int, doc: dict) -> KeysError:
    """A non-2xx answer as a KeysError. The store's own messages never contain a value (the contract)."""
    code = str(doc.get("error") or "")[:40]
    said = doc.get("message")
    said = " ".join(_CTRL.sub(" ", said).split())[:300] if isinstance(said, str) else ""
    if status == 401:
        return KeysError("The key store refused the Dashboard's signature. Check that the credential "
                         "file on the Dashboard host matches the writer key the key store expects, and that this "
                         "box's clock is right (60 seconds allowed).", "unauthorized", 401)
    if status == 429:
        return KeysError("Too many calls to the key store (it allows 30 a minute across every action). Wait a "
                         "minute and try again.", "rate_limited", 429)
    if status == 404 and code != "unknown_key":
        return KeysError("The key store's door didn't answer (404): the call didn't come from the Dashboard host's "
                         "allowed address, or the server isn't running the platform keys release yet.", "not_found", 404)
    return KeysError(said or f"The key store answered {status}.", code or "error", status)


def check_store_url(url: str) -> None:
    """The key store is reached over https only: the signature protects a request, not the privacy of the key VALUES in it. The one
    exception is a store on this machine (http://localhost or http://127.0.0.1, any port) for tests and a local door."""
    parts = urlsplit(url or "")
    if parts.scheme == "https" and parts.hostname:
        return
    if parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1"):
        return
    raise KeysError("The key store address must start with https:// (http:// is only allowed for localhost).", "refused")


def call(s: dict, method: str, path: str, body: bytes = b"") -> dict:
    """One signed call. Returns the store's JSON answer (2xx) or raises KeysError. Never logs the body."""
    check_store_url(s.get("url", ""))      # before the credential is read or anything is signed
    secret = read_secret(s["key_file"])
    headers = {**signed_headers(s["key_id"], secret, method, path, body), "Content-Type": "application/json",
               "Accept": "application/json", "User-Agent": USER_AGENT}
    del secret
    req = urllib.request.Request(s["url"] + path, data=body if method != "GET" else None, headers=headers,
                                 method=method)
    write = method != "GET"
    try:
        with _urlopen(req, timeout=TIMEOUT, context=ssl.create_default_context()) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        try:
            status, raw = exc.code, exc.read()
        except (OSError, http.client.HTTPException):
            status, raw = exc.code, b""
    except urllib.error.URLError as exc:
        # urllib wraps what fails while CONNECTING or SENDING (refused, no route, DNS, TLS): the request never got
        # through whole, so nothing was written.
        log.warning("platform keys: %s %s on %s unreachable (%s)", method, path, s["slug"],
                    type(exc.reason).__name__)
        raise KeysError(f"Couldn't reach the key store at {s['url']}. Is the NetBird mesh up?", "unreachable") from None
    except (http.client.HTTPException, TimeoutError, OSError) as exc:
        # What fails while READING the answer (a timeout, a reset, a broken reply): the request was sent, so a write
        # may have happened. Never "wasn't changed".
        log.warning("platform keys: %s %s on %s: no answer after sending (%s)", method, path, s["slug"],
                    type(exc).__name__)
        if write:
            raise KeysError(MAYBE_WRITTEN, "uncertain") from None
        raise KeysError(f"The key store at {s['url']} didn't answer. Try again in a minute.", "unreachable") from None
    finally:
        del req
    try:
        doc = json.loads(raw or b"{}")
    except (ValueError, UnicodeDecodeError):
        doc = None
    if not isinstance(doc, dict):
        log.warning("platform keys: %s %s on %s answered %s, not JSON", method, path, s["slug"], status)
        if write and status in (502, 504):     # the proxy lost the store mid-call: it may have written
            raise KeysError(MAYBE_WRITTEN, "uncertain", status)
        raise KeysError(f"The key store answered {status} with something that isn't JSON.", "not_json", status)
    if not 200 <= status < 300:
        err = _refusal(status, doc)
        log.warning("platform keys: %s %s on %s refused: %s %s", method, path, s["slug"], status, err.code)
        raise err
    return doc


def _name(name: str) -> str:
    if not NAME_RE.fullmatch(name or ""):
        raise KeysError("Not a key name.", "bad_name")
    return name


def actor_for(web_user: dict | None) -> str:
    """Who did it, for the store's audit ('Crewboss: <this>'): the signed-in person's name and sign-in. One line,
    120 characters. Sign-in off (the tests, a NetBird-only box) = 'owner'."""
    if not web_user:
        return "owner"
    name = str(web_user.get("name") or "").strip()
    username = str(web_user.get("username") or "").strip()
    text = f"{name} ({username})" if name and username and name != username else (name or username or "owner")
    return " ".join(_CTRL.sub(" ", text).split())[:120]


def get_status(s: dict) -> dict:
    return call(s, "GET", PREFIX)


def set_value(s: dict, name: str, value: str, *, no_grace: bool = False, actor: str = "") -> dict:
    """Set or rotate. `value` goes into the request body and nowhere else."""
    _name(name)
    value = (value or "").strip() if isinstance(value, str) else ""
    if not value:
        raise KeysError("Type or paste the new key first.", "empty")
    body = json.dumps({"value": value, "no_grace": bool(no_grace), "actor": actor}).encode("utf-8")
    del value
    try:
        out = call(s, "PUT", f"{PREFIX}/{name}", body)
    finally:
        del body
    log.info("platform key %s on %s: %s (version %s, propagation %s) by %s", name, s["slug"], out.get("action"),
             out.get("key_version"), out.get("propagation"), actor)
    return out


def set_meta(s: dict, name: str, meta: dict, *, actor: str = "") -> dict:
    """A metadata-only PUT (#563): no "value" key at all, so the store never touches the value. Works for bootstrap
    keys too."""
    _name(name)
    if not meta:
        raise KeysError("Nothing to change: pick a new expiry, rotation interval or last-rotated date.", "empty")
    if "value" in meta:
        raise KeysError("A metadata change never carries a value.", "bad_request")
    out = call(s, "PUT", f"{PREFIX}/{name}", json.dumps({**meta, "actor": actor}).encode("utf-8"))
    log.info("platform key %s on %s: metadata %s by %s", name, s["slug"], sorted(meta), actor)
    return out


def end_grace(s: dict, name: str, *, actor: str = "") -> dict:
    out = call(s, "POST", f"{PREFIX}/{_name(name)}/end-grace", json.dumps({"actor": actor}).encode("utf-8"))
    log.info("platform key %s on %s: end grace (changed %s) by %s", name, s["slug"], out.get("changed"), actor)
    return out


def clear(s: dict, name: str, *, actor: str = "") -> dict:
    out = call(s, "POST", f"{PREFIX}/{_name(name)}/clear", json.dumps({"actor": actor}).encode("utf-8"))
    log.info("platform key %s on %s: clear (changed %s) by %s", name, s["slug"], out.get("changed"), actor)
    return out


# ---------------------------------------------------------------------------------------------
# the metadata form
# ---------------------------------------------------------------------------------------------

def _date(text: str, what: str) -> str:
    text = (text or "").strip()
    if not _DATE_RE.fullmatch(text):
        raise KeysError(f"{what}: pick a date.", "invalid_meta")
    try:
        date.fromisoformat(text)
    except ValueError:
        raise KeysError(f"{what}: {text} isn't a real date.", "invalid_meta") from None
    return text


def meta_from_form(expires_mode: str = "keep", expires_on: str = "", rotate_mode: str = "keep",
                   rotate_months: str = "", last_rotated: str = "") -> dict:
    """The metadata-only body from the page's form; only the fields the person changed (the reference client's
    meta_body). The store checks the rules again (422 invalid_meta)."""
    meta: dict = {}
    if expires_mode == "date":
        meta["expires_on"] = _date(expires_on, "Expires on")
    elif expires_mode == "none":
        meta["expires_on"] = None
    elif expires_mode == "default":
        meta["expires_on"] = "default"
    elif expires_mode not in ("keep", "", None):
        raise KeysError("Expiry: pick one of the choices.", "invalid_meta")
    if rotate_mode == "months":
        months = (rotate_months or "").strip()
        if not months.isdigit() or not 1 <= int(months) <= 120:
            raise KeysError("Rotate every: a number of months from 1 to 120.", "invalid_meta")
        meta["rotate_every_months"] = int(months)
    elif rotate_mode in ("never", "default"):
        meta["rotate_every_months"] = rotate_mode
    elif rotate_mode not in ("keep", "", None):
        raise KeysError("Rotate every: pick one of the choices.", "invalid_meta")
    if (last_rotated or "").strip():
        meta["last_rotated"] = _date(last_rotated, "Last rotated")
    return meta


# ---------------------------------------------------------------------------------------------
# what the page shows
# ---------------------------------------------------------------------------------------------

def _when(v) -> datetime | None:
    if not isinstance(v, str) or not v:
        return None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def state_label(k: dict) -> tuple[str, str]:
    """(word, chip classes) for whether a key is set, and where from. Never colour alone."""
    applies, source = k.get("applies"), k.get("source")
    if applies == "bootstrap":
        return "Bootstrap (.env)", "bg-charcoal/[.07] text-charcoal/70"
    if applies == "server":
        return "Held by its server", "bg-charcoal/[.07] text-charcoal/70"
    if applies == "managed":
        return "Renewed by the ERP", "bg-charcoal/[.07] text-charcoal/70"
    if source == "store":
        return "Set", "bg-status-ok/10 text-status-ok"
    if source == "env":
        return "In the server's .env only", "bg-status-warn/15 text-charcoal dark:text-status-warn"
    if k.get("via"):
        return f"Not set (reads {k['via']})", "bg-status-warn/15 text-charcoal dark:text-status-warn"
    return "Not set", "bg-status-high/15 text-status-high"


def view(doc: dict | None, now: datetime | None = None) -> dict:
    """The status document arranged for the page: keys by group (registry order), each with its derived words. The
    document has no values (the contract); nothing is added that could hold one."""
    now = now or datetime.now(timezone.utc)
    doc = doc if isinstance(doc, dict) else {}
    groups: dict[str, list[dict]] = {}
    due = []
    for k in doc.get("keys") or []:
        if not isinstance(k, dict) or not NAME_RE.fullmatch(str(k.get("name") or "")):
            continue
        row = dict(k)
        row["state_word"], row["state_classes"] = state_label(k)
        until = _when(k.get("grace_until"))
        row["grace_active"] = bool(until and until > now)
        row["grace_until_dt"] = until
        row["last_changed_dt"] = _when(k.get("last_changed"))
        row["stage_meta"] = STAGE_META.get(k.get("stage") or "")
        row["can_clear"] = bool(k.get("writable")) and k.get("source") == "store"
        groups.setdefault(str(k.get("group") or "Other"), []).append(row)
        if RANK.get(k.get("stage") or "", 0) > 0:
            due.append(row)
    due.sort(key=lambda r: (-RANK[r["stage"]], r.get("next_due") or "9999", r["name"]))
    servers = [dict(sv, last_fetch_dt=_when(sv.get("last_fetch_at"))) for sv in doc.get("servers") or []
               if isinstance(sv, dict)]
    return {"server": doc.get("server"), "store_version": doc.get("store_version"),
            "grace_hours": doc.get("grace_hours"), "groups": list(groups.items()), "due": due, "servers": servers,
            "count": sum(len(v) for v in groups.values())}


# ---------------------------------------------------------------------------------------------
# stage reminders (1143)
# ---------------------------------------------------------------------------------------------

def eventid(slug: str, name: str) -> int:
    """A stable positive BIGINT per (store, key), as drift's findings."""
    return int.from_bytes(hashlib.sha256(f"{slug}:{name}".encode()).digest()[:8], "big") & (2 ** 63 - 1)


def _source_row(slug: str) -> str:
    return f"{SOURCE}:{slug}"


def reminder_title(k: dict, server: str) -> str:
    name, stage, due, days = k["name"], k.get("stage"), k.get("next_due"), k.get("days_left")
    if stage == "overdue":
        when = "due today" if days == 0 else f"overdue since {due}" if due else "overdue"
    elif isinstance(days, int):
        when = f"due in {days} day{'s' if days != 1 else ''}" + (f" ({due})" if due else "")
    else:
        when = f"due {due}" if due else STAGE_META[stage]["label"].lower()
    return f"Platform key {name} on {server}: {when}"[:500]


def down_eventid(slug: str) -> int:
    """The fixed eventid of a store's "can't read the key store" alarm (no key name is this short)."""
    return eventid(slug, "")


# observe() runs from the reminder loop AND from page views: one store's compare-and-notify at a time, so two reads
# can't both see the same stage change and push twice. In-process lock + a per-store Postgres advisory lock (held to
# commit) for anything outside this process. sync() already holds _lock (not re-entrant), hence a lock of its own.
_stage_lock = threading.Lock()


def observe(s: dict, doc: dict, now: datetime | None = None) -> list[dict]:
    """Record a status read: keep it (no values in it) for the page to fall back on, clear the store's "can't read"
    alarm, compare each key's stage with the last one seen, keep the Health reminders in step, and return the keys
    whose stage went UP (notified here, once). `now` = when the GET that fetched `doc` STARTED (taken before the call),
    so reads that finish out of order are recognised: one older than the last recorded read changes nothing."""
    now = now or datetime.now(timezone.utc)
    slug, server = s["slug"], str(doc.get("server") or s.get("server") or s["slug"])
    keys = [k for k in doc.get("keys") or [] if isinstance(k, dict) and NAME_RE.fullmatch(str(k.get("name") or ""))]
    went_up: list[dict] = []
    with _stage_lock, get_cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"platform_keys:{slug}",))
        # Out of order: a read STARTED before the last one recorded (a slow poll finishing after a page view) is older
        # news. Recording it would lower a stage the newer read already announced, and the next read would announce
        # it again. Skip it whole.
        cur.execute("SELECT last_ok_at FROM health_sources WHERE source = %s", (_source_row(slug),))
        last = cur.fetchone()
        if last and last["last_ok_at"] and now < last["last_ok_at"]:
            log.info("platform keys on %s: skipped a read older than the last one recorded", slug)
            return []
        cur.execute("INSERT INTO health_sources (source) VALUES (%s) ON CONFLICT DO NOTHING", (_source_row(slug),))
        cur.execute("UPDATE health_sources SET snapshot = %s, last_sync_at = %s, last_ok_at = %s, last_error = NULL, "
                    "failing_since = NULL WHERE source = %s", (Json(doc), now, now, _source_row(slug)))
        cur.execute("UPDATE health_problems SET resolved_at = %s WHERE source = %s AND eventid = %s "
                    "AND resolved_at IS NULL", (now, SOURCE, down_eventid(slug)))
        cur.execute("SELECT name, stage FROM platform_key_stages WHERE store = %s", (slug,))
        prev = {r["name"]: r["stage"] for r in cur.fetchall()}
        cur.execute("SELECT eventid, resolved_at FROM health_problems WHERE source = %s", (SOURCE,))
        stored = {r["eventid"]: r for r in cur.fetchall()}
        seen = set()
        for k in keys:
            name = k["name"]
            stage = k.get("stage") if k.get("stage") in STAGES else "ok"
            seen.add(name)
            old = prev.get(name)
            cur.execute(
                "INSERT INTO platform_key_stages (store, name, stage, next_due, days_left, seen_at, changed_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (store, name) DO UPDATE SET stage = EXCLUDED.stage, "
                "next_due = EXCLUDED.next_due, days_left = EXCLUDED.days_left, seen_at = EXCLUDED.seen_at, "
                "changed_at = CASE WHEN platform_key_stages.stage = EXCLUDED.stage THEN platform_key_stages.changed_at "
                "ELSE EXCLUDED.changed_at END",
                (slug, name, stage, k.get("next_due") if _DATE_RE.fullmatch(str(k.get("next_due") or "")) else None,
                 k.get("days_left") if isinstance(k.get("days_left"), int) else None, now, now))
            if RANK[stage] > RANK.get(old or "ok", 0):
                went_up.append(dict(k, stage=stage, previous=old))
            eid = eventid(slug, name)
            row = stored.get(eid)
            if RANK[stage] > 0:
                title, level = reminder_title(dict(k, stage=stage), server), LEVEL[stage]
                if row is None or row["resolved_at"] is not None:
                    # pushed_at = now: this module sends the one notification itself; the estate sweep must not.
                    cur.execute(
                        "INSERT INTO health_problems (source, eventid, host, name, severity, started_at, baseline, "
                        "pushed_at) VALUES (%s, %s, %s, %s, %s, %s, false, %s) ON CONFLICT (source, eventid) DO UPDATE "
                        "SET host = EXCLUDED.host, name = EXCLUDED.name, severity = EXCLUDED.severity, started_at = "
                        "EXCLUDED.started_at, resolved_at = NULL, escalated_at = NULL, baseline = false, pushed_at = "
                        "EXCLUDED.pushed_at", (SOURCE, eid, server[:200], title, level, now, now))
                else:
                    cur.execute("UPDATE health_problems SET host = %s, name = %s, severity = %s, seen_at = %s, "
                                "pushed_at = GREATEST(pushed_at, started_at) WHERE source = %s AND eventid = %s",
                                (server[:200], title, level, now, SOURCE, eid))
            elif row is not None and row["resolved_at"] is None:
                cur.execute("UPDATE health_problems SET resolved_at = %s WHERE source = %s AND eventid = %s",
                            (now, SOURCE, eid))
        for name in set(prev) - seen:               # gone from the registry: forget it, withdraw its reminder
            cur.execute("DELETE FROM platform_key_stages WHERE store = %s AND name = %s", (slug, name))
            cur.execute("UPDATE health_problems SET resolved_at = %s WHERE source = %s AND eventid = %s "
                        "AND resolved_at IS NULL", (now, SOURCE, eventid(slug, name)))
    if went_up:
        _notify(s, server, went_up)
    return went_up


def _notify(s: dict, server: str, keys: list[dict]) -> dict:
    """ONE phone notification for everything that went up in one read, worst first. Never raises."""
    from app import push

    keys = sorted(keys, key=lambda k: (-RANK[k["stage"]], k.get("next_due") or "9999", k["name"]))
    if len(keys) == 1:
        title = f"{STAGE_META[keys[0]['stage']]['label']}: {keys[0]['name']}"
    else:
        title = f"{len(keys)} platform keys need rotating · {STAGE_META[keys[0]['stage']]['label'].lower()}"
    lines = [reminder_title(k, server).removeprefix("Platform key ") for k in keys[:push.MAX_LINES]]
    if len(keys) > push.MAX_LINES:
        lines.append(f"+{len(keys) - push.MAX_LINES} more")
    payload = {"title": title, "body": "\n".join(lines), "url": f"{config.PUBLIC_URL}{page_path(s['slug'])}",
               "tag": f"mdd-keys-{s['slug']}-{int(time.time() * 1000)}", "renotify": True}
    try:
        result = push.deliver(payload)
    except Exception as exc:  # noqa: BLE001 — the reminder is on Health already; a failed push mustn't undo it
        log.warning("platform keys: the stage notification failed (%s)", type(exc).__name__)
        return {"failed": 1}
    log.info("platform keys on %s: %d stage change(s) notified (%s)", s["slug"], len(keys),
             result.get("off") or push.summary_line(result))
    return result


def record_failure(s: dict, error: str, now: datetime | None = None, *, credential: bool = False) -> bool:
    """A read that failed: shown on the page and Health. Once reads have failed for DOWN_MINUTES (or at once when there
    is no usable credential), the store's "can't read" alarm opens: health_problems source 'keys', its fixed
    down_eventid, severity high, so the estate sweep pushes it (once) and home_slot counts it. The next good read
    (observe) clears it. Returns whether the alarm is open. The error never holds a value or the credential."""
    now = now or datetime.now(timezone.utc)
    slug, server = s["slug"], str(s.get("server") or s["slug"])
    with get_cursor() as cur:
        cur.execute("INSERT INTO health_sources (source) VALUES (%s) ON CONFLICT DO NOTHING", (_source_row(slug),))
        cur.execute("UPDATE health_sources SET last_sync_at = %s, last_error = %s, failing_since = "
                    "COALESCE(failing_since, %s) WHERE source = %s RETURNING failing_since",
                    (now, error[:300], now, _source_row(slug)))
        since = cur.fetchone()["failing_since"]
        if not credential and now - since < timedelta(minutes=DOWN_MINUTES):
            return False
        title = (f"Platform keys: no usable credential for {server}'s key store: {error}" if credential else
                 f"Platform keys: can't read {server}'s key store (failing since {since:%Y-%m-%d %H:%M} UTC): {error}")
        # A re-opened alarm gets a new started_at, so the sweep pushes it again; an open one only updates its words.
        cur.execute(
            "INSERT INTO health_problems (source, eventid, host, name, severity, started_at, baseline) "
            "VALUES (%s, %s, %s, %s, %s, %s, false) ON CONFLICT (source, eventid) DO UPDATE SET host = EXCLUDED.host, "
            "name = EXCLUDED.name, severity = EXCLUDED.severity, seen_at = EXCLUDED.started_at, "
            "started_at = CASE WHEN health_problems.resolved_at IS NULL THEN health_problems.started_at "
            "ELSE EXCLUDED.started_at END, escalated_at = CASE WHEN health_problems.resolved_at IS NULL "
            "THEN health_problems.escalated_at END, resolved_at = NULL, baseline = false",
            (SOURCE, down_eventid(slug), server[:200], title[:500], DOWN_LEVEL, now))
    return True


def fresh(slug: str, now: datetime | None = None) -> bool:
    """Is the Dashboard's own key reminder feed live for this store (a good read within FRESH_MINUTES)?"""
    if slug not in config.PLATFORM_KEY_STORES:
        return False
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT last_ok_at FROM health_sources WHERE source = %s", (_source_row(slug),))
        row = cur.fetchone()
    ok_at = row["last_ok_at"] if row else None
    return bool(ok_at) and (now or datetime.now(timezone.utc)) - ok_at < timedelta(minutes=FRESH_MINUTES)


_lock = threading.Lock()


def sync(now: datetime | None = None) -> dict:
    """One periodic read of every store with a credential. A run already going is skipped. Never raises: a failed read
    is recorded (the page shows it) and keeps the previous reminders ("couldn't look" is not "fixed")."""
    if not _lock.acquire(blocking=False):
        return {"ok": True, "busy": True}
    out: dict = {"ok": True, "stores": {}}
    try:
        for s in stores():
            now_s = now or datetime.now(timezone.utc)
            problem = credential_problem(s)
            if problem:
                record_failure(s, problem, now_s, credential=True)
                out["ok"] = False
                out["stores"][s["slug"]] = "no credential"
                continue
            try:
                went_up = observe(s, get_status(s), now_s)
                out["stores"][s["slug"]] = {"went_up": [k["name"] for k in went_up]}
            except KeysError as exc:
                record_failure(s, str(exc), now_s)
                out["ok"] = False
                out["stores"][s["slug"]] = exc.code
            except Exception as exc:  # noqa: BLE001
                log.exception("platform keys sync on %s", s["slug"])
                record_failure(s, type(exc).__name__, now_s)
                out["ok"] = False
                out["stores"][s["slug"]] = "error"
        return out
    finally:
        _lock.release()


def last_snapshot(slug: str) -> tuple[dict | None, datetime | None, str]:
    """(the last status document read, when, the last error) for the page when a live read fails."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT snapshot, last_ok_at, last_error FROM health_sources WHERE source = %s",
                    (_source_row(slug),))
        row = cur.fetchone()
    if not row:
        return None, None, ""
    snap = row["snapshot"] if isinstance(row["snapshot"], dict) else None
    return snap, row["last_ok_at"], row["last_error"] or ""


def reminders(slug: str | None = None) -> dict:
    """The Health page's section: the open key reminders, worst first, and each store's last read."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT * FROM health_problems WHERE source = %s AND resolved_at IS NULL "
                    "ORDER BY severity DESC, started_at", (SOURCE,))
        open_rows = cur.fetchall()
    reads = []
    for s in stores():
        if slug and s["slug"] != slug:
            continue
        _snap, ok_at, error = last_snapshot(s["slug"])
        reads.append({"slug": s["slug"], "name": s["name"], "server": s.get("server"), "href": page_path(s["slug"]),
                      "last_ok_at": ok_at, "error": error,
                      "credential": credential_problem(s)})
    down = {down_eventid(s["slug"]) for s in stores()}
    return {"open": open_rows, "stores": reads, "poll_seconds": config.PLATFORM_KEYS_POLL_SECONDS, "down_ids": down}
