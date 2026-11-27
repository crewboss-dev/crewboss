"""Crewboss — FastAPI entrypoint. Wires the HTML pages, the /api JSON REST layer, and the
/mcp FastMCP server into one process."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, Response

from app import (connections, instance, proxy_check, tls_check, ca_mcp, ca_notify, changes, config, db, deploys, drift, server_access, erp_health, health, health_reports, job_mcp, jobs, mcp_server, merge_executor, platform_keys, push, repo,
                 roles, seed)
from app import plan as plan_module
from app import planning_reminders
from app.assets import VersionedStaticFiles
from app import front_door, web_auth
from app import access, stepup
from app.web_auth import WebAuthGate
from app.origin_guard import SameOriginGuard
from app.security_headers import SecurityHeaders
from app.routers import (admin_pages, ca_pages, releases_pages, api, auth_pages, local_auth_pages, frontdoor_pages, trust_pages, keys_pages, knowledge_pages, materials_pages, assets_pages, tabs_pages, connections_pages, handoffs_pages, pages, push_pages,
                         servers_pages, machines_pages, environment_pages, lives_pages, logic_pages, runner, runners_pages, approvals_pages, shell_pages, filter_pages, skills_pages)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("crewboss")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.wait_for_db()
    applied = db.run_migrations()
    if applied:
        log.info("applied migrations: %s", ", ".join(applied))
    seed.run()
    try:
        from app import skill_seed
        skill_seed.seed()                # the built-in skills (#718): create the missing, version the changed
    except Exception:  # noqa: BLE001 — a seed problem must not stop the app
        log.exception("built-in skills could not be seeded")
    connections.seed_from_config()      # instance.yaml `connections:` (#784): inserts the missing rows, never deletes
    log.info("instance %r, folder %s (%s)", instance.get_str("name", "Crewboss"), instance.HOME,
             "instance.yaml read" if instance.DATA else "NO instance.yaml: fresh install, no project roles filled")
    roles.warn_unmatched()
    web_auth.startup_check()
    tls_task = await tls_check.startup()      # task #707: `enforce` refuses to start without a good public certificate; `warn` never blocks
    poller = None
    if config.HEALTH_POLL_SECONDS > 0 and health.is_configured():
        poller = asyncio.create_task(_health_poller())
    # The ERP feed loop always runs (unless switched off): it does nothing until the URL + read token are there, and
    # picks them up without a restart once they are.
    erp_poller = asyncio.create_task(_erp_health_poller()) if config.ERP_HEALTH_POLL_SECONDS > 0 else None
    morning = asyncio.create_task(_morning_replanner()) if roles.slug("overseer") else None
    drift_poller = asyncio.create_task(_drift_poller()) if config.DRIFT_POLL_SECONDS > 0 else None
    # Phone notifications (#487): always runs (unless switched off); a no-op until the VAPID key and a phone are there.
    push_sweeper = asyncio.create_task(_push_sweeper()) if config.PUSH_SWEEP_SECONDS > 0 else None
    # Platform keys (#562): reads each key store's status for stage reminders; a no-op until its credential is there.
    planning_reminders = asyncio.create_task(_planning_reminders()) if config.PLANNING_REMINDER_SECONDS > 0 else None   # #915
    keys_poller = asyncio.create_task(_platform_keys_poller()) if config.PLATFORM_KEYS_POLL_SECONDS > 0 else None
    # Job leases (#627, P2): requeues the jobs of runners that stopped heartbeating; a no-op with no running jobs.
    job_sweeper = asyncio.create_task(_job_sweeper()) if config.JOB_SWEEP_SECONDS > 0 else None
    lan_renewal = asyncio.create_task(_lan_cert_renewal()) if front_door.lan_hosts() else None
    # Deploy executor (task #627, P5): a deploy that was running when the last process died is orphaned (never retried), then
    # approved deploys run here, in this process, one at a time.
    deploys.recover(startup=True)
    try:        # #829: a merge claimed by a process that died goes back to approved after 3 minutes
        merge_executor.recover()
    except Exception:  # noqa: BLE001 - never stop the app from starting
        log.exception("merge executor recover")
    deploy_executor = asyncio.create_task(_deploy_executor()) if config.DEPLOY_POLL_SECONDS > 0 else None
    # Server probe (#810): a login-free ssh-keyscan of each direct server every SERVER_PROBE_SECONDS (0 = off).
    server_prober = asyncio.create_task(_server_prober()) if config.SERVER_PROBE_SECONDS > 0 else None
    # The MCP mount's own lifespan never fires (Starlette Mount doesn't forward the lifespan
    # protocol) — start its session manager's task group here instead. See mcp_server.mounted_app.
    # CA (task #702): the scheduler thread fires reminders (missed ones on this first pass), tells finished jobs, fails dead chat turns.
    ca_scheduler = ca_notify.Scheduler()
    ca_scheduler.start()
    try:
        async with mcp_server.mcp.session_manager.run(), job_mcp.mcp.session_manager.run(), ca_mcp.mcp.session_manager.run():
            yield
    finally:
        ca_scheduler.stop()
        for task in (tls_task, poller, erp_poller, morning, drift_poller, push_sweeper, planning_reminders, keys_poller, job_sweeper, lan_renewal, deploy_executor, server_prober):
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task


async def _planning_reminders():
    """#915: one push for the tasks whose start_by arrived or whose waiting_until passed (app/planning_reminders). Idempotent, so a
    pass every PLANNING_REMINDER_SECONDS is safe; nothing goes out before PLANNING_REMINDER_HOUR local time."""
    await asyncio.sleep(60)
    while True:
        try:
            await asyncio.to_thread(planning_reminders.sweep)
        except Exception:  # noqa: BLE001 — never let the loop die
            log.exception("planning reminders")
        await asyncio.sleep(config.PLANNING_REMINDER_SECONDS)


async def _deploy_executor():
    """Task #627 (P5): run approved deploys (app/deploys.Executor), a pass every DEPLOY_POLL_SECONDS. The work is in a thread,
    so the event loop never waits on ssh."""
    executor = deploys.Executor()
    while True:
        await asyncio.sleep(config.DEPLOY_POLL_SECONDS)
        try:
            await asyncio.to_thread(executor.run_once)
        except Exception:  # noqa: BLE001 — never let the loop die
            log.exception("deploy executor")
        try:      # #829: approved merges whose first attempt hit a GitHub error are retried here (projects that opted in only)
            await asyncio.to_thread(merge_executor.run_once)
        except Exception:  # noqa: BLE001
            log.exception("merge executor")


async def _server_prober():
    """#810: probe every direct server (server_access.probe_all: ssh-keyscan against the pinned key, never a login). The scans block
    on the network, so the pass runs in a thread. The first pass waits a little so a restart doesn't read as an outage."""
    await asyncio.sleep(min(config.SERVER_PROBE_SECONDS, 30))
    while True:
        try:
            await asyncio.to_thread(server_access.probe_all)
        except Exception:  # noqa: BLE001 — never let the loop die
            log.exception("server probe")
        await asyncio.sleep(config.SERVER_PROBE_SECONDS)


