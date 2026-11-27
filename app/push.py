"""Phone notifications (#487, owner 2026-10-19: "we shouldn't get any alerts on the owner's own servers specifically. From
within ERP... I'll be installing dash dashboard as a PWA app on my phone and that's how I'd be getting notifications on
internal device failures"). Web Push (VAPID) to every subscribed device, for estate problems: health_problems, every
source (zabbix, erp, drift, reports, ...) on Zabbix's 0-5 severity scale.

THE RULE (q292), all in due_problems():
- High (4) and Disaster (5) push at once; Average (3) only once it is still open PUSH_AVERAGE_MINUTES after started_at;
  Warning and below never. Every source by its severity (drift warnings are 2, so they never push).
- Never a baseline row (open at a source's first sync) or anything already resolved; problems already open when push
  arrived count as pushed (db/migrations/0028_web_push.sql).
- Each problem pushes ONCE (health_problems.pushed_at, swept for: the dispatcher never relies on a NOTIFY). A problem
  that comes back (a new Zabbix event on the same trigger, a drift finding re-opened) is a new occurrence, but its key
  (source + trigger, else eventid) pushes again only PUSH_REPEAT_MINUTES after the last push, and only if still open
  then: a flapping trigger is at most one push an hour, never a stream.
- GROUPING: everything due in one sweep is ONE notification ("3 estate problems ..."). A problem that becomes due within
  PUSH_GROUP_SECONDS of a group's first push joins that group: the group's notification is re-sent under the SAME tag
  with renotify off, so the phone updates the one notification in place without buzzing again (if the owner already
  swiped it away it shows again, still one buzz at most). EXCEPT a joiner at High or above that is worse than everything
  already in the group (an Average group, then a High): that re-send buzzes (renotify). A new group gets its own unique
  tag + renotify, so it always alerts (the ERP's lesson: a repeated tag silently replaces).
- When every problem of a group has cleared: ONE quiet "resolved" notification under the group's tag (renotify off,
  silent), so the problem notification turns into the all-clear. PUSH_RESOLVED=0 switches that off.
- Tapping any of them opens the Health page.

SENDING (the ERP's hard-won lessons, app/clients/webpush.py there): the VAPID key is passed as a py_vapid Vapid02 OBJECT
(pywebpush 2.x misreads a PEM string), ttl=86400 (TTL 0 makes FCM drop a push to a sleeping phone), timeout=10, and a
404/410 deletes the subscription. No key file = push is off, shown in the avatar menu; never a crash.

Run on the host to make the key (once, before `docker compose up`): python -m app.push keygen"""
from __future__ import annotations

import base64
import ipaddress
import json
import logging
import os
import re
import stat
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from psycopg.types.json import Json

from app import config, roles
from app.db import get_cursor
from app.health import SEVERITY

log = logging.getLogger("crewboss.push")

HIGH, AVERAGE = 4, 3
TTL = 86400          # FCM holds a push for a sleeping phone this long; 0 = silently dropped
TIMEOUT = 10         # a hung push service never wedges the sweep
MAX_LINES = 5        # problem lines in one notification's body; the rest are "+N more"
B64URL_RE = re.compile(r"^[A-Za-z0-9_-]+={0,2}$")


# ---------------------------------------------------------------------------------------------
# the VAPID key
# ---------------------------------------------------------------------------------------------

_key_cache: tuple | None = None  # ((path, mtime_ns, size), Vapid02)


