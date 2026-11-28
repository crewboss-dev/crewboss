"""Server-rendered HTML pages + the owner's form-posting write endpoints (no Authorization header —
this is the browser, on the NetBird-gated network; actor is always "owner"). Every write here calls
the SAME app.repo functions the API and MCP use, so the outbox rule fires exactly once.

URLs: `/` is the read-only GLOBAL dashboard across every non-archived project; everything else lives
under its project, `/<slug>/…` (project_router, below). The old unprefixed routes (/tasks, /workflows/…)
answer with a redirect into the current project (legacy_router), so existing links keep working."""
from __future__ import annotations

import asyncio
import json
import re
import time
import unicodedata
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import (HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response,
                               StreamingResponse)

from app import agent_pins, changes as changes_mod, job_card, roles, run_plan
from app import (config, drift, jobs as jobs_mod, erp_health, health, health_reports, platform_keys, repo, rules as rules_mod,
                 skills as skills_mod, tree, web_auth)
from app import connections as connections_mod
from app import integrations as integrations_mod
from app.fold import fold_response, insert_block, is_htmx, remove_block
from app import task_summary, task_timeline
from app import access, environment, external_models, lives, server_reads, favourites, runs_on, nav as nav_mod, saved_filters, snapshot, tabs as tabs_mod
from app import shared as shared_mod
from app import plan as plan_module
from app import planning
from app.db import get_cursor
from app.repo import BadRequest, Conflict, Forbidden, NotFound
from app.templating import templates, word_diff

router = APIRouter()

CURRENT_PROJECT_COOKIE = "current_project"


def _current_project(request: Request) -> dict | None:
    """The project the owner was last in (cookie), else the default, else the first active one. Only
    used where a URL doesn't name a project: the legacy redirects and the global home's sidebar."""
    active = access.filter_projects(access.of_request(request), repo.list_projects(include_archived=False))
    by_slug = {p["slug"]: p for p in active}
    for slug in (request.cookies.get(CURRENT_PROJECT_COOKIE), roles.slug("default")):
        if slug in by_slug:
            return by_slug[slug]
    return active[0] if active else None


class SlugMoved(Exception):
    """A page request at a renamed project's OLD slug (#577): a GET/HEAD is redirected to the same page at its current
    slug; a write (location None) is a plain 404 — never the usual flash-and-redirect, so nothing lands on the
    renamed project from a stale form (handle_slug_moved)."""

    def __init__(self, location: str | None, current: str):
        super().__init__(location or current)
        self.location = location
        self.current = current


def handle_slug_moved(request: Request, exc: SlugMoved):
    if exc.location is None:
        return PlainTextResponse(f"This project was renamed: it is now /{exc.current}. Nothing was changed.",
                                 status_code=404)
    # 307, not 301/308: a browser must not cache it, since the owner may rename the project back to this slug.
    return RedirectResponse(exc.location, status_code=307)


# WHO MAY USE A /{slug}/... ROUTE (task #713). Every such route sits behind project_from_path (ProjectDep), which 404s a project
# the signed-in person can't see and refuses a write by someone who can't change it. On top of that the route's first path
# segment says how much more is needed (the route-coverage test in tests/test_isolation.py reads this table):
#   owner   the instance owner only (404 to anyone else): servers, deploy approvals, platform keys, health, integrations,
#           tree moves, external agents.
#   manage  the owner, or the person who created the project: rename, repo details, environments.
#   (none)  anyone who may see the project (reads) / change it (writes).
SLUG_OWNER_ONLY = ("servers", "approvals", "keys", "health", "integrations", "integrations.json", "move", "external-agent")
SLUG_MANAGE = ("details", "rename", "environments", "team", "settings", "connections")


def slug_policy(route_path: str) -> str:
    """'owner' | 'manage' | 'project' for a /{slug}/... route template."""
    rest = route_path[len("/{slug}"):].lstrip("/")
    first = rest.split("/", 1)[0]
    if first in SLUG_OWNER_ONLY:
        return "owner"
    if first in SLUG_MANAGE:
        return "manage"
    return "project"


def project_from_path(request: Request, slug: str) -> dict:
    """Dependency for every /{slug}/… route: the project named in the URL, or 404. A project the signed-in person may not see
    is the same 404 as a missing one (task #713, app/access.py); a write needs the right to change it. An OLD slug (the
    project was renamed, #577) redirects a GET/HEAD to the same page at the current slug; a write there is a 404, so a
    form can never land on a project its author didn't see."""
    if not repo.SLUG_RE.match(slug or ""):
        raise NotFound(f"no project {slug!r}")
    project = repo.get_project_by_slug(slug)  # NotFound -> 404; an old slug resolves through project_slug_aliases
    who = access.of_request(request)
    access.require_view(who, project, f"project {slug!r} not found")
    route = request.scope.get("route")
    policy = slug_policy(getattr(route, "path", "") or "") if route is not None else "project"
    if policy == "owner":
        access.require_owner(who)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        access.require_edit(who, project)
        if policy == "manage" and not access.owns(who, project):
            raise Forbidden("only the project's creator or the owner does this")
    if project["slug"] != slug:
        if request.method not in ("GET", "HEAD"):
            raise SlugMoved(None, project["slug"])
        rest = request.url.path[len(slug) + 1:]
        raise SlugMoved(f"/{project['slug']}{rest}" + (f"?{request.url.query}" if request.url.query else ""),
                        project["slug"])
    request.state.project = project
    return project


project_router = APIRouter(prefix="/{slug}")
ProjectDep = Depends(project_from_path)


def _base(project: dict) -> str:
    return f"/{project['slug']}"


# THE LEFT MENU BY LEVEL (owner, 2026-10-02; task #148). Each page shows only what it needs:
#   home     All projects (/): one full-width dashboard. Its menu drawer holds All projects and the pages of the
#            project you were last in (the ERP's drawer is on every page, #479); the header's project switcher
#            goes everywhere else.
#   account  an account (kind 'account', e.g. /acme): its rollups (Focus, Board, Changes over the account and
#            every project under it) and what it SHARES with them (AI Skills, Rules, Knowledge).
#   project  every project page, as before.
# key: (path under /<slug>, label, phone label, icon). The icon is a Material Symbols name (app/nav_icons.py, the ERP's
# set): the same glyph the ERP and its admin site draw for the same kind of page (#479).
NAV = {
    "summary": ("", "Summary", "Summary", "dashboard"),      # a project's landing page (#772): its own dashboard
    "list": ("/list", "List", "List", "view_kanban"),        # every task as a table (#772)
    "board": ("/board", "Board", "Board", "view_kanban"),
    "changes": ("/changes", "Changes", "Changes", "fact_check"),
    "workflows": ("/workflows", "Workflows", "Flows", "account_tree"),
    "logic": ("/logic", "Logic", "Logic", "account_tree"),                       # the app's workflows and AI processes, deploy-gated (#715)
    "integrations": ("/integrations", "Integrations", "Links", "hub"),
    # Platform keys (#562): only on the Dashboard project (#577: global, every store on one page), after Integrations.
    "keys": ("/keys", "Platform keys", "Keys", "key"),
    "build_skills": ("/build-skills", "Skills", "Skills", "smart_toy"),         # the build skills agents follow (#718)
    "ai_features": ("/ai-features", "App AI features", "AI", "target"),     # the AI steps found in the app's code (was "AI Skills")
    "rules": ("/rules", "Rules & AI Instructions", "Rules", "rule"),
    "knowledge": ("/knowledge", "Knowledge", "Knowledge", "menu_book"),
    "servers": ("/servers", "Servers", "Servers", "dns"),       # redirects to Machines (#810); kept so old pill/tab keys still resolve
    "runners": ("/runners", "Runners", "Runners", "computer"),
    "machines": ("/machines", "Machines", "Machines", "computer"),
    "approvals": ("/approvals", "Approvals", "Approvals", "rule_folder"),
    "releases": ("/releases", "Releases", "Releases", "rule_folder"),       # dev first, production by promotion (change #77)
    "environment": ("/environment", "Environment", "Env", "tune"),          # what a machine needs to build the project, checked per runner (#810)
    "where": ("/where", "Where it lives", "Where", "dns"),                   # per stage: server, folder, URL, shared services; test mode (#839)
    "team": ("/team", "Team", "Team", "hub"),
    "materials": ("/materials", "Materials", "Materials", "perm_media"),
    "objectives": ("/objectives", "Objectives", "Goals", "flag"),
    "activity": ("/activity", "Activity", "Activity", "history"),
    "health": ("/health", "Health", "Health", "monitor_heart"),
    "discussions": ("/discussions", "Discussions", "Talk", "forum"),
    "handoffs": ("/handoffs", "Handoffs", "Handoffs", "forum"),      # session-to-session messages and the account's shared docs, read-only (#908)
    "settings": ("/settings", "Settings", "Settings", "settings"),
}
# The owner's order (2026-09-30): Focus · Board · Changes, Workflows · AI Skills · Rules · Knowledge · Objectives ·
# Activity · Health, and Overview (the project's numbers) last.
# THE PILLS of a record page (#733): the menu is fixed (app/nav.py), so what used to be rows by level is a pill row on the project's or account's own
# page (partials/record_head.html). Servers and Runners are the profile menu's now; Settings is the owner's, top-level records only.
NAV_BY_LEVEL = {
    "home": (),
    "account": ("summary", "list", "board", "changes", "approvals", "build_skills", "ai_features", "rules", "knowledge", "materials", "discussions", "handoffs", "settings"),
    "project": ("summary", "list", "board", "changes", "workflows", "logic", "knowledge", "discussions", "integrations", "build_skills", "ai_features", "rules",
                "materials", "objectives", "activity", "health", "releases", "where", "environment", "team", "handoffs", "settings"),
}


def page_level(project: dict | None, nav_active: str) -> str:
    if nav_active == "home" or project is None:
        return "home"
    return "account" if tree.is_account(project) else "project"


def nav_items(level: str, project: dict | None = None) -> list[tuple]:
    """[(key, path, label, phone label, icon)] for the left menu (and the phone's tab bar) at this level. The
    Dashboard project (platform_keys.home_slug) also gets Platform keys, right after Integrations, while any key store
    is configured (#562; #577: platform keys are global, so the store projects themselves no longer carry it)."""
    keys = [key for key in NAV_BY_LEVEL[level]]
    if level == "project" and project and config.PLATFORM_KEY_STORES \
            and project.get("slug") == platform_keys.home_slug():
        keys.insert(keys.index("integrations") + 1 if "integrations" in keys else len(keys), "keys")
    items = [(key, *NAV[key]) for key in keys]
    return items


def switcher_rows(rows: list[dict]) -> list[dict]:
    """The project switcher's tree guide lines (task #148): `last` and `guides` per row. The same guide computation
    the All projects home tree uses (task #157), so both read the same levels — see tree.with_guides. An active
    row's siblings are the active ones (archived rows are hidden until "Show archived")."""
    return tree.with_guides(rows)


OWNER_NAV = frozenset({"servers", "approvals", "keys", "health", "integrations", "settings"})   # nav keys; see SLUG_OWNER_ONLY


def _nav_for(who: "access.Principal", items: list) -> list:
    """The menu a person gets: owner-only pages (see SLUG_OWNER_ONLY) are not listed for anyone else."""
    if who.is_owner:
        return items
    return [it for it in items if it[0] not in OWNER_NAV]


def record_tabs(who: "access.Principal", project: dict | None, level: str, nav_active: str | None = None, lay: dict | None = None) -> dict | None:
    """THE TAB BAR of a record page (#773), ONE definition for every record page: {tabs, catalogue, manage, slug, default}. `tabs` are the keys the
    project's people put on the bar (app/tabs.py; the starting layout until they change it), then Settings (pinned, for whoever may see it); the page
    you are on is appended as a plain tab when it is not on the bar (every page stays reachable by URL). `catalogue` = the "+" list: pages not on the bar.
    None when the page has no record head."""
    if project is None or level == "home" or nav_active in ("runners", "servers", "machines"):
        return None
    visible = [(k, path, label) for k, path, label, _short, _ico in _nav_for(who, nav_items(level, project))]
    allowed = [k for k, _p, _l in visible if k != "settings"]
    by_key = {k: (path, label) for k, path, label in visible}
    lay = lay if lay is not None else tabs_mod.layout(project["id"], level, allowed)
    top = who.is_owner and (tree.is_account(project) or tree.is_runner_home(project))
    base = f"/{project['slug']}"

    def tab(k: str, **kw) -> dict:
        path, orig = by_key[k]
        return dict(key=k, href=base + path, orig=orig, label=lay["labels"].get(k, orig), icon=tabs_mod.ICONS.get(k, ""),
                    active=(k == nav_active), default=(k == lay["default"]), transient=False, pinned=False, **kw)

    bar = [tab(k) for k in lay["tabs"]]
    if "settings" in by_key and (top or nav_active == "settings"):
        bar.append(dict(tab("settings"), pinned=True))
    if nav_active in allowed and nav_active not in lay["tabs"]:
        bar.append(dict(tab(nav_active), transient=True))
    manage = tabs_mod.can_manage(who, project)
    catalogue = []
    if manage:
        for k in allowed:
            if k not in lay["tabs"]:
                desc, prev = tabs_mod.info(k)
                catalogue.append(dict(key=k, label=by_key[k][1], icon=tabs_mod.ICONS.get(k, ""), desc=desc, preview=tabs_mod.PREVIEWS.get(prev, "")))
    return {"tabs": bar, "catalogue": catalogue, "manage": manage, "slug": project["slug"], "default": lay["default"]}


def tab_keys(who: "access.Principal | None", project: dict) -> list[str]:
    """The tab keys this person may place on the bar of this record (its level's pages they can see). who=None: every key of the level (the stored layout,
    including keys only the owner can see)."""
    level = "account" if tree.is_account(project) else "project"
    items = nav_items(level, project)
    return [k for k, *_ in (items if who is None else _nav_for(who, items)) if k != "settings"]


def _page_scope(project: dict | None, level: str, who: "access.Principal | None" = None) -> tuple[list[dict], list[dict]]:
    """(switcher rows, session dots) for a page (task #368). The GLOBAL pages (All projects, and the dashboard root's
    own pages: the global Dashboard level) show every account's tree and sessions. A page INSIDE an account shows only
    that account's: its tree (the account and everything under it, re-based so the account is the top level) and the
    sessions of its projects. The switcher's "All projects" link is the way back to the global view."""
    who = who or access.OWNER_PRINCIPAL
    # A non-owner's tree is THEIR tree: only projects they may see (a visible child of a hidden parent shows at the top).
    rows = tree.project_tree(include_archived=True, idx=access.filtered_index(who))
    sessions = repo.list_sessions()
    if not who.is_owner:
        shown = {r["slug"] for r in rows}
        sessions = [s_ for s_ in sessions if s_.get("project") in shown]
    if level == "home" or project is None or tree.is_global_root(project):
        return switcher_rows(rows), sessions
    members = tree.account_member_ids(project["id"])
    mine = [r for r in rows if r["id"] in members]
    base = min((r["depth"] for r in mine), default=0)
    mine = [dict(r, depth=r["depth"] - base) for r in mine]
    slugs = {r["slug"] for r in mine}
    return switcher_rows(mine), [s for s in sessions if s.get("project") in slugs]


def create_targets(who: "access.Principal", idx: dict | None = None, fav: set[int] | None = None) -> list[dict]:
    """The projects the header's + Create may offer: live, not an account (accounts hold no tasks, #148), and ones the person may change
    (access.editable_ids: everything for the owner, their own and shared-as-member for a designer, nothing for a viewer)."""
    idx = idx if idx is not None else access.filtered_index(who)
    editable = access.editable_ids(who)
    fav = fav if fav is not None else favourites.visible_ids(who, idx)
    return [{"slug": r["slug"], "name": r["name"], "short": r["short_name"], "depth": r["depth"], "fav": r["id"] in fav,
             # the account it sits under (its group heading in the picker, #778)
             "group": next((a["name"] for a in tree.ancestors(r, idx) if tree.is_account(a)), "")}
            for r in tree.project_tree(include_archived=False, idx=idx)
            if not tree.is_account(r) and (editable is None or r["id"] in editable)]


def _me_name(request: Request) -> str:
    me = getattr(request.state, "web_user", None)
    return ((me.get("name") or me.get("username")) if isinstance(me, dict) else None) or "Owner"


