"""CA notifications and reminders (task #702, v1). No AI runs here.

* In-app notices (ca_notices): a toast plus the drawer's badge, over the CA bus (SSE) and kept in the table until read. Each is
  also a Web Push (app/push.py) to the subscribed phones. Note: push subscriptions are per installation, not yet per user,
  so with several people on one server every subscribed device hears every notice (multi-user, #713, scopes it).
* "An agent finished": notify_job_finished() when a job ends done or failed. The scheduler also sweeps recently finished jobs,
  so one that failed by a lapsed lease (no hook on that path) is told too. A notice's `ref` (job:<id>) de-dupes: one per job.
* Reminders (ca_reminders): the person asks CA ("remind me at 4pm to ..."), the model calls create_reminder, and the scheduler
  fires them. If the server was down at the time, the first pass after it starts sends them with "(missed while offline)".
  A repeating reminder fires once however many occurrences were missed, then moves to its next FUTURE time.
* Scheduler: ONE daemon thread started with the app (CREWBOSS_CA_TICK_SECONDS, default 20s; 0 = off). Each pass fires due
  reminders, tells finished jobs, and fails dead chat turns (ca.expire_turns). Every pass is guarded: it never dies."""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app import ca, ca_files, config
from app.db import get_cursor
from app.repo import BadRequest, NotFound

log = logging.getLogger("crewboss.ca")

REPEATS = ("daily", "weekdays", "weekly")
LATE_GRACE_SECONDS = 120      # fired later than this after its time = "missed while offline"
MAX_PENDING = 100
JOB_SWEEP_MINUTES = 10


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(config.CA_TIMEZONE)
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")


# --- notices ---------------------------------------------------------------------------------------------------------

def _send_push(payload: dict) -> None:
    # TODO(#713): push.deliver sends to EVERY subscription of the install; multi-user must send only to this person's devices.
    """One Web Push, in a thread (delivery can take seconds). Never raises."""
    def go() -> None:
        try:
            from app import push
            push.deliver(payload)
        except Exception:  # noqa: BLE001 — a push problem must never fail the notice
            log.exception("ca push")
    threading.Thread(target=go, name="ca-push", daemon=True).start()


def _notice_json(n: dict) -> dict:
    return {"id": n["id"], "kind": n["kind"], "title": n["title"], "body": n["body"], "url": n["url"],
            "created_at": n["created_at"].isoformat() if n.get("created_at") else None}


def notify(user_id: int, kind: str, title: str, body: str = "", url: str = "", ref: str = "") -> dict | None:
    """Record a notice and tell the person: SSE (toast + badge) and a Web Push. A repeat of the same (user, kind, ref) is dropped
    (returns None), so a job or a reminder occurrence is told once."""
    with get_cursor() as cur:
        cur.execute("INSERT INTO ca_notices (user_id, kind, ref, title, body, url) VALUES (%s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT DO NOTHING RETURNING *", (user_id, kind, ref, title[:200], body[:400], url[:300]))
        n = cur.fetchone()
    if not n:
        return None
    announce(n)
    return dict(n)


def announce(n: dict) -> None:
    """Tell the person about a notice row that was just inserted (SSE toast and a Web Push). Never raises into the caller's work."""
    ca.publish(n["user_id"], "notice", _notice_json(n))
    _send_push({"title": n["title"], "body": n["body"], "url": config.PUBLIC_URL + (n["url"] or "/"), "tag": f"crewboss-ca-{n['id']}"})


def list_notices(user_id: int, limit: int = 20) -> dict:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT * FROM ca_notices WHERE user_id = %s ORDER BY id DESC LIMIT %s", (user_id, limit))
        rows = [_notice_json(r) | {"unread": r["read_at"] is None} for r in cur.fetchall()]
        cur.execute("SELECT count(*) AS n FROM ca_notices WHERE user_id = %s AND read_at IS NULL", (user_id,))
        unread = cur.fetchone()["n"]
    return {"unread": unread, "notices": rows}


def mark_read(user_id: int) -> int:
    with get_cursor() as cur:
        cur.execute("UPDATE ca_notices SET read_at = now() WHERE user_id = %s AND read_at IS NULL", (user_id,))
        return cur.rowcount


