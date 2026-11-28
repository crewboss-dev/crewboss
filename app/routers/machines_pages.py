"""Machines page (#810): /<account>/machines, ONE page for what used to be Servers and Runners. A machine can do two jobs:

  Runs jobs        a runner (device_tokens): the machine connects OUT; an agent writes and tests code in a throw-away worktree and
                   pushes a branch. It holds no SSH material, and a job can only ASK for a deploy.
  Receives deploys a server (commissioned_servers): Crewboss connects IN with a one-hour, single-command pass, after your OK, and runs
                   `git pull --ff-only` then a restart command.

The two roles stay separate blocks with their own trust line on one card (the security separation shows), and `device_tokens.server_id`
joins "this runner" and "that server" into one card when a person says they are the same machine (never automatically). /servers and
/runners redirect here (?role=deploys / ?role=jobs) so old links keep working.

Visibility is #713's, unchanged: the owner sees everything; a designer sees their own runners and the servers assigned to them, read-only.
The POSTs stay where they were (/servers/..., /runners/...: the forms are the same) and redirect back here; this module also holds the
join / rename routes and the one place that builds the page (`render_page`), which the others call to re-show a rejected form."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Form, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, RedirectResponse

from app import access, agent_pins, config, deploys, devices, gemini_keys, health_reports, host_signal, jobs, runner_ws, saved_filters, server_access, repo, tree
from app.db import get_cursor
from app.repo import BadRequest, Conflict, NotFound
from app.routers.pages import FLASH_COOKIE, ProjectDep, _cookie_safe, render
from app.routers.runners_pages import _account, _only_mine, _user

log = logging.getLogger("crewboss.machines")
project_router = APIRouter(prefix="/{slug}")

ROLES = ("all", "jobs", "deploys", "attention")
# The tabs (#838) replace the jobs/deploys role filter: ?role=jobs and ?role=deploys still arrive (old links, saved filters, notifications)
# and simply pick the tab. What is left to filter on, inside a tab, is "all" or "needs attention".
TABS = ("runners", "servers")
ROLE_TAB = {"jobs": "runners", "deploys": "servers"}
ROLE_LABELS = {"all": "All", "attention": "Needs attention"}
LIVE = ("queued", "running")
RECENT = 20


def _assigned_server_ids(user_id: int | None) -> set[int]:
    """The servers the owner pinned to this person (a designer sees only those, read-only)."""
    if user_id is None:
        return set()
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT DISTINCT server_id FROM server_assignments WHERE user_id = %s", (user_id,))
        return {r["server_id"] for r in cur.fetchall()}


def _signal(c: dict, visible, feeds: dict) -> dict | None:
    """The server's host signal (app/host_signal.py), over the projects the viewer can see only: a failure to read it must
    never break the page."""
    try:
        return host_signal.summary_for(c, visible_ids=visible, feeds=feeds)
    except Exception:    # noqa: BLE001
        log.warning("host signal for server %s", c.get("id"), exc_info=True)
        return None


def _manifest_chip(server_id: int) -> dict | None:
    """The server card's "Manifest & backups" line (#726): never generated / current / changed since, and how many apps declare no state.
    A failure never breaks the page."""
    from app import manifest
    try:
        st = manifest.status(server_id)
        return {"state": st["state"], "generated_at": st["stored"]["generated_at"] if st["stored"] else None, "apps": len(st["fresh"]["apps"]),
                "undeclared": len(st["fresh"]["summary"]["undeclared"])}
    except Exception:    # noqa: BLE001
        log.warning("manifest status for server %s", server_id, exc_info=True)
        return None


def pick_tab(tab: str, role: str, can_enrol: bool, owner: bool) -> str:
    """The tab to show: an explicit ?tab=, else the one a legacy ?role= names, else Runners (Servers for an owner who cannot enrol a runner)."""
    if tab in TABS:
        return tab
    if role in ROLE_TAB:
        return ROLE_TAB[role]
    return "servers" if (owner and not can_enrol) else "runners"


def build(request: Request, account: dict, role: str = "all", tab: str = "") -> dict:
    """Everything the page draws: one entry per MACHINE ({name, server, devices, suggest, attention}), the jobs split into live and
    recent, the orphaned locks, and the label suggestions."""
    who = access.of_request(request)
    owner = who.is_owner
    try:
        user = _user(request)
    except Exception:    # noqa: BLE001 - sign-in off: the owner browser still sees the machines, it just can't enrol a runner
        user = None
    mine = _only_mine(request, user) if user else (None if owner else -1)
    devs = [{**d, "connected": d.get("status") == "active" and runner_ws.connected(d["id"]),
             "attention": runner_ws.attention(d["id"]) if d.get("status") == "active" else [],
             "findings": runner_ws.findings(d["id"]) if d.get("status") == "active" else [],
             "capabilities": runner_ws.capabilities(d["id"]) if d.get("status") == "active" else {}}
            for d in devices.list_for(account["id"], user_id=mine)]
    all_jobs = jobs.list_for(account["id"], limit=200, user_id=mine if mine != -1 else -1)
    live_by_device = {j["device_id"]: j for j in all_jobs if j["state"] == "running" and j["device_id"]}
    default_for: dict[int, list[str]] = {}
    with get_cursor(commit=False) as cur:           # "Default for: app-one, app-two" (Runs on, #810): the projects whose saved runner this is
        cur.execute("SELECT r.device_id, p.slug FROM project_runner_prefs r JOIN projects p ON p.id = r.project_id "
                    "WHERE r.device_id = ANY(%s) ORDER BY p.slug", ([d["id"] for d in devs],))
        for r in cur.fetchall():
            default_for.setdefault(r["device_id"], []).append(r["slug"])
    for d in devs:
        d["current_job"] = live_by_device.get(d["id"])
        d["default_for"] = default_for.get(d["id"], [])
    cs = server_access.page_model(account) if (owner or _assigned_server_ids(who.user_id)) else {"servers": [], "ca_error": "", "via_options": []}
    by_server: dict[int, list] = {}
    for t in deploys.list_targets(account["id"]):
        by_server.setdefault(t["server_id"], []).append(t)
    servers = cs["servers"]
    if not owner:
        keep = _assigned_server_ids(who.user_id)
        servers = [c for c in servers if c["id"] in keep]
        for c in servers:                               # read-only: never the paste line or the setup commands
            c.update(line="", setup=[], via_options=[], readonly=True)
    visible = access.visible_ids(who)                  # None = the owner: every project
    feeds = health_reports.feeds()                     # health_feeds.yaml, read once per page
    for c in servers:
        # a designer sees only the "Used by" chips of projects they may see (a chip names the project)
        c["targets"] = [t for t in by_server.get(c["id"], []) if owner or access.can_view(who, t["project_id"])]
        c.setdefault("readonly", False)
        c["signal"] = _signal(c, visible, feeds)
        c["manifest"] = None if c["readonly"] else _manifest_chip(c["id"])
    machines: dict[str, dict] = {}
    for c in servers:
        machines[f"s{c['id']}"] = {"key": f"s{c['id']}", "name": c["name"], "server": c, "devices": [], "suggest": None}
    by_id = {c["id"]: c for c in servers}
    for d in devs:
        if d.get("server_id") in by_id:
            machines[f"s{d['server_id']}"]["devices"].append(d)
        else:
            sug = devices.suggest_server(d, servers) if (owner and d["status"] == "active") else None
            machines[f"d{d['id']}"] = {"key": f"d{d['id']}", "name": d["name"], "server": None, "devices": [d], "suggest": sug}
    for m in machines.values():
        sv = m["server"]
        m["att_server"] = bool(sv and sv["state"] != "ready")
        m["att_runner"] = any(d["attention"] for d in m["devices"])
        m["attention"] = m["att_server"] or m["att_runner"]
    filt = "attention" if role == "attention" else "all"
    tab = pick_tab(tab, role, can_enrol=user is not None, owner=owner)
    in_tab = [m for m in machines.values() if (m["devices"] if tab == "runners" else m["server"])]
    att = "att_runner" if tab == "runners" else "att_server"       # "Needs attention" is about the role this tab shows
    shown = [m for m in in_tab if filt == "all" or m[att]]
    free_servers = [m["server"] for m in machines.values() if m["server"] and not m["devices"]] if owner else []
    free_runners = [d for d in devs if d["status"] == "active" and not d.get("server_id")] if owner else []
    for j in all_jobs:
        j["elapsed"] = _elapsed(j)
    live = [j for j in all_jobs if j["state"] in LIVE]
    agent_cards = []
    if tab == "runners":       # only the Runners tab shows the Owner's review of unpinned agent / command / skill files (#829): no query on the others
        try:
            agent_cards = agent_pins.card(who, account["id"])
        except Exception:    # noqa: BLE001 - a failure here must never break the page
            log.warning("agent file review card", exc_info=True)
    return {"agent_cards": agent_cards, "machines": shown, "total": len(in_tab), "all_total": len(machines), "cs": cs, "devices": devs, "role": filt, "tab": tab,
            "free_servers": free_servers, "free_runners": free_runners,
            "counts": {"runners": sum(1 for m in machines.values() if m["devices"]), "servers": sum(1 for m in machines.values() if m["server"]),
                       "attention": sum(1 for m in in_tab if m[att])},
            "live_jobs": live, "recent_jobs": [j for j in all_jobs if j["state"] not in LIVE][:RECENT],
            "locks": [l for l in jobs.list_locks(account["id"]) if l["state"] == "orphaned"] if owner else [],
            "can_enrol": user is not None, "is_owner": owner, "me_id": user["id"] if user else None,
            "label_hints": _label_hints()}


def _elapsed(j: dict) -> str:
    """"12 min" since a job started (claimed, else queued), or how long it took once it ended."""
    start = j.get("claimed_at") or j["created_at"]
    end = j.get("finished_at") or datetime.now(timezone.utc)
    mins = max(0, int((end - start).total_seconds() // 60))
    return f"{mins} min" if mins < 120 else f"{mins // 60} h {mins % 60:02d} min"


def _label_hints() -> list[dict]:
    """Labels the agent profiles ask for, so the label box can suggest them ("android-usb, needed by android")."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT name, requires_labels FROM agent_profiles WHERE cardinality(requires_labels) > 0 ORDER BY name")
        out: dict[str, list[str]] = {}
        for r in cur.fetchall():
            for label in r["requires_labels"]:
                out.setdefault(label, []).append(r["name"])
    return [{"label": k, "needed_by": v} for k, v in out.items()]


