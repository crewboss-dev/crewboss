"""JSON REST API under /api. EVERY call — reads included — needs Authorization: Bearer <token> (task #368: a missing
token is a 401, never "the owner"); writes are recorded as actor <agent>:<stream>. The owner's HTML pages never call
/api.

PROJECT ISOLATION: a session token is bound to one node of the project tree (an account, task #253, or a project)
and may only read or write its reach — that node's subtree, less any branch with a session of its own, never
outside its account. Another account's or project's ids answer 404 exactly like missing ones (app/scope.py). Only
the overseer (dash) reads across accounts; the estate-wide feeds (/api/health, /api/pulse) are the overseer's and
the estate owner's account's (scope.sees_estate); /api/plan and /api/sessions/plan are the overseer's alone."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Request, Response

from app import (summary as summary_module, catalog_import, config, drift, erp_health, health, health_reports, knowledge_graph, repo, scope,
                 server_access, ssh_ca, tree)
from app.auth import (actor_for, require_manifest_server, require_catalog_importer, require_digest, require_reporter, require_satellite, require_session,
                      require_zabbix_poke)
from app.repo import BadRequest, Conflict, Forbidden, NotFound, TooMany
from app import planning
from app import pulse as pulse_module
from app.config import RUNNER_KINDS
from app.schemas import (AssignBody, CommentCreate, DeliveredBody, HeartbeatBody, OwnerPromptBody, KnowledgeArchive,
                          KnowledgeCreate, KnowledgePatch, LinkWorkflowBody, OutboxPush, ProjectCreate,
                          SatelliteRequest, SessionStopping, SshCertRequest,
                          ProposeSubtasksBody, SubtaskPatch, ZabbixPoke, SubtasksAdd, SuggestTriageBody, TaskCreate, TaskPatch, MilestoneCreate, MilestonePatch)

router = APIRouter(prefix="/api")


def _guard(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except NotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except BadRequest as exc:
        raise HTTPException(400, str(exc)) from exc
    except Conflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except Forbidden as exc:
        raise HTTPException(403, str(exc)) from exc
    except TooMany as exc:
        raise HTTPException(429, str(exc)) from exc


def _pid(session: dict | None, slug: str | None) -> int:
    """The project id a call acts on (a session token: only its own; 404 otherwise)."""
    return _guard(scope.project_for, session, slug)["id"]


def _own_stream(session: dict, requested: str | None) -> str:
    """A session token is bound to ONE stream (or 'all'): it can only read/claim/ground that. `requested`
    (a legacy ?stream= param) is accepted only when it matches the token's own stream."""
    own = session["stream"]
    if requested is not None and requested != own:
        raise HTTPException(403, f"this token belongs to stream {own!r}, not {requested!r}")
    return own


def _in_scope(fn, session, entity_id):
    """scope.task / scope.subtask / scope.knowledge as a 404."""
    return _guard(fn, session, entity_id)


# --- projects ---------------------------------------------------------------------------------

@router.get("/projects")
def api_list_projects(archived: bool = False, q: str | None = None, session: dict = Depends(require_session)):
    """Only the token's own project and the child projects it works."""
    rows = repo.list_projects(include_archived=archived, q=q)
    return [p for p in rows if p["id"] in scope.reach(session)]


@router.get("/projects/tree")
def api_project_tree(session: dict = Depends(require_session)):
    """The tree's SHAPE only (slug, parent, the session above, has a session): what the SendMessage guard needs
    to allow parent ↔ child session messages. No names, tasks or anything else. The overseer gets the whole tree;
    any other session only the nodes of its own account partition (task #368: the account and every project under
    it), so a target in another account is simply absent and the guard denies it."""
    if scope.is_overseer(session):
        return tree.tree_for_guard()
    return tree.tree_for_guard(tree.account_member_ids(session["project_id"]))


@router.get("/projects/{project_id}")
def api_get_project(project_id: int, session: dict = Depends(require_session)):
    _guard(scope.check_project_id, session, project_id, f"project {project_id}")
    return _guard(repo.get_project, project_id)


@router.post("/projects", status_code=201)
def api_create_project(body: ProjectCreate, session: dict = Depends(require_session)):
    """short_name is required (it names the project's sessions). OVERSEER ONLY (task #368): a new project lands
    directly under the dashboard root with its own auto-started session, i.e. outside every account, so no account's
    session may
    create one (another session gets the same 404 as any other cross-account call). The owner creates projects
    from the dashboard's New-project form."""
    if not scope.is_overseer(session):
        raise HTTPException(404, "not found")
    return _guard(repo.create_project, body.slug, body.name, body.type, actor_for(session),
                  short_name=body.short_name, session_roles=body.session_roles)