def _runs_on_pill(request: Request, project: dict | None, nav_active: str) -> dict | None:
    """The header pill "Runs on" (#810): a person with at least one live runner in this project's account picks which one the project's
    jobs go to. Only on a project's own pages (an account holds no tasks), for a signed-in person.
    GONE FROM THE HEADER (change #90): runners belong to the project (Settings, "Runners for this project") and Run chooses one by itself. The
    /runs-on POSTs stay for old forms."""
    return None
    if project is None or nav_active == "home" or tree.is_account(project) or not getattr(request.state, "web_user", None):
        return None
    try:
        user_id = request.state.web_user.get("user_id") or web_auth.signed_in_user_row(request)["id"]
        opts = runs_on.options(project["id"], user_id)
    except Exception:    # noqa: BLE001 - a header decoration must never take a page down
        return None
    return opts if opts["devices"] else None


def _base_ctx(request: Request, nav_active: str, **extra) -> dict:
    project = getattr(request.state, "project", None)
    level = page_level(project, nav_active)
    who = access.of_request(request)
    all_projects, sessions = _page_scope(project, level, who)
    # The drawer and the phone bar (#479): a project's own pages. All projects has NO project menu (#531, owner
    # 2026-10-22: "You have to go into a Company, Project or sub project to see menu items"): just All projects, the
    # other surfaces and you.
    cached = getattr(request.state, "tab_layout", None)       # landing_page already read the layout for this project
    pills = record_tabs(who, project, level, nav_active, cached[1] if cached and project and cached[0] == project["id"] else None)
    menu_idx = access.filtered_index(who)
    menu_projects, menu_accounts = nav_mod.records(who, menu_idx)
    menu_arch_projects, menu_arch_accounts = nav_mod.records(who, menu_idx, archived=True)
    menu_current = nav_mod.current_record_id(project if nav_active != "home" else None, menu_projects, menu_accounts, menu_idx)
    fav_ids = favourites.visible_ids(who, menu_idx)      # once per request: the menu, the stars and the Create picker share it
    create_projects = create_targets(who, menu_idx, fav_ids)       # once per page: the dialog's list and its starting project
    section = nav_mod.section_for(nav_active, who.is_owner)
    has_account = bool((getattr(request.state, "web_user", None) or {}).get("user_id"))
    area = nav_mod.area_for(nav_active, project, bool(project) and tree.is_account(project))
    ctx = {
        "request": request,
        "nav_active": nav_active,
        "level": level,
        "who": who,
        "is_owner": who.is_owner,
        "nav_items": _nav_for(who, nav_items(level, project)),
        # THE FIXED MENU (#733, app/nav.py): the same on every page; only `area` (the highlighted row) varies.
        "area": area,
        "record_tabs": pills,
        "area_label": next((a["label"] for a in nav_mod.AREAS if a["key"] == area), ""),
        "nav_areas": nav_mod.areas(),
        "nav_by_key": {a["key"]: a for a in nav_mod.areas()},
        "bar_slots": nav_mod.bar(),
        "nav_menu": nav_mod.menu(who.is_owner),
        # THE APP SWITCHER (#778): Build, Assets, Administration (owner only); the section owns its left menu and the Create pill.
        "section": section,
        "sections": nav_mod.sections(who.is_owner),
        "section_info": next(x for x in nav_mod.sections(True) if x["key"] == section),
        "section_menu": nav_mod.section_menu(section, nav_active, who.is_owner, has_account),
        "create_mode": nav_mod.create_mode(section, who.is_owner, web_auth.mode() == "local" and has_account),
        # The drawer's Projects and Accounts sections (#771): the live top-level records this person may see, and the row to light.
        "menu_projects": menu_projects,
        "menu_accounts": menu_accounts,
        "menu_current": menu_current,
        "menu_groups": nav_mod.account_groups(who, menu_idx, menu_current, (menu_projects, menu_accounts), (menu_arch_projects, menu_arch_accounts)),
        # FAVOURITES (#778): this person's starred live projects (the menu's top section) and the ids, for the star on every row and the record head.
        "menu_favourites": favourites.rows(who, menu_idx, fav_ids),
        "fav_ids": fav_ids,
        "menu_landing": nav_mod.landing,
        # The header's + Create dialog (#778): the live projects this person may CHANGE (accounts hold no tasks), and the one to start on.
        "create_projects": create_projects,
        "create_current": (project["slug"] if project and nav_active != "home" and project["slug"] in {p["slug"] for p in create_projects} else ""),
        "menu_icon_d": nav_mod.menu_slot_icon(),
        "me_initials": nav_mod.initials(_me_name(request)),
        # The switcher keeps the section when it exists on the target's level (an account has no Workflows…).
        "account_sections": {NAV[k][0] for k in NAV_BY_LEVEL["account"]},
        "current_project": project,
        # In TREE order (dash first, accounts, children under parents), each with its depth (task #132) — on a page
        # inside an account, only that account's (task #368, _page_scope).
        "all_projects": all_projects,
        "known_servers": config.KNOWN_SERVERS,
        "sessions": sessions,
        "runs_on_pill": _runs_on_pill(request, project, nav_active),
    }
    ctx.update(extra)
    return ctx


FLASH_COOKIE = "crewboss_flash"


def render(request: Request, template: str, nav_active: str, **extra):
    flash = request.cookies.get(FLASH_COOKIE)
    resp = templates.TemplateResponse(request, template, _base_ctx(request, nav_active, flash=flash, **extra))
    if flash:
        resp.delete_cookie(FLASH_COOKIE)
    # Remember the (active) project the owner is looking at: the legacy redirects and the global home's
    # sidebar use it. The URL, not the cookie, decides what a project page shows.
    project = getattr(request.state, "project", None)
    if project and not project["archived_at"] and nav_active != "home" \
            and request.cookies.get(CURRENT_PROJECT_COOKIE) != project["slug"]:
        resp.set_cookie(CURRENT_PROJECT_COOKIE, project["slug"], max_age=60 * 60 * 24 * 365, samesite="lax")
    return resp


def partial(request: Request, template: str, ctx: dict, headers: dict | None = None):
    """An htmx fragment. `base` comes from request.state.project via the template context processor."""
    return templates.TemplateResponse(request, template, ctx, headers=headers)


def safe_local_path(value: str | None, default: str = "/") -> str:
    """Only a relative, same-site path ('/tasks?x=1'): never a scheme, a host, '//evil' or '/\\evil'
    (browsers treat a backslash as a slash). Anything else falls back to `default`."""
    if not value or not value.startswith("/") or value.startswith(("//", "/\\")):
        return default
    if any(c in value for c in "\\\r\n\t") or any(ord(c) < 0x20 for c in value):
        return default
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return default
    return value


def _referer_path(request: Request, default: str = "/") -> str:
    """The Referer reduced to its path+query (the same-origin guard already vetted where it came from)."""
    ref = request.headers.get("referer") or ""
    parts = urlsplit(ref)
    path = parts.path + (f"?{parts.query}" if parts.query else "")
    return safe_local_path(path, default)


def fold_or_redirect(request: Request, message: str, redirect_to: str, *, inserts=(), cookie_message: str | None = None,
                     tone: str = "info") -> Response:
    """The answer to an action that REMOVES (or moves) an item (#582, app/fold.py). htmx: 200 with an empty body (plus
    `inserts`, rows that land in another list on the page) and the message as a toast: the page folds the item away in
    place, never reloading or scrolling. No HX-Request header (no JS): the 303 back with a flash cookie, as always."""
    if is_htmx(request):
        return fold_response(message, inserts=inserts, tone=tone)
    resp = RedirectResponse(redirect_to, status_code=303)
    resp.set_cookie(FLASH_COOKIE, _cookie_safe(cookie_message or message), max_age=60, samesite="lax", httponly=True)
    return resp


def handle_repo_error(request: Request, exc: Exception):
    """App-level handler for repo.BadRequest / repo.NotFound escaping a PAGE route (the API converts its
    own via _guard). A browser write gets a flash, never a 500: htmx requests get a 4xx with an
    HX-Trigger 'mdd:flash' toast and no swap; full-page form posts are redirected back with a flash."""
    status = 404 if isinstance(exc, NotFound) else 403 if isinstance(exc, Forbidden) else \
        409 if isinstance(exc, Conflict) else 400
    message = str(exc)[:300]
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": message}, status_code=status)
    if request.method in ("GET", "HEAD"):
        return PlainTextResponse(message, status_code=status)
    if is_htmx(request):
        return PlainTextResponse(message, status_code=status, headers={
            # A Conflict is a friendly "not now" (e.g. the WIP limit), not an error: a calm toast, no flame.
            "HX-Trigger": json.dumps({"mdd:flash": {"message": message,
                                                    "tone": "info" if isinstance(exc, Conflict) else "error"}}),
            "HX-Reswap": "none",
        })
    project = getattr(request.state, "project", None)
    resp = RedirectResponse(_referer_path(request, _base(project) if project else "/"), status_code=303)
    resp.set_cookie(FLASH_COOKIE, message, max_age=60, samesite="lax", httponly=True)
    return resp


# =================================================================================================
# project management (owner-only, no auth header — the browser IS the owner here)
# =================================================================================================

@router.post("/projects")
def create_project_form(request: Request, slug: str = Form(...), name: str = Form(...), type: str = Form("internal"),
                        short_name: str = Form(...), sessions: str = Form(""),
                        parent: str = Form(""), kind: str = Form("project"), repo_name: str = Form("", alias="repo"),
                        repo_path: str = Form(""), dev_env: str = Form(None), prod_server: str = Form(""),
                        dev_server: str = Form("")):
    """New project. `sessions`: 'none' (its parent's session works it), 'all' = its own one session,
    'per-stream' = one per stream. Left out: an ACCOUNT gets its own session (task #253: an account's session works
    every project under it), a project gets none. The dashboard never starts anything itself: the sessions-sync timer (every
    minute on the sessions server) sees the project in /api/sessions/plan, mints its tokens and starts its sessions.
    Tree (task #132): `parent` = the slug it sits under (blank = directly under dash, the root), `kind` project |
    account, its repo (+ folder), and its environments: prod always; dev too when the 'Dev environment' box is
    ticked, then the two are kept in sync. Each on a server picked from config.KNOWN_SERVERS. All or nothing:
    tree.create_placed_project validates everything, then creates in one transaction.
    (The 'mini dashboard on their server' option is gone: that model was retired, #126.)"""
    who = access.of_request(request)
    if not who.is_owner:
        # A designer's project: a plain project (never an account) at the top or under one they can change, no standing
        # tmux session (that would start Claude on the owner's dev box), a server only from the ones assigned to them.
        sessions = sessions or "none"
        parent_row = None
        if parent.strip():
            try:
                parent_row = repo.get_project_by_slug(parent.strip())
            except NotFound:
                raise NotFound("no such parent project") from None
        access.check_new_project(who, parent_row, kind, [] if sessions == "none" else [sessions])
        prod_server = dev_server = ""
        owner_user_id = who.user_id
    else:
        owner_user_id = access.canonical_owner_id()       # the instance owner is ONE principal (#713)
    sessions = sessions or ("all" if kind == "account" else "none")
    roles = {"all": [repo.ALL_ROLE], "per-stream": list(repo.SESSION_STREAMS), "none": []}.get(sessions)
    if roles is None:
        raise BadRequest("sessions must be one session, one per stream, or none")
    project = tree.create_placed_project(slug.strip().lower(), name, type, "owner",
                                         short_name=short_name.strip().lower(), session_roles=roles,
                                         parent_slug=parent.strip(), kind=kind, repo_name=repo_name,
                                         repo_path=repo_path, dev_env=dev_env == "1", prod_server=prod_server,
                                         dev_server=dev_server, owner_user_id=owner_user_id)
    resp = RedirectResponse(_base(project), status_code=303)
    resp.set_cookie(CURRENT_PROJECT_COOKIE, project["slug"], max_age=60 * 60 * 24 * 365, samesite="lax")
    if roles:
        names = ", ".join(repo.session_name(project, r) for r in roles)
        plural = "sessions start" if len(roles) > 1 else "session starts"
        resp.set_cookie(FLASH_COOKIE, f"Created {project['name']}. Its Claude {plural} ({names}) automatically "
                                      f"within a minute.", max_age=60, samesite="lax", httponly=True)
    return resp


# The per-project "Mini dashboard" toggle is gone (owner, 2026-10-02): every piece of software gets a dashboard, and
# client dashboards live in the Customer Portal. The satellite columns and the /api/satellite/* feed stay.


def _confirm_name(project: dict, confirm: str) -> None:
    """The Danger zone's named confirm: the owner types the project's name (task #148)."""
    if " ".join((confirm or "").split()) != " ".join(project["name"].split()):
        raise BadRequest(f"type the name, {project['name']}, to confirm")


def _manage_or_refuse(request: Request, project: dict) -> None:
    """Archive / unarchive: the owner, or the person who created the project (never a mere share)."""
    who = access.of_request(request)
    if not (access.owns(who, project) and access.can_edit(who, project["id"])):
        raise Forbidden("only the project's creator or the owner does this")


@router.post("/projects/{project_id}/archive")
def archive_project_form(project_id: int, request: Request, confirm: str = Form("")):
    """From the project's (or account's) Danger zone. repo refuses a project that still has active children."""
    project = access.require_view(access.of_request(request), repo.get_project(project_id), f"project {project_id} not found")
    _manage_or_refuse(request, project)
    _confirm_name(project, confirm)
    repo.set_project_archived(project_id, True, "owner")
    resp = RedirectResponse(_referer_path(request), status_code=303)
    resp.set_cookie(FLASH_COOKIE, f"Archived {project['name']}.", max_age=60, samesite="lax", httponly=True)
    return resp


@router.post("/projects/{project_id}/unarchive")
def unarchive_project_form(project_id: int, request: Request, confirm: str = Form("")):
    """repo refuses a project whose parent is archived (unarchive the parent first)."""
    project = access.require_view(access.of_request(request), repo.get_project(project_id), f"project {project_id} not found")
    _manage_or_refuse(request, project)
    _confirm_name(project, confirm)
    repo.set_project_archived(project_id, False, "owner")
    return RedirectResponse(_referer_path(request), status_code=303)


# =================================================================================================
# legacy unprefixed routes -> the current project's path
# =================================================================================================
# /tasks, /tasks/new, /workflows/<id>, /subtasks/<id>/toggle … predate per-project URLs. They redirect to
# the same path under the current project (cookie, else the default): 301 for GET/HEAD — the browser keeps
# the #fragment across it, so the ERP's /tasks/new#prefill=… link lands on the form with its prefill — and
# 308 for writes (method + body preserved) so a page left open across the deploy still works. The target
# depends on a cookie, so the redirect is never cached.

LEGACY_SECTIONS = ("tasks", "subtasks", "workflows", "skills", "rules", "knowledge", "objectives",
                   "activity", "health")
legacy_router = APIRouter()
_ALL_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]


def _legacy_redirect(request: Request) -> Response:
    project = _current_project(request)
    raw = request.scope.get("raw_path") or request.url.path.encode()
    if project is None:
        target = "/"  # no active project at all: the global home
    else:
        target = _base(project) + raw.decode("latin-1")
        if request.url.query:
            target += "?" + request.url.query
    status = 301 if request.method in ("GET", "HEAD") else 308
    return Response(status_code=status, headers={"Location": safe_local_path(target),
                                                 "Cache-Control": "no-store", "Vary": "Cookie"})


for _section in LEGACY_SECTIONS:
    legacy_router.add_api_route(f"/{_section}", _legacy_redirect, methods=_ALL_METHODS, include_in_schema=False)
    legacy_router.add_api_route(f"/{_section}/{{rest:path}}", _legacy_redirect, methods=_ALL_METHODS,
                                include_in_schema=False)


# =================================================================================================
# / — the GLOBAL dashboard (read-only, every non-archived project)
# =================================================================================================

HOME_FILTERS = {"stream": "", "subproject": "", "status": "", "q": "", "view": "board", "assignee": "",
                "category": "", "layout": "home"}


@router.get("/", response_class=HTMLResponse)
def global_home(request: Request):
    """ONE full-width dashboard of everything up and coming (task #148): dash's Plan, what's waiting on the owner
    (questions, change decisions, checks, Now / Up next), the project tree as cards with each branch's counts, then
    the quieter panels. No left menu: the topbar's switcher is the navigation."""
    request.state.project = _current_project(request)  # `base` for the shared partials; no project is shown
    overview = repo.global_overview()
    return render(request, "pages/home.html", "home", o=overview, focus=repo.focus(None), global_focus=True,
                  plan=_plan_for(request),
                  filters=dict(HOME_FILTERS), query="layout=home", health_slot=erp_health.home_slot(),
                  SEVERITY=health.SEVERITY)


# --- the global PLAN panel (task #129): dash plans, the owner overrides ---------------------------------------

def _plan_for(request: Request) -> dict:
    """The global plan is the owner's and dash's: anyone else gets it empty (and plan_for_home is filtered anyway)."""
    plan = plan_module.plan_for_home()
    if access.of_request(request).is_owner:
        return plan
    return dict(plan, now=[], next=[], later=[], today=[], empty=True, note="")