def _where_choice() -> bool:
    """Show "Where is this computer?" only when the install has BOTH a local-network address and a public one (#976 D7)."""
    from app import front_door
    st = front_door.state()
    return bool(st["lan"] and st["public"])


def render_page(request: Request, account: dict, role: str = "all", status: int = 200, code: dict | None = None, error: str = "",
                add_error: str = "", add_form: dict | None = None, tab: str = ""):
    q = request.query_params
    page = build(request, account, role if role in ROLES else "all", tab or q.get("tab", ""))
    resp = render(request, "pages/machines.html", "machines", roles=ROLE_LABELS, code=code, error=error, add_error=add_error,
                  add_form=add_form or {}, modes=devices.MODES, max_labels=jobs.MAX_LABELS, runner_repo=config.RUNNER_REPO, profile_hint=config.PROFILE_HINT,
                  runner_repo_private=config.RUNNER_REPO_PRIVATE, where_choice=_where_choice(), envs=deploys.ENVS, added=q.get("added", ""),
                  checked=q.get("checked", ""), **page)
    resp.status_code = status
    if code:
        resp.headers["Cache-Control"] = "no-store"        # the enrolment code is on this response only
    return resp


@project_router.get("/machines", response_class=HTMLResponse)
def machines_page(request: Request, project: dict = ProjectDep, role: str = "", tab: str = ""):
    if not tree.is_runner_home(project):
        return _belongs_to_account(request, project)     # #987: a project's own Machines page points at the account's, never a dead end
    account = _account(project)
    if not request.query_params:
        # a bare arrival opens the person's saved default (#772), unless they came from this very page ("All" is a choice)
        ref = urlsplit(request.headers.get("referer") or "")
        if not (ref.path and ref.path == request.url.path):
            found = saved_filters.default_for(access.of_request(request), "machines", None)
            if found and found["data"].get("role") in ROLES and found["data"]["role"] != "all":
                saved = found["data"]["role"]
                return RedirectResponse(f"/{account['slug']}/machines?" + (f"tab={ROLE_TAB[saved]}" if saved in ROLE_TAB else f"role={saved}"), status_code=302)
    return render_page(request, account, role or "all")