# Archiving / unarchiving a project is the OWNER's alone (the dashboard pages, same-origin): a session token can
# never do it. Archiving hides a project and stops its sessions, so it is never a session's call (task #132 audit).


# --- client mini dashboard (satellite token only) ---------------------------------------------
# The satellite's token is bound to ONE project: these routes take no project from the request at all.

@router.get("/satellite/snapshot")
def api_satellite_snapshot(sat: dict = Depends(require_satellite)):
    if not sat["project"]["satellite_enabled"]:
        raise HTTPException(403, "the mini dashboard is turned off for this project")
    return repo.satellite_snapshot(sat["project"])


@router.post("/satellite/requests", status_code=201)
def api_satellite_request(body: SatelliteRequest, sat: dict = Depends(require_satellite)):
    if not sat["project"]["satellite_enabled"]:
        raise HTTPException(403, "the mini dashboard is turned off for this project")
    row = _guard(repo.create_client_request, sat["project"], body.title, body.body, body.requested_by,
                 body.request_id)
    # Only what the satellite needs to show the request as its own: never the task's internals.
    return {"id": row["id"], "title": row["title"], "status": row["status"], "request_id": row["client_request_id"]}


# --- tasks ------------------------------------------------------------------------------------

@router.get("/tasks")
def api_list_tasks(project: str | None = None, stream: str | None = None, status: str | None = None,
                    priority: str | None = None, subproject: str | None = None, q: str | None = None,
                    assignee: str | None = None, category: str | None = None,
                    session: dict = Depends(require_session)):
    """With a session token and no `project`: every project it works (task #253); an account named: the projects
    under it. Twin of the MCP list_tasks tool."""
    return _guard(scope.list_tasks, session, project, stream=stream, status=status, priority=priority,
                  subproject=subproject, q=q, assignee=assignee, category=category)


@router.get("/my-tasks")
def api_my_tasks(assignee: str = "ai", project: str | None = None, session: dict = Depends(require_session)):
    """The TOKEN's stream's open tasks with that assignee ('ai' = yours to do; 'owner' = waiting on the
    owner), each with subtask progress — in every project the token works (an account session: the whole account,
    task #253), or one of them (`project`). Twin of the MCP my_tasks tool."""
    # within the token's reach only (404 otherwise)
    projects = [_guard(scope.project_for, session, project)] if project else scope.reach_projects(session)
    return _guard(repo.my_tasks, session["stream"], None, assignee, projects=projects)


@router.get("/tasks/{task_id}")
def api_get_task(task_id: int, session: dict = Depends(require_session)):
    _in_scope(scope.read_task, session, task_id)
    return _guard(repo.get_task, task_id)


@router.post("/tasks", status_code=201)
def api_create_task(body: TaskCreate, session: dict = Depends(require_session)):
    pid = _guard(scope.task_home, session, body.project)["id"]  # an account session names one of its projects
    _guard(scope.check_links, session, body.links)  # links name only knowledge this session can read
    return _guard(repo.create_task, pid, body.title, body.body, body.priority, body.stream,
                  body.subprojects, body.links, actor_for(session), needs_owner=body.needs_owner,
                  assignee=body.assignee, subtasks=body.subtasks, status=body.status,
                  planning_patch=_guard(planning.patch_from, due_on=body.due_on, lead_days=body.lead_days,
                                        start_by=body.start_by, milestone=body.milestone, waiting_on=body.waiting_on,
                                        waiting_for=body.waiting_for, waiting_until=body.waiting_until))


@router.patch("/tasks/{task_id}")
def api_update_task(task_id: int, body: TaskPatch, session: dict = Depends(require_session)):
    task = _in_scope(scope.task, session, task_id)
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    if "project" in patch and patch["project"] != _guard(repo.get_project, task["project_id"])["slug"]:
        # moving a task OUT of its project is the owner's call (the drawer), never a session's
        raise HTTPException(403, "moving a task to another project is owner-only")
    patch.pop("project", None)
    clear = patch.pop("clear", None)
    patch.update(_guard(planning.patch_from, clear=clear,
                        **{k: patch.pop(k) for k in planning.TASK_FIELDS if k in patch}))
    _guard(scope.check_links, session, patch.get("links"))
    return _guard(repo.update_task, task_id, patch, actor_for(session))