async def _lan_cert_renewal():
    """#976: once a day, near the end of the LAN certificate, announce the new one to the runners and switch it in if all are ready."""
    while True:
        try:
            await asyncio.to_thread(front_door.auto_renew)
        except Exception:  # noqa: BLE001 — never let the loop die
            log.exception("LAN certificate renewal")
        await asyncio.sleep(24 * 3600)


async def _job_sweeper():
    """Task #627 (P2): expire the leases of running jobs on the database clock (app/jobs.Sweeper)."""
    sweeper = jobs.Sweeper()
    while True:
        await asyncio.sleep(config.JOB_SWEEP_SECONDS)
        try:
            await asyncio.to_thread(sweeper.run)
        except Exception:  # noqa: BLE001 — never let the loop die
            log.exception("job sweeper")


async def _morning_replanner():
    """Task #129: once a day (06:00 local time) nudge dash to re-plan across every project, and send the ONE
    follow-up nudge for changes made inside the re-plan cooldown once it ends. Checks every minute."""
    while True:
        await asyncio.sleep(60)
        try:
            if await asyncio.to_thread(plan_module.maybe_morning_replan):
                log.info("global plan: morning re-plan nudge sent")
            if await asyncio.to_thread(plan_module.flush_pending_nudge):
                log.info("global plan: follow-up nudge after the cooldown")
        except Exception:  # noqa: BLE001 — never let the loop die
            log.exception("re-plan nudger")


async def _health_poller():
    """System Health catch-up: sync with Zabbix now, then every HEALTH_POLL_SECONDS (the poke covers the gap)."""
    while True:
        try:
            result = await asyncio.to_thread(health.sync)
            if result.get("new") or result.get("resolved"):
                log.info("health sync: %s", result)
        except Exception:  # noqa: BLE001 — sync() records its own failures; never let the loop die
            log.exception("health poller")
        await asyncio.sleep(config.HEALTH_POLL_SECONDS)