def _plan_partial(request: Request):
    return partial(request, "partials/global_plan.html", {"plan": _plan_for(request)})


@router.get("/all/plan", response_class=HTMLResponse)
def global_plan_partial(request: Request):
    request.state.project = _current_project(request)
    return _plan_partial(request)


@router.post("/all/plan/{task_id}/pin", response_class=HTMLResponse)
def global_plan_pin(request: Request, task_id: int, pinned: str = Form("1")):
    """The owner pins (dash leaves it there) or unpins a plan card. Same-origin page write: the owner only."""
    access.require_owner(access.of_request(request))      # the global plan is the owner's (and dash's), nobody else's (#713)
    request.state.project = _current_project(request)
    plan_module.owner_pin(task_id, pinned == "1", "owner")
    return _plan_partial(request)


@router.post("/all/plan/{task_id}/today", response_class=HTMLResponse)
def global_plan_today(request: Request, task_id: int, today: str = Form("1")):
    """The owner marks a Now/Next card for today, or clears it. Every today mark expires at 06:00 (local time)."""
    access.require_owner(access.of_request(request))
    request.state.project = _current_project(request)
    plan_module.owner_today(task_id, today == "1", "owner")
    return _plan_partial(request)


@router.post("/all/plan/move", response_class=HTMLResponse)
def global_plan_move(request: Request, task_id: int = Form(...), bucket: str = Form(...),
                     after_id: str = Form(None)):
    """The owner drags a plan card: it lands after `after_id` (none = the top) in `bucket`, pinned."""
    request.state.project = _current_project(request)
    access.require_owner(access.of_request(request))
    after = int(after_id) if after_id and after_id.isdigit() else None
    plan_module.owner_move(task_id, bucket, after, "owner")
    return _plan_partial(request)


@router.get("/all/focus", response_class=HTMLResponse)
def global_focus_partial(request: Request):
    """The global home's Focus fragment — what it refetches after a change (mdd:board-stale)."""
    request.state.project = _current_project(request)
    return partial(request, "partials/focus.html", {"focus": repo.focus(None), "global_focus": True,
                                                    "filters": dict(HOME_FILTERS), "query": "layout=home"})


# --- jump to a task NUMBER (task #197) ---------------------------------------------------------------------------
# Sessions talk about "#123"; the owner types 123 (or #123) into the topbar's finder (partials/topbar.html,
# static/js/switcher.js) and lands on that task's drawer in ITS project, whichever project the page was on.

def _task_url(found: dict) -> str:
    return f"/{found['project_slug']}/board?open={found['id']}"


@router.get("/all/tasks/{ref}", response_class=HTMLResponse)
def jump_to_task(request: Request, ref: str):
    """/all/tasks/123 (or %23123): straight to the task's drawer on its project's Board. No such task: a friendly
    'No task #123' page (404), never a bare error."""
    number = repo.task_number(ref)
    found = repo.locate_task(number) if number else None
    if found:
        return RedirectResponse(_task_url(found), status_code=302)
    request.state.project = _current_project(request)
    resp = render(request, "pages/task_missing.html", "home", number=number, ref=ref[:40])
    resp.status_code = 404
    return resp


@router.get("/all/tasks/{ref}/peek")
def peek_task(ref: str):
    """What the finder shows as you type a number: the task's title and project, or 'No task #N'. Reads are open on
    the gated network (like every page); the title is the only text, and the browser renders it as text."""
    number = repo.task_number(ref)
    found = repo.locate_task(number) if number else None
    if not found:
        label = f"No task #{number}" if number else "Not a task number"
        return JSONResponse({"found": False, "id": number, "message": label}, status_code=404)
    return JSONResponse({"found": True, "id": found["id"], "title": found["title"],
                         "status": config.STATUS_META.get(found["status"], {}).get("label", found["status"]),
                         "project": found["project_name"], "archived": found["project_archived"],
                         "url": _task_url(found),
                         # The drawer panel in the task's own project, so the finder can open it without a page load.
                         "panel": f"/{found['project_slug']}/tasks/{found['id']}/panel"})


# =================================================================================================
# live updates — server→browser SSE (task #177)
# =================================================================================================
# Every open tab holds an EventSource to /all/events (app/static/js/live.js). When ANY session
# (Claude / Gemini / the owner / another tab / the phone) advances the board, the stream pushes a
# `changed` ping and the client dispatches `mdd:board-stale` — the event every panel (board_status,
# focus, global_plan, tasklist, triage_banner) is already wired to refetch on. No websockets, no
# pub/sub: a tiny loop polls the activity cursor (repo.activity_cursor = max activity id) a few times a
# second and emits only when it rises. A SHORT-LIVED read-only connection PER POLL (never one held open
# for the life of the stream), so there is no connection/NOTIFY leak (test-suite-leak lessons).
#
# PROXY NOTE: SSE must NOT be buffered or compressed by any reverse proxy in front of this app, or the
# browser receives nothing until the (never-ending) response closes. The response sets
# `X-Accel-Buffering: no` (nginx) and `Cache-Control: no-cache`; Caddy's reverse_proxy does not buffer
# streaming responses by default, so no Caddyfile change is needed — but if a buffering/compression
# directive is ever added in front of the dashboard host, exclude /all/events from it.

EVENTS_POLL_SECONDS = 2.5      # how often the loop checks the activity cursor (a handful of tabs → cheap)
EVENTS_HEARTBEAT_SECONDS = 20  # idle comment frame so proxies/Caddy don't drop the connection


def _sse(event: str | None, data: str) -> bytes:
    """One Server-Sent-Events frame. A bare comment (event None, data starting ':') is a heartbeat."""
    if event is None:
        return f"{data}\n\n".encode()
    return f"event: {event}\ndata: {data}\n\n".encode()


async def _board_event_stream(request: Request, who: "access.Principal | None" = None):
    """Yield SSE frames until the client disconnects. Emits `changed` with the new cursor whenever the board
    advances, and a `: ping` comment at least every EVENTS_HEARTBEAT_SECONDS to keep the pipe warm."""
    # Tell the client how long to wait before auto-reconnecting (EventSource default is ~3s anyway), and open
    # the stream immediately with a comment so the browser's `onopen` fires and proxies flush headers.
    yield b"retry: 5000\n\n"
    yield _sse(None, ": connected")
    who = who or access.OWNER_PRINCIPAL

    def _cursor() -> int:
        # A person's stream moves only for activity in the projects they may see (#713); the owner's for all of it.
        return repo.activity_cursor(access.visible_list(who))

    try:
        cursor = await asyncio.to_thread(_cursor)
    except Exception:  # noqa: BLE001 — a DB hiccup must not kill the stream; start from 0 and catch up
        cursor = 0
    last_beat = time.monotonic()
    while True:
        if await request.is_disconnected():
            break
        await asyncio.sleep(EVENTS_POLL_SECONDS)
        try:
            latest = await asyncio.to_thread(_cursor)
        except Exception:  # noqa: BLE001 — skip this poll, keep the stream alive, try again next tick
            continue
        if latest > cursor:
            cursor = latest
            yield _sse("changed", str(cursor))
            last_beat = time.monotonic()
        elif time.monotonic() - last_beat >= EVENTS_HEARTBEAT_SECONDS:
            yield _sse(None, ": ping")
            last_beat = time.monotonic()


@router.get("/all/events")
async def board_events(request: Request):
    """Live-update SSE stream for every open tab (task #177). Reads are open on the gated network, so no auth —
    it carries only a 'the board changed' ping and the activity id, never any record content."""
    return StreamingResponse(
        _board_event_stream(request, access.of_request(request)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # tell nginx (if ever in front) never to buffer this stream
        },
    )


# =================================================================================================
# tasks — kanban + list, drawer, comments, quick-add, drag/status changes
# =================================================================================================

def _web_actor(request: Request) -> str:
    """The text actor of the signed-in person for owner-level decisions: 'owner' for the instance owner (as before), else
    'user:<id>'. repo.answer_question / changes.decide then require them to be Admin/Owner of the project."""
    who = access.of_request(request)
    return repo.OWNER_ACTOR if who.is_owner else repo.person_actor(who.user_id)


def _task_in(project: dict, task_id: int) -> dict:
    """The task, only if it belongs to the project in the URL (404 otherwise): a /<slug>/ route never
    reads or writes another project's task."""
    task = repo.get_task(task_id)
    if task["project_id"] != project["id"]:
        raise NotFound(f"task {task_id} is not in {project['name']}")
    return task


def _subtask_in(project: dict, subtask_id: int) -> dict:
    sub = repo.get_subtask(subtask_id)
    _task_in(project, sub["task_id"])
    return sub


# The three ways to look at a project's tasks (owner 2026-09-30, ADHD-friendly):
#   FOCUS  /<slug>        — the owner's "what now": Now (<= 3 in progress), Review, Up next (3), Done today
#   BOARD  /<slug>/board  — status columns Backlog · Ready · In progress · Review · Done (this week)
#   PLAN   /<slug>/plan   — MoSCoW columns Must · Should · Could (+ Won't) for prioritising and triage
# Every board write answers with the fragment for the layout it came from (?layout=…), so one set of endpoints
# serves both views (and the global home's cross-project Focus, layout=home). The MoSCoW "Plan" board was
# retired 2026-10-01 (owner: one board, by status; priority is the card stripe and the order inside a column).
LAYOUTS = ("board", "focus", "home")
LAYOUT_PATH = {"board": "/board", "focus": "/focus"}
PRIORITY_INDEX = {"must": 0, "should": 1, "could": 2, "wont": 3}


def _task_filters(request: Request, default_layout: str = "board") -> dict:
    q = request.query_params
    assignee = q.get("assignee") or ""
    category = q.get("category") or ""
    status = repo.STATUS_ALIASES.get(q.get("status") or "", q.get("status") or "")
    layout = q.get("layout") if q.get("layout") in LAYOUTS else default_layout
    return {
        "priority": q.get("priority") if q.get("priority") in PRIORITY_INDEX else "",
        "stream": q.get("stream") if q.get("stream") in repo.VALID_STREAM else "",
        "subproject": q.get("subproject") or "",
        "status": status if status in repo.VALID_STATUS else "",
        "q": q.get("q") or "",
        "view": "list" if q.get("view") == "list" else "board",
        "assignee": assignee if assignee in repo.VALID_ASSIGNEE else "",
        "category": category if category in repo.VALID_CATEGORY + ("untriaged",) else "",
        "layout": layout,
        # An ACCOUNT's rolled-up view (task #148): every write from it goes to the task's own project and carries
        # the account back, so the answer is the account's view again. Validated in _rollup.
        "rollup": q.get("rollup") if repo.SLUG_RE.match(q.get("rollup") or "") else "",
    }


def _query(filters: dict) -> str:
    """The query string every board write carries back (filters + layout, never the list/board toggle)."""
    return urlencode({k: v for k, v in filters.items() if v and k != "view"})


def _filter_href(base: str, filters: dict, **overrides) -> str:
    merged = {**filters, **overrides}
    path = LAYOUT_PATH.get(merged.get("layout"), "/board")
    qs = urlencode({k: v for k, v in merged.items()
                    if v and k not in ("layout", "rollup") and not (k == "view" and v == "board")})
    return f"{base}{path}" + (f"?{qs}" if qs else "")


def _filtered_tasks(project_id: int | None, filters: dict, project_ids: list[int] | None = None) -> list[dict]:
    return repo.list_tasks(
        project_id,
        project_ids=project_ids,
        stream=filters["stream"] or None,
        status=filters["status"] or None,
        priority=filters["priority"] or None,
        subproject=filters["subproject"] or None,
        q=filters["q"] or None,
        assignee=filters["assignee"] or None,
        category=filters["category"] or None,
        with_progress=True,
    )


def _status_columns(project_id: int | None, filters: dict, project_ids: list[int] | None = None) -> dict:
    """Backlog · Ready · In progress · Review · Done (this week). Each column sorted Must → Should → Could →
    Won't, then rank (list_tasks' order). project_ids: an account's rollup."""
    tasks = _filtered_tasks(project_id, filters, project_ids)
    this_week = set(repo.done_this_week(project_id, project_ids))
    columns: dict = {s: [] for s in repo.VALID_STATUS}
    for t in tasks:
        if t["status"] == "done" and t["id"] not in this_week:
            continue
        columns[t["status"]].append(t)
    return columns


def _open_task_or_redirect(request: Request, project: dict, path: str):
    """?open=ID (or ?edit=ID): (the id to open in the drawer, a redirect when the task lives in another project — an
    old link, or it was moved — to the same view there, the task's project). An ACCOUNT's page opens a task of any
    project under it in place (task #148)."""
    raw = request.query_params.get("open") or request.query_params.get("edit")
    if not (raw and raw.isdigit()):
        return None, None, None
    try:
        task_project = repo.get_project(repo.get_task(int(raw))["project_id"])
    except NotFound:
        return None, None, None
    if not access.can_view(access.of_request(request), task_project["id"]):
        return None, None, None          # a task in a project they can't see is not opened, nor is its slug revealed
    if tree.is_account(project) and task_project["id"] in _descendants(request, project["id"]):
        return int(raw), None, task_project
    if task_project["id"] != project["id"]:
        return None, RedirectResponse(f"{_base(task_project)}{path}?open={int(raw)}", status_code=302), None
    return int(raw), None, task_project


def _descendants(request: Request, account_id: int) -> set[int]:
    """tree.descendant_ids, less the projects this person may not see (a shared account never shows hidden children)."""
    ids = tree.descendant_ids(account_id)
    vis = access.visible_ids(access.of_request(request))
    return ids if vis is None else ids & vis


def _rollup(request: Request, project: dict, filters: dict) -> dict | None:
    """The ACCOUNT whose rolled-up view this is (task #148): the page's own project when it is an account, else the
    `rollup` an account view's write carried (the URL then names the task's own project, which must sit under it).
    None = an ordinary one-project view."""
    if tree.is_account(project):
        return project
    if not filters.get("rollup"):
        return None
    try:
        account = repo.get_project_by_slug(filters["rollup"])
    except NotFound:
        return None
    if (tree.is_account(account) and access.can_view(access.of_request(request), account["id"])
            and project["id"] in _descendants(request, account["id"])):
        return account
    return None


def _rollup_ids(request: Request, project: dict, filters: dict) -> list[int] | None:
    """The projects an account's rolled-up board merges (a drag there places a card among all of them), else None."""
    account = _rollup(request, project, filters)
    return sorted(_descendants(request, account["id"])) if account else None


def task_targets(account: dict, who: "access.Principal | None" = None) -> list[dict]:
    """The projects under an account a new task may go to (accounts hold no tasks), in tree order: those this person may
    change (everything, for the owner)."""
    who = who or access.OWNER_PRINCIPAL
    editable = access.editable_ids(who)
    return [p for p in tree.subtree(account["id"], access.filtered_index(who))
            if not tree.is_account(p) and (editable is None or p["id"] in editable)]


def _layout_ctx(request: Request, project: dict, filters: dict, **extra) -> dict:
    """Everything the task-view fragments need for a layout. On an account (or a write from one), the view rolls up
    the account and every project under it: `rollup` = the account, `view_base` = its URL (the fragment refetches
    there), and each task carries its own project's slug for its links, writes and chip."""
    layout = filters["layout"]
    account = _rollup(request, project, filters) if layout != "home" else None
    ids = sorted(_descendants(request, account["id"])) if account else None
    filters = dict(filters, rollup=account["slug"] if account else "")  # an account it isn't under is dropped
    ctx = {"filters": filters, "query": _query(filters), **extra}
    if account:
        ctx.update(rollup=account, view_base=_base(account), task_targets=task_targets(account, access.of_request(request)))
    if layout == "focus":
        ctx["focus"] = repo.focus(project_ids=ids) if account else repo.focus(project["id"])
    elif layout == "home":
        ctx["focus"] = repo.focus(None)
        ctx["global_focus"] = True
    elif filters["view"] == "list":
        ctx["tasks"] = _filtered_tasks(project["id"], filters, ids)
    else:
        ctx["status_columns"] = _status_columns(project["id"], filters, ids)
        ctx["wip"] = repo.focus_wip()
    return ctx


LAYOUT_TEMPLATE = {"focus": "partials/focus.html", "home": "partials/focus.html", "board": "partials/board_status.html"}


def _board_response(request: Request, project_id: int, filters: dict, **extra):
    project = getattr(request.state, "project", None) or repo.get_project(project_id)
    ctx = _layout_ctx(request, project, filters, **extra)
    template = "partials/tasklist.html" if (filters["view"] == "list" and filters["layout"] == "board") \
        else LAYOUT_TEMPLATE[filters["layout"]]
    return partial(request, template, ctx)