# --- milestones (#915) ------------------------------------------------------------------------

@router.get("/milestones")
def api_list_milestones(project: str | None = None, include_archived: bool = False,
                        session: dict = Depends(require_session)):
    """Twin of the MCP list_milestones tool: derived status and task counts, a session's reach (the overseer: any project)."""
    return _guard(scope.milestones_list, session, project, include_archived)


@router.post("/milestones", status_code=201)
def api_create_milestone(body: MilestoneCreate, session: dict = Depends(require_session)):
    pid = _guard(scope.task_home, session, body.project)["id"]   # never an account, never outside the reach
    return _guard(planning.create_milestone, pid, body.name, body.due_on, body.note, actor_for(session))


@router.patch("/milestones/{milestone_id}")
def api_update_milestone(milestone_id: int, body: MilestonePatch, session: dict = Depends(require_session)):
    _in_scope(scope.milestone, session, milestone_id)
    return _guard(planning.update_milestone, milestone_id, body.model_dump(), actor_for(session))


@router.post("/tasks/{task_id}/assign")
def api_assign_task(task_id: int, body: AssignBody, session: dict = Depends(require_session)):
    """To the owner: any task of the token's project. To 'ai': only a task in the token's own stream (403
    otherwise; an 'all' token: any stream of its project)."""
    _in_scope(scope.task, session, task_id)
    return _guard(repo.assign_task, task_id, body.assignee, actor_for(session))


@router.post("/tasks/{task_id}/subtasks", status_code=201)
def api_add_subtasks(task_id: int, body: SubtasksAdd, session: dict = Depends(require_session)):
    """Direct adds only on a task this session created; elsewhere propose (403 says so)."""
    _in_scope(scope.task, session, task_id)
    return _guard(repo.add_subtasks, task_id, body.items, actor_for(session))


@router.patch("/subtasks/{subtask_id}")
def api_update_subtask(subtask_id: int, body: SubtaskPatch, session: dict = Depends(require_session)):
    _in_scope(scope.subtask, session, subtask_id)
    return _guard(repo.update_subtask, subtask_id, actor_for(session), done=body.done, text=body.text)


@router.post("/tasks/{task_id}/subtask-proposals", status_code=201)
def api_propose_subtasks(task_id: int, body: ProposeSubtasksBody, session: dict = Depends(require_session)):
    _in_scope(scope.task, session, task_id)
    changes = [c.model_dump(exclude_none=True) for c in body.changes]
    return _guard(repo.propose_subtask_changes, task_id, changes, body.rationale, actor_for(session))


@router.post("/tasks/{task_id}/comments", status_code=201)
def api_comment_task(task_id: int, body: CommentCreate, session: dict = Depends(require_session)):
    """The author is the token's session (claude:<stream>); a different `author` is refused."""
    _in_scope(scope.task, session, task_id)
    actor = actor_for(session)
    if body.author is not None and body.author != actor:
        raise HTTPException(403, f"comments from this token are by {actor!r}; author can't be set")
    return _guard(repo.comment_task, task_id, body.body, actor)


@router.post("/tasks/{task_id}/link-workflow", status_code=201)
def api_link_task_workflow(task_id: int, body: LinkWorkflowBody, session: dict = Depends(require_session)):
    _in_scope(scope.task, session, task_id)
    return _guard(repo.link_task_workflow, task_id, body.workflow_id, body.step, actor_for(session))


@router.post("/tasks/{task_id}/suggest-triage")
def api_suggest_triage(task_id: int, body: SuggestTriageBody, session: dict = Depends(require_session)):
    """Claude challenges the owner's triage — see repo.suggest_triage. The owner responds with
    Accept/Keep mine/Argue from the UI (those are owner-only page actions, not API writes)."""
    _in_scope(scope.task, session, task_id)
    return _guard(repo.suggest_triage, task_id, actor_for(session), priority=body.priority,
                  rank_hint=body.rank_hint, stream=body.stream, rationale=body.rationale,
                  business_case=body.business_case, category=body.category, needed_now=body.needed_now,
                  needed_reason=body.needed_reason, assignee=body.assignee)


# --- knowledge --------------------------------------------------------------------------------

