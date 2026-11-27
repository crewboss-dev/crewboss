"""Planning dates (task #915; owner, 2026-11-22: "we should have deadlines in crewboss, or planning dates ... there will be
times we need to wait for something or someone").

THE MODEL (docs/planning-dates.md; db/migrations/0112_planning_dates.sql)
- A MILESTONE is a named date in one project (`milestones`). A task may belong to one milestone of ITS OWN project (a database
  trigger refuses any other).
- A task carries `due_on`, `lead_days` (effort + expected waiting), `start_by`, and a WAITING state (`waiting_on`, `waiting_for`,
  `waiting_until`).
- EFFECTIVE DUE DATE is computed AT READ TIME: the task's own due_on, else its milestone's due_on (rows carry it as
  `due_effective`, with `due_source` 'own' | 'milestone'). Nothing is copied, so moving a milestone moves the tasks that follow it.
- `start_by` is STORED. Unless it was set explicitly (`start_by_manual`) it is effective due - lead_days, recomputed whenever due_on,
  lead_days or the milestone (its task or its date) changes. Giving start_by explicitly pins it; clearing it hands it back.
- WAITING: waiting_on is who/what ('owner', 'ai', a project slug, or free text); clearing it clears waiting_for and
  waiting_until too (also a CHECK constraint).
- A MILESTONE'S STATUS is derived, never stored: done (every counted task done) / late (past due_on with an open task) /
  at_risk (an open task whose start_by has PASSED while it is still backlog or ready) / on_track. Won't-have tasks are not counted.

All dates are ISO (YYYY-MM-DD) and "today" is the instance's own calendar day (config.CA_TIMEZONE). Owner rule k79: a person never
fills these in; the AI does, over MCP/REST, and the pages only display them."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from app import config
from app.db import get_cursor
from app.repo import BadRequest, NotFound

LEAD_MAX = 365
NAME_MAX = 120
NOTE_MAX = 500
WAITING_ON_MAX = 120
WAITING_FOR_MAX = 200
SOON_DAYS = 7
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TASK_FIELDS = ("due_on", "lead_days", "start_by", "milestone", "waiting_on", "waiting_for", "waiting_until")
CLEARABLE = ("due_on", "lead_days", "start_by", "milestone", "waiting")


def today() -> date:
    """The instance's calendar day. Tests monkeypatch this."""
    try:
        tz = ZoneInfo(config.CA_TIMEZONE)
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("UTC")
    return datetime.now(tz).date()


# ---------------------------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------------------------

def clean_date(value, name: str = "date") -> date | None:
    """'2026-12-11' -> date. None or '' -> None (clears). Anything but a real calendar date in YYYY-MM-DD is a BadRequest."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        raise BadRequest(f"{name} must be a date, YYYY-MM-DD")
    if isinstance(value, date):
        d = value
    else:
        if not isinstance(value, str) or not _ISO.match(value.strip()):
            raise BadRequest(f"{name} must be an ISO date, YYYY-MM-DD (got {str(value)[:20]!r})")
        try:
            d = date.fromisoformat(value.strip())
        except ValueError:
            raise BadRequest(f"{name} is not a real date: {value.strip()!r}") from None
    if not 2000 <= d.year <= 2100:
        raise BadRequest(f"{name} must be between 2000 and 2100")
    return d


def clean_lead(value) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise BadRequest(f"lead_days must be a whole number of days, 0..{LEAD_MAX}")
    if not 0 <= value <= LEAD_MAX:
        raise BadRequest(f"lead_days must be 0..{LEAD_MAX}")
    return value


def clean_line(value, name: str, limit: int) -> str | None:
    """One line of text, at most `limit` characters; None / blank -> None (clears)."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise BadRequest(f"{name} must be text")
    if "\n" in value or "\r" in value:
        raise BadRequest(f"{name} must be one line")
    value = value.strip()
    if len(value) > limit:
        raise BadRequest(f"{name} must be at most {limit} characters")
    return value or None


def clean_name(value) -> str:
    name = clean_line(value, "name", NAME_MAX)
    if not name:
        raise BadRequest("a milestone needs a name")
    return name


# ---------------------------------------------------------------------------------------------
# a task's planning fields (called from repo.create_task / repo.update_task)
# ---------------------------------------------------------------------------------------------