def _move_targets(project: dict, idx: dict | None = None) -> list[dict] | None:
    """The valid 'Move to…' parent options for a project (task #166): every active project EXCEPT this one and its
    descendants (tree.descendant_ids keeps it cycle-safe), in tree order, each carrying its depth for the indent.
    None for the dashboard root, which can't move. set_place/validate_place still has the final say — a refusal
    (cycle, archived or non-root account parent, open-task account conversion…) comes back to the owner as a flash."""
    if roles.is_role(project, "overseer"):
        return None
    idx = idx if idx is not None else tree.index()
    excluded = tree.descendant_ids(project["id"], idx)
    return [p for p in tree.project_tree(idx=idx) if p["id"] not in excluded]


def _task_view(request: Request, project: dict, layout: str, nav: str, template: str, path: str, **extra):
    open_task_id, redirect, open_task_project = _open_task_or_redirect(request, project, path)
    if redirect:
        return redirect
    filters = _task_filters(request, default_layout=layout)
    filters["layout"] = layout
    base = _base(project)
    ctx = _layout_ctx(request, project, filters, open_task_id=open_task_id, open_task_project=open_task_project,
                      subprojects=repo.list_subprojects(project["id"]),
                      filter_href=lambda **ov: _filter_href(base, filters, **ov), **extra)
    return render(request, template, nav, **ctx)


def _account_summary(account: dict, who: "access.Principal | None" = None) -> dict:
    """The account page's head (task #148): its projects as cards with each branch's open work, and what it shares.
    Only the projects the person may see."""
    idx = access.filtered_index(who or access.OWNER_PRINCIPAL)
    rows = tree.subtree(account["id"], idx)
    counts = repo.open_task_counts([p["id"] for p in rows])
    roll = tree.rollups(rows, counts)
    return {"projects": [dict(p, branch=roll.get(p["id"], {})) for p in rows if p["id"] != account["id"]],
            "shared": shared_mod.counts(account["id"])}


def _connections_ctx(request: Request, project: dict, editor: bool = False) -> dict:
    """The Connections of a project for the Summary / Integrations / Settings templates (#784): only rows whose other end this person may see.
    `editor` adds the Settings controls (only for someone who can manage the project)."""
    who = access.of_request(request)
    conns = connections_mod.for_project(project["id"], access.visible_ids(who), connections_mod.index(request))
    ctx = {"conns": conns, "conns_shown": connections_mod.any_shown(conns), "conn_labels": connections_mod.KIND_LABELS}
    if editor and project["kind"] != "account" and access.owns(who, project):      # the same people the manage route policy lets write
        ctx.update(conn_editor=True, conn_picker=connections_mod.picker(who, project))
    return ctx


@project_router.get("", response_class=HTMLResponse)
def landing_page(request: Request, project: dict = ProjectDep):
    """A record's landing page is its SUMMARY (#772; an account's too, #773: a roll-up over its visible projects), unless its people chose another
    default tab ("Set as default" on the tab bar), which a bare /<slug> then redirects to."""
    level = "account" if tree.is_account(project) else "project"
    lay = tabs_mod.layout(project["id"], level, tab_keys(access.of_request(request), project))
    request.state.tab_layout = (project["id"], lay)
    default = lay["default"]
    if default and default != "summary":
        path = dict((k, p) for k, p, *_ in nav_items(level, project)).get(default)
        if path:
            return RedirectResponse(f"{_base(project)}{path}", status_code=302)
    return summary_page(request, project)


def summary_page(request: Request, project: dict):
    """SUMMARY (#772): a dashboard of this one project (and the projects under it): open against closed over time, what is outstanding by
    status, what waits on the owner, the agent jobs, the latest activity. Scoped to the project's branch AND to what the person may see."""
    ids = sorted(_descendants(request, project["id"]))
    extra: dict = {}
    if tree.is_account(project):         # an account's Summary rolls up its visible projects and lists them, with what it shares with them (#773)
        extra["account"] = _account_summary(project, access.of_request(request))
    else:                                # what Overview had that Summary lacked (#773): workflows, objectives, changes, health, tasks by stream
        extra.update(kpis=repo.dashboard_kpis(project["id"]), changes=repo.recent_workflow_changes(project["id"], limit=6),
                     objectives=repo.list_objectives(project["id"]),
                     health_slot=erp_health.home_slot() if roles.is_role(project, "health") else None)
    if not tree.is_account(project):
        extra.update(_connections_ctx(request, project))
        extra["milestones"] = planning.list_milestones(ids)      # #915: the milestone strip (display only)
    from app import handoffs
    extra["handoffs_open"] = handoffs.open_count(project, access.visible_ids(access.of_request(request)))      # the Handoffs entry's count (#908)
    return render(request, "pages/summary.html", "summary", s=snapshot.project_summary(ids), project_ids=ids, **extra)


@project_router.get("/focus", include_in_schema=False)
def focus_page(request: Request, project: dict = ProjectDep):
    """FOCUS is gone from records (#773): the Now page is that view, and Now cannot be scoped to one project, so an old /<slug>/focus link goes to
    the record's Summary."""
    return RedirectResponse(_base(project), status_code=302)


@project_router.get("/board", response_class=HTMLResponse)
def board_page(request: Request, project: dict = ProjectDep):
    redirect = _saved_default_redirect(request, project, "board", f"{_base(project)}/board")
    if redirect:
        return redirect
    return _task_view(request, project, "board", "board", "pages/tasks.html", "/board",
                      milestones=None if tree.is_account(project) else planning.list_milestones([project["id"]]))


# --- LIST (#772): every task as a table ------------------------------------------------------------------
# Same filters as the Board (_task_filters: assignee, priority, stream, category, tag, status, q), plus: sort (click a column header),
# group (status / priority / stream / assignee), done=1 (Show done: off by default) and cols (the visible columns). The table is a
# fragment (partials/task_table.html) that refreshes itself on mdd:board-stale, like the Board.
LIST_SORTS = ("number", "title", "assignee", "reporter", "priority", "status", "progress", "created", "updated")
LIST_GROUPS = ("status", "priority", "stream", "assignee")
LIST_COLS = ("type", "status", "priority", "assignee", "reporter", "progress", "created", "updated")      # every column but # and Title can be hidden
LIST_DEFAULT = ("status", "priority", "assignee", "updated")            # the short default set (with # and Title); the rest is off until picked
LIST_PARAMS = ("assignee", "priority", "stream", "category", "subproject", "status", "q", "group", "sort", "dir", "done", "cols")


def _list_options(request: Request) -> dict:
    q = request.query_params
    sort = q.get("sort") if q.get("sort") in LIST_SORTS else ""
    direction = q.get("dir") if q.get("dir") in ("asc", "desc") else ("desc" if sort in ("created", "updated", "number") else "asc")
    cols = [c for c in (q.get("cols") or "").split(",") if c in LIST_COLS] if "cols" in q else []
    return {"sort": sort, "dir": direction, "group": q.get("group") if q.get("group") in LIST_GROUPS else "",
            "done": q.get("done") == "1", "cols": ",".join(cols), "cols_set": "cols" in q}


def _sort_key(sort: str):
    def assignee(t): return (t["assignee"] or "", t.get("owner_stream") or "")
    def progress(t):
        p = t.get("progress") or {}
        return (p.get("done", 0) / p["total"]) if p.get("total") else -1
    return {"number": lambda t: t["id"], "title": lambda t: t["title"].lower(), "assignee": assignee,
            "reporter": lambda t: (t.get("created_by") or "").lower(), "priority": lambda t: PRIORITY_INDEX.get(t["priority"], 9),
            "status": lambda t: config.STATUS_ORDER.index(t["status"]) if t["status"] in config.STATUS_ORDER else 9,
            "progress": progress, "created": lambda t: t["created_at"], "updated": lambda t: t["updated_at"]}[sort]


def _group_of(t: dict, group: str) -> tuple[str, str]:
    if group == "status":
        return t["status"], config.STATUS_META.get(t["status"], {}).get("label", t["status"])
    if group == "priority":
        return t["priority"], config.PRIORITY_META.get(t["priority"], {}).get("label", t["priority"])
    if group == "stream":
        return t["stream"], config.STREAM_META.get(t["stream"], {}).get("label", t["stream"])
    mine = t["assignee"] == "owner"
    return ("owner" if mine else "ai"), ("Me" if mine else "AI")


def _list_ctx(request: Request, project: dict) -> dict:
    filters = _task_filters(request, default_layout="board")
    opts = _list_options(request)
    account = _rollup(request, project, filters)
    ids = sorted(_descendants(request, account["id"])) if account else None
    tasks = _filtered_tasks(project["id"], filters, ids)
    total = len(tasks)
    if not opts["done"] and filters["status"] != "done":
        tasks = [t for t in tasks if t["status"] != "done"]
    if opts["sort"]:
        tasks.sort(key=_sort_key(opts["sort"]), reverse=opts["dir"] == "desc")
    steps = repo.steps_for([t["id"] for t in tasks])
    groups = None
    if opts["group"]:
        order: dict = {}
        for t in tasks:
            key, label = _group_of(t, opts["group"])
            order.setdefault(key, {"key": key, "label": label, "tasks": []})["tasks"].append(t)
        rank = {"status": list(config.STATUS_ORDER), "priority": list(PRIORITY_INDEX), "stream": list(repo.VALID_STREAM), "assignee": ["owner", "ai"]}[opts["group"]]
        groups = sorted(order.values(), key=lambda g: rank.index(g["key"]) if g["key"] in rank else 99)
    carry = {k: v for k, v in {**filters, **opts, "done": "1" if opts["done"] else ""}.items() if k in LIST_PARAMS and v}
    if opts["cols_set"]:
        carry["cols"] = opts["cols"]
    base = _base(account or project)
    def list_href(**ov):
        merged = {**carry, **{k: v for k, v in ov.items()}}
        qs = urlencode({k: v for k, v in merged.items() if v})
        return f"{base}/list" + (f"?{qs}" if qs else "")
    who = access.of_request(request)
    can_edit = bool(task_targets(account, who)) if account else access.can_edit(who, project["id"])      # a viewer gets a read-only List
    return {"can_edit": can_edit, "tasks": tasks, "groups": groups, "steps": steps, "total": total, "shown": len(tasks), "filters": filters, "opts": opts,
            "list_href": list_href, "query": urlencode(carry), "is_rollup": bool(account), "vbase": base,
            "rollup": account, "task_targets": task_targets(account, access.of_request(request)) if account else [],
            "subprojects": repo.list_subprojects(project["id"]), "list_cols": LIST_COLS,
            "hidden_cols": [c for c in LIST_COLS if c not in (opts["cols"].split(",") if opts["cols_set"] else LIST_DEFAULT)]}


def _saved_default_redirect(request: Request, project: dict | None, page: str, path: str):
    """A bare arrival on a page with a saved default: open the default (#772). Anything in the query means the person chose. Clicking the
    page's own "All" / "clear" is NOT an arrival: it comes from the same page (Referer) or carries `all=1`, and shows the unfiltered page."""
    if request.query_params:
        return None
    ref = urlsplit(request.headers.get("referer") or "")
    if ref.path and ref.path == request.url.path:
        return None
    who = access.of_request(request)
    found = saved_filters.default_for(who, page, project["id"] if (project and page not in saved_filters.GLOBAL_PAGES) else None)
    if not found or not found["data"]:
        return None
    return RedirectResponse(path + "?" + urlencode(found["data"]), status_code=302)


@project_router.get("/list", response_class=HTMLResponse)
def list_page(request: Request, project: dict = ProjectDep):
    redirect = _saved_default_redirect(request, project, "list", f"{_base(project)}/list")
    if redirect:
        return redirect
    open_task_id, redir, open_task_project = _open_task_or_redirect(request, project, "/list")
    if redir:
        return redir
    return render(request, "pages/list.html", "list", open_task_id=open_task_id, open_task_project=open_task_project,
                  **_list_ctx(request, project))


@project_router.get("/tasks/list", response_class=HTMLResponse)
def list_partial(request: Request, project: dict = ProjectDep):
    """The List's table on its own: what it refetches after a change (mdd:board-stale)."""
    return partial(request, "partials/task_table.html", _list_ctx(request, project))


@project_router.get("/plan", include_in_schema=False)
def plan_page(request: Request, project: dict = ProjectDep):
    """The MoSCoW Plan board is retired (2026-10-01): old links land on the status Board (filters, ?open= kept)."""
    qs = request.url.query
    return RedirectResponse(f"{_base(project)}/board" + (f"?{qs}" if qs else ""), status_code=302)


@project_router.get("/tasks", include_in_schema=False)
def tasks_page(request: Request, project: dict = ProjectDep):
    """The old task page is the status Board now (same filters, ?open= kept)."""
    qs = request.url.query
    return RedirectResponse(f"{_base(project)}/board" + (f"?{qs}" if qs else ""), status_code=302)


@project_router.get("/overview", include_in_schema=False)
def project_overview(request: Request, project: dict = ProjectDep):
    """OVERVIEW was merged into Summary (#773); old links go there."""
    return RedirectResponse(_base(project), status_code=302)


def _runner_alloc(who, project: dict) -> dict | None:
    """The "Runners for this project" card: the account's runners the person may see (the owner sees all, anyone else their own) and the ticked ones."""
    if tree.is_account(project):
        return None
    account_id = tree.account_root_id(project["id"])
    if account_id is None:
        return None
    from app import devices
    # The instance owner sees every runner of the account; a project's Owner or Admin every runner of a person who works this project (the allocation
    # only NARROWS what may claim, it grants nothing); anyone else their own.
    admin = who.is_owner or access.is_admin(who, project["id"])
    devs = [d for d in devices.list_for(account_id, user_id=None if admin else who.user_id) if d["status"] == "active"]
    if admin and not who.is_owner:
        members: dict[int, bool] = {}
        devs = [d for d in devs if members.setdefault(d["user_id"], d["user_id"] == who.user_id
                                                      or access.role_in(access.principal_for_user_id(d["user_id"]), project["id"]) is not None)]
    from app import runner_ws
    return {"devices": [{**d, "connected": bool(runner_ws.connected(d["id"]))} for d in devs], "allocated": set(run_plan.allocated(project["id"])),
            "machines_url": f"/{repo.get_project(account_id)['slug']}/machines?tab=runners"}


