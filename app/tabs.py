"""The record page tabs (#773): the Jira-style bar under a project's or account's title.

ONE shared definition for every record page: the catalogue below (key, original label, icon, description, preview), the starting layout, the
per-project layout (db/migrations/0073_project_tabs.sql: ordered keys, renamed labels, the default tab) and the edits. The bar shows only what
the project's people chose; every other page stays reachable by its URL and sits in the "+" catalogue. Settings is pinned (never in the
catalogue, never removable). Who may edit: the instance owner and the project's admins (`can_manage`); everyone else only sees the bar."""
from __future__ import annotations

from app import access
from app.db import get_cursor
from app.repo import BadRequest

START = {"project": ("summary", "list", "board"), "account": ("summary", "list", "board", "approvals")}
PINNED = "settings"
MAX_LABEL = 24

# key -> (description, preview kind). Labels come from the page table (pages.NAV); icons are the stroke paths below.
INFO = {
    "summary": ("A dashboard of this record: open against closed over time, what is outstanding, what waits on you, its workflows and goals.", "cards"),
    "list": ("Every task as a table you can sort, group and filter.", "table"),
    "board": ("Tasks as cards in columns by status, to drag along.", "columns"),
    "changes": ("Changes the AI recommends, waiting for your decision.", "checks"),
    "workflows": ("The workflows the code runs, as a map you can open step by step.", "tree"),
    "logic": ("Every button, timer, webhook and AI step the app runs, drawn as a flow, with what is not yet mapped and what changed per release.", "tree"),
    "integrations": ("The systems this project talks to and how each link is doing.", "tree"),
    "keys": ("The keys each connected store holds.", "lines"),
    "build_skills": ("The skills agents follow when they build: built-in, global and this project's own, each at a version you can pin.", "lines"),
    "ai_features": ("The AI steps found in the app's code, grouped by what each is told to do. They feed the logic map.", "lines"),
    "rules": ("The rules and plain-language instructions that steer the workflows.", "checks"),
    "knowledge": ("Decisions, facts and notes worth keeping, searchable.", "lines"),
    "approvals": ("Deployments and merges waiting for a second person.", "checks"),
    "releases": ("What is on dev and production, and what is ready to promote.", "columns"),
    "where": ("Which server, folder and address each stage lives at, what shares that server, and how the project is tested.", "columns"),
    "environment": ("What a machine needs installed to build this project, and which of your runners have it.", "checks"),
    "team": ("Who works on this project and in what role.", "people"),
    "materials": ("Logos, templates, fonts and other files the project keeps.", "grid"),
    "objectives": ("The goals this project is working towards, and progress on each.", "bars"),
    "activity": ("A running record of what changed, and who changed it.", "lines"),
    "health": ("How the running system is doing: errors, checks and alerts.", "pulse"),
    "discussions": ("Conversations about this project, in one place.", "chat"),
    "handoffs": ("Messages between this project's AI session and the sessions of the account's other projects, and the docs the account shares.", "lines"),
}