def patch_from(clear=None, **given) -> dict:
    """The planning part of a patch from keyword args (None = not given). `clear` lists fields to clear (CLEARABLE).
    Returns {field: value}, where None means clear."""
    if isinstance(clear, str):
        clear = [clear]
    clear = clear or []
    bad = [c for c in clear if c not in CLEARABLE]
    if bad:
        raise BadRequest(f"clear may name {CLEARABLE}, not {bad}")
    patch = {k: v for k, v in given.items() if v is not None and k in TASK_FIELDS}
    for c in clear:
        patch["waiting_on" if c == "waiting" else c] = None
    return patch


def has_planning(patch: dict) -> bool:
    return any(k in patch for k in TASK_FIELDS)


def _milestone_id(cur, project_id: int, ref) -> int | None:
    """A milestone of THIS project by id (int or digits) or by name; archived ones can't be joined. NotFound otherwise."""
    if ref is None or ref == "":
        return None
    if isinstance(ref, bool):
        raise BadRequest("milestone is an id or a name")
    if isinstance(ref, int) or (isinstance(ref, str) and ref.strip().isdigit()):
        cur.execute("SELECT id, archived_at FROM milestones WHERE id = %s AND project_id = %s", (int(ref), project_id))
    elif isinstance(ref, str):
        cur.execute("SELECT id, archived_at FROM milestones WHERE project_id = %s AND lower(name) = lower(%s) "
                    "AND archived_at IS NULL", (project_id, ref.strip()))
    else:
        raise BadRequest("milestone is an id or a name")
    row = cur.fetchone()
    if not row:
        raise NotFound(f"milestone {ref!r} not found in this project")
    if row["archived_at"]:
        raise BadRequest(f"milestone {ref!r} is archived; a task can't join it")
    return row["id"]


def apply(cur, project_id: int, before: dict | None, patch: dict) -> tuple[dict, list[str]]:
    """Validate the planning keys of `patch` against the task's current row (`before`; None on create) and return
    ({column: value} to write, [change summaries]). Only columns that change are returned. Recomputes start_by."""
    cols: dict = {}
    cur_ = dict(before or {})

    if "due_on" in patch:
        cols["due_on"] = clean_date(patch["due_on"], "due_on")
    if "lead_days" in patch:
        cols["lead_days"] = clean_lead(patch["lead_days"])
    if "milestone" in patch:
        cols["milestone_id"] = _milestone_id(cur, project_id, patch["milestone"])
    manual = bool(cur_.get("start_by_manual", False))
    explicit_start = None
    if "start_by" in patch:
        explicit_start = clean_date(patch["start_by"], "start_by")
        manual = explicit_start is not None
        cols["start_by_manual"] = manual
    # waiting: clearing waiting_on clears the other two
    if "waiting_on" in patch and clean_line(patch["waiting_on"], "waiting_on", WAITING_ON_MAX) is None:
        cols.update(waiting_on=None, waiting_for=None, waiting_until=None)
    else:
        if "waiting_on" in patch:
            cols["waiting_on"] = clean_line(patch["waiting_on"], "waiting_on", WAITING_ON_MAX)
        if "waiting_for" in patch:
            cols["waiting_for"] = clean_line(patch["waiting_for"], "waiting_for", WAITING_FOR_MAX)
        if "waiting_until" in patch:
            cols["waiting_until"] = clean_date(patch["waiting_until"], "waiting_until")
        if (cols.get("waiting_for") or cols.get("waiting_until")) and not cols.get("waiting_on", cur_.get("waiting_on")):
            raise BadRequest("set waiting_on (who or what you wait on) before waiting_for / waiting_until")
    # start_by: explicit, else computed from the effective due date
    eff_due = cols["due_on"] if "due_on" in cols else cur_.get("due_on")
    if eff_due is None:
        mid = cols["milestone_id"] if "milestone_id" in cols else cur_.get("milestone_id")
        if mid:
            cur.execute("SELECT due_on FROM milestones WHERE id = %s", (mid,))
            m = cur.fetchone()
            eff_due = m["due_on"] if m else None
    lead = cols["lead_days"] if "lead_days" in cols else cur_.get("lead_days")
    if manual:
        if "start_by" in patch:
            cols["start_by"] = explicit_start
    else:
        cols["start_by"] = (eff_due - timedelta(days=lead)) if (eff_due is not None and lead is not None) else None
    # keep only what changes; a changed date re-arms its reminder
    if before is None:
        out = {k: v for k, v in cols.items() if v is not None and v is not False}
        return out, []
    out = {k: v for k, v in cols.items() if v != before.get(k)}
    if "start_by" in out:
        out["start_reminded_for"] = None
    if "waiting_until" in out:
        out["chase_reminded_for"] = None
    notes = [f"{k.replace('_id', '')} → {v if v is not None else 'cleared'}" for k, v in out.items()
             if k in ("due_on", "start_by", "waiting_until", "lead_days", "waiting_on", "waiting_for", "milestone_id")]
    return out, notes