def _belongs_to_account(request: Request, project: dict):
    """Runners and servers belong to the ACCOUNT, not a project inside it (#987). Say so, and link the account's Machines page; with agent
    files waiting for review in the account, the link goes straight to that card."""
    root = tree.account_root_id(project["id"])
    if root is None or root == project["id"]:
        raise NotFound("runners belong to an account")
    account = repo.get_project(root)
    pending = agent_pins.pending_in_account(root)
    return render(request, "pages/machines_account.html", "machines", account=account, pending=pending,
                  account_url=f"/{account['slug']}/machines?tab=runners" + ("#agent-files" if pending else ""))


def _back(account: dict, message: str, anchor: str = "", tab: str = ""):
    tab = tab if tab in TABS else ""                      # a form field: only ever one of ours
    resp = RedirectResponse(f"/{account['slug']}/machines" + (f"?tab={tab}" if tab else "") + (f"#{anchor}" if anchor else ""), status_code=303)
    resp.set_cookie(FLASH_COOKIE, _cookie_safe(message), max_age=60, samesite="lax", httponly=True)    # fixed text; made cookie-safe anyway
    return resp


@project_router.post("/machines/{device_id:int}/join", dependencies=[Depends(access.require_fresh())])
def join_server(request: Request, device_id: int, project: dict = ProjectDep, server_id: int = Form(...), tab: str = Form("")):
    """"Is Desktop the same machine as web-1? Yes, join them." Owner only (a server is the owner's). Never automatic."""
    account = _account(project)
    user = _user(request)
    access.require_owner(access.of_request(request))
    devices.link_server(account["id"], device_id, server_id, f"user:{user['id']}")
    return _back(account, "Joined into one machine.", f"dev-{device_id}", tab)