@router.get("/knowledge")
def api_list_knowledge(project: str | None = None, kind: str | None = None,
                        subproject: str | None = None, needs_review: bool | None = None,
                        q: str | None = None, archived: bool = False,
                        session: dict = Depends(require_session)):
    """archived=true includes archived entries (hidden by default). A session naming no project: every project
    it works (task #253)."""
    return _guard(scope.knowledge_list, session, project, kind=kind, subproject=subproject,
                  needs_review=needs_review, q=q, include_archived=archived)


@router.get("/knowledge/graph")
def api_knowledge_graph(project: str | None = None, session: dict = Depends(require_session)):
    """How one project's knowledge links together (task #550; app/knowledge_graph.py): nodes (entries + the tasks
    they reference), edges, broken [[links]] and the housekeeping lists. Read-only. The project must be one the
    session may read (scope.read_project_for); naming none means the session's own node."""
    return _guard(lambda: knowledge_graph.build(scope.read_project_for(session, project)))


@router.get("/knowledge/{knowledge_id}")
def api_get_knowledge(knowledge_id: int, session: dict = Depends(require_session)):
    _in_scope(scope.read_knowledge, session, knowledge_id)
    return _guard(repo.get_knowledge, knowledge_id)


@router.post("/knowledge", status_code=201)
def api_create_knowledge(body: KnowledgeCreate, session: dict = Depends(require_session)):
    pid = _pid(session, body.project)
    return _guard(repo.create_knowledge, pid, body.kind, body.title, body.body, body.subprojects,
                  body.source, body.needs_review, actor_for(session), target_value=body.target_value,
                  target_period=body.target_period, summary=body.summary, target_unit=body.target_unit)


@router.patch("/knowledge/{knowledge_id}")
def api_update_knowledge(knowledge_id: int, body: KnowledgePatch, session: dict = Depends(require_session)):
    _in_scope(scope.knowledge, session, knowledge_id)
    patch = {k: v for k, v in body.model_dump().items() if v is not None}
    return _guard(repo.update_knowledge, knowledge_id, patch, actor_for(session))


@router.post("/knowledge/{knowledge_id}/archive")
def api_archive_knowledge(knowledge_id: int, body: KnowledgeArchive, session: dict = Depends(require_session)):
    _in_scope(scope.knowledge, session, knowledge_id)
    return _guard(repo.archive_knowledge, knowledge_id, body.reason, actor_for(session))


# --- activity ---------------------------------------------------------------------------------

@router.get("/activity")
def api_activity(project: str | None = None, actor: str | None = None, entity: str | None = None,
                  limit: int = 50, session: dict = Depends(require_session)):
    ids = _guard(scope.list_project_ids, session, project)  # a session naming none: every project it works
    return repo.recent_activity(actor=actor, entity=entity, limit=limit, project_ids=ids)


# --- objectives + business pulse ----------------------------------------------------------------

@router.get("/objectives")
def api_list_objectives(project: str | None = None, session: dict = Depends(require_session)):
    return _guard(scope.objectives, session, project)  # a session naming no project: every project it works


@router.get("/pulse")
def api_get_pulse(session: dict = Depends(require_session)):
    """The estate owner's business pulse: the overseer's and the estate owner's account's only (scope.sees_estate,
    task #368); any other session gets a 404. Null until phase 3 wires a real read-only ERP connection."""
    if not scope.sees_estate(session):
        raise HTTPException(404, "not found")
    return pulse_module.get_pulse()


# --- servers (task #367) -----------------------------------------------------------------------

@router.get("/servers")
def api_list_servers(session: dict = Depends(require_session)):
    """The servers the owner added on the token's account's Servers page, with their status (no host keys, no CA
    material). The overseer lists every account's (its cross-account reads); another account's never appear."""
    try:
        ids = scope.server_read_account_ids(session)
    except NotFound:  # an external agent's token (#574)
        raise HTTPException(404, "not found")
    slugs = {p["id"]: p["slug"] for p in tree.index().values()}
    rows = server_access.list_for(ids)
    names = {r["id"]: r["name"] for r in rows}
    return [dict(server_access.public_view(r, names.get(r["via_server_id"])), account=slugs.get(r["account_id"]))
            for r in rows]