@project_router.post("/settings/runners")
def save_project_runners(request: Request, device_id: list[int] = Form(default=[]), project: dict = ProjectDep):
    """Allocate runners to the project (the Owner or an Admin of it). Only runners of its account that the person may see; ticking none clears it."""
    who = access.of_request(request)
    if not access.is_admin(who, project["id"]):
        raise Forbidden("only the project's Owner or an Admin allocates its runners")
    ra = _runner_alloc(who, project)
    allowed = {d["id"] for d in (ra["devices"] if ra else [])}
    chosen = sorted(set(device_id) & allowed)
    with get_cursor() as cur:
        keep_hidden = [r["device_id"] for r in _hidden_alloc(cur, project["id"], allowed)]   # runners this person cannot see keep their allocation
        cur.execute("DELETE FROM project_runners WHERE project_id = %s AND device_id <> ALL(%s)", (project["id"], [*chosen, *keep_hidden]))
        for d in chosen:
            cur.execute("INSERT INTO project_runners (project_id, device_id, added_by) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                        (project["id"], d, f"user:{who.user_id}"))
        repo.record_activity(cur, project["id"], f"user:{who.user_id}", "set", "project_runners", project["id"],
                             f"Runners for this project: {len(chosen)} allocated" if chosen else "Runners for this project: none allocated (any runner of the starter)")
    resp = RedirectResponse(f"{_base(project)}/settings#runners", status_code=303)
    resp.set_cookie(FLASH_COOKIE, "Runners saved.", max_age=60, samesite="lax", httponly=True)
    return resp


def _hidden_alloc(cur, project_id: int, visible: set):
    cur.execute("SELECT device_id FROM project_runners WHERE project_id = %s AND device_id <> ALL(%s)", (project_id, list(visible)))
    return cur.fetchall()


@project_router.get("/settings", response_class=HTMLResponse)
def project_settings(request: Request, project: dict = ProjectDep):
    """SETTINGS (#733): where the project sits, its repo and environments, who works it, and the rename / move / external agent /
    danger zone forms (they were the foot of Overview). The creator or the owner (SLUG_MANAGE); the pill is the owner's."""
    who = access.of_request(request)
    idx = access.filtered_index(who)          # parents and children the person can't see are not named here (#713)
    place = {"ancestors": list(reversed(tree.ancestors(project, idx))), "children": tree.children(project["id"], idx),
             "environments": tree.list_environments(project["id"]),
             "worked_by": tree.session_holder(project["id"], idx)}
    subs = [dict(p, depth=p["depth"] - (tree.project_tree(include_archived=True, idx=idx) and 0))
            for p in tree.project_tree(include_archived=False, idx=idx) if p["id"] in tree.descendant_ids(project["id"], idx) and p["id"] != project["id"]]
    base_depth = min((s["depth"] for s in subs), default=0)
    subs = [dict(s, depth=s["depth"] - base_depth) for s in subs]
    return render(request, "pages/settings.html", "settings", place=place, subs=subs, runner_alloc=_runner_alloc(who, project),
                  move_targets=_move_targets(project, tree.index()) if who.is_owner else None, slug_locked=tree.slug_locked(project),
                  **_external_agent_ctx(project), **_connections_ctx(request, project, editor=True))


@project_router.get("/discussions", response_class=HTMLResponse)
def project_discussions(request: Request, project: dict = ProjectDep):
    """DISCUSSIONS (#733): the CA conversations linked to this project (a conversation's content is only its owner's: others see a soft link)."""
    from app import ca
    user = None
    try:
        user = web_auth.signed_in_user_row(request)
    except Exception:  # noqa: BLE001 - sign-in off: the list is empty, the page still opens
        user = None
    d = ca.discussions_of(user["id"], "project", project["slug"]) if user else {"discussions": [], "jobs": []}
    return render(request, "pages/discussions.html", "discussions", d=d, signed_in=bool(user))


def _task_home(project: dict, target_slug: str | None, who: "access.Principal | None" = None) -> dict:
    """Where a new task from this page goes: the page's project, or on an ACCOUNT page (accounts hold no tasks, task
    #148) the project picked from those under it. repo.create_task refuses an account too."""
    if not tree.is_account(project):
        return project
    if not target_slug:
        raise BadRequest(f"{project['name']} is an account: pick which of its projects the task is for")
    target = next((p for p in task_targets(project, who) if p["slug"] == target_slug), None)
    if target is None:
        raise BadRequest(f"pick one of {project['name']}'s projects for the task")
    return target


@project_router.post("/tasks", response_class=HTMLResponse)
def create_task_form(request: Request, title: str = Form(...), stream: str = Form(...),
                      priority: str = Form("should"), assignee: str = Form("ai"), target: str = Form(""),
                      project: dict = ProjectDep):
    """Board quick-add: a plain request (title only), for the AI by default. On an account: `target` = its project."""
    home = _task_home(project, target, access.of_request(request))
    repo.create_task(home["id"], title, "", priority, stream, [], {}, "owner", assignee=assignee)
    return _board_response(request, project["id"], _task_filters(request))


@project_router.get("/tasks/board", response_class=HTMLResponse)
def board_partial(request: Request, project: dict = ProjectDep):
    """The current view's fragment alone (?layout=focus|board|plan) — refetched when the drawer changes a task.
    ?layout=home (the global home's cross-account Focus) is never rendered under a project URL (task #368): it goes
    to its own global URL, /all/focus."""
    filters = _task_filters(request)
    if filters["layout"] == "home":
        return RedirectResponse("/all/focus", status_code=303)
    return _board_response(request, project["id"], filters)


@project_router.post("/tasks/{task_id}/assignee", response_class=HTMLResponse)
def set_task_assignee(request: Request, task_id: int, assignee: str = Form(...), project: dict = ProjectDep):
    _task_in(project, task_id)
    repo.assign_task(task_id, assignee, "owner")
    return _board_response(request, project["id"], _task_filters(request))


# --- new request (blank task) — from the board, a workflow, a step, a rule, or the ERP's prefill ------

_WF_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,120}$")
_STEP_ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,80}$")
MAX_CONDITIONS = 4000


def parse_subtask_lines(text: str | None) -> list[str]:
    return [line for line in (" ".join(l.split()) for l in (text or "").splitlines()) if line]


def parse_workflow_links(raw: str | None) -> list[dict]:
    """Hidden JSON field from the new-request form (possibly filled from an untrusted #prefill):
    a list of {id, step?} with slug-shaped strings only; anything else is dropped."""
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise BadRequest("workflow links aren't valid JSON") from exc
    out = []
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not _WF_ID_RE.match(item["id"]):
            continue
        step = item.get("step")
        entry = {"id": item["id"]}
        if isinstance(step, str) and _STEP_ID_RE.match(step):
            entry["step"] = step
        if entry not in out:
            out.append(entry)
    return out[:10]


def parse_conditions(raw: str | None):
    """The ERP's optional suggestion.conditions blob: any JSON value, bounded, kept in task links for
    the AI to read (never relayed, rendered escaped)."""
    if not raw:
        return None
    if len(raw) > MAX_CONDITIONS:
        raise BadRequest(f"conditions must be at most {MAX_CONDITIONS} characters of JSON")
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise BadRequest("conditions aren't valid JSON") from exc


def _new_request_links(project_id: int, workflows_json: str | None, conditions_json: str | None,
                       rule_id: str | None) -> dict:
    links: dict = {}
    workflows = parse_workflow_links(workflows_json)
    if rule_id:
        rule = rules_mod.get_rule(project_id, rule_id)
        links["rules"] = [rule["id"]]
        ws = rule["workflow_step"]
        if ws and ws["workflow"]:
            entry = {"id": ws["workflow"], **({"step": ws["step"]} if ws["step"] else {})}
            if entry not in workflows:
                workflows.append(entry)
    if workflows:
        links["workflows"] = workflows
    conditions = parse_conditions(conditions_json)
    if conditions is not None:
        links["conditions"] = conditions
    return links


@project_router.get("/tasks/new", response_class=HTMLResponse)
def new_task_page(request: Request, project: dict = ProjectDep):
    """Blank request form. `?workflow=&step=` / `?rule=` pre-link it server-side (validated); the ERP's
    `#prefill=<base64url JSON>` is read client-side only (static/js/prefill.js) and never reaches us
    until the owner submits the form."""
    q = request.query_params
    preset_links: list[dict] = []
    rule = None
    if q.get("rule"):
        rule = rules_mod.get_rule(project["id"], q["rule"])
        ws = rule["workflow_step"]
        if ws and ws["workflow"]:
            preset_links.append({"id": ws["workflow"], **({"step": ws["step"]} if ws["step"] else {})})
    elif q.get("workflow") and _WF_ID_RE.match(q["workflow"]):
        entry = {"id": q["workflow"]}
        if q.get("step") and _STEP_ID_RE.match(q["step"]):
            entry["step"] = q["step"]
        preset_links.append(entry)
    targets = task_targets(project, access.of_request(request)) if tree.is_account(project) else None
    return render(request, "pages/task_new.html", "board", preset_links=preset_links, rule=rule,
                  task_targets=targets, preset_target=q.get("target") or "")


@project_router.post("/tasks/new")
def create_new_request(request: Request, title: str = Form(...), body: str = Form(""),
                       subtasks: str = Form(""), stream: str = Form("feature"), assignee: str = Form("ai"),
                       priority: str = Form("should"), workflows: str = Form(""), conditions: str = Form(""),
                       rule: str = Form(""), target: str = Form(""), project: dict = ProjectDep):
    title = " ".join(title.split())
    if not title:
        raise BadRequest("say what you need in the title")
    home = _task_home(project, target, access.of_request(request))
    links = _new_request_links(home["id"], workflows, conditions, rule or None)
    task = repo.create_task(home["id"], title[:300], body, priority, stream, [], links, "owner",
                            assignee=assignee, subtasks=parse_subtask_lines(subtasks))
    if "application/json" in request.headers.get("accept", ""):      # the header's + Create dialog (#778): the same route, answered as data
        return JSONResponse({"id": task["id"], "title": task["title"], "project": home["slug"],
                             "url": f"/{home['slug']}/board?open={task['id']}"}, status_code=201)
    # An account's new task opens on the account's Board (it rolls the task up); a project's on its own.
    return RedirectResponse(f"{_base(project)}/board?open={task['id']}", status_code=303)


@project_router.post("/tasks/{task_id}/priority", response_class=HTMLResponse)
def set_task_priority(request: Request, task_id: int, priority: str = Form(...),
                       after_id: str = Form(None), end: str = Form(None), project: dict = ProjectDep):
    """Set a task's priority. A Board drag says where (after_id, or none = the top of the band). The List's bulk and inline changes send
    end=1: an unchanged priority is left alone, a real change goes to the END of the new band, so no task jumps to the top."""
    task = _task_in(project, task_id)
    after = int(after_id) if after_id and after_id.isdigit() else None
    filters = _task_filters(request)
    if end:
        if task["priority"] != priority:
            repo.reorder_task(task_id, priority, repo.band_tail(task["project_id"], priority, task_id), "owner")
        return _board_response(request, project["id"], filters)
    repo.reorder_task(task_id, priority, after, "owner", project_ids=_rollup_ids(request, project, filters))
    return _board_response(request, project["id"], filters)


@project_router.post("/tasks/{task_id}/status", response_class=HTMLResponse)
def set_task_status(request: Request, task_id: int, status: str = Form(...), after_id: str = Form(None),
                    top: str = Form(None), project: dict = ProjectDep):
    """Move a card to a status column. A drag also says WHERE in its priority band it landed: after_id (the
    card above it in that band) or top=1; neither = keep its rank."""
    before = _task_in(project, task_id)
    filters = _task_filters(request)
    row = repo.update_task(task_id, {"status": status}, "owner")
    if (after_id and after_id.isdigit()) or top:
        repo.reorder_task(task_id, row["priority"], int(after_id) if after_id and after_id.isdigit() else None, "owner",
                          project_ids=_rollup_ids(request, project, filters))
    finished = row["status"] == "done" and before["status"] != "done"
    return _board_response(request, project["id"], filters, celebrate_id=task_id if finished else None)


BOARD_STALE = {"HX-Trigger": "mdd:board-stale"}


def _task_ctx(request: Request, task_id: int, run_extra: dict | None = None) -> dict:
    task = repo.get_task(task_id)
    # Move targets: the active projects IN THE TASK'S OWN ACCOUNT (task #368: a task never moves across accounts;
    # repo.update_task refuses it too) — plus the task's own even if archived, so saving never moves it by accident.
    # Never an account: accounts hold no tasks (task #148).
    members = tree.account_member_ids(task["project_id"])
    projects = [p for p in repo.list_projects() if p["id"] in members
                and (not tree.is_account(p) or p["id"] == task["project_id"])]
    if task["project_id"] not in {p["id"] for p in projects}:
        projects = [repo.get_project(task["project_id"]), *projects]
    task_project = next((p for p in projects if p["id"] == task["project_id"]), None)
    run = {**_run_context(request, task, task_project), **(run_extra or {})}
    timeline = task_timeline.build(task, _viewer_id(request))
    summary = task_summary.view(task_id, _viewer_id(request))
    if summary["pending"]:
        task_summary.schedule(task_id, _viewer_id(request))   # debounced: many views and events make one refresh (a runner turn, else the auto summary)
    return {"task": task, "word_diff": word_diff, "projects": projects, "task_project": task_project, "run": run,
            "timeline": timeline, "code": task_timeline.code_line(task, task_project), "summary": summary,
            "waiting_on_you": bool(timeline["waiting_ids"]),
            "now_text": task_timeline.now_sentence(task, run, bool(timeline["waiting_ids"]))}


def _viewer_id(request: Request) -> int | None:
    """The signed-in person's user id (the Discussions in the timeline are theirs to open), or None."""
    if not getattr(request.state, "web_user", None):
        return None
    try:
        return web_auth.signed_in_user_row(request)["id"]
    except PermissionError:
        return None


def _on_full_page(request: Request, task_id: int) -> bool:
    """True when the htmx request came FROM the task's full page (/<slug>/tasks/<id>): its writes answer with the two-column layout."""
    path = urlsplit(request.headers.get("hx-current-url") or "").path.rstrip("/")
    return bool(re.search(rf"/tasks/{int(task_id)}$", path))


def _drawer(request: Request, task_id: int, changed: bool = False, run_extra: dict | None = None):
    return partial(request, "partials/task_drawer.html",
                   {**_task_ctx(request, task_id, run_extra), "full_page": _on_full_page(request, task_id)},
                   headers=BOARD_STALE if changed else None)


def _run_context(request: Request, task: dict, task_project: dict | None) -> dict:
    """What the drawer's "Run on a PC" block needs: the task's current job (state, device, branch), the profiles to pick from,
    and whether this viewer may start one (a real web session only; the project must have a git remote for the runner to
    clone)."""
    job = jobs_mod.latest_for_task(task["id"])
    signed_in = bool(getattr(request.state, "web_user", None))
    me = None
    if signed_in:
        try:
            me = web_auth.signed_in_user_row(request)["id"]
        except PermissionError:
            me = None
    return {"job": job, "card": job_card.view(job, task_project), "outcome": _outcome(task["id"], job, task_project), "profiles": jobs_mod.list_profiles(), "can_run": signed_in and access.can_edit(access.of_request(request), task["project_id"]),
            "is_starter": bool(job and me is not None and job["user_id"] == me),
            "client_text": jobs_mod.client_authored(task),
            "has_remote": bool(task_project and task_project.get("git_remote")),
            "auth_off": not web_auth.required(),
            "live": bool(job and job["state"] in jobs_mod.LIVE_STATES),
            # "Runs on" (#810): the runner choice in the Run form, and the offer for a job stuck behind an offline runner
            "runs_on": runs_on.options(task["project_id"], me) if me is not None and signed_in else None,
            "stuck": runs_on.stuck_offer(job) if job and me is not None and job["user_id"] == me else None,
            # an Installed project is tested on the runner the task is pinned to (#839)
            "server_reads": server_reads.for_job(job["id"]) if job else [],
            "model_uses": external_models.for_job(job["id"]) if job else [],
            "agent_files": _agent_files(request, task, job),
            "plan": _run_plan_line(task, me, job) if me is not None and signed_in and task_project and task_project.get("git_remote") else None,
            "can_start": task["status"] != "done" or bool(job and job["state"] == "cancelled"),
            "test_line": lives.run_line(task_project or {}, (job or {}).get("pin_device") or (job or {}).get("device") or "")}


def _agent_files(request: Request, task: dict, job: dict | None) -> dict | None:
    """The unreviewed-agent-files block for NOW (0.1.2): for a job held for them, or one that failed on them before 0.1.1, while the project still has
    a pending file. Nothing otherwise."""
    if not job:
        return None
    held = job["state"] == "queued" and bool(job.get("hold_reason"))
    old = job["state"] == "failed" and jobs_mod.is_unreviewed_release(job.get("summary") or "")
    if not (held or old):
        return None
    return agent_pins.trust_block(access.of_request(request), task["project_id"])


def _outcome(task_id: int, job: dict | None, project: dict | None) -> dict | None:
    """The outcome card, anchored on the latest BUILDER job (a review or a test run is a sub-result of it, never "the change"); with no builder job
    (only reviews so far) on the latest job."""
    if not job or job["state"] in jobs_mod.LIVE_STATES:
        return None
    anchor = jobs_mod.latest_builder_for_task(task_id) or job
    oc = job_card.outcome(anchor)
    if oc is not None:
        oc["followups"] = jobs_mod.followups_since(task_id, anchor["id"])
        oc["branch_url"] = job_card.branch_url((project or {}).get("git_remote"), oc["branch"]) if anchor["state"] == "done" else ""
    return oc


def _run_plan_line(task: dict, user_id: int, job: dict | None) -> dict | None:
    """The one line under the Run button ("Will run on <runner>") and the automatic choices behind it, only while there is something to run."""
    if job and job["state"] in jobs_mod.LIVE_STATES:
        return None
    try:
        return run_plan.plan(task, user_id)
    except NotFound:
        return None


def _checklist(request: Request, task_id: int):
    """Checklist writes swap only the checklist (unsaved title/description edits above it survive) and
    tell the board to refresh the card's progress."""
    task = repo.get_task(task_id)
    return partial(request, "partials/checklist.html", {"task": task, "word_diff": word_diff}, headers=BOARD_STALE)


@project_router.post("/tasks/{task_id}", response_class=HTMLResponse)
def update_task_form(request: Request, task_id: int, title: str = Form(None), body: str = Form(None),
                      priority: str = Form(None), status: str = Form(None), stream: str = Form(None),
                      assignee: str = Form(None), category: str = Form(None), move_to: str = Form(None),
                      client_visible: str = Form(None), project: dict = ProjectDep):
    _task_in(project, task_id)
    patch = {k: v for k, v in {"title": title, "body": body, "priority": priority, "status": status,
                                "stream": stream, "assignee": assignee, "category": category}.items()
             if v is not None}
    if client_visible is not None:
        # hidden "0" + checkbox "1": the last value wins, so a ticked box arrives as "1"
        patch["client_visible"] = client_visible == "1"
    if move_to and move_to != project["slug"]:
        # The person must be able to change the DESTINATION. One that is missing, hidden, archived or in another account reads
        # as the same 404 for anyone who cannot see it, so nothing tells them it exists.
        who = access.of_request(request)
        try:
            dest = repo.get_project_by_slug(move_to)
        except NotFound:
            dest = None
        if dest is None or not access.can_view(who, dest["id"]):
            raise NotFound(f"no project {move_to!r}")
        access.require_edit(who, dest)
        patch["project"] = dest["slug"]
    row = repo.update_task(task_id, patch, "owner")   # text actor = authority; the activity row also stores the real person (access.current_user_id)
    if row["project_id"] != project["id"]:
        # Moved out of this project: this view can't keep editing it — say where it went, link there.
        return partial(request, "partials/task_moved.html",
                       {"task": row, "target": repo.get_project(row["project_id"])}, headers=BOARD_STALE)
    return _drawer(request, task_id, changed=True)