async def _erp_health_poller():
    """ERP health feed (task #196): fetch the ERP's /api/health now, then every ERP_HEALTH_POLL_SECONDS (a no-op while
    the feed isn't configured, or the ERP pushes instead), and check every pushing project still reports (#404)."""
    while True:
        try:
            if erp_health.is_pulling():
                result = await asyncio.to_thread(erp_health.sync)
                if result.get("new") or result.get("resolved") or result.get("down_escalated"):
                    log.info("erp health sync: %s", result)
            # Pushed reports (task #404): a project that stopped reporting raises its alarm on this beat.
            stale = await asyncio.to_thread(health_reports.check)
            if stale.get("new") or stale.get("resolved"):
                log.info("health reports: %s", stale)
        except Exception:  # noqa: BLE001 — sync() records its own failures; never let the loop die
            log.exception("erp health poller")
        await asyncio.sleep(config.ERP_HEALTH_POLL_SECONDS)


async def _drift_poller():
    """No drift (#402): check the linked admin sites' catalogs and the servers' timers every DRIFT_POLL_SECONDS. The
    first run waits a minute so the ERP feed has been fetched."""
    await asyncio.sleep(60)
    while True:
        try:
            result = await asyncio.to_thread(drift.sync)
            if result.get("new") or result.get("resolved") or not result.get("ok", True):
                log.info("drift sync: %s", result)
        except Exception:  # noqa: BLE001 — sync() records its own failures; never let the loop die
            log.exception("drift poller")
        await asyncio.sleep(config.DRIFT_POLL_SECONDS)


async def _push_sweeper():
    """Phone notifications (#487): push due estate problems (and the quiet all-clears) every PUSH_SWEEP_SECONDS. The
    rule and the grouping are app/push.py; the first sweep waits a little so the health loops have caught up."""
    await asyncio.sleep(min(config.PUSH_SWEEP_SECONDS, 20))
    while True:
        await asyncio.to_thread(changes.prune_owner_prompts)  # transient owner prompts (#581): drop the expired
        result = await asyncio.to_thread(push.sweep)  # never raises; logs what it sent
        if not result.get("ok", True):
            log.warning("push sweep: %s", result)
        await asyncio.sleep(config.PUSH_SWEEP_SECONDS)


async def _platform_keys_poller():
    """Platform keys (#562): read each key store's status every PLATFORM_KEYS_POLL_SECONDS; a key whose rotation stage
    went up (ok -> due_30 -> due_7 -> overdue) gets a Health reminder and one phone notification (app/platform_keys.py).
    The first read waits a little so a restart doesn't crowd the store's 30-a-minute door."""
    await asyncio.sleep(min(config.PLATFORM_KEYS_POLL_SECONDS, 90))
    while True:
        result = await asyncio.to_thread(platform_keys.sync)  # never raises; records its own failures
        if not result.get("ok", True):
            log.warning("platform keys sync: %s", result)
        await asyncio.sleep(config.PLATFORM_KEYS_POLL_SECONDS)


app = FastAPI(title="Crewboss", docs_url="/api/docs", redoc_url=None, lifespan=lifespan)

# No CORS: nothing calls this app cross-origin (pages, htmx, the channel, hooks and MCP clients are
# all same-origin or non-browser). Browser writes outside /api + /mcp must come from the dashboard's
# own origin — see app/origin_guard.py.
# Dashboard sign-in (#394): every page needs a signed-in super admin; /api + /mcp keep their Bearer tokens. Added
# BEFORE the origin guard, so the guard runs first (outermost) and a cross-site write is refused before anything.
app.add_middleware(WebAuthGate)
app.add_middleware(SameOriginGuard)
app.add_middleware(SecurityHeaders)   # outermost: every answer, a refusal too (N2: nosniff, referrer, frame-ancestors; no full CSP yet)


@app.middleware("http")
async def proxy_trust_notice(request, call_next):
    """R1: warn once when an untrusted non-loopback peer sends X-Forwarded-For (see app/proxy_check.py)."""
    proxy_check.observe(request.client.host if request.client else "", request.headers.get("x-forwarded-for", ""))
    return await call_next(request)