@router.post("/servers/{server_id}/ssh-cert")
def api_ssh_cert(server_id: int, body: SshCertRequest, session: dict = Depends(require_session)):
    """A one-hour SSH pass for ONE commissioned server of the token's own account: the session's public key signed by
    the account CA for principal srv-<id>, plus the pinned known_hosts line to connect with StrictHostKeyChecking=yes.
    Another account's (or a missing) server is a 404, the overseer included (signing is a write); a server that isn't
    commissioned yet is a 409. the instance's SSH helper is the client."""
    if session.get("stream") in RUNNER_KINDS:  # a runner (device/job) principal never gets a pass (task #627)
        raise HTTPException(403, "a runner token can't do this")
    try:
        account = scope.server_account(session)
    except NotFound:  # an external agent's token (#574)
        account = None
    if account is None:
        raise HTTPException(404, f"server {server_id} not found")
    try:
        return _guard(server_access.issue_pass, account, server_id, body.public_key,
                      f"{actor_for(session)}/{session['name']}")
    except ssh_ca.CaUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc


# --- workflows --------------------------------------------------------------------------------

@router.get("/workflows")
def api_list_workflows(project: str | None = None, group: str | None = None, q: str | None = None,
                        ai_only: bool = False, stale: bool = False, broken: bool = False,
                        session: dict = Depends(require_session)):
    # a session naming no project: every project it works (each workflow then names its `project`)
    return _guard(scope.workflows, session, project, group=group, q=q, ai_only=ai_only, stale_only=stale,
                  broken_only=broken)


@router.get("/workflows/{workflow_id}")
def api_get_workflow(workflow_id: str, project: str | None = None, session: dict = Depends(require_session)):
    return _guard(scope.workflow, session, project, workflow_id)  # no project: any project the session works


@router.post("/catalog/import", status_code=201)
def api_import_catalog(catalog: dict, project: str | None = None,
                        session: dict = Depends(require_catalog_importer)):
    """Import one scanner catalog (JSON body, docs/catalog-schema.md). Bearer token must be the
    `scanner` role (scanner/auto_rescan.py) or the `features` session. Same code path as the
    `python -m app.catalog_import` CLI: app.catalog_import.import_document."""
    if session.get("project") is not None:  # the features session: its own project only
        project = _guard(scope.project_for, session, project)["slug"]
    return _guard(catalog_import.import_document, catalog, project or catalog_import.DEFAULT_PROJECT)


@router.post("/logic/report", status_code=201)
def api_logic_report(report: dict, sha: str, project: str | None = None, session: dict = Depends(require_catalog_importer)):
    """Store the logic map report of one commit (`python -m app.cli logic report`, app/logic_map.py): the release gate reads it. Same
    tokens as the catalog import (the scanner role, or a session on its own project)."""
    from app import logic
    if session.get("project") is not None:      # a session: its own project (or one it works)
        proj = _guard(scope.project_for, session, project)
    else:                                       # the scanner role names the project
        proj = _guard(repo.get_project_by_slug, project or "")
    row = _guard(logic.record_report, proj["id"], sha, report, actor_for(session))
    return {"id": row["id"], "sha": row["sha"], "workflows": len(report["workflows"]), "uncovered": len(report["uncovered"]),
            "stale": len(report["stale"]), "errors": len(report["errors"])}


# --- the owner's prompts (task #581) ----------------------------------------------------------

@router.post("/owner-prompt")
def api_owner_prompt(body: OwnerPromptBody, session: dict = Depends(require_session)):
    """The UserPromptSubmit hook posts each prompt typed into this session (never channel text: that doesn't fire
    the hook). Kept ~30 minutes for this token only, so record_decision can tell a real quote from an invented one."""
    from app import changes as changes_mod
    changes_mod.note_owner_prompt(session["id"], body.prompt)
    return {"ok": True}


# --- outbox -----------------------------------------------------------------------------------

@router.get("/outbox")
def api_list_outbox(stream: str | None = None, undelivered: bool = False,
                    session: dict = Depends(require_session)):
    """The token's OWN outbox: its project, and its stream (an 'all' token: every stream of its project that
    has no session of its own)."""
    return repo.session_outbox(session["project"], _own_stream(session, stream), undelivered_only=undelivered)


@router.post("/outbox/{outbox_id}/delivered")
def api_mark_delivered(outbox_id: int, via: str | None = None, body: DeliveredBody | None = Body(default=None),
                       session: dict = Depends(require_session)):
    """CLAIM a row for delivery: 200 = you won and must relay it; 409 = someone else already did;
    404 = no such row on your stream. `?via=` wins over the (optional) body; default 'hook'."""
    delivered_via = via or (body.delivered_via if body else "hook")
    return _guard(repo.mark_outbox_delivered, outbox_id, delivered_via, stream=session["stream"],
                  project=session["project"])


