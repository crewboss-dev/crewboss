"""Project isolation for session tokens (owner rule, 2026-09-30; accounts fully partitioned, task #368): sessions
WITHIN a project may work together, sessions in DIFFERENT projects never touch each other's data, and an account
NEVER sees another account's data.

A session token is bound to one node of the project tree (auth.resolve_token): a project, or — the standard since
task #253 (owner, 2026-10-12) — an ACCOUNT. Its REACH is that node's SUBTREE, less any branch that runs a session
of its own (app/tree.py routed_project_ids), and never outside the node's own ACCOUNT PARTITION (tree.account_root_id).
So an account session reads and writes every project under its account and two account sessions never see each
other's work. Every REST route and MCP tool resolves what it touches through these helpers:
- the project a call names must be in the token's reach, else NotFound — the same 404 a missing project gives, so
  another project's existence never leaks;
- a task / subtask / knowledge entry / outbox row of another project is NotFound with the SAME message as an
  id that doesn't exist.

session=None means the OWNER, and only the owner's HTML pages (no login, network-gated by design) ever pass it. The
REST API (/api, auth.require_session) and every MCP tool (mcp_server._require_session) refuse a call without a
session token before reaching these helpers (task #368), so None never reaches them from a machine caller.

THE OVERSEER (the overseer role's 'all' session, i.e. dash) is the one session that crosses accounts, and
only to READ (the read_* helpers: every ACTIVE project) and to COMMUNICATE: file a request task (request_home),
comment (comment_target) and suggest triage (suggest_task) on another project's task. Its own reach — what every
other write checks — is its own partition, i.e. the dashboard root project alone: it never edits, moves or closes
another account's tasks.
"""
from __future__ import annotations

from app import access, repo, roles
from app.repo import NotFound


def reach(session: dict) -> set[int]:
    """The projects a session token may read and write: its own node plus every descendant it works (those with
    no session of their own whose nearest session-running ancestor is this node; app/tree.py), and only inside its
    own account partition (task #368). A descendant that runs its own session belongs to that session, not this one.
    The overseer is no exception here: its reach is the dashboard root alone (the root is a partition of one), so
    its writes stay in its own project; its cross-account READS and requests go through the read_* / request_home /
    comment_target / suggest_task helpers below. Cached on the session dict."""
    if "_reach" not in session:
        from app import tree
        idx = tree.index()
        own = session["project_id"]
        ids = (tree.routed_project_ids(own, idx) & tree.account_member_ids(own, idx)) or {own}
        # Multi-user (#713): a token acts for the person who owns its project and reaches no further than THEY may see. The
        # owner's tokens (and any token on an owner-less project) are unfiltered, as before.
        seen = access.visible_ids(access.of_session(session))
        session["_reach"] = ids if seen is None else (ids & seen) | {own}
    return session["_reach"]


def sees_estate(session: dict | None) -> bool:
    """May this caller see the estate owner's own whole-business data — System Health (Zabbix + the ERP feed) and the
    business pulse (task #368)? The owner (None, pages only), the overseer, or a session in the account that owns
    the health role (the estate account). Never another account's session."""
    from app import tree
    if session is not None and session.get("agent") == tree.EXTERNAL_AGENT:
        return False  # an external agent's token (#574) never sees the estate, wherever its project sits
    if session is None or is_overseer(session):
        return True
    return tree.in_estate_account(session["project_id"])


def check_links(session: dict, links: dict | None) -> None:
    """A session's task `links` may only name tasks and knowledge it can read (task #368, #581): another account's knowledge id is
    NotFound, exactly like a missing one, so a link can't be used to probe or surface another account's entries."""
    if not isinstance(links, dict):
        return
    tids = links.get("tasks")
    for tid in (tids if isinstance(tids, list) else []):
        if isinstance(tid, bool) or not (isinstance(tid, int) or (isinstance(tid, str) and tid.isdigit())):
            continue  # not an id: dropped when the links are stored
        read_task(session, int(tid))  # another account's task is NotFound (the overseer may read any active one)
    ids = links.get("knowledge")
    if not ids:
        return
    for kid in ids if isinstance(ids, list) else [ids]:
        if isinstance(kid, bool) or not (isinstance(kid, int) or (isinstance(kid, str) and kid.isdigit())):
            continue  # not an id: never resolved anywhere (knowledge_titles / changes drop it)
        kid = int(kid)
        try:
            read_knowledge(session, kid)
        except NotFound:
            raise NotFound(f"knowledge {kid} not found") from None