ICONS = {
    "summary": "M4 5h7v6H4zM13 5h7v3h-7zM13 11h7v8h-7zM4 13h7v6H4z",
    "list": "M4 6h16v12H4zM4 10h16M4 14h16M9 6v12",
    "board": "M4 5h4.5v14H4zM9.75 5h4.5v9h-4.5zM15.5 5H20v11h-4.5z",
    "changes": "M4 5h16v14H4zM8 12l3 3 5-6",
    "workflows": "M5 4h5v4H5zM14 16h5v4h-5zM5 16h5v4H5zM7.5 8v8M7.5 12H14v4",
    "logic": "M5 4h5v4H5zM14 4h5v4h-5zM9.5 16h5v4h-5zM7.5 8v3h9V8M12 11v5",
    "integrations": "M10 14a4 4 0 005.6 0l3-3a4 4 0 00-5.6-5.6l-1 1M14 10a4 4 0 00-5.6 0l-3 3a4 4 0 005.6 5.6l1-1",
    "keys": "M8 15a4 4 0 110-8 4 4 0 010 8zM11 11h10M18 11v3M15 11v2",
    "build_skills": "M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8zM18 16l.8 2.2L21 19l-2.2.8L18 22l-.8-2.2L15 19l2.2-.8z",
    "ai_features": "M9 3h6v3H9zM5 8h14v10a2 2 0 01-2 2H7a2 2 0 01-2-2zM9 12h.01M15 12h.01M9.5 16h5",
    "rules": "M5 4h14v16H5zM9 9h6M9 13h6M9 17h3",
    "knowledge": "M5 4h11a3 3 0 013 3v13H8a3 3 0 01-3-3zM5 17a3 3 0 013-3h11",
    "approvals": "M12 3l8 3v6c0 4.5-3.2 7.8-8 9-4.8-1.2-8-4.5-8-9V6zM8.5 12l2.5 2.5 4.5-5",
    "releases": "M4 4h8l8 8-8 8-8-8zM8 8h.01",
    "environment": "M4 6h16v12H4zM8 10l2 2 4-4M8 15h8",
    "where": "M4 5h16v5H4zM4 14h16v5H4zM7.5 7.5h.01M7.5 16.5h.01",
    "team": "M9 11a3 3 0 100-6 3 3 0 000 6zM3 20c0-3.3 2.7-6 6-6s6 2.7 6 6M16 11a2.5 2.5 0 100-5M17 14.2c2.3.5 4 2.5 4 5.8",
    "materials": "M4 5h16v14H4zM4 16l5-5 4 4 3-3 4 4M9 9h.01",
    "objectives": "M6 21V4M6 5h11l-2 4 2 4H6",
    "activity": "M3 12h4l3-7 4 14 3-7h4",
    "health": "M12 20s-7-4.5-7-10a4 4 0 017-2.6A4 4 0 0119 10c0 5.5-7 10-7 10zM8 11h2l1.5-2 2 4 1-2h1.5",
    "discussions": "M4 5h16v11H9l-5 4z",
    "handoffs": "M4 8h13l-3-3M20 16H7l3 3",
    "settings": "M12 9a3 3 0 100 6 3 3 0 000-6zM19.4 13a7.6 7.6 0 000-2l2-1.5-2-3.4-2.3 1a7.6 7.6 0 00-1.8-1L15 3.5h-4l-.3 2.6a7.6 7.6 0 00-1.8 1l-2.3-1-2 3.4 2 1.5a7.6 7.6 0 000 2l-2 1.5 2 3.4 2.3-1c.5.4 1.2.8 1.8 1l.3 2.6h4l.3-2.6c.6-.2 1.3-.6 1.8-1l2.3 1 2-3.4z",
}

# Simple preview illustrations (inline SVG, theme colours via the .pv-* classes in css/shell.css). Trusted constants (Markup).
_W = '<svg viewBox="0 0 160 100" class="pv" aria-hidden="true" focusable="false">'
from markupsafe import Markup  # noqa: E402