@project_router.post("/tasks/{task_id}/autosave", response_class=HTMLResponse)
def autosave_task(request: Request, task_id: int, title: str = Form(None), body: str = Form(None), client_visible: str = Form(None),
                  project: dict = ProjectDep):
    """Edit in place (#91): the header's title, the Original request and the client toggle save by themselves. Answers with the small "Saved" tick
    only, so what you are typing is never replaced. The same rules as the form route (a client-visible title is the owner's alone)."""
    _task_in(project, task_id)
    patch: dict = {}
    if title is not None:
        title = " ".join(title.split())
        if not title:
            raise BadRequest("a task needs a title")
        patch["title"] = title[:300]
    if body is not None:
        patch["body"] = body
    if client_visible is not None:
        patch["client_visible"] = client_visible == "1"
    if patch:
        repo.update_task(task_id, patch, "owner")
    return partial(request, "partials/task_saved.html", {"task": {"id": task_id}, "saved": True}, headers=BOARD_STALE)


def _summary_block(request: Request, task_id: int):
    task = repo.get_task(task_id)
    summary = task_summary.view(task_id, _viewer_id(request))
    if summary["pending"]:
        task_summary.schedule(task_id, _viewer_id(request))
    return partial(request, "partials/task_summary.html", {"task": task, "summary": summary})


@project_router.get("/tasks/{task_id}/summary", response_class=HTMLResponse)
def task_summary_block(request: Request, task_id: int, project: dict = ProjectDep):
    """The summary block on its own (it re-fetches itself every few seconds while a refresh is pending)."""
    _task_in(project, task_id)
    return _summary_block(request, task_id)


@project_router.post("/tasks/{task_id}/summary", response_class=HTMLResponse)
def save_task_summary(request: Request, task_id: int, what: str = Form(""), done: str = Form(""), left: str = Form(""), project: dict = ProjectDep):
    """The owner's own three lines (shown until something new happens on the task)."""
    _task_in(project, task_id)
    task_summary.save_owner(task_id, what, done, left, _viewer_id(request))
    return _summary_block(request, task_id)


@project_router.post("/tasks/{task_id}/summary/restore", response_class=HTMLResponse)
def restore_task_summary(request: Request, task_id: int, project: dict = ProjectDep):
    """"Restore your version": the owner's text a newer AI summary replaced comes back."""
    _task_in(project, task_id)
    task_summary.restore_owner(task_id, _viewer_id(request))
    return _summary_block(request, task_id)


@project_router.post("/tasks/{task_id}/comments", response_class=HTMLResponse)
def add_comment_form(request: Request, task_id: int, body: str = Form(...), project: dict = ProjectDep):
    _task_in(project, task_id)
    repo.comment_task(task_id, body, "owner")
    return _drawer(request, task_id)


@project_router.post("/questions/{question_id}/answer", response_class=HTMLResponse)
def answer_question_form(request: Request, question_id: int, answer: str = Form(...), project: dict = ProjectDep):
    """The owner answers a session's question: a choice button or the free-text box, from the drawer or Focus
    (?layout=… = Focus: re-render it). The session is told with an ids-only outbox event."""
    q = repo.get_question(question_id)
    _task_in(project, q["task_id"])
    repo.answer_question(question_id, answer, _web_actor(request), "dashboard")
    if request.query_params.get("layout"):
        return _board_response(request, project["id"], _task_filters(request))
    return _drawer(request, q["task_id"], changed=True)


def _run_user(request: Request) -> dict:
    """The person starting or cancelling a job: a real Entra web session only (the same-origin guard already ran). A device or
    job token, or no session, is a 403."""
    try:
        return web_auth.signed_in_user_row(request)
    except PermissionError as exc:
        raise HTTPException(403, f"jobs are started by a signed-in person ({exc})") from None


@project_router.post("/tasks/{task_id}/run/trust-agent-files", response_class=HTMLResponse, dependencies=[Depends(access.require_fresh())])
def trust_agent_files_and_run(request: Request, task_id: int, snap: str = Form(""), sig: str = Form(""), project: dict = ProjectDep):
    """"Trust these files and run" (0.1.2): the project Owner pins every pending agent file of THIS project (agent_pins.pin's own rules, and the
    same fresh-factor step-up as the Machines page), which frees a held job; a job that already failed on them (before 0.1.1) is run afresh."""
    user = _run_user(request)
    task = _task_in(project, task_id)
    who = access.of_request(request)
    root = tree.account_root_id(task["project_id"]) or task["project_id"]
    job = jobs_mod.latest_for_task(task_id)
    old = bool(job and job["state"] == "failed" and jobs_mod.is_unreviewed_release(job.get("summary") or ""))
    try:
        agent_pins.trust_project(who, user["id"], task["project_id"], root, snap, sig)
    except agent_pins.ListChanged as exc:     # something changed since the page was drawn: nothing pinned, the block is drawn again with the new list
        return _drawer(request, task_id, run_extra={"trust_notice": str(exc)})
    if old:
        return run_task_form(request, task_id, profile="", labels="", unattended=None, runner="", queue_offline="", make_default="", details="", project=project)
    return _drawer(request, task_id, changed=True)


@project_router.post("/tasks/{task_id}/run", response_class=HTMLResponse)
def run_task_form(request: Request, task_id: int, profile: str = Form(""), labels: str = Form(""),
                  unattended: str = Form(None), runner: str = Form(""), queue_offline: str = Form(""),
                  make_default: str = Form(""), details: str = Form(""), project: dict = ProjectDep):
    """"Run" (change #90): queue a job of this task for the signed-in person. Nothing needs choosing: the PROFILE comes from the task and the
    RUNNER from the project's allocation and what is online (app/run_plan.py); both can be overridden under "More options" (`profile`, `runner`).
    The starter's own click is their approval, so a subscription runner of theirs can take it; an unattended job (API runners) needs a profile that
    allows it and never a client-authored task. The project needs a git remote (the runner clones it). `details` = words for the AI, added to the
    task as a comment the agent reads.

    A runner chosen by hand ("Runs on", #810): `runner` is a runner id or 'any'; if it is offline and another of the person's could take the job now,
    nothing is queued: the form offers "Run on <other>" or "Queue for <pinned>" (queue_offline=1), with a tick to make the other the project's
    default (make_default=1). A job is never moved by itself."""
    user = _run_user(request)
    task = _task_in(project, task_id)
    if not project.get("git_remote"):
        raise BadRequest("set the project's git remote first (project Overview, Edit repo and environments)")
    rp = run_plan.plan(task, user["id"], profile, runner, labels)
    if not rp["profile"]:
        raise BadRequest(rp["why"])
    if rp.get("refuse"):
        raise BadRequest(rp["message"])      # nobody's allocated runner could ever claim it: say so instead of queueing it for ever
    pin, requires = rp["pin"], rp["requires"]
    again = {"profile": rp["profile"], "labels": labels, "unattended": unattended == "1"}
    manual = runner.strip() not in ("", "auto", "default")
    if manual:
        # A runner that lacks a label the job needs could never claim it: never pin silently. Say which label, and which runners have it.
        gap = runs_on.label_gap(task["project_id"], user["id"], pin, requires)
        if gap:
            return _drawer(request, task_id, run_extra={"gap": {**gap, **again}})
        if pin is not None and queue_offline != "1":
            offer = runs_on.offline_offer(task["project_id"], user["id"], pin, requires)
            if offer:
                return _drawer(request, task_id, run_extra={"offer": {**offer, **again}})
        if make_default == "1" and pin is not None:
            runs_on.set_pref(task["project_id"], user["id"], pin, f"user:{user['id']}")
    if details.strip():
        repo.comment_task(task_id, "For the AI, on the next run: " + details.strip()[:2000], _web_actor(request))
    jobs_mod.create_job(task["id"], user["id"], rp["profile"], unattended=unattended == "1", approved=True,
                        extra_labels=labels, pin_device_id=pin, base_branch=jobs_mod.carry_branch(task_id))   # Run again carries the last build's commits
    return _drawer(request, task_id, changed=True)


def _follow_up(request: Request, project: dict, task_id: int, profile: str, comment: str):
    """A next step that is itself a job (a review, the full tests): a comment saying what to do, then a job with that profile. With no such profile
    on this instance the comment stands alone as a request on the task."""
    user = _run_user(request)
    task = _task_in(project, task_id)
    repo.comment_task(task_id, comment, _web_actor(request))
    if not project.get("git_remote") or not any(p["name"] == profile for p in jobs_mod.list_profiles()):
        return _drawer(request, task_id, changed=True)
    rp = run_plan.plan(task, user["id"], profile)
    jobs_mod.create_job(task["id"], user["id"], profile, approved=True, pin_device_id=rp["pin"], base_branch=jobs_mod.carry_branch(task_id))
    return _drawer(request, task_id, changed=True)


@project_router.post("/tasks/{task_id}/run/review", response_class=HTMLResponse)
def run_review(request: Request, task_id: int, project: dict = ProjectDep):
    """Next step "Review the change": a code-auditor job on the branch (or, with no reviewer profile, a review request on the task)."""
    job = jobs_mod.latest_builder_for_task(task_id)
    return _follow_up(request, project, task_id, "code-auditor",
                      f"Please review the change on branch {job['branch'] if job else '(see the last job)'}: correctness and security. Report, do not fix.")


@project_router.post("/tasks/{task_id}/run/tests", response_class=HTMLResponse)
def run_tests(request: Request, task_id: int, project: dict = ProjectDep):
    job = jobs_mod.latest_builder_for_task(task_id)
    return _follow_up(request, project, task_id, "chores",
                      f"Please run the full test suite on branch {job['branch'] if job else '(see the last job)'} and report the result. Do not change code.")


@project_router.post("/tasks/{task_id}/run/done", response_class=HTMLResponse)
def run_mark_done(request: Request, task_id: int, project: dict = ProjectDep):
    _run_user(request)
    task = _task_in(project, task_id)
    if not access.can_edit(access.of_request(request), task["project_id"]):
        raise Forbidden("you can read this task but not change it")
    job = jobs_mod.latest_builder_for_task(task_id) or jobs_mod.latest_for_task(task_id)
    repo.update_task(task_id, {"status": "done"}, _web_actor(request))
    if job:   # the person's own words only: the agent's summary is shown on the card and never stored under a person's name
        repo.comment_task(task_id, f"Marked done from the last run (job {job['id']}).", _web_actor(request))
    return _drawer(request, task_id, changed=True)


@project_router.post("/tasks/{task_id}/run/needs-work", response_class=HTMLResponse)
def run_needs_work(request: Request, task_id: int, words: str = Form(""), project: dict = ProjectDep):
    """The owner says it is not fixed (the card's box): the task is "Needs more work" with their words attached, ready for Run again."""
    _run_user(request)
    _task_in(project, task_id)
    jobs_mod.mark_needs_work(task_id, words, _web_actor(request))
    return _drawer(request, task_id, changed=True)


@project_router.get("/tasks/{task_id}/run/card", response_class=HTMLResponse)
def run_card(request: Request, task_id: int, was: str = "", project: dict = ProjectDep):
    """The job card's status on its own (#987): the card polls this while the job is live (every 3 s, every 15 s while held or queued over 2 minutes),
    with a few small queries (no task context, no runner lists). When the job has just ended (the poll said `was=live`), the whole task record is
    sent once instead, so the outcome card and the Run form appear without a reload."""
    _task_in(project, task_id)
    job = jobs_mod.latest_for_task(task_id)
    if not job:
        return HTMLResponse("", status_code=204)
    live = job["state"] in jobs_mod.LIVE_STATES
    if was == "live" and not live:
        return partial(request, "partials/task_drawer.html", {**_task_ctx(request, task_id), "full_page": _on_full_page(request, task_id)},
                       headers={**BOARD_STALE, "HX-Retarget": "#drawer-content", "HX-Reswap": "innerHTML"})
    me = None
    if getattr(request.state, "web_user", None):
        try:
            me = web_auth.signed_in_user_row(request)["id"]
        except PermissionError:
            me = None
    run = {"job": job, "card": job_card.view(job, project), "live": live,
           "can_run": me is not None and access.can_edit(access.of_request(request), project["id"]),
           "is_starter": me is not None and job["user_id"] == me,
           "stuck": runs_on.stuck_offer(job) if me is not None and job["user_id"] == me else None,
           "server_reads": server_reads.for_job(job["id"]), "model_uses": external_models.for_job(job["id"])}
    return partial(request, "partials/job_card_status.html", {"task": {"id": task_id}, "job": job, "run": run})


@project_router.post("/tasks/{task_id}/run/move", response_class=HTMLResponse)
def move_run_form(request: Request, task_id: int, device_id: int = Form(...), project: dict = ProjectDep):
    """A queued job stuck behind an offline runner: its starter moves it to another of their runners (their click)."""
    user = _run_user(request)
    _task_in(project, task_id)
    job = jobs_mod.latest_for_task(task_id)
    if not job or job["user_id"] != user["id"]:
        raise NotFound("no job of yours on this task")
    runs_on.repin(job["id"], user["id"], device_id)
    return _drawer(request, task_id, changed=True)


@project_router.post("/runs-on")
def set_runs_on(request: Request, device_id: str = Form(""), next: str = Form(""), project: dict = ProjectDep):
    """The header pill's choice: the signed-in person's runner for this project ('' = any of my runners)."""
    user = _run_user(request)
    try:
        picked = int(device_id) if device_id.strip() else None
        runs_on.set_pref(project["id"], user["id"], picked, f"user:{user['id']}")
    except ValueError:
        raise BadRequest("pick one of your runners, or any") from None
    if picked is not None:       # moving a project to a runner checks that runner's environment first (#810 step 6); a warning, never a block
        environment.request_check(project["id"], picked, user["id"])
    resp = RedirectResponse(safe_local_path(next, f"/{project['slug']}"), status_code=303)
    resp.set_cookie(FLASH_COOKIE, "Runs on saved.", max_age=60, samesite="lax", httponly=True)
    return resp


@project_router.post("/runs-on/label")
def add_runs_on_label(request: Request, device_id: int = Form(...), label: str = Form(...), next: str = Form(""),
                      project: dict = ProjectDep):
    """The labels guard's "Add the label": add one label a profile asks for to the person's own runner (the runner can't label itself)."""
    from app import devices
    user = _run_user(request)
    label = label.strip().lower()
    if label not in runs_on.profile_labels():
        raise BadRequest("that is not a label any profile asks for")
    account_id = tree.account_root_id(project["id"])
    mine = {d["id"]: d for d in runs_on.my_devices(account_id, user["id"])}
    if device_id not in mine:
        raise NotFound(f"runner {device_id} not found")
    devices.set_labels(account_id, device_id, [*mine[device_id]["labels"], label], f"user:{user['id']}", user_id=user["id"])
    resp = RedirectResponse(safe_local_path(next, f"/{project['slug']}"), status_code=303)
    resp.set_cookie(FLASH_COOKIE, "Label added.", max_age=60, samesite="lax", httponly=True)
    return resp


@project_router.post("/tasks/{task_id}/run/cancel", response_class=HTMLResponse)
def cancel_run_form(request: Request, task_id: int, project: dict = ProjectDep):
    """Cancel the task's live job (its starter only): the lease ends cleanly and the runner's next heartbeat tells it to stop."""
    user = _run_user(request)
    _task_in(project, task_id)
    job = jobs_mod.latest_for_task(task_id)
    if not job or job["state"] not in jobs_mod.LIVE_STATES:
        raise BadRequest("this task has no live job to cancel")
    jobs_mod.cancel_job(tree.account_root_id(project["id"]), job["id"], user["id"])
    return _drawer(request, task_id, changed=True)


@project_router.get("/tasks/{task_id}/panel", response_class=HTMLResponse)
def task_panel(request: Request, task_id: int, project: dict = ProjectDep):
    """The task for THE TASK SHEET (owner 11-18: tasks always open full page, in a window over the current page): always the full-page
    layout. The sheet then puts the task's URL in the address bar, so the writes inside it answer with the same layout (_on_full_page)."""
    _task_in(project, task_id)
    return partial(request, "partials/task_drawer.html", {**_task_ctx(request, task_id), "full_page": True})


