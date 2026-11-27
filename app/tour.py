"""The first-run how-to tour (#938): a person's progress, and the steps it walks through.

The STEP TEXT lives in one file, app/tour_steps.yaml (docs/getting-started.md is checked against it by tests/test_tour.py). Progress is one row per
PERSON in tour_progress (db/migrations/0113_tour_progress.sql; user_id NULL = the sign-in-off owner), so it follows them across devices:
  no row      never seen: the tour starts by itself on their next page
  active      started (or restarted) and not done: it resumes at last_step
  finished    reached the end;   skipped    closed early. Neither starts again by itself; "How-to" in the profile menu restarts it.
The page draws the tour (app/static/js/tour.js). The server only says what to show, where, and for whom: `go` is filled from the person's own
work (their first visible project and its newest task) and a step whose needs are not met carries its `else_text` instead of a target."""
from __future__ import annotations

import os
import re

import yaml

from app import access, tree
from app.db import get_cursor

STEPS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tour_steps.yaml")
ACTIONS = ("step", "finish", "skip", "restart")
NEEDS = ("project", "task", "owner", "runner")
_KEYS = {"id", "title", "text", "doc", "target", "open", "go", "page", "needs", "else_text", "interactive", "do"}
_cache: dict | None = None
# A path on this site: one leading slash NOT followed by / or \ (a browser reads both as a host), and no control characters (the URL
# parser drops a tab or newline inside, so a tab between two slashes would become //evil). The same rule as safePath in static/js/tour.js.
_SAFE_PATH = re.compile(r"^/(?![/\\])(?!.*[\x00-\x1f\x7f])", re.S)


def safe_path(u) -> bool:
    return isinstance(u, str) and _SAFE_PATH.match(u) is not None