def notify_job_finished(job_id: int) -> dict | None:
    """"Task #N done: <title>" for the person who started the job (idempotent). Cancelled jobs say nothing: the person did it."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT j.id, j.task_id, j.state, j.user_id, j.summary, t.title, p.slug FROM jobs j JOIN tasks t ON t.id = j.task_id "
                    "JOIN projects p ON p.id = t.project_id WHERE j.id = %s AND j.state IN ('done', 'failed')", (job_id,))
        j = cur.fetchone()
    if not j:
        return None
    word = "done" if j["state"] == "done" else "failed"
    return notify(j["user_id"], "job", f"Task #{j['task_id']} {word}: {j['title'][:90]}", (j["summary"] or "")[:200],
                  f"/{j['slug']}/board", ref=f"job:{j['id']}")


def sweep_finished_jobs() -> int:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT id FROM jobs WHERE state IN ('done', 'failed') AND finished_at > now() - make_interval(mins => %s) "
                    "ORDER BY id", (JOB_SWEEP_MINUTES,))
        ids = [r["id"] for r in cur.fetchall()]
    return sum(1 for i in ids if notify_job_finished(i))


# --- reminders -------------------------------------------------------------------------------------------------------

def create_reminder(user_id: int, text: str, due_at: str, repeat: str | None = None) -> dict:
    text = " ".join((text or "").split())[:500]
    if not text:
        raise BadRequest("a reminder needs text")
    if repeat in ("", "none", "once"):
        repeat = None
    if repeat is not None and repeat not in REPEATS:
        raise BadRequest(f"repeat must be one of {', '.join(REPEATS)} (or leave it out for once)")
    try:
        due = datetime.fromisoformat(str(due_at).strip().replace("Z", "+00:00"))
    except ValueError:
        raise BadRequest("due_at must be an ISO 8601 time with a UTC offset, like 2026-11-09T16:00:00+11:00") from None
    if due.tzinfo is None:
        due = due.replace(tzinfo=_tz())
    now = datetime.now(timezone.utc)
    if due < now - timedelta(minutes=1):
        raise BadRequest("that time has already passed")
    if due > now + timedelta(days=366 * 3):
        raise BadRequest("that is too far ahead")
    with get_cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM ca_reminders WHERE user_id = %s AND cancelled_at IS NULL AND (repeat IS NOT NULL OR sent_at IS NULL)",
                    (user_id,))
        if cur.fetchone()["n"] >= MAX_PENDING:
            raise BadRequest(f"at most {MAX_PENDING} pending reminders")
        cur.execute("INSERT INTO ca_reminders (user_id, text, due_at, repeat) VALUES (%s, %s, %s, %s) RETURNING *",
                    (user_id, text, due.astimezone(timezone.utc), repeat))
        return _rem_json(cur.fetchone())


def _rem_json(r: dict) -> dict:
    return {"id": r["id"], "text": r["text"], "due_at": r["due_at"].astimezone(_tz()).isoformat(), "repeat": r["repeat"]}


def list_reminders(user_id: int) -> list[dict]:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT * FROM ca_reminders WHERE user_id = %s AND cancelled_at IS NULL AND (repeat IS NOT NULL OR sent_at IS NULL) "
                    "ORDER BY due_at LIMIT 100", (user_id,))
        return [_rem_json(r) for r in cur.fetchall()]


def cancel_reminder(user_id: int, reminder_id: int) -> dict:
    with get_cursor() as cur:
        cur.execute("UPDATE ca_reminders SET cancelled_at = now() WHERE id = %s AND user_id = %s AND cancelled_at IS NULL RETURNING id",
                    (reminder_id, user_id))
        if not cur.fetchone():
            raise NotFound(f"reminder {reminder_id} not found")
    return {"cancelled": reminder_id}


def next_due(due: datetime, repeat: str, after: datetime) -> datetime:
    """The first occurrence of a repeating reminder strictly after `after`, on the same local wall-clock time (so a daylight-saving
    change doesn't move it)."""
    tz = _tz()
    t = due.astimezone(tz)
    after = after.astimezone(tz)
    for _ in range(4000):
        t = t + timedelta(days=7 if repeat == "weekly" else 1)
        if repeat == "weekdays" and t.weekday() >= 5:
            continue
        if t > after:
            return t.astimezone(timezone.utc)
    raise BadRequest("no next time")


def fire_due_reminders() -> int:
    """Send every due reminder (time on the DATABASE clock). A one-off is marked sent; a repeating one moves to its next future time.
    One notice per reminder per pass, however many occurrences were missed. Returns how many were sent."""
    sent: list[tuple[int, int, str, datetime, bool]] = []
    with get_cursor() as cur:
        cur.execute("SELECT r.*, now() AS db_now, (now() - r.due_at > make_interval(secs => %s)) AS late FROM ca_reminders r "
                    "WHERE r.cancelled_at IS NULL AND r.due_at <= now() AND (r.repeat IS NOT NULL OR r.sent_at IS NULL) "
                    "ORDER BY r.due_at FOR UPDATE SKIP LOCKED", (LATE_GRACE_SECONDS,))
        for r in cur.fetchall():
            if r["repeat"]:
                cur.execute("UPDATE ca_reminders SET sent_at = now(), due_at = %s WHERE id = %s",
                            (next_due(r["due_at"], r["repeat"], r["db_now"]), r["id"]))
            else:
                cur.execute("UPDATE ca_reminders SET sent_at = now() WHERE id = %s", (r["id"],))
            sent.append((r["user_id"], r["id"], r["text"], r["due_at"], r["late"]))
    for user_id, rid, text, due, late in sent:
        notify(user_id, "reminder", "Reminder", text + (" (missed while offline)" if late else ""), "/",
               ref=f"rem:{rid}:{int(due.timestamp())}")
    return len(sent)


# --- the scheduler thread --------------------------------------------------------------------------------------------

def tick() -> dict:
    """One scheduler pass. Each part is guarded on its own, so one failing never stops the others."""
    out = {"reminders": 0, "jobs": 0, "failed_turns": 0, "stripped_uploads": 0}
    for key, fn in (("reminders", fire_due_reminders), ("jobs", sweep_finished_jobs),
                    ("failed_turns", lambda: ca.expire_turns()["failed"]),
                    ("stripped_uploads", lambda: sum(ca_files.sweep().values()))):   # #727: uploads are temporary
        try:
            out[key] = fn()
        except Exception:  # noqa: BLE001
            log.exception("ca scheduler: %s", key)
    return out


class Scheduler:
    """The CA's server-side scheduler thread. start() runs a first pass at once (that is what sends reminders missed while the
    server was down), then one every `every` seconds until stop()."""

    def __init__(self, every: float | None = None):
        self.every = config.CA_TICK_SECONDS if every is None else every
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        if self.every <= 0 or self._thread is not None:
            return False
        self._thread = threading.Thread(target=self._run, name="ca-scheduler", daemon=True)
        self._thread.start()
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            tick()
            self._stop.wait(self.every)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