def recompute_milestone_tasks(cur, milestone_id: int, new_due: date) -> int:
    """A milestone's date moved: tasks that follow it (no own due_on, start_by not set by hand) get a fresh start_by and a re-armed
    start reminder when it changed."""
    cur.execute(
        "UPDATE tasks SET start_by = %s::date - lead_days, start_reminded_for = NULL "
        "WHERE milestone_id = %s AND due_on IS NULL AND NOT start_by_manual AND lead_days IS NOT NULL "
        "AND start_by IS DISTINCT FROM (%s::date - lead_days)", (new_due, milestone_id, new_due))
    return cur.rowcount


# ---------------------------------------------------------------------------------------------
# reading: effective due date, flags, milestone status
# ---------------------------------------------------------------------------------------------

def decorate(rows: list[dict], on: date | None = None) -> list[dict]:
    """Rows from `tasks` gain milestone_name / milestone_due_on, due_effective + due_source, and the display flags due_state
    ('late' | 'start_reached' | None) and waiting_overdue. One query for the whole list. Done tasks carry no flags."""
    on = on or today()
    ids = {r["milestone_id"] for r in rows if r.get("milestone_id")}
    ms: dict = {}
    if ids:
        with get_cursor(commit=False) as cur:
            cur.execute("SELECT id, name, due_on FROM milestones WHERE id = ANY(%s)", (list(ids),))
            ms = {m["id"]: m for m in cur.fetchall()}
    out = []
    for r in rows:
        r = dict(r)
        m = ms.get(r.get("milestone_id"))
        r["milestone_name"] = m["name"] if m else None
        r["milestone_due_on"] = m["due_on"] if m else None
        own, inherited = r.get("due_on"), (m["due_on"] if m else None)
        r["due_effective"] = own or inherited
        r["due_source"] = "own" if own else "milestone" if inherited else None
        r.update(flags(r, on))
        out.append(r)
    return out


def flags(r: dict, on: date) -> dict:
    """due_state / waiting_overdue for a task row that already carries due_effective."""
    open_ = r.get("status") != "done"
    due = r.get("due_effective")
    state = None
    if open_ and due is not None and due < on:
        state = "late"
    elif open_ and r.get("start_by") is not None and r["start_by"] <= on and r.get("status") in ("backlog", "ready"):
        state = "start_reached"
    wu = r.get("waiting_until")
    return {"due_state": state, "waiting_overdue": bool(open_ and r.get("waiting_on") and wu is not None and wu < on)}


def milestone_status(due_on: date, tasks: list[dict], on: date | None = None) -> str:
    """done | late | at_risk | on_track, from the milestone's COUNTED tasks (Won't-have excluded)."""
    on = on or today()
    counted = [t for t in tasks if t.get("priority") != "wont"]
    open_ = [t for t in counted if t["status"] != "done"]
    if counted and not open_:
        return "done"
    if open_ and due_on < on:
        return "late"
    if any(t.get("start_by") is not None and t["start_by"] < on and t["status"] in ("backlog", "ready") for t in open_):
        return "at_risk"
    return "on_track"


# ---------------------------------------------------------------------------------------------
# milestones
# ---------------------------------------------------------------------------------------------

def _row(cur, milestone_id: int) -> dict:
    cur.execute("SELECT * FROM milestones WHERE id = %s", (milestone_id,))
    row = cur.fetchone()
    if not row:
        raise NotFound(f"milestone {milestone_id} not found")
    return row


def get_milestone(milestone_id: int) -> dict:
    with get_cursor(commit=False) as cur:
        return dict(_row(cur, milestone_id))