@project_router.get("/tasks/{task_id:int}", response_class=HTMLResponse)
def task_page(request: Request, task_id: int, project: dict = ProjectDep):
    """THE TASK'S FULL PAGE (#816): the drawer's record on a page of its own, with its own URL, so a link (the assistant's, a message, a
    bookmark) opens it without a drawer that hides what you were doing. A task of another project the person can see redirects to that
    project's page; one they can't is the same 404 as a missing one."""
    task = repo.get_task(task_id)
    if task["project_id"] != project["id"]:
        found = repo.locate_task(task_id)       # only what this person may see
        if found and not tree.is_account(project):
            return RedirectResponse(f"/{found['project_slug']}/tasks/{task_id}", status_code=302)
        raise NotFound(f"task {task_id} is not in {project['name']}")
    return render(request, "pages/task_page.html", "board", **_task_ctx(request, task_id), full_page=True)


@router.get("/all/tasks/{ref}/page")
def task_page_by_number(request: Request, ref: str):
    """/all/tasks/123/page: the task's full page in whichever project it lives (what the assistant's "task #123" links use)."""
    number = repo.task_number(ref)
    found = repo.locate_task(number) if number else None
    if found:
        return RedirectResponse(f"/{found['project_slug']}/tasks/{found['id']}", status_code=302)
    request.state.project = _current_project(request)
    resp = render(request, "pages/task_missing.html", "home", number=number, ref=ref[:40])
    resp.status_code = 404
    return resp


# --- the checklist (owner) ------------------------------------------------------------------------

@project_router.post("/tasks/{task_id}/subtasks", response_class=HTMLResponse)
def add_subtask_form(request: Request, task_id: int, text: str = Form(...), project: dict = ProjectDep):
    _task_in(project, task_id)
    repo.add_subtasks(task_id, parse_subtask_lines(text) or [text], "owner")
    return _checklist(request, task_id)


@project_router.post("/subtasks/{subtask_id}/toggle", response_class=HTMLResponse)
def toggle_subtask_form(request: Request, subtask_id: int, project: dict = ProjectDep):
    sub = _subtask_in(project, subtask_id)
    repo.update_subtask(subtask_id, "owner", done=not sub["done"])
    return _checklist(request, sub["task_id"])


@project_router.post("/subtasks/{subtask_id}/edit", response_class=HTMLResponse)
def edit_subtask_form(request: Request, subtask_id: int, text: str = Form(...), project: dict = ProjectDep):
    sub = _subtask_in(project, subtask_id)
    repo.update_subtask(subtask_id, "owner", text=text)
    return _checklist(request, sub["task_id"])


@project_router.post("/subtasks/{subtask_id}/delete", response_class=HTMLResponse)
def delete_subtask_form(request: Request, subtask_id: int, project: dict = ProjectDep):
    sub = _subtask_in(project, subtask_id)
    repo.delete_subtask(subtask_id, "owner")
    return _checklist(request, sub["task_id"])


@project_router.post("/tasks/{task_id}/subtasks/reorder", response_class=HTMLResponse)
def reorder_subtasks_form(request: Request, task_id: int, order: str = Form(...), project: dict = ProjectDep):
    _task_in(project, task_id)
    try:
        ids = [int(x) for x in order.split(",") if x.strip()]
    except ValueError as exc:
        raise BadRequest("order must be a list of step ids") from exc
    repo.reorder_subtasks(task_id, ids, "owner")
    return _checklist(request, task_id)


@project_router.post("/subtasks/{subtask_id}/proposal/{decision}", response_class=HTMLResponse)
def resolve_one_proposal(request: Request, subtask_id: int, decision: str, project: dict = ProjectDep):
    if decision not in ("accept", "reject"):
        raise NotFound("no such decision")
    _subtask_in(project, subtask_id)
    task = repo.resolve_proposals("owner", accept=decision == "accept", subtask_id=subtask_id)
    return _checklist(request, task["id"])


@project_router.post("/tasks/{task_id}/proposals/{decision}", response_class=HTMLResponse)
def resolve_all_proposals(request: Request, task_id: int, decision: str, project: dict = ProjectDep):
    if decision not in ("accept-all", "reject-all"):
        raise NotFound("no such decision")
    _task_in(project, task_id)
    repo.resolve_proposals("owner", accept=decision == "accept-all", task_id=task_id)
    return _checklist(request, task_id)


# --- Claude's triage challenge: owner responds with Accept / Keep mine / Argue -------------------

def _triage_response(request: Request, project: dict, task_id: int):
    """From a Plan card: the refreshed board. From the drawer (?from=drawer): the drawer, and every view
    refreshes itself (mdd:board-stale)."""
    if request.query_params.get("from") == "drawer":
        return _drawer(request, task_id, changed=True)
    return _board_response(request, project["id"], _task_filters(request))


@project_router.post("/tasks/{task_id}/triage/accept", response_class=HTMLResponse)
def triage_accept(request: Request, task_id: int, project: dict = ProjectDep):
    _task_in(project, task_id)
    repo.accept_triage(task_id, "owner")
    return _triage_response(request, project, task_id)


@project_router.post("/tasks/{task_id}/triage/override", response_class=HTMLResponse)
def triage_override(request: Request, task_id: int, project: dict = ProjectDep):
    _task_in(project, task_id)
    repo.override_triage(task_id, "owner")
    return _triage_response(request, project, task_id)


@project_router.post("/tasks/{task_id}/triage/discuss", response_class=HTMLResponse)
def triage_discuss(request: Request, task_id: int, project: dict = ProjectDep):
    _task_in(project, task_id)
    repo.discuss_triage(task_id, "owner")
    return _triage_response(request, project, task_id)


# =================================================================================================
# workflows
# =================================================================================================

@project_router.get("/workflows", response_class=HTMLResponse)
def workflows_page(request: Request, project: dict = ProjectDep):
    q = request.query_params
    group = q.get("group") or None
    search = q.get("q") or None
    ai_only = q.get("ai_only") == "1"
    trigger = q.get("trigger") if q.get("trigger") in config.TRIGGER_META else ""
    impact = q.get("impact") if q.get("impact") in config.IMPACT_META else ""
    workflows = repo.list_workflows(project["id"], group=group, q=search, ai_only=ai_only,
                                    trigger=trigger or None, impact=impact or None)
    scan = repo.latest_scan(project["id"])
    grouped: dict = {}
    for wf in workflows:
        grouped.setdefault(wf["group"], []).append(wf)
    return render(request, "pages/workflows_list.html", "workflows",
                  grouped=grouped, group=group or "", search=search or "", ai_only=ai_only,
                  trigger=trigger, impact=impact, TRIGGER_META=config.TRIGGER_META,
                  IMPACT_META=config.IMPACT_META, scan=scan)


@project_router.get("/workflows/{workflow_id}", response_class=HTMLResponse)
def workflow_detail(request: Request, workflow_id: str, project: dict = ProjectDep):
    wf = repo.get_workflow(project["id"], workflow_id)
    q = request.query_params
    failed_step = q.get("failed_step")
    error = q.get("error")
    tasks = repo.attach_progress(wf["tasks"])
    tasks_by_step: dict = {}
    open_tasks: dict = {}
    for t in tasks:
        for link in (t["links"] or {}).get("workflows", []):
            if link.get("id") == workflow_id:
                key = link.get("step") or "__workflow__"
                tasks_by_step.setdefault(key, []).append(t)
                if t["status"] != "done":
                    open_tasks[key] = open_tasks.get(key, 0) + 1
    rule_counts = rules_mod.step_counts(project["id"], workflow_id)
    skill_by_step = skills_mod.skills_by_step(project["id"], workflow_id)
    path = [p for p in (q.get("path") or "").split(",") if _STEP_ID_RE.match(p)][:200]
    wf_data = {
        "run": {"failedStep": failed_step, "error": (error or "")[:500] or None, "path": path or None},
        "extras": {"base": _base(project), "tasks": True, "openTasks": open_tasks, "ruleCounts": rule_counts,
                   "skills": skill_by_step},
    }
    step_labels = {"__trigger__": "Trigger", **{s["id"]: s["label"] for s in wf["steps"]}}
    scan = repo.latest_scan(project["id"])
    canvas_data = workflow_canvas_data(project, wf, wf_data)
    return render(request, "pages/workflow_detail.html", "workflows",
                  wf=wf, tasks=tasks, canvas_data=canvas_data, failed_step=failed_step, error=error,
                  tasks_by_step=tasks_by_step, step_labels=step_labels, rule_counts=rule_counts,
                  skill_by_step=skill_by_step, scan=scan,
                  has_trigger_step=any(s.get("kind") == "trigger" for s in wf["steps"]))


def workflow_canvas_data(project: dict, wf: dict, extra: dict) -> dict:
    """What the shared canvas draws for one workflow (docs/canvas-contract.md, kind=workflow): the catalog workflow as
    scanned, the slice of the integrations export its steps use, plus Crewboss's run view and task extras."""
    workflow = {k: v for k, v in wf.items() if k not in ("changes", "tasks", "stale", "broken")}
    doc = integrations_mod.latest(project["id"])
    return {"workflow": workflow, "integrations": integrations_mod.subset_for_workflow(wf, doc), **extra}


@project_router.post("/workflows/{workflow_id}/tasks")
def create_task_from_flow(request: Request, workflow_id: str, title: str = Form(...),
                           body: str = Form(""), priority: str = Form("should"),
                           stream: str = Form(...), step: str = Form(None), assignee: str = Form("ai"),
                           subtasks: str = Form(""), project: dict = ProjectDep):
    """A plain request from a workflow or one of its step cards, pre-linked to it."""
    repo.get_workflow(project["id"], workflow_id)  # 404 for an unknown workflow
    if step and not _STEP_ID_RE.match(step):
        raise BadRequest("unknown step")
    task = repo.create_task(project["id"], title, body, priority, stream, [], {}, "owner", assignee=assignee,
                            subtasks=parse_subtask_lines(subtasks))
    repo.link_task_workflow(task["id"], workflow_id, step or None, "owner")
    suffix = f"?step={step}#tasks" if step else "#tasks"
    return RedirectResponse(f"{_base(project)}/workflows/{workflow_id}{suffix}", status_code=303)


@project_router.post("/workflows/{workflow_id}/link")
def link_existing_task_to_flow(request: Request, workflow_id: str, task_id: int = Form(...),
                                step: str = Form(None), project: dict = ProjectDep):
    _task_in(project, task_id)
    repo.link_task_workflow(task_id, workflow_id, step or None, "owner")
    return RedirectResponse(f"{_base(project)}/workflows/{workflow_id}", status_code=303)


# =================================================================================================
# Integrations (#393): the ERP's graded links to other systems, read from its export via the scanner
# =================================================================================================

@project_router.get("/integrations", response_class=HTMLResponse)
def integrations_page(request: Request, project: dict = ProjectDep):
    """Summary by grade, one diagram per module (the shared canvas), then the list — filterable by module, grade and
    kind; each row anchored `i-<id>`. Empty state for a project whose latest scan carries no export."""
    q = request.query_params
    srcs = integrations_mod.sources(project)
    doc = integrations_mod.merged(srcs)
    own = dict(srcs).get(project["slug"])   # workflows resolve against the project's own list
    rows = list((doc or {}).get("integrations") or [])
    modules = integrations_mod.modules(rows)
    module = q.get("module") if q.get("module") in {m for m, _ in modules} else ""
    grade = q.get("grade") if q.get("grade") in integrations_mod.GRADES else ""
    kinds = sorted({r["kind"] for r in rows})
    kind = q.get("kind") if q.get("kind") in kinds else ""
    shown = [r for r in rows if (not module or r["module"] == module) and (not grade or r["grade"] == grade)
             and (not kind or r["kind"] == kind)]
    used_by: dict[str, list[dict]] = {}
    scan = repo.latest_scan(project["id"])
    for wf in ((scan["catalog"] or {}).get("workflows") or []) if (scan and own) else []:
        for step in wf.get("steps") or []:
            for iid in integrations_mod.resolve_step(step, own):
                entry = {"workflow": wf["id"], "name": wf.get("name", wf["id"])}
                if entry not in used_by.setdefault(iid, []):
                    used_by[iid].append(entry)
    params = {"module": module, "grade": grade, "kind": kind}

    def href(**ov):
        merged = {**params, **ov}
        qs = urlencode({k: v for k, v in merged.items() if v})
        return f"{_base(project)}/integrations" + (f"?{qs}" if qs else "")

    return render(request, "pages/integrations.html", "integrations", doc=doc, rows=rows, shown=shown,
                  modules=modules, module_names=(doc or {}).get("modules") or {}, module=module, grade=grade,
                  kind=kind, kinds=kinds, href=href,
                  summary=integrations_mod.summary(rows), vocab=(doc or {}).get("vocab") or {}, used_by=used_by,
                  scan=scan, GRADES=integrations_mod.GRADES, **_connections_ctx(request, project))


@project_router.get("/integrations.json")
def integrations_json(project: dict = ProjectDep):
    """The canvas's data-src for kind=integrations: {"integrations": <export over every source>} — or null for a
    project without one."""
    return JSONResponse({"integrations": integrations_mod.merged(integrations_mod.sources(project))},
                        headers={"Cache-Control": "no-cache"})


# =================================================================================================
# App AI features (was AI Skills) + Rules & AI Instructions (read-only inventories)
# =================================================================================================

def _shared_page(request: Request, project: dict, kind: str, nav: str):
    """An ACCOUNT's Rules or AI Skills page (task #148): the authored items it shares with every project under it."""
    q = request.query_params
    open_id = int(q["open"]) if (q.get("open") or "").isdigit() else None
    show_archived = q.get("archived") == "1"
    entries = repo.list_knowledge(project["id"], kind=kind, include_archived=show_archived)
    return render(request, "pages/account_shared.html", nav, kind=kind, entries=entries, open_id=open_id,
                  show_archived=show_archived, requests=repo.open_knowledge_requests(project["id"]),
                  projects=task_targets(project, access.of_request(request)), asked=int(q["asked"]) if (q.get("asked") or "").isdigit() else None)


@project_router.get("/skills")
def skills_old_url(request: Request, project: dict = ProjectDep):
    """The page that was called AI Skills is App AI features (#718); "Skills" is now the build skills page at /build-skills."""
    qs = request.url.query
    return RedirectResponse(f"/{project['slug']}/ai-features" + (f"?{qs}" if qs else ""), status_code=308)


@project_router.get("/ai-features", response_class=HTMLResponse)
def ai_features_page(request: Request, project: dict = ProjectDep):
    if tree.is_account(project):
        return _shared_page(request, project, "skill", "ai_features")
    q = request.query_params
    subproject = q.get("subproject") or ""
    shared = q.get("shared") == "1"  # (the "used by 2+ workflows" filter, not account sharing)
    skills = skills_mod.list_skills(project["id"])
    subprojects = sorted({sp for s in skills for sp in s["subprojects"]})
    if subproject:
        skills = [s for s in skills if subproject in s["subprojects"]]
    if shared:
        skills = [s for s in skills if len(s["used_by"]) > 1]
    open_id = q.get("open") or ""
    selected = next((s for s in skills if s["id"] == open_id), None)
    all_rules = rules_mod.all_rules(project["id"])[0]
    instructions = {s["id"]: [r for r in all_rules if r["skill"] == s["id"]] for s in skills}
    return render(request, "pages/skills.html", "ai_features", skills=skills, selected=selected,
                  subprojects=subprojects, subproject=subproject, shared=shared, instructions=instructions,
                  inherited=shared_mod.inherited(project))


@project_router.get("/rules", response_class=HTMLResponse)
def rules_page(request: Request, project: dict = ProjectDep):
    if tree.is_account(project):
        return _shared_page(request, project, "rule", "rules")
    q = request.query_params
    kind = q.get("kind") if q.get("kind") in rules_mod.VALID_KIND else ""
    show_disabled = q.get("disabled") == "1"
    workflow = q.get("workflow") or ""
    step = q.get("step") or ""
    skill = q.get("skill") or ""
    rows, origin = rules_mod.list_rules(project["id"], kind=kind or None, show_disabled=show_disabled,
                                        workflow=workflow or None, step=step or None, skill=skill or None)
    skills = {s["id"]: s for s in skills_mod.list_skills(project["id"])}
    params = {"kind": kind, "disabled": "1" if show_disabled else "", "workflow": workflow, "step": step,
              "skill": skill}
    base = _base(project)

    def href(**ov):
        merged = {**params, **ov}
        qs = urlencode({k: v for k, v in merged.items() if v})
        return f"{base}/rules" + (f"?{qs}" if qs else "")

    return render(request, "pages/rules.html", "rules", rules=rows, origin=origin, kind=kind,
                  show_disabled=show_disabled, workflow=workflow, step=step, skill=skill, skills=skills,
                  href=href, kind_label=rules_mod.KIND_LABEL, inherited=shared_mod.inherited(project))


