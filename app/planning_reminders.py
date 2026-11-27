"""Planning reminders (#915): ONE push per task when its start_by day arrives, ONE when its waiting_until passes.

THE RULE
- start: a task still backlog/ready whose start_by is today or earlier ("Start today: <title> - to make <milestone> (<date>)").
- chase: an open task with waiting_on whose waiting_until is before today ("Chase: <title> - waiting on <who>, expected back <date>").
- Never repeated: the date a push was sent for is stored on the task (start_reminded_for / chase_reminded_for) and a task is due only
  while that differs from its date. Changing the date (app/planning.apply) clears it, which re-arms the reminder.
- NEVER INUNDATE: everything due in one pass is ONE push. One item = its own line as the title; several = "N planning reminders"
  with up to MAX_LINES lines and "+N more". Push goes through app/push.py (like the estate problems: to every subscribed device;
  the install has one owner, and #713 will scope it per person).
- Quiet hours: nothing goes out before config.PLANNING_REMINDER_HOUR (local, default 07:00). A pass is cheap and idempotent, so the
  loop (main._planning_reminders) just runs it every PLANNING_REMINDER_SECONDS.
- Like push.sweep: while push is off or no device is subscribed NOTHING is marked, so the first phone to subscribe gets what is due."""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from app import config, planning, push
from app.db import get_cursor

log = logging.getLogger("crewboss.planning")

MAX_LINES = 5
_lock = threading.Lock()

_SELECT = ("SELECT t.id, t.title, t.status, t.start_by, t.waiting_on, t.waiting_for, t.waiting_until, p.slug AS project, "
           "m.name AS milestone_name, COALESCE(t.due_on, m.due_on) AS due_effective "
           "FROM tasks t JOIN projects p ON p.id = t.project_id AND p.archived_at IS NULL "
           "LEFT JOIN milestones m ON m.id = t.milestone_id WHERE t.priority <> 'wont' AND ")


def due(on) -> dict:
    """{'start': [...], 'chase': [...]} task rows due a reminder on day `on`, oldest date first."""
    with get_cursor(commit=False) as cur:
        cur.execute(_SELECT + "t.status IN ('backlog', 'ready') AND t.start_by IS NOT NULL AND t.start_by <= %s "
                    "AND t.start_reminded_for IS DISTINCT FROM t.start_by ORDER BY t.start_by, t.id", (on,))
        start = cur.fetchall()
        cur.execute(_SELECT + "t.status <> 'done' AND t.waiting_on IS NOT NULL AND t.waiting_until IS NOT NULL "
                    "AND t.waiting_until < %s AND t.chase_reminded_for IS DISTINCT FROM t.waiting_until "
                    "ORDER BY t.waiting_until, t.id", (on,))
        chase = cur.fetchall()
    return {"start": start, "chase": chase}


def _day(d) -> str:
    return f"{d.day} {d.strftime('%b')}"


def line(kind: str, t: dict) -> str:
    if kind == "start":
        goal = f"to make {t['milestone_name']} ({_day(t['due_effective'])})" if t["milestone_name"] and t["due_effective"] \
            else f"due {_day(t['due_effective'])}" if t["due_effective"] else ""
        return f"Start today: {t['title']}" + (f" — {goal}" if goal else "")
    why = f" for {t['waiting_for']}" if t["waiting_for"] else ""
    return f"Chase: {t['title']} — waiting on {t['waiting_on']}{why}, expected back {_day(t['waiting_until'])}"


def payload(items: list[tuple[str, dict]], on) -> dict:
    """One notification for everything due: a single item reads as itself, several as a digest."""
    url = config.PUBLIC_URL + "/"
    if len(items) == 1:
        kind, t = items[0]
        return {"title": line(kind, t), "body": "", "url": f"{config.PUBLIC_URL}/{t['project']}/tasks/{t['id']}",
                "tag": f"crewboss-planning-{on.isoformat()}", "renotify": True}
    lines = [line(k, t) for k, t in items[:MAX_LINES]]
    if len(items) > MAX_LINES:
        lines.append(f"+{len(items) - MAX_LINES} more")
    return {"title": f"{len(items)} planning reminders", "body": "\n".join(lines), "url": url,
            "tag": f"crewboss-planning-{on.isoformat()}", "renotify": True}


def sweep(now: datetime | None = None) -> dict:
    """One pass: send the due reminders as ONE push and mark each task. A pass already running is skipped. Never raises."""
    if not _lock.acquire(blocking=False):
        return {"ok": True, "busy": True}
    try:
        return _sweep_once(now)
    except Exception as exc:  # noqa: BLE001 — the loop must live on
        log.exception("planning reminders")
        return {"ok": False, "error": str(exc)[:300]}
    finally:
        _lock.release()


def _sweep_once(now: datetime | None) -> dict:
    try:
        tz = ZoneInfo(config.CA_TIMEZONE)
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("UTC")
    local = (now.astimezone(tz) if now else datetime.now(tz))
    on = local.date()
    if local.hour < config.PLANNING_REMINDER_HOUR:
        return {"ok": True, "sent": 0, "early": True}
    todo = due(on)
    items = [("start", t) for t in todo["start"]] + [("chase", t) for t in todo["chase"]]
    if not items:
        return {"ok": True, "sent": 0}
    vapid, reason = push._load_vapid()
    if vapid is None:
        return {"ok": True, "sent": 0, "off": reason}
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT count(*) AS n FROM push_subscriptions")
        if not cur.fetchone()["n"]:
            return {"ok": True, "sent": 0, "off": "no device subscribed"}
    result = push.deliver(payload(items, on))
    # Marked whatever the delivery said (push.sweep's rule): a failed send is on the subscription's last_error, the push
    # service queues for a sleeping phone, and re-sending every pass would be the nagging this module exists to avoid.
    with get_cursor() as cur:
        for kind, t in items:
            col, src = ("start_reminded_for", "start_by") if kind == "start" else ("chase_reminded_for", "waiting_until")
            cur.execute(f"UPDATE tasks SET {col} = {src} WHERE id = %s", (t["id"],))
    log.info("planning reminders: %d item(s), %s", len(items), push.summary_line(result))
    return {"ok": True, "sent": len(items), "push": result}