@project_router.post("/machines/{device_id:int}/unjoin", dependencies=[Depends(access.require_fresh())])
def unjoin_server(request: Request, device_id: int, project: dict = ProjectDep, tab: str = Form("")):
    account = _account(project)
    user = _user(request)
    access.require_owner(access.of_request(request))
    devices.link_server(account["id"], device_id, None, f"user:{user['id']}")
    return _back(account, "Split into two machines.", f"dev-{device_id}", tab)


@project_router.post("/machines/agent-files/pin", dependencies=[Depends(access.require_fresh())])
def pin_agent_files(request: Request, project: dict = ProjectDep, ids: list[int] = Form(default=[])):
    """"Pin all" on the review card: the project Owner read these exact files (the ids the card listed) and lets jobs load them. Fresh factor."""
    account = _account(project)
    user = _user(request)
    try:
        n = agent_pins.pin(access.of_request(request), user["id"], ids, account["id"])
    except BadRequest as exc:
        return _back(account, str(exc), "agent-files")
    return _back(account, f"Pinned {n} file(s). Jobs on that project may now load them.", "agent-files")


@project_router.post("/machines/agent-files/{file_id:int}/unpin", dependencies=[Depends(access.require_fresh())])
def unpin_agent_file(request: Request, file_id: int, project: dict = ProjectDep):
    account = _account(project)
    user = _user(request)
    agent_pins.unpin(access.of_request(request), user["id"], file_id, account["id"])
    return _back(account, "Unpinned. Jobs that carry that file are blocked again until it is pinned.", "agent-files")


@project_router.post("/machines/{device_id:int}/rename")
def rename_runner(request: Request, device_id: int, project: dict = ProjectDep, name: str = Form("")):
    account = _account(project)
    user = _user(request)
    try:
        devices.rename(account["id"], device_id, name, f"user:{user['id']}", user_id=_only_mine(request, user))
    except BadRequest as exc:
        return render_page(request, account, error=str(exc), status=400, tab="runners")
    return _back(account, "Runner renamed.", f"dev-{device_id}", "runners")


def _key_route_guard(request: Request, project: dict, device_id: int) -> tuple[dict, dict]:
    """A Google API key: a person's own runner, or the owner's on any runner, and then only with a very recent step-up (the short window for
    secrets, as for server access). Sync (it reads the database): the async routes run it in the threadpool. It runs BEFORE the form is read,
    so a refused request never touches the key. Returns (account, user)."""
    account = _account(project)
    user = _user(request)
    owner_of = gemini_keys.runner_user(account["id"], device_id)
    if owner_of is None:
        raise NotFound(f"device {device_id} not found")
    if owner_of != user["id"]:
        if not access.of_request(request).is_owner:
            raise NotFound(f"device {device_id} not found")
        access.require_fresh(access.STEPUP_SHORT_SECONDS)(request)      # another person's runner: a passkey tap in the last five minutes
    return account, user


@project_router.post("/machines/{device_id:int}/gemini-key")
async def set_gemini_key(request: Request, device_id: int, project: dict = ProjectDep):
    """Paste a Google API key: it is passed straight to the runner's key file over its socket and NEVER stored, logged or shown here (only its
    last four characters, a fingerprint and a time are kept). The body is read by hand (not as a Form parameter) so that no validation error can
    ever echo it. An offline runner is an error, never a queued key."""
    account, user = await run_in_threadpool(_key_route_guard, request, project, device_id)
    form = await request.form()
    key, everywhere = form.get("key"), form.get("everywhere") == "1"
    try:
        results = await asyncio.to_thread(gemini_keys.set_key, account["id"], device_id, key, f"user:{user['id']}",
                                          _only_mine(request, user), everywhere)
    except (BadRequest, Conflict, NotFound) as exc:
        return _back(account, str(exc)[:300], f"dev-{device_id}", "runners")
    finally:
        key = None
    return _back(account, gemini_keys.summary(results), f"dev-{device_id}", "runners")


@project_router.post("/machines/{device_id:int}/gemini-key/remove")
async def remove_gemini_key(request: Request, device_id: int, project: dict = ProjectDep):
    account, user = await run_in_threadpool(_key_route_guard, request, project, device_id)
    form = await request.form()
    try:
        results = await asyncio.to_thread(gemini_keys.remove_key, account["id"], device_id, f"user:{user['id']}",
                                          _only_mine(request, user), form.get("everywhere") == "1")
    except (BadRequest, Conflict, NotFound) as exc:
        return _back(account, str(exc)[:300], f"dev-{device_id}", "runners")
    return _back(account, gemini_keys.summary(results, removed=True), f"dev-{device_id}", "runners")