# =================================================================================================
# knowledge + objectives: app/routers/knowledge_pages.py
# =================================================================================================


# =================================================================================================
# activity
# =================================================================================================

@project_router.get("/activity", response_class=HTMLResponse)
def activity_page(request: Request, project: dict = ProjectDep):
    q = request.query_params
    actor = q.get("actor") or None
    entity = q.get("entity") or None
    entries = repo.recent_activity(project["id"], actor=actor, entity=entity, limit=200)
    return render(request, "pages/activity.html", "activity", entries=entries,
                  actor=actor or "", entity=entity or "")


# =================================================================================================
# system health (placeholder)
# =================================================================================================

HEALTH_VIEWS = ("overview", "ai-calls")


def _int_param(value: str | None) -> int | None:
    return int(value) if value and value.isdigit() else None


@project_router.get("/health", response_class=HTMLResponse)
def health_page(request: Request, project: dict = ProjectDep):
    """Zabbix problems, drift (#402) + the ERP health feed (task #196) for the health project (the health role); other
    projects have no source yet. ?view=ai-calls is the ERP's AI call log, paged live with ?before=<id>&limit=<n>."""
    is_health = roles.is_role(project, "health")
    view = request.query_params.get("view", "overview")
    view = view if view in HEALTH_VIEWS else "overview"
    h = erp = calls = drift_s = reports = keys = None
    if is_health and view == "ai-calls":
        q = request.query_params
        calls = erp_health.ai_calls_page(_int_param(q.get("before")),
                                         _int_param(q.get("limit")) or erp_health.AI_CALLS_DEFAULT_LIMIT)
    elif is_health:
        h, erp, drift_s, reports = health.summary(), erp_health.summary(), drift.summary(), health_reports.summary()
        keys = platform_keys.reminders()     # platform key rotation reminders (#562), every store
    return render(request, "pages/health.html", "health", h=h, erp=erp, calls=calls, view=view, drift=drift_s,
                  reports=reports, keys=keys,
                  is_health=is_health, SEVERITY=health.SEVERITY, SEVERITY_CLASSES=health.SEVERITY_CLASSES,
                  health_project=roles.slug("health"), fmt=erp_health.fmt)


@project_router.post("/health/erp/events/{event_id}/resolve", response_class=HTMLResponse)
def erp_event_resolve(request: Request, event_id: int, project: dict = ProjectDep):
    """The Resolve button on an ERP event: the ERP does the write (health:resolve token); the dashboard only asks, and
    only for an event open in the current fresh snapshot (anything else: 404, nothing sent). htmx gets the row back
    (resolved, or with the error under the button) or, on a 404, the house toast; a plain form post gets a redirect +
    flash."""
    if not roles.is_role(project, "health"):
        raise NotFound("no ERP health feed on this project")
    event = next((e for e in erp_health.summary()["events"] if e["id"] == event_id), None)  # before it's pruned
    result = erp_health.resolve_event(event_id)
    htmx = is_htmx(request)
    if result.get("status") == 404 or event is None:
        msg = result.get("error") or erp_health.NOT_OPEN
        headers = {"HX-Trigger": json.dumps({"mdd:flash": {"message": msg, "tone": "error"}})} if htmx else None
        return PlainTextResponse(msg, status_code=404, headers=headers)
    if htmx and result["ok"]:
        return fold_response(f"ERP event {event_id} resolved.")      # the row folds away (#582); the 60s refresh agrees
    if htmx:   # the ERP refused: the row comes back with the reason under it
        return partial(request, "partials/erp_event_row.html",
                       {"e": event, "result": result, "SEVERITY_CLASSES": health.SEVERITY_CLASSES})
    resp = RedirectResponse(f"{_base(project)}/health", status_code=303)
    msg = f"ERP event {event_id} resolved." if result["ok"] else result["error"]
    resp.set_cookie(FLASH_COOKIE, _cookie_safe(msg), max_age=60, samesite="lax", httponly=True)
    return resp


_PUNCT_FOLD = str.maketrans({"\u2014": "-", "\u2013": "-", "\u2018": "'", "\u2019": "'", "\u201c": '"',
                             "\u201d": '"', "\u2026": "...", "\u00a0": " "})


def _cookie_safe(text: str, limit: int = 300) -> str:
    """A flash cookie value must survive a latin-1 header: fold typographic punctuation and accents to ASCII and drop
    anything else (an ERP error detail can carry an em-dash or any other character)."""
    folded = unicodedata.normalize("NFKD", (text or "").translate(_PUNCT_FOLD))
    return folded.encode("ascii", "ignore").decode("ascii")[:limit]


# =================================================================================================
# the Change log (task #134): Recommended → Decided → Done
# =================================================================================================

@project_router.get("/changes", response_class=HTMLResponse)
def changes_page(request: Request, project: dict = ProjectDep):
    """One project's Change log, or an account's: every project under it, grouped the same way (task #148)."""
    if tree.is_account(project):
        ids = sorted(_descendants(request, project["id"]))
        return render(request, "pages/changes.html", "changes", g=changes_mod.grouped(None, project_ids=ids),
                      rollup=project)
    return render(request, "pages/changes.html", "changes", g=changes_mod.grouped(project["id"]))


@project_router.get("/changes/{change_id:int}", response_class=HTMLResponse)
def change_page(request: Request, change_id: int, project: dict = ProjectDep):
    """A change's FULL PAGE (#816): its own URL, so the assistant's link opens it in the main area."""
    row = changes_mod.get(change_id)
    if row["project_id"] != project["id"]:
        raise NotFound(f"change {change_id} not found")
    return render(request, "pages/change_page.html", "changes", c=dict(row, project_slug=project["slug"], project_name=project["name"]))


@project_router.post("/changes/{change_id}/decide")
def decide_change_form(request: Request, change_id: int, decision: str = Form(...), note: str = Form(""),
                       project: dict = ProjectDep):
    """The owner's decision (same-origin guard: the browser IS the owner). Tells the project's session, ids only."""
    row = changes_mod.get(change_id)
    if row["project_id"] != project["id"]:  # card forms post to the change's own project, from any page
        raise NotFound(f"change {change_id} not found")
    decided = changes_mod.decide(change_id, decision, _web_actor(request), note=note)
    if is_htmx(request) and re.search(rf"/changes/{int(change_id)}$", urlsplit(request.headers.get("hx-current-url") or "").path.rstrip("/")):
        # From the change's own full page (#816): the card is replaced by the decided view, not folded away to an empty page.
        return partial(request, "partials/change_decided_view.html",
                       {"c": dict(decided, project_slug=project["slug"], project_name=project["name"])})
    msg = f"Change #{change_id} {decision}." + (f" {decided['override_note']}" if decided.get("override_note") else "")
    return fold_or_redirect(request, msg,
                            _referer_path(request, f"{_base(project)}/changes"),
                            inserts=_change_moves(request, project, decided, row))


def _change_moves(request: Request, project: dict, row: dict, before: dict) -> list[str]:
    """On the Changes page a decided card also LANDS in its next list (Decided, or Done for a rejection) — the card
    folds out of Waiting and its row folds into place, with no reload. From Focus the card only leaves.
    An override of a chat decision that CHANGES the call (approve to reject, or "changed") replaces its existing row:
    the old Decided/Done row folds away and the re-rendered one folds into the right list. Acknowledging, or overriding
    with the same call, keeps the row and only the buttons fold (the caller returns no inserts)."""
    if not is_htmx(request):
        return []
    replace = before["stage"] != "recommended"
    if replace and before["decision"] == row["decision"] and before.get("decision_note") == row.get("decision_note"):
        return []
    parts = urlsplit(request.headers.get("hx-current-url") or "").path.strip("/").split("/")
    if len(parts) != 2 or parts[1] != "changes":
        return []
    c = dict(row, project_slug=project["slug"], project_name=project["name"])
    closed = row["decision"] == "rejected" or row["stage"] == "done"   # a made change stays in Done whatever the call
    html = templates.get_template("partials/change_done_row.html" if closed else "partials/change_decided_row.html").render(
        c=c, is_rollup=parts[0] != project["slug"])
    out = [insert_block("#changes-done-list" if closed else "#changes-decided-list", html, "afterbegin")]
    if replace:
        out += [remove_block(f'[data-fold="dec:{row["id"]}"]'), remove_block(f'[data-fold="done:{row["id"]}"]')]
    return out


@project_router.post("/changes/{change_id}/acknowledge")
def acknowledge_change_form(request: Request, change_id: int, project: dict = ProjectDep):
    """The owner has seen a decision a session recorded in chat (task #581); confirms an unverified one. Same-origin.
    htmx (#582): the "Decided in chat" row folds away in place; without JS it is the 303 + flash."""
    row = changes_mod.get(change_id)
    if row["project_id"] != project["id"]:
        raise NotFound(f"change {change_id} not found")
    changes_mod.acknowledge(change_id, _web_actor(request))
    return fold_or_redirect(request, f"Change #{change_id} acknowledged.", _referer_path(request, f"{_base(project)}/changes"))


# =================================================================================================
# the project's place in the tree + its environments (task #132), owner only
# =================================================================================================

@project_router.post("/details")
def project_details_form(request: Request, repo_name: str = Form("", alias="repo"), repo_path: str = Form(""),
                         dev_prod_sync: str = Form(None), git_remote: str = Form(None), project: dict = ProjectDep):
    tree.set_details(project["id"], "owner", repo_name=repo_name, repo_path=repo_path,
                     dev_prod_sync=dev_prod_sync == "1", git_remote=git_remote)
    resp = RedirectResponse(f"{_base(project)}/settings", status_code=303)
    resp.set_cookie(FLASH_COOKIE, "Project details saved.", max_age=60, samesite="lax", httponly=True)
    return resp


@project_router.post("/move")
def move_project_form(request: Request, parent_id: str = Form(""), project: dict = ProjectDep):
    """From the Move project block on the Overview / account page. tree.set_place validates the move (cycle-safe,
    account + root + archived-parent guards); a refusal becomes a flash via handle_repo_error, never a 500. On
    success the slug is unchanged, so the owner lands back on the same page with a confirmation and the structural
    move is logged in the project's Change log as a done entry (task #166)."""
    pid = int(parent_id) if parent_id.strip() else None
    tree.set_place(project["id"], "owner", pid)
    where = repo.get_project(pid)["name"] if pid else "the top level"
    changes_mod.record_structural(project["id"], f"Moved to {where}", "owner",
                                  recommendation=f"Moved {project['name']} under {where} in the project tree.")
    resp = RedirectResponse(_referer_path(request, f"{_base(project)}/settings"), status_code=303)
    resp.set_cookie(FLASH_COOKIE, f"Moved {project['name']} to {where}.", max_age=60, samesite="lax", httponly=True)
    return resp


def _external_agent_ctx(project: dict, token: str | None = None) -> dict:
    """What partials/external_agent.html needs. None for an account and the dashboard root (they can't be external)."""
    if tree.is_account(project) or roles.is_role(project, "overseer"):
        return {"external": None}
    on = bool(project.get("external_agent"))
    return {"external": {"on": on, "tokens": tree.list_external_tokens(project["id"]) if on else [],
                         "has_tmux": bool(project.get("session_roles")), "url": config.PUBLIC_URL}}


@project_router.post("/external-agent")
def external_agent_form(request: Request, on: str = Form(""), project: dict = ProjectDep):
    """From the 'Worked by an external agent' block on the Overview (#574). Owner only, behind the same-origin guard,
    as Move/Rename; tree.set_external_agent validates (exclusive with the project's tmux sessions) and a refusal comes
    back as a flash. Turning it off also revokes the project's external tokens."""
    want = on == "1"
    tree.set_external_agent(project["id"], "owner", want)
    if want != bool(project.get("external_agent")):
        changes_mod.record_structural(
            project["id"], "Worked by an external agent" if want else "No longer worked by an external agent", "owner",
            recommendation=("Marked " + project["name"] + " as worked by an external agent (a Claude Code session off "
                            "the sessions server): own outbox, no tmux session.") if want else
            ("Cleared the external agent of " + project["name"] + "; its tokens were revoked."))
    resp = RedirectResponse(f"{_base(project)}/settings", status_code=303)
    resp.set_cookie(FLASH_COOKIE, "External agent turned on." if want else "External agent turned off; its tokens were revoked.",
                    max_age=60, samesite="lax", httponly=True)
    return resp


@project_router.post("/external-agent/token", response_class=HTMLResponse, dependencies=[Depends(access.require_fresh())])
def external_agent_token_form(request: Request, project: dict = ProjectDep):
    """Issue the external agent's token. The token is shown ONCE, on the page this POST returns (no redirect, no
    cookie, no flash, no activity text, Cache-Control: no-store); the database keeps only its hash."""
    token = tree.issue_external_token(project["id"], "owner")
    resp = render(request, "pages/external_token.html", "settings", token=token, dash_url=config.PUBLIC_URL)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@project_router.post("/external-agent/revoke")
def external_agent_revoke_form(request: Request, token_id: str = Form(""), project: dict = ProjectDep):
    """Revoke ONE token (token_id) or, with none, all of the project's external tokens."""
    tid = int(token_id) if token_id.strip().isdigit() else None
    if token_id.strip() and tid is None:
        raise BadRequest("bad token id")
    n = tree.revoke_external_tokens(project["id"], "owner", token_id=tid)
    resp = RedirectResponse(f"{_base(project)}/settings", status_code=303)
    resp.set_cookie(FLASH_COOKIE, f"Revoked {n} token{'s' if n != 1 else ''}." if n else "No token to revoke.",
                    max_age=60, samesite="lax", httponly=True)
    return resp


@project_router.post("/rename")
def rename_project_form(request: Request, name: str = Form(""), new_slug: str = Form("", alias="slug"),
                        project: dict = ProjectDep):
    """From the Rename block on the Overview / account page (#577). Owner only, behind the same-origin guard, as Move;
    tree.rename_project validates (slug format, unique against current AND old slugs, settings-pinned slugs, no slug
    change while the project runs its own session) and a refusal comes back as a flash. A new slug keeps the old one
    as an alias, so the owner lands on the same page at the new slug and every old link still redirects. The short
    name is never changed here (or anywhere): session tokens are filed under it."""
    row = tree.rename_project(project["id"], "owner", name, slug=new_slug)
    if row["name"] != project["name"] or row["slug"] != project["slug"]:
        what = f"Renamed {project['name']} to {row['name']}" if row["name"] != project["name"] \
            else f"Renamed {project['name']}"
        detail = f"slug now {row['slug']}, was {project['slug']}" if row["slug"] != project["slug"] else ""
        title = what + (f" ({detail})" if detail else "")
        changes_mod.record_structural(project["id"], title, "owner",
                                      recommendation=f"{what}."
                                      + (f" Old links to /{project['slug']} redirect to /{row['slug']}."
                                         if row["slug"] != project["slug"] else ""))
        msg = title + "."
    else:
        msg = "Nothing to change."
    back = _referer_path(request, f"{_base(project)}/settings")
    if row["slug"] != project["slug"] and (back == _base(project) or back.startswith(_base(project) + "/")
                                           or back.startswith(_base(project) + "?")):
        back = _base(row) + back[len(_base(project)):]
    resp = RedirectResponse(back, status_code=303)
    resp.set_cookie(FLASH_COOKIE, _cookie_safe(msg), max_age=60, samesite="lax", httponly=True)
    if request.cookies.get(CURRENT_PROJECT_COOKIE) == project["slug"]:
        resp.set_cookie(CURRENT_PROJECT_COOKIE, row["slug"], max_age=60 * 60 * 24 * 365, samesite="lax")
    return resp


@project_router.post("/environments/{env}")
def project_environment_form(request: Request, env: str, server: str = Form(""), path: str = Form(""),
                             url: str = Form(""), deploy_method: str = Form(""), zabbix_host: str = Form(""),
                             remove: str = Form(None), project: dict = ProjectDep):
    if remove == "1":
        tree.delete_environment(project["id"], env, "owner")
    else:
        if server and server not in config.KNOWN_SERVERS:
            raise BadRequest(f"unknown server {server!r}")
        tree.set_environment(project["id"], env, "owner", server=server, path=path, url=url,
                             deploy_method=deploy_method, zabbix_host=zabbix_host)
    resp = RedirectResponse(f"{_base(project)}/settings", status_code=303)
    resp.set_cookie(FLASH_COOKIE, f"Environment {env} {'removed' if remove == '1' else 'saved'}.", max_age=60,
                    samesite="lax", httponly=True)
    return resp
