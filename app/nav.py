"""The menu: ONE fixed definition, drawn the same on every page (task #733, change #80).

The ERP's rule, which the owner asked for here: the left menu never changes. Three rows (Now, Dashboard, Recent) and two collapsible
sections (Projects, Accounts: the live top-level records the person may see, #771), the same on every page; only the
highlight moves. Everything that used to be a menu row by level (Focus, Board, Changes ...) is a PILL on the record
page (routers/pages.py nav_items, partials/record_head.html). The profile menu (avatar: the foot of the drawer on a
desktop, the top-left avatar on a phone) holds Machines.

Nothing in the templates builds a menu by hand: base.html reads these tuples, so a row added here appears in the
drawer and the phone bar, and the test (tests/test_nav_fixed.py) pins that it is identical wherever you are.
"""
from __future__ import annotations

from app import nav_icons

# key, label, href, icon (Material Symbols name, app/nav_icons.py), phone label
AREAS = (
    {"key": "now", "label": "Now", "href": "/", "icon": "home", "short": "Now"},
    {"key": "dashboard", "label": "Dashboard", "href": "/dashboard", "icon": "dashboard", "short": "Dashboard"},
    {"key": "recent", "label": "Recent", "href": "/recent", "icon": "history", "short": "Recent"},
)

# The ADMINISTRATION group at the foot of the left menu (#771, owner 11-12): the owner's pages about machines and people, not about a project.
# Drawn for the instance owner only; the routes are the gate as well (/servers and /admin/users refuse anyone else; /runners lists only the
# devices a person enrolled themselves, so a designer still reaches their OWN runner from the profile menu, see menu()).
ADMIN = (
    {"key": "machines", "label": "Machines", "href": "/machines", "needs_user": False},
    {"key": "users", "label": "Users", "href": "/admin/users", "needs_user": True},       # the #713 Users page: local sign-in accounts only
)

# THE APP SWITCHER (#778, owner 11-12: "like Jira ... Build (the Crewboss interface), Assets, and Administration"). The header's grid icon opens it; the
# header then names the section you are in. Each section owns its LEFT MENU (build: the fixed rows above; assets and admin: the lists below).
# `owner` sections are drawn for the instance owner only and their routes refuse everyone else.
SECTIONS = (
    {"key": "build", "label": "Build", "href": "/", "icon": "view_kanban", "owner": False, "blurb": "Projects, tasks and the board"},
    {"key": "assets", "label": "Assets", "href": "/assets", "icon": "perm_media", "owner": False, "blurb": "Shared skills, rules and materials"},
    {"key": "admin", "label": "Administration", "href": "/admin", "icon": "key", "owner": True, "blurb": "Users, runners and servers"},
)

# nav_active (what a page passes to render()) -> the section it belongs to and the row to light in that section's menu.
ADMIN_ACTIVE = {"admin_home": "overview", "admin": "users", "machines": "machines", "keys": "keys", "front_door": "front_door"}
ASSETS_ACTIVE = {"assets": "foryou", "assets_recent": "recent", "assets_skills": "skills", "assets_rules": "rules", "assets_materials": "materials"}

# The phone bar's slots, in order: the assistant is the round button in the exact middle (the ERP's #634), the four
# rows around it; "menu" is a button that opens the left drawer (the Projects and Accounts lists live there, #771). "ca" is the
# assistant. There is no More tab: every page is either one of these, a pill on a record, or a row of the avatar's menu.
BAR_SLOTS = ("now", "dashboard", "ca", "recent", "menu")

# nav_active (what a page passes to render()) -> the menu row that is highlighted. Anything absent highlights nothing
# (the profile pages, the user's security page).
NAV_AREA = {"home": "now", "dashboard": "dashboard", "recent": "recent"}


def landing(project: dict) -> str:
    """Where a link to a record goes: the record's landing page."""
    return f"/{project['slug']}"