def _load_vapid():
    """(Vapid02, None) or (None, why push is off). Read at use time, so dropping the key in needs no restart."""
    global _key_cache
    path = config.VAPID_KEY_FILE
    try:
        st = os.stat(path)
        if not stat.S_ISREG(st.st_mode):  # docker made a directory: the file wasn't there at `compose up`
            return None, "the VAPID key path on the server is not a file"
        sig = (path, st.st_mtime_ns, st.st_size)
        if _key_cache and _key_cache[0] == sig:
            return _key_cache[1], None
        with open(path, "rb") as fh:
            pem = fh.read()
    except OSError:
        return None, "no VAPID key on the server"
    if not pem.strip():
        return None, "the VAPID key file on the server is empty"
    try:
        from py_vapid import Vapid02

        vapid = Vapid02.from_pem(pem)  # the OBJECT, never the PEM string (pywebpush 2.x can't parse that)
    except Exception as exc:  # noqa: BLE001 — a bad key is "off", never a crash
        log.warning("VAPID key unreadable: %s", exc.__class__.__name__)
        return None, "the VAPID key file on the server is not a usable key"
    _key_cache = (sig, vapid)
    return vapid, None


def public_key() -> str | None:
    """The applicationServerKey a browser subscribes with: the raw P-256 point, base64url, no padding."""
    vapid, _ = _load_vapid()
    if vapid is None:
        return None
    from cryptography.hazmat.primitives import serialization

    raw = vapid.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def status(endpoint: str | None = None) -> dict:
    """What the avatar menu shows: is push on (and why not), how many devices are subscribed, and (given the browser's
    endpoint) whether THIS device is one of them: a subscription the server pruned is still held by the browser."""
    vapid, reason = _load_vapid()
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT count(*) AS n, count(*) FILTER (WHERE endpoint = %s) AS here FROM push_subscriptions",
                    (endpoint or "",))
        row = cur.fetchone()
    return {"enabled": vapid is not None, "reason": reason or "", "devices": row["n"], "this_device": row["here"] > 0}


# ---------------------------------------------------------------------------------------------
# subscriptions
# ---------------------------------------------------------------------------------------------

def valid_endpoint(url) -> bool:
    """The dispatcher later POSTs to a stored endpoint blindly, so a hostile one would be a blind SSRF into the mesh
    (the ERP's REVIEW P2). Real push services are public https DNS names (fcm.googleapis.com, *.push.services.mozilla
    .com, ...): require https + a dotted name that is not an IP literal, localhost or a private suffix."""
    if not isinstance(url, str) or len(url) > 2048:
        return False
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    if parts.scheme != "https" or "." not in host:
        return False
    if host == "localhost" or host.endswith((".local", ".internal", ".lan", ".home.arpa", ".localhost")):
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        return True


def subscribe(sub, user_agent: str = "") -> int:
    """Store a browser's PushSubscription JSON ({endpoint, keys: {p256dh, auth}}); the same endpoint again updates its
    keys. Returns the number of subscribed devices. ValueError on anything malformed."""
    sub = sub if isinstance(sub, dict) else {}
    keys = sub.get("keys") if isinstance(sub.get("keys"), dict) else {}
    endpoint, p256dh, auth = sub.get("endpoint"), keys.get("p256dh"), keys.get("auth")
    if not valid_endpoint(endpoint):
        raise ValueError("not a push service endpoint")
    for k in (p256dh, auth):
        if not isinstance(k, str) or not (8 <= len(k) <= 256) or not B64URL_RE.match(k):
            raise ValueError("bad subscription keys")
    with get_cursor() as cur:
        cur.execute("INSERT INTO push_subscriptions (endpoint, p256dh, auth, user_agent) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (endpoint) DO UPDATE SET p256dh = EXCLUDED.p256dh, auth = EXCLUDED.auth, "
                    "user_agent = EXCLUDED.user_agent, last_error = NULL",
                    (endpoint, p256dh, auth, (user_agent or "")[:300]))
        cur.execute("SELECT count(*) AS n FROM push_subscriptions")
        return cur.fetchone()["n"]


def unsubscribe(endpoint) -> bool:
    if not isinstance(endpoint, str) or not endpoint:
        return False
    with get_cursor() as cur:
        cur.execute("DELETE FROM push_subscriptions WHERE endpoint = %s", (endpoint,))
        return cur.rowcount > 0


# ---------------------------------------------------------------------------------------------
# sending
# ---------------------------------------------------------------------------------------------