_PREVIEW_SRC = {
    "cards": _W + '<rect class="pv-a" x="8" y="8" width="44" height="30" rx="4"/><rect class="pv-a" x="58" y="8" width="44" height="30" rx="4"/><rect class="pv-a" x="108" y="8" width="44" height="30" rx="4"/><rect class="pv-a" x="8" y="46" width="144" height="46" rx="4"/><path class="pv-s" d="M16 80l22-16 20 8 24-20 30 10 30-14"/></svg>',
    "table": _W + '<rect class="pv-a" x="8" y="8" width="144" height="14" rx="3"/><g class="pv-a"><rect x="8" y="28" width="144" height="10" rx="2"/><rect x="8" y="42" width="144" height="10" rx="2"/><rect x="8" y="56" width="144" height="10" rx="2"/><rect x="8" y="70" width="144" height="10" rx="2"/></g><rect class="pv-b" x="12" y="30" width="30" height="6" rx="2"/></svg>',
    "columns": _W + '<g class="pv-a"><rect x="8" y="8" width="42" height="84" rx="4"/><rect x="59" y="8" width="42" height="84" rx="4"/><rect x="110" y="8" width="42" height="84" rx="4"/></g><g class="pv-b"><rect x="13" y="16" width="32" height="14" rx="3"/><rect x="13" y="36" width="32" height="14" rx="3"/><rect x="64" y="16" width="32" height="14" rx="3"/><rect x="115" y="16" width="32" height="14" rx="3"/><rect x="115" y="36" width="32" height="14" rx="3"/></g></svg>',
    "target": _W + '<circle class="pv-s" cx="80" cy="50" r="38"/><circle class="pv-s" cx="80" cy="50" r="24"/><circle class="pv-f" cx="80" cy="50" r="8"/></svg>',
    "checks": _W + '<g class="pv-a"><rect x="8" y="10" width="144" height="22" rx="4"/><rect x="8" y="39" width="144" height="22" rx="4"/><rect x="8" y="68" width="144" height="22" rx="4"/></g><path class="pv-s" d="M16 21l4 4 7-8M16 50l4 4 7-8M16 79l4 4 7-8"/></svg>',
    "tree": _W + '<g class="pv-a"><rect x="8" y="38" width="36" height="24" rx="4"/><rect x="62" y="10" width="36" height="24" rx="4"/><rect x="62" y="66" width="36" height="24" rx="4"/><rect x="116" y="38" width="36" height="24" rx="4"/></g><path class="pv-s" d="M44 50h9l9-28M44 50h9l9 28M98 22h9l9 28M98 78h9l9-28"/></svg>',
    "lines": _W + '<g class="pv-a"><rect x="8" y="10" width="90" height="8" rx="3"/><rect x="8" y="28" width="144" height="6" rx="3"/><rect x="8" y="42" width="144" height="6" rx="3"/><rect x="8" y="56" width="120" height="6" rx="3"/><rect x="8" y="70" width="136" height="6" rx="3"/></g><rect class="pv-b" x="8" y="84" width="40" height="8" rx="3"/></svg>',
    "people": _W + '<g class="pv-a"><circle cx="40" cy="36" r="14"/><circle cx="80" cy="36" r="14"/><circle cx="120" cy="36" r="14"/><rect x="22" y="58" width="36" height="28" rx="8"/><rect x="62" y="58" width="36" height="28" rx="8"/><rect x="102" y="58" width="36" height="28" rx="8"/></g></svg>',
    "grid": _W + '<g class="pv-a"><rect x="8" y="8" width="42" height="38" rx="4"/><rect x="59" y="8" width="42" height="38" rx="4"/><rect x="110" y="8" width="42" height="38" rx="4"/><rect x="8" y="54" width="42" height="38" rx="4"/><rect x="59" y="54" width="42" height="38" rx="4"/></g><rect class="pv-b" x="110" y="54" width="42" height="38" rx="4"/></svg>',
    "bars": _W + '<g class="pv-a"><rect x="14" y="52" width="22" height="40" rx="3"/><rect x="48" y="30" width="22" height="62" rx="3"/><rect x="82" y="44" width="22" height="48" rx="3"/></g><rect class="pv-b" x="116" y="14" width="22" height="78" rx="3"/></svg>',
    "pulse": _W + '<rect class="pv-a" x="8" y="8" width="144" height="84" rx="6"/><path class="pv-s" d="M14 54h28l10-30 16 56 14-40 10 14h54"/></svg>',
    "chat": _W + '<rect class="pv-a" x="8" y="10" width="92" height="28" rx="8"/><rect class="pv-b" x="60" y="46" width="92" height="28" rx="8"/><rect class="pv-a" x="8" y="80" width="64" height="12" rx="6"/></svg>',
}
PREVIEWS = {k: Markup(v) for k, v in _PREVIEW_SRC.items()}      # trusted constants above, so safe to render without a template |safe


def info(key: str) -> tuple[str, str]:
    return INFO.get(key, ("", "lines"))


def start_layout(level: str) -> list[str]:
    return list(START["account" if level == "account" else "project"])


# --- storage ---------------------------------------------------------------------------------------------------------------------------

def row(project_id: int) -> dict | None:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT tabs, labels, default_tab FROM project_tabs WHERE project_id = %s", (project_id,))
        return cur.fetchone()