def records(who, idx: dict | None = None, archived: bool = False) -> tuple[list[dict], list[dict]]:
    """(projects, accounts) for the drawer's two sections (#771): LIVE records only (archived=True: the ARCHIVED ones, which the
    section's "Show archived" toggle reveals), that the person may see. Projects = EVERY project, sub-projects included, A to Z (owner 11-16:
    "all projects be on the list and we just mark as favorite the ones we are currently working on"; the flat model of #784). Accounts in tree order."""
    from app import access, tree
    idx = idx if idx is not None else access.filtered_index(who)
    projects, accounts = [], []
    for r in tree.project_tree(include_archived=True, idx=idx):
        if tree.is_global_root(r) or bool(r["archived_at"]) != archived:
            continue
        if tree.is_account(r):
            accounts.append(r)
            continue
        projects.append(r)
    projects.sort(key=lambda r: (r.get("name") or r.get("slug") or "").lower())
    return projects, accounts


def owning_account_id(r: dict, idx: dict) -> int | None:
    """The nearest account above a project in the person's index (the group it is listed under), or None."""
    from app import tree
    return next((a["id"] for a in tree.ancestors(r, idx) if tree.is_account(a)), None)


def account_groups(who, idx: dict | None = None, current_id: int | None = None, live: tuple | None = None, archived: tuple | None = None) -> dict:
    """THE MENU's one "Accounts" section (#778, owner 11-12): each account the person can see is a group holding ALL its projects, flat, A to Z (owner 11-16:
    sub-projects too, under the account above them). {groups: [{account, projects, current}], other: [projects that sit under no (live) account], archived_count}. A project the
    person cannot see is never listed (records() works from their filtered index); an account with no visible projects still shows. Archived rows ride along
    with is_archived, hidden until the section's "Show archived". `current` = the group holds the page you are on (always unfolded)."""
    from app import access, tree
    idx = idx if idx is not None else access.filtered_index(who)
    live_p, live_a = live if live is not None else records(who, idx)               # the caller may pass the lists it already built
    arch_p, arch_a = archived if archived is not None else records(who, idx, archived=True)
    groups = {a["id"]: {"account": a, "projects": [], "archived": False} for a in live_a}
    for a in arch_a:
        groups[a["id"]] = {"account": a, "projects": [], "archived": True}
    other: list[dict] = []
    for r in live_p + arch_p:
        g = groups.get(owning_account_id(r, idx))
        if g is not None and not (g["archived"] and not r["archived_at"]):
            g["projects"].append(r)
        else:
            other.append(r)
    out = list(groups.values())
    for g in out:
        g["current"] = current_id is not None and (g["account"]["id"] == current_id or any(p["id"] == current_id for p in g["projects"]))
    n_arch = sum(1 for g in out if g["archived"]) + sum(1 for g in out for p in g["projects"] if p["archived_at"]) + sum(1 for p in other if p["archived_at"])
    return {"groups": out, "other": other, "archived_count": n_arch}


def current_record_id(project: dict | None, projects: list[dict], accounts: list[dict], idx: dict) -> int | None:
    """The menu row to light for the page's record: the record itself when it is listed, else its nearest listed ancestor."""
    if project is None:
        return None
    from app import tree
    listed = {r["id"] for r in projects} | {r["id"] for r in accounts}
    for node in (project, *tree.ancestors(project, idx)):
        if node["id"] in listed:
            return node["id"]
    return None


def areas() -> list[dict]:
    return [dict(a, icon_d=nav_icons.PATHS.get(a["icon"], "")) for a in AREAS]


def admin(is_owner: bool, has_account: bool) -> list[dict]:
    """The Administration rows this person gets: all of them for the owner (Users only for a signed-in account, as the page needs one), none for anyone else."""
    if not is_owner:
        return []
    return [dict(m) for m in ADMIN if has_account or not m["needs_user"]]