def reach_projects(session: dict) -> list[dict]:
    """reach() as project rows in tree order (the session's own node first): what a session's list reads default
    to when it names no project (task #253)."""
    from app import tree
    ids = reach(session)
    return [p for p in tree.project_tree() if p["id"] in ids] or [session["project"]]


def list_project_ids(session: dict | None, slug: str | None) -> list[int]:
    """The projects a LIST read (tasks, changes, activity) covers (task #253):
    - a session naming no project: its whole reach (an account session: every project it works);
    - an ACCOUNT named (by anyone allowed to read it): the account and the projects under it — within the
      session's reach, or every active one for the overseer and the owner;
    - any other project named, or the owner naming none: just that project (read_project_for rules apply)."""
    from app import tree
    project = read_project_for(session, slug)
    if session is not None and not slug:
        return [p["id"] for p in reach_projects(session)]
    if tree.is_account(project):
        ids = tree.descendant_ids(project["id"])
        if session is not None and not is_overseer(session):
            ids &= reach(session)
        return [p["id"] for p in tree.project_tree() if p["id"] in ids] or [project["id"]]
    return [project["id"]]


def read_project_ids(session: dict | None, slug: str | None) -> list[int]:
    """The projects a per-project READ (knowledge, workflows, objectives) covers (task #253): a session naming no
    project covers its whole reach (its own node first, in tree order); a named project — an account included, whose
    own entries are its SHARED items, never a rollup — or the owner's call is that one project, by read_project_for
    (the overseer may name any active project; with none it reads its own, as before)."""
    if session is not None and not slug:
        return [p["id"] for p in reach_projects(session)]
    return [read_project_for(session, slug)["id"]]


def _slugs(ids: list[int]) -> dict[int, str]:
    from app import tree
    idx = tree.index()
    return {i: idx[i]["slug"] for i in ids if i in idx}


def knowledge_list(session: dict | None, slug: str | None, **filters) -> list[dict]:
    """GET /api/knowledge: list_knowledge over read_project_ids (each row carries its project_id)."""
    return [row for pid in read_project_ids(session, slug) for row in repo.list_knowledge(pid, **filters)]


def knowledge_search(session: dict | None, slug: str | None, query: str, limit: int = 10) -> list[dict]:
    """The search_knowledge tool over read_project_ids, the session's own node's hits first."""
    return [row for pid in read_project_ids(session, slug) for row in repo.search_knowledge(pid, query, limit)][:limit]


def objectives(session: dict | None, slug: str | None) -> list[dict]:
    return [row for pid in read_project_ids(session, slug) for row in repo.list_objectives(pid)]


def workflows(session: dict | None, slug: str | None, **filters) -> list[dict]:
    """list_workflows over read_project_ids. Workflow ids are per project, so across several projects each row also
    carries its `project` slug."""
    ids = read_project_ids(session, slug)
    if len(ids) == 1:
        return repo.list_workflows(ids[0], **filters)
    slug_of = _slugs(ids)
    return [dict(w, project=slug_of.get(pid)) for pid in ids for w in repo.list_workflows(pid, **filters)]


def workflow(session: dict | None, slug: str | None, workflow_id: str) -> dict:
    """get_workflow: the named project's, or — a session naming none — the first project in its reach (tree order)
    that has it, with its `project` slug. NotFound when none does."""
    ids = read_project_ids(session, slug)
    if len(ids) == 1:
        return repo.get_workflow(ids[0], workflow_id)
    slug_of = _slugs(ids)
    for pid in ids:
        try:
            return dict(repo.get_workflow(pid, workflow_id), project=slug_of.get(pid))
        except NotFound:
            continue
    raise NotFound(f"workflow {workflow_id!r} not found")


def list_tasks(session: dict | None, slug: str | None, **filters) -> list[dict]:
    """The list_tasks read (MCP tool and GET /api/tasks): one project's tasks as ever, or — several projects in
    list_project_ids — the rolled-up list, each task carrying its project_slug."""
    ids = list_project_ids(session, slug)
    if len(ids) == 1:
        return repo.list_tasks(ids[0], with_progress=True, **filters)
    return repo.list_tasks(None, project_ids=ids, with_progress=True, **filters)