def _send(vapid, sub: dict, payload: dict) -> tuple[str, str | None]:
    """One push -> ('ok' | 'gone' | 'failed', error). Never raises."""
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        return "failed", "pywebpush is not installed"
    try:
        webpush(subscription_info={"endpoint": sub["endpoint"], "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}},
                data=json.dumps(payload),
                vapid_private_key=vapid,
                vapid_claims={"sub": config.VAPID_SUB},  # a fresh dict each call: pywebpush adds aud/exp to it
                ttl=TTL, timeout=TIMEOUT)
        return "ok", None
    except WebPushException as exc:
        code = getattr(getattr(exc, "response", None), "status_code", None)
        if code in (404, 410):
            return "gone", str(code)
        return "failed", (f"HTTP {code}: " if code else "") + (str(exc)[:250] or "push refused")
    except Exception as exc:  # noqa: BLE001 — one bad subscription never stops the rest
        return "failed", str(exc)[:250] or exc.__class__.__name__


def deliver(payload: dict) -> dict:
    """Send one notification to every subscribed device. Records last_ok_at / last_error per subscription and deletes
    the ones the push service says are gone. -> {sent, failed, pruned} (+ off: why, when push is off)."""
    out = {"sent": 0, "failed": 0, "pruned": 0}
    vapid, reason = _load_vapid()
    if vapid is None:
        return {**out, "off": reason}
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT id, endpoint, p256dh, auth FROM push_subscriptions ORDER BY id")
        subs = cur.fetchall()
    for sub in subs:
        result, error = _send(vapid, sub, payload)
        with get_cursor() as cur:
            if result == "ok":
                out["sent"] += 1
                cur.execute("UPDATE push_subscriptions SET last_ok_at = now(), last_error = NULL WHERE id = %s",
                            (sub["id"],))
            elif result == "gone":
                out["pruned"] += 1
                cur.execute("DELETE FROM push_subscriptions WHERE id = %s", (sub["id"],))
            else:
                out["failed"] += 1
                log.warning("web push to subscription %s failed: %s", sub["id"], error)
                cur.execute("UPDATE push_subscriptions SET last_error = %s WHERE id = %s", (error, sub["id"]))
    return out


def summary_line(r: dict) -> str:
    return f"sent {r['sent']}, failed {r['failed']}, pruned {r['pruned']}"


def health_url() -> str:
    """ABSOLUTE (the ERP's lesson): a service worker resolves a relative url against its own origin, and one registered
    on a retired hostname would keep opening it. The payload is the only thing that reaches such a worker."""
    return f"{config.PUBLIC_URL}/{roles.slug('health')}/health"


def test_payload() -> dict:
    return {"title": "Crewboss test", "body": "Phone notifications work. Tap to open Health.", "url": health_url(),
            "tag": f"mdd-test-{int(time.time() * 1000)}", "renotify": True}


# ---------------------------------------------------------------------------------------------
# the rule + the sweep
# ---------------------------------------------------------------------------------------------

def _key(row: dict) -> tuple[str, int]:
    """A problem's identity across occurrences: Zabbix -> its trigger (objectid); every other source -> its eventid
    (drift / reports / erp ids are already stable per finding)."""
    return row["source"], int(row["objectid"] or row["eventid"])


def due_problems(now: datetime | None = None) -> list[dict]:
    """THE RULE (q292): open, not baseline, this occurrence not yet pushed, High/Disaster at once or Average open for
    PUSH_AVERAGE_MINUTES; minus a key pushed in the last PUSH_REPEAT_MINUTES. Worst first."""
    now = now or datetime.now(timezone.utc)
    with get_cursor(commit=False) as cur:
        cur.execute(
            "SELECT * FROM health_problems WHERE resolved_at IS NULL AND NOT baseline "
            "AND (severity >= %s OR (severity = %s AND started_at <= %s)) "
            "AND (pushed_at IS NULL OR pushed_at < started_at) "  # a re-opened finding resets started_at
            "ORDER BY severity DESC, started_at, source, eventid",
            (HIGH, AVERAGE, now - timedelta(minutes=config.PUSH_AVERAGE_MINUTES)))
        rows = cur.fetchall()
        if not rows:
            return []
        cur.execute("SELECT source, objectid, eventid FROM health_problems WHERE pushed_at > %s",
                    (now - timedelta(minutes=config.PUSH_REPEAT_MINUTES),))
        recent = {_key(r) for r in cur.fetchall()}
    due, seen = [], set()
    for r in rows:
        k = _key(r)
        if k in recent or k in seen:
            continue
        seen.add(k)
        due.append(r)
    return due


def _member(row: dict) -> dict:
    return {"source": row["source"], "eventid": int(row["eventid"]), "severity": int(row["severity"]),
            "host": row["host"] or row["source"], "name": row["name"], "started_at": row["started_at"].isoformat()}


def _lines(members: list[dict]) -> str:
    lines = [f"{m['host']} · {m['name']}" for m in members[:MAX_LINES]]
    if len(members) > MAX_LINES:
        lines.append(f"+{len(members) - MAX_LINES} more")
    return "\n".join(lines)


def _tag(group_id: int) -> str:
    return f"mdd-estate-{group_id}"


def problem_payload(group_id: int, members: list[dict], alert: bool) -> dict:
    if len(members) == 1:
        m = members[0]
        title, body = f"{SEVERITY.get(m['severity'], m['severity'])}: {m['host']}", m["name"]
    else:
        worst = max(m["severity"] for m in members)
        title, body = f"{len(members)} estate problems · {SEVERITY.get(worst, worst)}", _lines(members)
    # A new group (or a worse joiner) alerts; any other join re-sends the notification in place, quietly (module doc).
    return {"title": title, "body": body, "url": health_url(), "tag": _tag(group_id), "renotify": alert}


def resolved_payload(group_id: int, members: list[dict]) -> dict:
    n = len(members)
    title = f"Resolved: {members[0]['host']}" if n == 1 else f"Resolved: {n} estate problems"
    return {"title": title, "body": _lines(members), "url": health_url(), "tag": _tag(group_id),
            "renotify": False, "silent": True}


def _cleared_groups() -> list[dict]:
    """Groups whose problems have ALL cleared: resolved, gone, or replaced by a later occurrence (another group's)."""
    out = []
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT * FROM estate_pushes WHERE resolved_sent_at IS NULL ORDER BY id")
        for g in cur.fetchall():
            members = g["members"] or []
            if not members:
                continue
            cur.execute("SELECT p.source, p.eventid, p.started_at, p.resolved_at FROM health_problems p "
                        "JOIN unnest(%s::text[], %s::bigint[]) AS m(source, eventid) USING (source, eventid)",
                        ([m["source"] for m in members], [m["eventid"] for m in members]))
            rows = {(r["source"], r["eventid"]): r for r in cur.fetchall()}
            still_open = False
            for m in members:
                r = rows.get((m["source"], m["eventid"]))
                if r and r["resolved_at"] is None and r["started_at"] <= datetime.fromisoformat(m["started_at"]):
                    still_open = True
                    break
            if not still_open:
                out.append(g)
    return out


_lock = threading.Lock()


def sweep(now: datetime | None = None) -> dict:
    """One pass: the quiet all-clears, then the due problems as one notification. Does nothing (and marks nothing)
    while push is off or no device is subscribed, so the first phone to subscribe gets what is due then. A sweep
    already running is skipped. Never raises."""
    if not _lock.acquire(blocking=False):
        return {"ok": True, "busy": True}
    try:
        return _sweep_once(now or datetime.now(timezone.utc))
    except Exception as exc:  # noqa: BLE001 — the loop must live on
        log.exception("push sweep")
        return {"ok": False, "error": str(exc)[:300]}
    finally:
        _lock.release()


def _sweep_once(now: datetime) -> dict:
    vapid, reason = _load_vapid()
    if vapid is None:
        return {"ok": True, "off": reason}
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT count(*) AS n FROM push_subscriptions")
        if not cur.fetchone()["n"]:
            return {"ok": True, "off": "no device subscribed"}
    out = {"ok": True, "pushed": 0, "resolved": 0}

    if config.PUSH_RESOLVED:
        for g in _cleared_groups():
            r = deliver(resolved_payload(g["id"], g["members"]))
            with get_cursor() as cur:
                cur.execute("UPDATE estate_pushes SET resolved_sent_at = %s WHERE id = %s", (now, g["id"]))
            out["resolved"] += 1
            log.info("estate push %s resolved: %s", g["id"], summary_line(r))

    due = due_problems(now)
    if not due:
        return out
    new = [_member(r) for r in due]
    with get_cursor() as cur:
        cur.execute("SELECT * FROM estate_pushes WHERE resolved_sent_at IS NULL AND opened_at > %s "
                    "ORDER BY id DESC LIMIT 1", (now - timedelta(seconds=config.PUSH_GROUP_SECONDS),))
        group = cur.fetchone()
        if group is None:
            cur.execute("INSERT INTO estate_pushes (opened_at, sent_at, members) VALUES (%s, %s, %s) RETURNING *",
                        (now, now, Json([])))
            group = cur.fetchone()
            first = True
        else:
            first = False
    old = group["members"] or []
    members = old + new
    # A joiner WORSE than anything already in the group, at High or above, buzzes again (an Average group turning
    # High must reach the owner); same-or-lower joiners update the notification quietly.
    worse = bool(old) and max(m["severity"] for m in new) >= HIGH and \
        max(m["severity"] for m in new) > max(m["severity"] for m in old)
    r = deliver(problem_payload(group["id"], members, first or worse))
    # Marked whatever the delivery said: a failure is on the subscription's last_error, and the push service itself
    # queues for a sleeping phone (TTL); re-sending every sweep would be the nagging the rule forbids.
    with get_cursor() as cur:
        cur.execute("UPDATE estate_pushes SET members = %s, sent_at = %s, result = %s WHERE id = %s",
                    (Json(members), now, summary_line(r), group["id"]))
        for row in due:
            cur.execute("UPDATE health_problems SET pushed_at = %s WHERE source = %s AND eventid = %s",
                        (now, row["source"], row["eventid"]))
    out["pushed"] = len(due)
    out["group"] = group["id"]
    log.info("estate push %s (%s): %d problem(s), %s", group["id"], "new" if first else "joined", len(due),
             summary_line(r))
    return out


# ---------------------------------------------------------------------------------------------
# key generation (run on the HOST, before `docker compose up`)
# ---------------------------------------------------------------------------------------------

DEFAULT_HOST_KEY = os.path.join(os.environ.get("CREWBOSS_SECRETS_DIR") or os.path.expanduser("~/.config/crewboss"), "vapid-private.pem")


def keygen(path: str = DEFAULT_HOST_KEY) -> tuple[bool, str]:
    """Write a new P-256 VAPID private key (PKCS#8 PEM, 0600) at `path` if there is none. -> (created, public key).
    An existing key is never replaced: a new key orphans every subscription (each phone must Enable again)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    if os.path.isdir(path):
        raise SystemExit(f"{path} is a directory (docker created it because the key was missing at `compose up`): "
                         f"remove it (sudo rmdir) and run this again")
    if os.path.exists(path):
        with open(path, "rb") as fh:
            key = serialization.load_pem_private_key(fh.read(), password=None)
        created = False
    else:
        os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(pem)
        created = True
    raw = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return created, base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3) or sys.argv[1] != "keygen":
        print(f"usage: python -m app.push keygen [path]   (default {DEFAULT_HOST_KEY})", file=sys.stderr)
        sys.exit(2)
    target = sys.argv[2] if len(sys.argv) == 3 else DEFAULT_HOST_KEY
    made, pub = keygen(target)
    print(f"{'created' if made else 'kept the existing key'}: {target}\npublic key: {pub}")