@app.middleware("http")
async def satellite_front_door(request, call_next):
    """DORMANT (task #126): the :8443 satellite door was closed 2026-10-01 and the client mini dashboard moves to the ERP
    Customer Portal (push). Kept as a cheap second gate in case a door is ever re-added: such a door stamps every
    request `X-Crewboss-Front-Door: satellite` (the pre-rename `X-MDD-Front-Door` is still honoured: TODO(#589 compat), drop it
    once the web server's config says the new name). Through that door nothing but /api/satellite/* exists:
    a client peer must never see the owner's pages, the open reads or /mcp (task #106 audit, P0). Caddy is the first
    gate; this is the second. The main vhosts strip the header, so it can't be used to dodge anything there."""
    door = "satellite" in (request.headers.get("x-crewboss-front-door"), request.headers.get("x-mdd-front-door"))   # TODO(#589 compat)
    if door and not request.url.path.startswith("/api/satellite/"):
        return Response(status_code=404)
    return await call_next(request)

# A validation error on a page write shows the owner a flash instead of a 500 (see pages.handle_repo_error).
app.add_exception_handler(repo.BadRequest, pages.handle_repo_error)
app.add_exception_handler(repo.NotFound, pages.handle_repo_error)
app.add_exception_handler(repo.Conflict, pages.handle_repo_error)  # e.g. the owner's WIP limit
app.add_exception_handler(repo.Forbidden, pages.handle_repo_error)  # owner-only writes (e.g. a tree move)
app.add_exception_handler(access.StepUpRequired, stepup.handle_stepup)  # a recent passkey tap is needed (#809)
app.add_exception_handler(pages.SlugMoved, pages.handle_slug_moved)  # a renamed project's old slug (#577)

# ORDER MATTERS: `/{slug}/…` (pages.project_router) is a catch-all for the first path segment, so every
# fixed top-level path is registered before it. Project slugs can't take these words (repo.RESERVED_SLUGS).
app.mount("/static", VersionedStaticFiles(directory="app/static"), name="static")
app.include_router(api.router)
app.mount("/mcp", mcp_server.mounted_app())
app.mount("/runner/mcp", job_mcp.mounted_app())   # the job MCP (task #627, P2): job token only; before /{slug}
app.mount("/runner/ca-mcp", ca_mcp.mounted_app())  # the CA's MCP (task #702): a CA turn token only


@app.api_route("/runner/mcp", methods=["GET", "POST", "DELETE"], include_in_schema=False)
def job_mcp_no_slash():
    return Response(status_code=307, headers={"Location": "/runner/mcp/"})


@app.api_route("/runner/ca-mcp", methods=["GET", "POST", "DELETE"], include_in_schema=False)
def ca_mcp_no_slash():
    return Response(status_code=307, headers={"Location": "/runner/ca-mcp/"})


@app.api_route("/mcp", methods=["GET", "POST", "DELETE"], include_in_schema=False)
def mcp_no_slash():
    """MCP clients are configured with https://dash.example.com/mcp (no slash). Starlette used to answer
    that with a 307 to /mcp/ (redirect_slashes); with the /{slug} routes a bare /mcp would otherwise match a
    project route and 404/405. Keep the same relative 307 (method + body preserved)."""
    return Response(status_code=307, headers={"Location": "/mcp/"})


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.api_route("/favicon.ico", methods=["GET", "HEAD"], include_in_schema=False)   # HEAD too: install tools probe with it
def favicon():
    """The Crewboss mark: small mark at 16/32, full mark at 48 (app/static/brand/crewboss, tools/brand/build_icons.py)."""
    return FileResponse("app/static/brand/crewboss/favicon.ico", media_type="image/x-icon",
                        headers={"Cache-Control": "public, max-age=86400"})