def task_home(session: dict, slug: str | None) -> dict:
    """The project a session's new task goes to: project_for, but never an account (accounts hold no tasks, task
    #148) — an account session must name one of its projects, and the refusal lists them."""
    from app import tree
    project = project_for(session, slug)
    if tree.is_account(project):
        options = [p["slug"] for p in reach_projects(session) if not tree.is_account(p)]
        raise repo.BadRequest(f"{project['name']} is an account: tasks live in its projects. Name one with "
                              f"project=: {', '.join(options) or '(none in your reach)'}")
    return project


def request_home(session: dict, slug: str | None) -> tuple[dict, bool]:
    """Where a new task goes, and whether it's an overseer REQUEST to another session (owner, 2026-10-13: "I can
    build everything from dash if I want and it just sends requests to things below to do"). The overseer may file
    a task in any active, non-account project; the owning session picks it up. Dash still can't edit, move or close
    it there. Returns (project, is_cross_project_request)."""
    from app import tree
    if is_overseer(session) and slug and slug != session["project"]["slug"]:
        project = read_project_for(session, slug)
        if project["id"] not in reach(session):
            if tree.is_account(project):
                raise repo.BadRequest(f"{project['name']} is an account: name one of its projects")
            return project, True
    return task_home(session, slug), False


def project_for(session: dict | None, slug: str | None) -> dict:
    """The project a call acts on. With a session token: the token's project, or a child project it works
    (naming anything else is a 404). Without one: the named project, else the default."""
    if session is not None:
        own = session["project"]
        if slug and slug != own["slug"]:
            try:
                other = repo.get_project_by_slug(slug)
            except NotFound:
                raise NotFound(f"project {slug!r} not found") from None
            if other["id"] not in reach(session) or other.get("archived_at"):
                raise NotFound(f"project {slug!r} not found")
            return other
        return own
    return repo.get_project_by_slug(slug or roles.slug("default"))


def check_project_id(session: dict | None, project_id: int | None, what: str) -> None:
    if session is not None and project_id not in reach(session):
        raise NotFound(f"{what} not found")


def task(session: dict | None, task_id: int) -> dict:
    """The task, if the caller may see it (NotFound exactly like a missing id)."""
    row = repo.get_task(task_id)
    check_project_id(session, row["project_id"], f"task {task_id}")
    return row


def milestone(session: dict | None, milestone_id: int) -> dict:
    """A milestone (#915), if the caller may touch it: its project is in the session's reach. Another account's id is NotFound
    exactly like a missing one. The overseer gets no cross-account WRITE here (it reads through milestones_list)."""
    from app import planning
    row = planning.get_milestone(milestone_id)
    check_project_id(session, row["project_id"], f"milestone {milestone_id}")
    return row


def milestones_list(session: dict | None, slug: str | None, include_archived: bool = False) -> list[dict]:
    """list_milestones over read_project_ids: a session's whole reach, a named project (the overseer: any active one)."""
    from app import planning
    return planning.list_milestones(read_project_ids(session, slug), include_archived)


def subtask(session: dict | None, subtask_id: int) -> dict:
    sub = repo.get_subtask(subtask_id)
    try:
        task(session, sub["task_id"])
    except NotFound as exc:
        raise NotFound(f"subtask {subtask_id} not found") from exc
    return sub


def knowledge(session: dict | None, knowledge_id: int) -> dict:
    row = repo.get_knowledge(knowledge_id)
    check_project_id(session, row["project_id"], f"knowledge {knowledge_id}")
    return row


def ground_slug(session: dict) -> str:
    """A session's briefing is its own node (plus the projects it works, listed under it)."""
    return session["project"]["slug"]


# --- the overseer's cross-project READS (task #129) ------------------------------------------------------------

def is_overseer(session: dict | None) -> bool:
    """dash: the OVERSEER_PROJECT's one 'all' session. Never a stream session, never another project's."""
    return bool(session and session.get("stream") == "all"
                and roles.is_role(session["project"], "overseer")
                and not session["project"].get("archived_at"))


def _active(project: dict, what: str) -> dict:
    if project.get("archived_at"):
        raise NotFound(f"{what} not found")
    return project


def read_project_for(session: dict | None, slug: str | None) -> dict:
    """Like project_for, but the overseer may name any ACTIVE project (no slug = its own)."""
    if is_overseer(session) and slug and slug != session["project"]["slug"]:
        return _active(repo.get_project_by_slug(slug), f"project {slug!r}")
    return project_for(session, slug)