def load() -> dict:
    """The parsed steps file ({steps: [...], not_in_tour: [...]}), checked once: a typo in it fails here, not in a person's browser."""
    global _cache
    if _cache is None:
        with open(STEPS_FILE, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        steps = data.get("steps") or []
        ids = [s["id"] for s in steps]
        if not steps or len(set(ids)) != len(ids):
            raise ValueError("tour_steps.yaml: steps must be a non-empty list with unique ids")
        for s in steps:
            extra = set(s) - _KEYS
            if extra or not s.get("title") or not s.get("text"):
                raise ValueError(f"tour_steps.yaml: step {s.get('id')!r} has unknown keys {sorted(extra)} or no title/text")
            if not set(_needs(s)) <= set(NEEDS):
                raise ValueError(f"tour_steps.yaml: step {s['id']!r} needs {s.get('needs')!r}")
            if s.get("go") and not safe_path(s["go"]):
                raise ValueError(f"tour_steps.yaml: step {s['id']!r} go must be a path on this site")
            if s.get("interactive") and not (s.get("target") and s.get("do")):
                raise ValueError(f"tour_steps.yaml: interactive step {s['id']!r} needs a target and a `do` label")
            if s.get("open") and s["open"] not in ("nav", "profile", "details"):
                raise ValueError(f"tour_steps.yaml: step {s['id']!r} open {s['open']!r}")
        _cache = {"steps": steps, "not_in_tour": list(data.get("not_in_tour") or [])}
    return _cache


def _needs(step: dict) -> list[str]:
    """What a step needs of the person before it can point at anything: its `needs` (a name or a list), plus whatever its `go` is built from
    ({project} needs a project, {task} a task and so a project), so a path is never built with a blank in it."""
    n = step.get("needs") or []
    n = [n] if isinstance(n, str) else list(n)
    go = step.get("go") or ""
    if "{task}" in go:
        n += ["task"]
    if "{project}" in go or "{task}" in go:
        n += ["project"]
    return sorted(set(n))


def step_ids() -> list[str]:
    return [s["id"] for s in load()["steps"]]


def _row(who: "access.Principal") -> dict | None:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT status, last_step FROM tour_progress WHERE user_id IS NOT DISTINCT FROM %s", (who.user_id,))
        return cur.fetchone()


def _context(who: "access.Principal") -> dict:
    """What the person has, so a step can send them to a real page: their first project (A to Z) that has a task and its newest task, else just the
    first project; whether any project of theirs can host a runner (/runners 404s without one). Only what they may see."""
    idx = access.filtered_index(who)
    all_rows = tree.project_tree(include_archived=False, idx=idx)
    runner = any(tree.is_runner_home(r, idx) for r in all_rows)
    rows = sorted((r for r in all_rows if not tree.is_account(r) and not tree.is_global_root(r)), key=lambda r: r["name"].lower())
    out = {"project": None, "task": None, "runner": runner}
    if not rows:
        return out
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT DISTINCT ON (project_id) project_id, id FROM tasks WHERE project_id = ANY(%s) ORDER BY project_id, id DESC", ([r["id"] for r in rows],))
        newest = {t["project_id"]: t["id"] for t in cur.fetchall()}
    for r in rows:
        if r["id"] in newest:
            return {**out, "project": r["slug"], "task": newest[r["id"]]}
    return {**out, "project": rows[0]["slug"]}


def _render(step: dict, who: "access.Principal", ctx: dict) -> dict:
    out = {k: step[k] for k in ("id", "title", "text") if k in step}
    for k in ("target", "open", "page", "else_text", "interactive", "do"):
        if step.get(k):
            out[k] = step[k]
    have = {"project": bool(ctx["project"]), "task": bool(ctx["task"]), "owner": who.is_owner, "runner": ctx["runner"]}
    met = all(have[n] for n in _needs(step))
    out["met"] = met
    if met and step.get("go"):
        go = step["go"].replace("{project}", ctx["project"] or "").replace("{task}", str(ctx["task"] or ""))
        if safe_path(go):                                           # a path on this site, never another host
            out["go"] = go
    if not met:
        out.pop("target", None)
        out.pop("interactive", None)
        out.pop("do", None)
    return out


def _light(who: "access.Principal") -> dict:
    row = _row(who)
    return {"status": row["status"] if row else "new", "step": row["last_step"] if row else ""}


def state(who: "access.Principal") -> dict:
    """{status: new|active|finished|skipped, step: last step id or ''}, and for a person the tour can still show (new or active) also `steps`: the rendered
    steps. A finished or skipped person gets only the first two, so an ordinary page load costs one small query."""
    out = _light(who)
    if out["status"] in ("finished", "skipped"):
        return out
    ctx = _context(who)
    out["steps"] = [_render(s, who, ctx) for s in load()["steps"]]
    return out


def save(who: "access.Principal", action: str, step: str = "") -> dict:
    """Record progress: `step` = reached this step (active), `finish` / `skip` = done, `restart` = active again from the first step.
    A step id that is not in the file, or an action that is not one of ACTIONS, is a ValueError (the route answers 400)."""
    ids = step_ids()
    if action not in ACTIONS:
        raise ValueError("unknown action")
    if step and step not in ids:
        raise ValueError("unknown step")
    if action == "step" and not step:
        raise ValueError("unknown step")
    if action == "restart":
        status, last, ended = "active", ids[0], False
    elif action == "step":
        status, last, ended = "active", step, False
    else:
        status, ended = ("finished" if action == "finish" else "skipped"), True
        last = step or (_row(who) or {}).get("last_step") or ""
    with get_cursor() as cur:
        cur.execute(
            "INSERT INTO tour_progress (user_id, status, last_step, ended_at) VALUES (%s, %s, %s, CASE WHEN %s THEN now() END) "
            "ON CONFLICT (COALESCE(user_id, 0)) DO UPDATE SET status = EXCLUDED.status, last_step = EXCLUDED.last_step, updated_at = now(), "
            "ended_at = EXCLUDED.ended_at, started_at = CASE WHEN %s THEN now() ELSE tour_progress.started_at END",
            (who.user_id, status, last, ended, action == "restart"))
    return state(who) if action == "restart" else _light(who)       # only a restart needs the steps again