def create_milestone(project_id: int, name: str, due_on, note: str, actor: str) -> dict:
    from app import repo
    name = clean_name(name)
    due = clean_date(due_on, "due_on")
    if due is None:
        raise BadRequest("a milestone needs a due_on date, YYYY-MM-DD")
    note = clean_line(note or "", "note", NOTE_MAX) or ""
    with get_cursor() as cur:
        repo._refuse_account(cur, project_id)
        cur.execute("SELECT 1 FROM milestones WHERE project_id = %s AND lower(name) = lower(%s) AND archived_at IS NULL",
                    (project_id, name))
        if cur.fetchone():
            raise repo.Conflict(f"this project already has a milestone named {name!r}")
        cur.execute("INSERT INTO milestones (project_id, name, due_on, note, created_by) VALUES (%s, %s, %s, %s, %s) "
                    "RETURNING *", (project_id, name, due, note, actor))
        row = cur.fetchone()
        repo.record_activity(cur, project_id, actor, "created", "milestone", row["id"],
                             f"Created milestone '{name}' due {due.isoformat()}")
    return with_status([row])[0]


def update_milestone(milestone_id: int, patch: dict, actor: str) -> dict:
    """patch keys: name, due_on, note, archived (bool). A moved date recomputes start_by for the tasks that follow it."""
    from app import repo
    with get_cursor() as cur:
        before = _row(cur, milestone_id)
        sets, params, notes = [], [], []
        if patch.get("name") is not None:
            name = clean_name(patch["name"])
            if name != before["name"]:
                cur.execute("SELECT 1 FROM milestones WHERE project_id = %s AND lower(name) = lower(%s) AND id <> %s "
                            "AND archived_at IS NULL", (before["project_id"], name, milestone_id))
                if cur.fetchone():
                    raise repo.Conflict(f"this project already has a milestone named {name!r}")
                sets.append("name = %s")
                params.append(name)
                notes.append(f"renamed to '{name}'")
        new_due = before["due_on"]
        if patch.get("due_on") is not None:
            new_due = clean_date(patch["due_on"], "due_on")
            if new_due is None:
                raise BadRequest("a milestone's due_on can't be cleared")
            if new_due != before["due_on"]:
                sets.append("due_on = %s")
                params.append(new_due)
                notes.append(f"due {new_due.isoformat()}")
        if patch.get("note") is not None:
            note = clean_line(patch["note"], "note", NOTE_MAX) or ""
            if note != before["note"]:
                sets.append("note = %s")
                params.append(note)
                notes.append("note updated")
        if patch.get("archived") is not None and bool(patch["archived"]) != (before["archived_at"] is not None):
            sets.append("archived_at = now()" if patch["archived"] else "archived_at = NULL")
            notes.append("archived" if patch["archived"] else "restored")
        if sets:
            cur.execute(f"UPDATE milestones SET {', '.join(sets)} WHERE id = %s", params + [milestone_id])
            if new_due != before["due_on"]:
                recompute_milestone_tasks(cur, milestone_id, new_due)
            repo.record_activity(cur, before["project_id"], actor, "updated", "milestone", milestone_id,
                                 f"Milestone '{before['name']}': " + "; ".join(notes))
        row = _row(cur, milestone_id)
    return with_status([row])[0]


def with_status(rows: list[dict], on: date | None = None) -> list[dict]:
    """Milestone rows gain status and counts {total, done, open} (Won't-have tasks aren't counted)."""
    on = on or today()
    ids = [r["id"] for r in rows]
    by: dict[int, list] = {i: [] for i in ids}
    if ids:
        with get_cursor(commit=False) as cur:
            cur.execute("SELECT milestone_id, status, priority, start_by FROM tasks WHERE milestone_id = ANY(%s)", (ids,))
            for t in cur.fetchall():
                by[t["milestone_id"]].append(t)
    out = []
    for r in rows:
        ts = [t for t in by[r["id"]] if t["priority"] != "wont"]
        done = sum(1 for t in ts if t["status"] == "done")
        out.append({**dict(r), "status": milestone_status(r["due_on"], by[r["id"]], on),
                    "counts": {"total": len(ts), "done": done, "open": len(ts) - done}})
    return out


def list_milestones(project_ids: list[int], include_archived: bool = False) -> list[dict]:
    """Milestones of these projects, soonest first, each with its project slug, derived status and task counts."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT m.*, p.slug AS project FROM milestones m JOIN projects p ON p.id = m.project_id "
                    "WHERE m.project_id = ANY(%s) AND (%s OR m.archived_at IS NULL) ORDER BY m.due_on, m.id",
                    (list(project_ids), include_archived))
        rows = cur.fetchall()
    return with_status(rows)