@router.post("/outbox/{outbox_id}/undelivered")
def api_unclaim_delivered(outbox_id: int, via: str = "channel", session: dict = Depends(require_session)):
    """Undo your own claim after the relay failed, so the row is offered again."""
    return _guard(repo.unclaim_outbox, outbox_id, via, session["stream"], project=session["project"])


@router.post("/outbox", status_code=201)
def api_push_outbox(body: OutboxPush, session: dict = Depends(require_session)):
    """Generic outbox insert for producers other than a task write — e.g. System Health (phase 3)
    escalating a production failure to the errors stream with a dedupe_key so repeats coalesce."""
    pid = _pid(session, body.project)  # its own project: sessions within a project may talk, never across
    if body.task_id is not None:
        _in_scope(scope.task, session, body.task_id)
    _guard(repo.push_outbox, body.stream, body.kind, body.message, project_id=pid, task_id=body.task_id,
           dedupe_key=body.dedupe_key)
    return {"ok": True}


# --- sessions (presence, heartbeated by the per-session channel MCP server) --------------------

@router.post("/sessions/heartbeat")
def api_session_heartbeat(state: str | None = None, body: HeartbeatBody | None = Body(default=None),
                          session: dict = Depends(require_session)):
    """No body = 'online' (the channel's plain POST). `?state=` or {"state": …} for e.g. 'starting'. The
    presence is the token's (project, stream); a `project` in the body must be the token's own."""
    if body is not None and body.project:
        _pid(session, body.project)
    return _guard(repo.heartbeat_session, session["stream"], state or (body.state if body else "online"),
                  project_id=session["project_id"])


@router.post("/sessions/stopping")
def api_session_stopping(body: SessionStopping, session: dict = Depends(require_session)):
    """crewboss-sessions.sh sync, with the stopping session's OWN token: phase notice (tell it to wrap up — a
    fixed, ids-only outbox event — and show 'stopping' in the Sessions panel), cancel (back in the plan), or
    stopped (recorded in the project's activity). 409 if the session is still in the plan."""
    return _guard(repo.session_stopping, session["project"], session["stream"], body.phase)


@router.get("/sessions")
def api_list_sessions(session: dict = Depends(require_session)):
    """The token's own project's sessions only."""
    rows = repo.list_sessions()
    return [r for r in rows if r["project"] == session["project"]["slug"]]


@router.get("/sessions/plan")
def api_session_plan(session: dict = Depends(require_session)):
    """The standing sessions sessions/crewboss-sessions.sh starts: [{project, short_name, role, stream,
    session_name}] for every ACTIVE project that runs sessions. No secrets (tokens live in files). It spans every
    account, so it is the OVERSEER's only (task #368; any other session: 404). The launcher reads the same JSON
    inside the container instead (`python -m app.sessions_plan`), with no token."""
    if not scope.is_overseer(session):
        raise HTTPException(404, "not found")
    return repo.session_plan()


# --- ground (REST twin of the MCP tool, for tmux hooks) ----------------------------------------

@router.get("/ground", response_class=Response)
def api_ground(stream: str | None = None, project: str | None = None, session: dict = Depends(require_session)):
    """Plain-text briefing for a hook to drop straight into a terminal, for the TOKEN's stream. Does
    NOT mark the outbox delivered — poll GET /api/outbox?undelivered=1 and claim with
    POST /api/outbox/{id}/delivered for that."""
    _pid(session, project)  # a session's briefing is strictly its own project
    g = _guard(repo.build_ground, _own_stream(session, stream), None, mark_delivered=False,
               session_project=session["project"], include_pulse=scope.sees_estate(session))
    return Response(content=repo.render_ground_text(g), media_type="text/plain")


# --- System Health (phase 3) --------------------------------------------------------------------

@router.post("/health/zabbix/poke", status_code=202)
def api_zabbix_poke(background: BackgroundTasks, body: ZabbixPoke | None = Body(default=None),
                    _zabbix: dict = Depends(require_zabbix_poke)):
    """Zabbix media type 49 says 'something changed' (a problem opened or cleared). The body is NEVER trusted:
    its eventid is validated and ignored; the catch-up asks Zabbix itself over the read-only token. Answers at
    once (Zabbix waits at most 8 s) and syncs in the background."""
    health.note_poke()
    background.add_task(health.sync)
    return {"ok": True}