def menu(is_owner: bool) -> list[dict]:
    """The PROFILE menu's page rows: none for the owner (Runners, Servers and Users are the Administration group now); a non-owner keeps
    Runners there, since their own runner is theirs to manage and they have no Administration group."""
    return [] if is_owner else [dict(m) for m in ADMIN if m["key"] == "machines"]


def menu_slot_icon() -> str:
    return nav_icons.PATHS["menu"]


def bar() -> list[str]:
    return list(BAR_SLOTS)


def area_for(nav_active: str, project: dict | None, is_account: bool) -> str:
    """The highlighted fixed row (Now, Dashboard, Recent). A record page lights none of them; the profile pages light nothing."""
    if nav_active == "machines":
        return ""
    if project is not None and nav_active != "home":
        return ""        # a record's pages light the record's own row in the Projects / Accounts sections (#771)
    return NAV_AREA.get(nav_active, "")


def initials(name: str | None) -> str:
    """Two letters for the avatar: first letters of the first and last word of a name (or the first two of a one-word name / an address)."""
    words = [w for w in (name or "").replace("@", " ").replace(".", " ").split() if w]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][:2].upper()
    return (words[0][0] + words[-1][0]).upper()


def web_auth_local() -> bool:
    from app import web_auth
    return web_auth.mode() == "local"


def sections(is_owner: bool) -> list[dict]:
    """The switcher's entries for this person: Administration only for the instance owner."""
    return [dict(x, icon_d=nav_icons.PATHS.get(x["icon"], "")) for x in SECTIONS if is_owner or not x["owner"]]


def section_for(nav_active: str, is_owner: bool) -> str:
    """The section a page belongs to. Runners, Servers and Platform keys are the owner's Administration pages; a designer's own Runners
    page (they have no Administration) stays in Build."""
    if nav_active in ASSETS_ACTIVE:
        return "assets"
    if is_owner and nav_active in ADMIN_ACTIVE:
        return "admin"
    return "build"


def section_menu(section: str, nav_active: str, is_owner: bool, has_account: bool) -> list[dict]:
    """The left menu of the Administration and Assets sections: [{key, label, href, icon_d, active, group}]. Build's menu is the fixed one."""
    if section == "admin":
        if not is_owner:
            return []
        rows = [("overview", "Overview", "/admin", "dashboard")]
        if has_account:
            rows.append(("users", "Users", "/admin/users", "person"))
        rows += [("machines", "Machines", "/machines", "computer")]
        if web_auth_local():
            rows.append(("front_door", "Front door", "/admin/front-door", "key"))
        from app import config, platform_keys
        if config.PLATFORM_KEY_STORES and platform_keys.home_slug():
            rows.append(("keys", "Platform keys", platform_keys.page_path(), "key"))
        cur = ADMIN_ACTIVE.get(nav_active, "")
        return [{"key": k, "label": lbl, "href": h, "icon_d": nav_icons.PATHS.get(i, ""), "active": k == cur, "group": ""} for k, lbl, h, i in rows]
    if section == "assets":
        rows = [("foryou", "For you", "/assets", "home", ""), ("recent", "Recent", "/assets/recent", "history", ""),
                ("skills", "Skills", "/assets/skills", "smart_toy", "Asset types"),
                ("rules", "Rules & AI instructions", "/assets/rules", "rule", "Asset types"),
                ("materials", "Materials", "/assets/materials", "perm_media", "Asset types")]
        cur = ASSETS_ACTIVE.get(nav_active, "")
        return [{"key": k, "label": lbl, "href": h, "icon_d": nav_icons.PATHS.get(i, ""), "active": k == cur, "group": g} for k, lbl, h, i, g in rows]
    return []


def create_mode(section: str, is_owner: bool, local_sign_in: bool) -> str:
    """What the header's Create pill does: 'task' (Build; the dialog), 'invite' (Administration, when the #713 invite flow exists, i.e. local
    sign-in), else 'none' (Assets until assets can be created: TODO #778)."""
    if section == "build":
        return "task"
    if section == "admin" and is_owner and local_sign_in:
        return "invite"
    return "none"