# The web-app manifest (installable: Chromium "Install", Omarchy's web-app installer, and the owner's phone, which gets
# estate problems as notifications from the service worker at /sw.js, #487). Icons are the Crewboss set
# (app/static/brand/crewboss, generated by tools/brand/build_icons.py, #705) at STABLE unversioned URLs so an installer can
# keep pointing at them; /static serves them with Cache-Control: no-cache and Content-Type: image/png. "any" = the accent
# tile; "maskable" = the same tile full-bleed with the mark inside the 80% safe circle (W3C maskable spec).
MANIFEST = {
    "name": "Crewboss",
    "short_name": "Crewboss",
    "id": "/",
    "description": "Crewboss: every project's focus, tasks, workflows and sessions.",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#f6f5f0",  # the Crewboss light page colour (#705)
    "theme_color": "#f6f5f0",       # the page too: the header is page-coloured chrome, as the ERP's <meta> (#451)
    "icons": [
        {"src": "/static/brand/crewboss/favicon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/static/brand/crewboss/favicon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {"src": "/static/brand/crewboss/maskable-192.png", "sizes": "192x192", "type": "image/png", "purpose": "maskable"},
        {"src": "/static/brand/crewboss/maskable-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
    ],
}


@app.api_route("/manifest.webmanifest", methods=["GET", "HEAD"], include_in_schema=False)   # HEAD too: install tools probe with it
@app.api_route("/manifest.json", methods=["GET", "HEAD"], include_in_schema=False)   # HEAD too: install tools probe with it
def manifest():
    return JSONResponse(MANIFEST, media_type="application/manifest+json", headers={"Cache-Control": "no-cache"})


# ROOT-CONVENTION paths (#750, the ERP's ec43ff3d fix). Every page is behind sign-in, so an installer that isn't signed
# in ("install as web app", Omarchy's installer, a link previewer) gets "/" -> 302 -> the sign-in page and finds no app
# icon or name. Naive fetchers look for these well-known paths at the site root and often don't follow redirects, so
# they're served as BYTES, public (web_auth._OPEN_EXACT), with no redirect to /static.
@app.api_route("/apple-touch-icon.png", methods=["GET", "HEAD"], include_in_schema=False)   # HEAD too: install tools probe with it
@app.api_route("/apple-touch-icon-precomposed.png", methods=["GET", "HEAD"], include_in_schema=False)   # HEAD too: install tools probe with it
def apple_touch_icon():
    return FileResponse("app/static/brand/crewboss/favicon-180.png", media_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})


app.include_router(ca_pages.router)                 # /ca/… the CA drawer's JSON + SSE (task #702; signed-in web session)
app.include_router(runner.router)                   # /runner/identity, /runner/enrol (task #627; code/signature auth)
app.include_router(auth_pages.router)               # /auth/login, /auth/callback, /auth/logout (#394)
app.include_router(local_auth_pages.router)         # local sign-in: /auth/login (POST), /auth/enrol, /account/security (#706)
app.include_router(admin_pages.home_router)          # /admin: the Administration overview (#778)
app.include_router(frontdoor_pages.router)         # /admin/front-door: the Front door card (#976)
app.include_router(trust_pages.router)              # /trust: open page, the LAN fingerprint and the click-through help (#976)
app.include_router(admin_pages.router)              # /admin/users: the owner's Users page (#713)
app.include_router(push_pages.router)               # /sw.js, /push/… (phone notifications, #487)
app.include_router(pages.router)                    # /  (global home)
app.include_router(filter_pages.router)             # /all/filters: saved filters (#772)
app.include_router(skills_pages.router)           # /assets/skills: the global skills layer (#718); before /assets/{kind}
app.include_router(assets_pages.router)             # /assets: the Assets section (#778)
app.include_router(shell_pages.router)              # /dashboard, /accounts, /projects, /runners, /servers (the fixed menu's pages, #733)
app.include_router(knowledge_pages.router)          # /media/knowledge/<id>.<ext> (global)
app.include_router(materials_pages.router)          # /media/materials/<id>/<filename> (global, task #295)
app.include_router(pages.legacy_router)             # /tasks, /knowledge, … -> 301/308 into the current project
app.include_router(pages.project_router)            # /{slug}, /{slug}/board, …
app.include_router(knowledge_pages.project_router)  # /{slug}/knowledge…, /{slug}/objectives
app.include_router(materials_pages.project_router)  # /{slug}/materials (task #295)
app.include_router(handoffs_pages.project_router)   # /{slug}/handoffs: read-only (#908)
app.include_router(skills_pages.project_router)      # /{slug}/build-skills: a project's skills (#718)
app.include_router(lives_pages.project_router)         # /{slug}/where (#839, #726)
app.include_router(logic_pages.project_router)         # /{slug}/logic (#715)
app.include_router(environment_pages.project_router)   # /{slug}/environment (#810 step 6)
app.include_router(machines_pages.project_router)    # /{slug}/machines (#810: Servers and Runners in one page)
app.include_router(servers_pages.project_router)    # /{slug}/servers (task #300)
app.include_router(releases_pages.project_router)   # /{slug}/releases, /{slug}/team (change #77)
app.include_router(runners_pages.project_router)    # /{slug}/runners (task #627)
app.include_router(approvals_pages.project_router)   # /{slug}/approvals (task #627, P5)
app.include_router(tabs_pages.project_router)       # /{slug}/tabs/<action>: the record page tab bar (#773)
app.include_router(connections_pages.project_router)   # /{slug}/connections/<action>: project connections (#784)
app.include_router(keys_pages.project_router)       # /{slug}/keys (platform keys, #562)