def layout(project_id: int, level: str, valid: list[str]) -> dict:
    """{tabs: [keys on the bar, in order], labels: {key: label}, default: key or ''} for one record. `valid` = the keys that exist for this record
    (its level's pages); stored keys that no longer exist are ignored. No row = the starting layout."""
    r = row(project_id)
    tabs = list(r["tabs"]) if r else start_layout(level)
    seen: list[str] = []
    for k in tabs:
        if k in valid and k != PINNED and k not in seen:
            seen.append(k)
    labels = {k: v for k, v in ((r or {}).get("labels") or {}).items() if k in valid and isinstance(v, str) and v.strip()}
    default = (r or {}).get("default_tab") or ""
    return {"tabs": seen, "labels": labels, "default": default if default in seen else ""}


def _save(project_id: int, tabs: list[str], labels: dict, default: str, by: int | None) -> None:
    import json
    with get_cursor() as cur:
        cur.execute("INSERT INTO project_tabs (project_id, tabs, labels, default_tab, updated_by) VALUES (%s, %s, %s::jsonb, %s, %s) "
                    "ON CONFLICT (project_id) DO UPDATE SET tabs = EXCLUDED.tabs, labels = EXCLUDED.labels, default_tab = EXCLUDED.default_tab, "
                    "updated_by = EXCLUDED.updated_by, updated_at = now()",
                    (project_id, tabs, json.dumps(labels), default, by))


def can_manage(who: "access.Principal", project: dict) -> bool:
    """The instance owner, or an admin of this project (its creator or an admin member)."""
    return bool(who.is_owner or access.is_admin(who, project["id"]))


def require_manage(who: "access.Principal", project: dict) -> None:
    if not can_manage(who, project):
        from app.repo import Forbidden
        raise Forbidden("only the project's owner or an admin changes its tabs")


def _need(key: str, allowed: list[str]) -> str:
    if key == PINNED:
        raise BadRequest("Settings stays on the bar")
    if key not in allowed:
        raise BadRequest("no such tab")
    return key


def stored(project_id: int, level: str, level_keys: list[str]) -> dict:
    """The layout as STORED, filtered only to the keys that exist for the record's level (never to what one person may see): the keys, labels and default
    that an editor without the right to see some page (an owner-only one) must not wipe when they save."""
    return layout(project_id, level, level_keys)


def edit(project: dict, level: str, valid: list[str], action: str, key: str, value: str = "", by: int | None = None,
         level_keys: list[str] | None = None) -> dict:
    """Apply one edit and return the new layout. `valid` = the keys THIS editor may touch (the pages they can see). The edit is applied to the whole stored
    layout (`level_keys`: every key of the level), so tabs, labels and a default the editor cannot see (an owner-only page the owner put on the bar) survive
    their save. action: add | remove | left | right | rename | default."""
    lay = stored(project["id"], level, level_keys if level_keys is not None else valid)
    tabs, labels, default = lay["tabs"], dict(lay["labels"]), lay["default"]
    key = _need(key, valid)
    if action == "add":
        if key not in tabs:
            tabs.append(key)
    elif action == "remove":
        if key not in tabs:
            raise BadRequest("that tab is not on the bar")
        tabs.remove(key)
        labels.pop(key, None)
        if default == key:
            default = ""
    elif action in ("left", "right"):
        if key not in tabs:
            raise BadRequest("that tab is not on the bar")
        # move among the tabs this editor can SEE: the slots they occupy keep their places, hidden tabs never shift
        slots = [i for i, k in enumerate(tabs) if k in valid]
        order = [tabs[i] for i in slots]
        i = order.index(key)
        j = i - 1 if action == "left" else i + 1
        if 0 <= j < len(order):
            order[i], order[j] = order[j], order[i]
            for slot, k in zip(slots, order):
                tabs[slot] = k
    elif action == "rename":
        if key not in tabs:
            raise BadRequest("that tab is not on the bar")
        label = " ".join((value or "").split())
        if len(label) > MAX_LABEL:
            raise BadRequest(f"a tab name is at most {MAX_LABEL} characters")
        if label:
            labels[key] = label
        else:
            labels.pop(key, None)         # empty = back to the original name
    elif action == "default":
        if key not in tabs:
            raise BadRequest("that tab is not on the bar")
        default = key
    else:
        raise BadRequest("unknown action")
    _save(project["id"], tabs, labels, default, by)
    return {"tabs": tabs, "labels": labels, "default": default}