def read_check_project_id(session: dict | None, project_id: int | None, what: str) -> None:
    if is_overseer(session) and project_id is not None:
        _active(repo.get_project(project_id), what)
        return
    check_project_id(session, project_id, what)


def read_task(session: dict | None, task_id: int) -> dict:
    row = repo.get_task(task_id)
    read_check_project_id(session, row["project_id"], f"task {task_id}")
    return row


def inherited_accounts(session: dict) -> set[int]:
    """The accounts ABOVE a project in the session's reach (task #148): their shared rules, AI skills and knowledge
    are in the session's briefing, so it may READ them (get_knowledge). Writing there follows reach as ever: only a
    session whose reach includes the account itself (the account's own session, since task #253). Cached
    on the session dict."""
    if "_inherited" not in session:
        from app import shared
        session["_inherited"] = shared.inherited_account_ids(reach(session))
    return session["_inherited"]


def read_knowledge(session: dict | None, knowledge_id: int) -> dict:
    row = repo.get_knowledge(knowledge_id)
    if session is not None and row["project_id"] in inherited_accounts(session):
        from app import shared
        if row["archived_at"] is None and row["kind"] not in shared.NOT_INHERITED:
            return row  # an inherited shared item: readable, never writable from here
    read_check_project_id(session, row["project_id"], f"knowledge {knowledge_id}")
    return row


def comment_target(session: dict | None, task_id: int) -> dict:
    """The overseer may COMMENT on any active project's task (owner, 2026-10-13: "dash just needs to communicate").
    A comment changes nothing on the task; the owning session gets an ids-only event and reads it as a request.
    Every other write (status, checklist, fields) stays limited to the session's own reach."""
    return read_task(session, task_id) if is_overseer(session) else task(session, task_id)


def suggest_task(session: dict | None, task_id: int) -> dict:
    """A triage suggestion (Accept / Keep mine) is the one write the overseer may make on another project's task:
    the owner decides, nothing changes until they do."""
    return read_task(session, task_id) if is_overseer(session) else task(session, task_id)


# --- materials (task #295) -------------------------------------------------------------------------------------

def materials_project(session: dict | None, slug: str | None) -> dict:
    """Whose effective materials list_materials reads: the overseer any active project, a session its own reach."""
    return read_project_for(session, slug)


def check_material_readable(session: dict | None, project_id: int) -> None:
    """A material is readable where it applies: on a node the session may read, or on an ANCESTOR of one up to and
    including its account — never above the account (task #368: nothing is inherited from the dashboard root or across
    accounts; tree.inherit_chain). Anything else is a 404, like every other cross-project read."""
    from app import tree
    if is_overseer(session) or session is None:
        read_check_project_id(session, project_id, "material")
        return
    idx = tree.index()
    allowed = set(reach(session))
    for pid in list(allowed):
        if pid in idx:
            allowed.update(a["id"] for a in tree.inherit_chain(idx[pid], idx))
    if project_id not in allowed:
        raise repo.NotFound("material not found")


# --- servers (task #367) ---------------------------------------------------------------------------------------

def server_account(session: dict) -> dict | None:
    """The account whose servers a session may be signed SSH passes for: its own account partition's root, when that
    is an account (tree.account_root_id), else None. Signing is a write, so the overseer is no exception: its partition
    is the dashboard root, which has no servers, and another account's server answers 404 like a missing one."""
    from app import tree
    if session.get("agent") == tree.EXTERNAL_AGENT:
        raise NotFound("server not found")  # an external agent (#574) gets no SSH passes and no server list
    if is_overseer(session):
        return None     # the overseer sits inside the estate account now (change #76) but is still given no SSH passes
    idx = tree.index()
    root = idx.get(tree.account_root_id(session["project_id"], idx))
    return root if tree.is_account(root) and not root.get("archived_at") else None


def server_read_account_ids(session: dict) -> set[int]:
    """Whose servers a session may LIST (no secrets): its own account's, or every active account's for the overseer
    (its cross-account reads, task #368)."""
    from app import tree
    if is_overseer(session):
        return {p["id"] for p in tree.project_tree() if tree.is_account(p)}
    account = server_account(session)
    return {account["id"]} if account else set()