@router.post("/health/report")
async def api_health_report(request: Request, project: str | None = None,
                            reporter: dict = Depends(require_reporter)):
    """Pushed app health (task #404, docs/health-report-contract.md): a batch of reports from one of the token's
    project's servers. The project is the TOKEN's: naming another one is a 404, like any other project's data. The
    body is read in chunks and refused past HEALTH_REPORT_MAX_BYTES (413) without buffering more. Answers each report:
    accepted / duplicate / rejected."""
    if project is not None and project != reporter["project"]["slug"]:
        raise HTTPException(404, "not found")
    limit = config.HEALTH_REPORT_MAX_BYTES
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(413, f"body larger than {limit} bytes: send a smaller batch")
    raw = bytearray()
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > limit:
            raise HTTPException(413, f"body larger than {limit} bytes: send a smaller batch")
    try:
        return await asyncio.to_thread(health_reports.receive, reporter, bytes(raw))
    except health_reports.ReportError as exc:
        raise HTTPException(exc.status, str(exc)) from None


@router.get("/summary")
def api_summary(_digest: dict = Depends(require_digest)):
    """Read-only digest feed (#828, docs/digest-summary-contract.md): open code faults, releases waiting for approval and the
    owner's tasks in review, each a count plus at most 10 titled items. ONLY a 'digest' role token; never a task body."""
    return summary_module.build()


@router.get("/servers/{server_id}/manifest")
def api_server_manifest(server_id: int, format: str = "json", server: int = Depends(require_manifest_server)):
    """Read-only server manifest (#726): what is deployed on this server and what must be backed up. ONLY that server's own manifest
    token. format=json (default), yaml or skill (Markdown)."""
    from app import manifest as manifest_module
    cur = manifest_module.current(server)
    if format == "yaml":
        return Response(manifest_module.to_yaml(cur["manifest"]), media_type="application/yaml")
    if format == "skill":
        return Response(cur["skill"], media_type="text/markdown")
    if format != "json":
        raise HTTPException(400, "format: json, yaml or skill")
    return {"hash": cur["hash"], "generated_at": cur["generated_at"], "manifest": cur["manifest"]}


@router.get("/health")
def api_health(session: dict = Depends(require_session)):
    """System Health (the whole estate's Zabbix problems + the ERP feed's state): sync state, open problems and
    recent recoveries. The overseer's and the estate owner's account's only (scope.sees_estate, task #368); any
    other session gets a 404. The owner reads it on the Health page."""
    if not scope.sees_estate(session):
        raise HTTPException(404, "not found")
    s = health.summary()
    s["slot"] = health.slot(s)
    # The ERP feed's state + slot (task #196); the raw snapshot stays on the Health page (it isn't re-served here).
    e = erp_health.summary()
    s["erp"] = {"state": e["state"], "slot": erp_health.slot(e), "last_sync_at": e["source"].get("last_sync_at"),
                "generated_at": e["source"].get("generated_at"), "last_error": e["source"].get("last_error")}
    s["slot_all"] = erp_health.combined_slot(s["slot"], s["erp"]["slot"])
    # No drift (#402): the open findings only (the page has the detail).
    d = drift.summary()
    s["drift"] = {"checked_at": d["ran_at"], "last_error": d["last_error"],
                  "open": [{"host": r["host"], "name": r["name"], "level": "error" if r["severity"] >= drift.ERROR
                            else "warning", "since": r["started_at"]} for r in d["open"]]}
    return s


# --- the global plan (task #129) ----------------------------------------------------------------

@router.get("/plan")
def api_global_plan(session: dict = Depends(require_session)):
    """The current global plan (Now / Next / Later + today) as the global home shows it. It spans EVERY project,
    so only the overseer may read it here (the owner sees it on the global home); any other session gets a 404 (like
    another project's data); a role token a 403; no token a 401 (task #368). dash writes it over MCP
    (set_global_plan)."""
    from app import plan as plan_module

    if not scope.is_overseer(session):
        raise HTTPException(404, "not found")

    out = plan_module.plan_for_home()
    out["planned_at"] = out["planned_at"].isoformat() if out["planned_at"] else None
    return out
