"""The project TREE (owner, 2026-10-01; dashboard k30, task #132).

There is no project root (change #76): level 0 = accounts (kind 'account') and top-level personal projects; below an
account, its projects, which may have children of their own. dash (the overseer role) is an ordinary project, usually under
the estate account, with its own session.

Sessions attach to ACCOUNTS (owner, 2026-10-12; task #253): each account runs ONE standing session named by its
short name (e.g. `acme`), which works its whole subtree; dash is the overseer (its own project, the global plan, reads
everywhere). The model stays general: ANY node may run a session (personal-pa, a top-level personal project, does);
an account gets one by default. A project with no session of its own is worked by its nearest ancestor that has
one (`session_holder`): that session's token reaches it (scope.py, via `routed_project_ids`), its events land in that
session's outbox (repo._outbox_scope) and it shows in that session's briefing. A descendant that runs its own
session, and its branch, belongs to that session instead.

A project = a repo, or a folder inside one (repo_path). Environments are OPTIONAL: most projects are worked on live
(prod only); the ERP and the AiOS have dev + prod, kept in sync (dev_prod_sync).
"""
from __future__ import annotations

import re

import psycopg

from app import config, repo, roles
from app.db import get_cursor
from app.repo import BadRequest, Forbidden, NotFound, is_owner, record_activity

ENV_FIELDS = ("server", "path", "url", "deploy_method", "zabbix_host")
_REPO_RE = re.compile(r"^[A-Za-z0-9._/-]{0,200}$")


# --- reading the tree -------------------------------------------------------------------------------------------

def _index(cur) -> dict[int, dict]:
    cur.execute("SELECT * FROM projects")
    return {r["id"]: r for r in cur.fetchall()}


def index() -> dict[int, dict]:
    with get_cursor() as cur:
        return _index(cur)


def _children(idx: dict[int, dict]) -> dict:
    kids: dict = {}
    for p in idx.values():
        parent = p.get("parent_id")
        kids.setdefault(parent if parent in idx else None, []).append(p)
    return kids


def _order(p: dict):
    return (0 if roles.is_role(p, "overseer") else 1,
            0 if p.get("kind") == "account" else 1, p["name"].lower())


def project_tree(include_archived: bool = False, idx: dict[int, dict] | None = None) -> list[dict]:
    """Every project in tree order (dash first, then accounts and their projects, children under parents), each
    with `depth`, `child_count` and `parent_name`. An archived project, and its branch, is left out unless asked."""
    idx = idx if idx is not None else index()
    kids = _children(idx)
    out: list[dict] = []

    def walk(parent, depth):
        for p in sorted(kids.get(parent, []), key=_order):
            if p["archived_at"] and not include_archived:
                continue
            live_kids = [c for c in kids.get(p["id"], []) if include_archived or not c["archived_at"]]
            parent_row = idx.get(p.get("parent_id"))
            out.append(dict(p, depth=depth, child_count=len(live_kids),
                            parent_name=parent_row["name"] if parent_row else None))
            walk(p["id"], depth + 1)

    walk(None, 0)
    return out


def with_guides(rows: list[dict]) -> list[dict]:
    """Add the tree GUIDE LINES to a list of rows already in tree order (task #148; #157). Per row, `last` = it is
    the last of its (active) siblings, so it branches └ rather than ├; `guides` = for each ancestor level between the
    root and it (depth >= 1), whether a line still runs on down past that ancestor (it has a sibling still to come).
    Shared by the topbar switcher (switcher_rows) and the All projects home tree, so both read the same levels.
    Rows carry `id`, `parent_id`, `depth` and `archived_at` (project_tree rows do). Returns fresh dicts."""
    by_id = {r["id"]: r for r in rows}
    sibs: dict = {}
    for r in rows:
        sibs.setdefault(r.get("parent_id") if r.get("parent_id") in by_id else None, []).append(r)
    last = {}
    for group in sibs.values():
        for r in group:
            pool = group if r.get("archived_at") else [x for x in group if not x.get("archived_at")]
            last[r["id"]] = not pool or pool[-1]["id"] == r["id"]
    out = []
    for r in rows:
        chain, cur, seen = [], by_id.get(r.get("parent_id")), {r["id"]}
        while cur is not None and cur["id"] not in seen:
            seen.add(cur["id"])
            chain.append(cur)
            cur = by_id.get(cur.get("parent_id"))
        chain = [a for a in reversed(chain) if a.get("depth", 0) >= 1]
        out.append(dict(r, last=last[r["id"]], guides=[not last[a["id"]] for a in chain]))
    return out


def ancestors(project: dict, idx: dict[int, dict] | None = None) -> list[dict]:
    """Nearest first (a breadcrumb reads them reversed)."""
    idx = idx if idx is not None else index()
    out, seen, cur_id = [], {project["id"]}, project.get("parent_id")
    while cur_id and cur_id in idx and cur_id not in seen:
        seen.add(cur_id)
        out.append(idx[cur_id])
        cur_id = idx[cur_id].get("parent_id")
    return out


def children(project_id: int, idx: dict[int, dict] | None = None) -> list[dict]:
    idx = idx if idx is not None else index()
    return sorted([p for p in idx.values() if p.get("parent_id") == project_id and not p["archived_at"]], key=_order)


def is_account(p: dict | None) -> bool:
    return bool(p) and p.get("kind") == "account"


def is_runner_home(p: dict | None, idx: dict[int, dict] | None = None) -> bool:
    """May runners be enrolled for this node (task #627, #713)? An account, or the top of a personal partition (a designer's own
    top-level project: nothing sits above it but the dashboard root). Never the dashboard root itself."""
    if not p or is_global_root(p):
        return False
    return is_account(p) or account_root_id(p["id"], idx) == p["id"]


def descendant_ids(project_id: int, idx: dict[int, dict] | None = None) -> set[int]:
    """The project and every non-archived descendant (an archived project's branch is left out, as in
    project_tree). What an account page rolls up (task #148)."""
    idx = idx if idx is not None else index()
    if project_id not in idx:
        return set()
    kids = _children(idx)
    out, todo = {project_id}, [c["id"] for c in kids.get(project_id, [])]
    while todo:
        pid = todo.pop()
        if pid in out or idx[pid]["archived_at"]:
            continue
        out.add(pid)
        todo += [c["id"] for c in kids.get(pid, [])]
    return out


def subtree(project_id: int, idx: dict[int, dict] | None = None) -> list[dict]:
    """descendant_ids in tree order, each with `depth` relative to the project (the project itself is 0)."""
    idx = idx if idx is not None else index()
    ids = descendant_ids(project_id, idx)
    rows = [p for p in project_tree(idx=idx) if p["id"] in ids]
    base = rows[0]["depth"] if rows else 0
    return [dict(p, depth=p["depth"] - base) for p in rows]


def account_ancestors(project: dict, idx: dict[int, dict] | None = None) -> list[dict]:
    """The active ACCOUNT above a project, as a list (nearest first): whose shared rules, AI skills and knowledge it
    inherits (task #148). Inheritance never crosses an account boundary (task #368), so this is at most one: the
    nearest account above it — never an account above that one."""
    idx = idx if idx is not None else index()
    for a in ancestors(project, idx):
        if is_account(a):
            return [] if a["archived_at"] else [a]
    return []


# --- ACCOUNT PARTITIONS (task #368, owner rule) -----------------------------------------------------------------
# Accounts are fully partitioned: a project sees itself and its descendants, a parent everything below it up to its
# account, and an account NEVER another account's data. Only the global Dashboard level (the owner's pages and the
# overseer session) sees across accounts. A node's PARTITION ROOT (`account_root_id`) is:
#   - the nearest account at or above it;
#   - else (a top-level personal project such as personal-pa, or a test tree with no accounts) the topmost node: that
#     node's subtree is its own partition.
# The tree has NO project root (change #76): accounts and personal projects are top level, and the overseer's project is an
# ordinary project (usually inside the estate account) that keeps its own session and the overseer role. It is the "global
# root" only in the sense that its READS span accounts; its WRITES are its own routed projects (app/scope.py).

def _global_root_id(idx: dict[int, dict]) -> int | None:
    if not roles.slug("overseer"):
        return None
    root = roles.holder(idx, "overseer")
    return root["id"] if root else None


def is_global_root(project: dict | None) -> bool:
    return bool(project) and roles.is_role(project, "overseer")


def account_root_id(project_id: int | None, idx: dict[int, dict] | None = None) -> int | None:
    """The id of the partition root a project belongs to (see above); None for an unknown id."""
    idx = idx if idx is not None else index()
    if project_id not in idx:
        return None
    project = idx[project_id]
    chain = [project, *ancestors(project, idx)]
    for node in chain:
        if is_account(node):
            return node["id"]
    return chain[-1]["id"]          # no account above: the topmost node is its own partition


def account_member_ids(project_id: int, idx: dict[int, dict] | None = None) -> set[int]:
    """Every project (archived ones included) in the same account partition as this one."""
    idx = idx if idx is not None else index()
    mine = account_root_id(project_id, idx)
    if mine is None:
        return set()
    return {pid for pid in idx if account_root_id(pid, idx) == mine}


def same_account(a_id: int | None, b_id: int | None, idx: dict[int, dict] | None = None) -> bool:
    idx = idx if idx is not None else index()
    ra = account_root_id(a_id, idx)
    return ra is not None and ra == account_root_id(b_id, idx)


def inherit_chain(project: dict, idx: dict[int, dict] | None = None) -> list[dict]:
    """The project and its ancestors, nearest first, up to and INCLUDING its partition root and never above it:
    what a project inherits from (materials, task #368). Nothing inherits from the global root."""
    idx = idx if idx is not None else index()
    stop = account_root_id(project["id"], idx)
    chain = [project]
    if project["id"] == stop:
        return chain
    for node in ancestors(project, idx):
        chain.append(node)
        if node["id"] == stop:
            break
    return chain


def estate_account_id(idx: dict[int, dict] | None = None) -> int | None:
    """The partition that owns the estate's own business data (System Health, the business pulse): the account of
    the health role (acme-erp → the Acme account). None when that project doesn't exist."""
    idx = idx if idx is not None else index()
    hp = roles.holder(idx, "health")
    return account_root_id(hp["id"], idx) if hp is not None else None


def in_estate_account(project_id: int | None, idx: dict[int, dict] | None = None) -> bool:
    idx = idx if idx is not None else index()
    est = estate_account_id(idx)
    return est is not None and account_root_id(project_id, idx) == est


def has_session(p: dict) -> bool:
    """Does this node have a session of its own for ROUTING (own outbox, its branch left out of the parent's reach)?
    A standing tmux session (session_roles) or an external agent (#574: a Claude Code session on another machine,
    e.g. the owner's desktop). Only the first is in repo.session_plan; see runs_tmux_session."""
    return (bool(p.get("session_roles")) or bool(p.get("external_agent"))) and not p.get("archived_at")


def runs_tmux_session(p: dict) -> bool:
    """A standing tmux session on the sessions server (session_roles) -- what the launcher starts and sync winds down."""
    return bool(p.get("session_roles")) and not p.get("archived_at")


def _same_owner(a: dict, b: dict, group: frozenset | None = None) -> bool:
    """Multi-user (#713): work never routes ACROSS people. A session (a Claude on the owner's dev box) works the projects its own
    person owns; a designer's project under the owner's tree is not its session's to read or be told about.
    The INSTANCE OWNER is one principal however many users rows they have (a legacy Microsoft row and a local account): every
    owner-role user, and a project with no recorded owner, count as the same person (`group`: access.owner_user_ids())."""
    if group is None:
        from app import access
        group = access.owner_user_ids()
    norm = lambda u: None if u is None or u in group else u   # noqa: E731
    return norm(a.get("owner_user_id")) == norm(b.get("owner_user_id"))


def _holder_id(idx: dict[int, dict], project_id: int) -> int | None:
    """Consistent with routed_project_ids: the walk STOPS at an archived project (none of its branch is routed), and at a
    project another person owns."""
    seen, cur_id = set(), project_id
    from app import access
    group = access.owner_user_ids()
    while cur_id and cur_id in idx and cur_id not in seen:
        seen.add(cur_id)
        if idx[cur_id]["archived_at"] or not _same_owner(idx[cur_id], idx[project_id], group):
            return None
        if has_session(idx[cur_id]):
            return cur_id
        cur_id = idx[cur_id].get("parent_id")
    return None


def session_holder(project_id: int, idx: dict[int, dict] | None = None) -> dict | None:
    """The project whose Claude session works this one: itself if it runs a session, else its nearest ancestor
    that does (None if none does)."""
    idx = idx if idx is not None else index()
    hid = _holder_id(idx, project_id)
    return idx.get(hid) if hid else None


def routed_project_ids(project_id: int, idx: dict[int, dict] | None = None) -> set[int]:
    """What a session of `project_id` is responsible for: itself plus every non-archived descendant whose nearest
    session-running ancestor is this project. Used for a session token's reach and for its outbox."""
    idx = idx if idx is not None else index()
    if project_id not in idx:
        return set()
    kids = _children(idx)
    out = {project_id}
    todo = [c["id"] for c in kids.get(project_id, [])]
    from app import access
    group = access.owner_user_ids()
    while todo:
        pid = todo.pop()
        p = idx[pid]
        if p["archived_at"] or has_session(p) or not _same_owner(p, idx[project_id], group):
            continue  # archived, worked by its own session (with its branch), or another person's (#713)
        out.add(pid)
        todo += [c["id"] for c in kids.get(pid, [])]
    return out


def session_parent_slug(slug: str, idx: dict[int, dict] | None = None) -> str | None:
    """For the SendMessage guard: the project whose session sits directly above this project's session (the
    nearest ancestor of its parent that runs a session)."""
    idx = idx if idx is not None else index()
    me = next((p for p in idx.values() if p["slug"] == slug), None)
    if not me or not me.get("parent_id"):
        return None
    hid = _holder_id(idx, me["parent_id"])
    return idx[hid]["slug"] if hid else None


def tree_for_guard(only_ids: set[int] | None = None) -> list[dict]:
    """[{slug, parent, session_parent, has_session}] for every active project — or only those in `only_ids` (a
    session's own account partition, task #368): what the SendMessage guard needs (no secrets, no task data)."""
    idx = index()
    return [{"slug": p["slug"], "parent": idx[p["parent_id"]]["slug"] if p.get("parent_id") in idx else None,
             "session_parent": session_parent_slug(p["slug"], idx), "has_session": has_session(p)}
            for p in idx.values() if not p["archived_at"] and (only_ids is None or p["id"] in only_ids)]


def rollups(tree: list[dict], counts: dict[int, dict]) -> dict[int, dict]:
    """Per project: its own open-task counts plus every descendant's (a parent card shows its whole branch).
    `questions` = unanswered questions: a session waiting on the owner (the home's "blocked")."""
    by_id = {p["id"]: p for p in tree}
    keys = ("open", "must", "doing", "review", "questions")
    roll = {pid: dict.fromkeys(keys, 0) for pid in by_id}
    for pid, c in counts.items():
        cur_id, seen = pid, set()
        while cur_id in by_id and cur_id not in seen:
            seen.add(cur_id)
            for k in keys:
                roll[cur_id][k] += int(c.get(k) or 0)
            cur_id = by_id[cur_id].get("parent_id")
    return roll


# --- changing the tree (owner only) ------------------------------------------------------------------------------

def validate_place(idx: dict[int, dict], project: dict | None, parent_id: int | None, kind: str) -> None:
    if kind not in config.PROJECT_KINDS:
        raise BadRequest(f"kind must be one of {config.PROJECT_KINDS}")
    if kind == "account" and parent_id is not None:
        raise BadRequest("an account sits at the top of the tree")        # the tree has no project root (change #76)
    if project is not None and roles.is_role(project, "overseer") and kind != "project":
        raise BadRequest("the dashboard project is a project, not an account")
    if kind == "account" and project is not None and project.get("kind") != "account":
        # Accounts hold no tasks (task #148): a project with open work can't become one until its tasks move.
        n = repo.count_open_tasks(project["id"])
        if n:
            raise BadRequest(f"{project['name']} has {n} open task{'s' if n != 1 else ''}; an account holds none. "
                             "Move them to one of its projects first")
    if parent_id is None:
        return
    if parent_id not in idx:
        raise BadRequest(f"parent project {parent_id} not found")
    parent = idx[parent_id]
    if parent["archived_at"]:
        raise BadRequest("the parent project is archived")
    if project is not None:
        seen, cur_id = set(), parent_id
        while cur_id and cur_id in idx and cur_id not in seen:
            if cur_id == project["id"]:
                raise BadRequest("that would make the project its own ancestor")
            seen.add(cur_id)
            cur_id = idx[cur_id].get("parent_id")


def set_place(project_id: int, actor: str, parent_id: int | None, kind: str | None = None) -> dict:
    """Owner only: move a project in the tree (and/or change its kind)."""
    if not is_owner(actor):
        raise Forbidden("only the owner moves projects in the tree")
    with get_cursor() as cur:
        idx = _index(cur)
        if project_id not in idx:
            raise NotFound(f"project {project_id} not found")
        project = idx[project_id]
        kind = kind or project.get("kind") or "project"
        validate_place(idx, project, parent_id, kind)
        cur.execute("UPDATE projects SET parent_id = %s, kind = %s WHERE id = %s RETURNING *",
                    (parent_id, kind, project_id))
        row = cur.fetchone()
        # Connections (#784) must stay inside one account and never touch an account: a move or kind change that would break any is refused
        # (raising rolls the UPDATE back). The owner sees everything, so the count is exact.
        cur.execute("SELECT count(*) AS n FROM project_connections c JOIN projects p ON p.id = c.project_id JOIN projects o ON o.id = c.other_id "
                    "WHERE p.kind = 'account' OR o.kind = 'account' OR project_account_root(c.project_id) IS DISTINCT FROM project_account_root(c.other_id)")
        broken = cur.fetchone()["n"]
        if broken:
            raise BadRequest(f"{row['name']} has connections that this move would break ({broken} connection{'s' if broken != 1 else ''} "
                             "would cross accounts or touch an account): remove its connections first")
        # Handoffs and shared docs (#908) must stay inside one account too: a move that would leave a handoff with an end outside its
        # account, send one to an account, or leave a doc's account no longer a partition root is refused.
        cur.execute("SELECT count(*) AS n FROM handoffs h WHERE project_account_root(h.from_project_id) IS DISTINCT FROM h.account_id "
                    "OR project_account_root(h.to_project_id) IS DISTINCT FROM h.account_id "
                    "OR EXISTS (SELECT 1 FROM projects t WHERE t.id = h.to_project_id AND t.kind = 'account')")
        split = cur.fetchone()["n"]
        cur.execute("SELECT count(*) AS n FROM account_docs WHERE project_account_root(account_id) IS DISTINCT FROM account_id")
        split += cur.fetchone()["n"]
        if split:
            raise BadRequest(f"{row['name']} has handoffs or shared docs that this move would split across accounts ({split}): they must stay inside "
                             "one account, so this project cannot move out of it while they exist")
        where = idx[parent_id]["name"] if parent_id else "the top"
        record_activity(cur, project_id, actor, "moved", "project", project_id,
                        f"Project '{row['name']}' placed under {where} ({kind})")
    return row


def _pinned_slugs() -> dict[str, str]:
    """{slug: what names it} for every project the server's settings name BY SLUG."""
    from app import drift, health_reports
    pinned = {roles.slug("overseer"): "the dashboard root", roles.slug("dashboard"): "the Dashboard project",
              roles.slug("health"): "the System Health feed", roles.slug("default"): "the default project",
              roles.slug("legacy_token"): "the legacy session tokens", roles.slug("export"): "the catalog export",
              config.FALLBACK_SESSION_SLUG: "the session launcher's boot fallback (sessions/streams.sh)"}
    pinned.update({s: "a platform key store" for s in config.PLATFORM_KEY_STORES})
    pinned.update({s: "a health feed (CREWBOSS_HOME/health_feeds.yaml)" for s in health_reports.feeds()})
    pinned.update({s: "a linked admin site (CREWBOSS_HOME/drift.yaml)" for s in drift.load_config().get("admin_sites") or {}})
    pinned.pop("", None)
    return pinned


def slug_locked(project: dict) -> str:
    """Why this project's SLUG can't change now, or '' if it can (#577); its name can always change.
    - The server's settings name it by slug (_pinned_slugs): renaming would silently cut that wiring.
    - It runs its own Claude session: the running session keeps its project slug in its environment (CREWBOSS_PROJECT, or MALLEN_PROJECT before the rename), so its wind-down and the
      SendMessage guard would break. A project worked by an ancestor's session (no session_roles) is free: events are
      routed to that session by project id (repo._outbox_scope / routed_project_ids), never by slug."""
    why = _pinned_slugs().get(project["slug"])
    if why:
        return f"the server's settings name it as {why}"
    if runs_tmux_session(project):
        names = ", ".join(repo.session_name(project, r) for r in project["session_roles"])
        return (f"this project runs its own Claude session ({names}); change the slug when its sessions are off, "
                "or rename only the name")
    return ""


def rename_project(project_id: int, actor: str, name: str, slug: str | None = None) -> dict:
    """Owner only (#577): change a project's name and, optionally, its slug. A changed slug leaves the old one as an
    ALIAS (project_slug_aliases), so old URLs redirect and callers naming it still resolve; renaming back to an old
    slug of its own drops that alias. The slug is unique against current slugs AND every other project's old slugs
    (whose links still lead there), and can't change while slug_locked says so. The SHORT NAME never changes: the
    session launcher picks a token by tokens/<short>/<role>, so a freed and reused short name would start another
    project's session with this one's token."""
    if not is_owner(actor):
        raise Forbidden("only the owner renames projects")
    name = " ".join((name or "").split())
    if not name:
        raise BadRequest("give the project a name")
    with get_cursor() as cur:
        idx = _index(cur)
        if project_id not in idx:
            raise NotFound(f"project {project_id} not found")
        project = idx[project_id]
        slug = (slug or "").strip() or project["slug"]
        changed = []
        if slug != project["slug"]:
            locked = slug_locked(project)
            if locked:
                raise BadRequest(f"{project['name']}'s slug can't change: {locked}")
            repo.validate_slug(slug)
            if any(p["slug"] == slug for p in idx.values()):
                raise BadRequest(f"a project with slug {slug!r} already exists")
            owner_id = repo.slug_alias_owner(cur, slug)
            if owner_id is not None and owner_id != project_id:
                raise BadRequest(f"{slug!r} is {idx[owner_id]['name']}'s old slug (its old links still lead there); "
                                 "pick another")
            changed.append(f"slug {project['slug']} → {slug}")
        if name != project["name"]:
            changed.insert(0, f"'{project['name']}' → '{name}'")
        if not changed:
            return project
        try:
            cur.execute("UPDATE projects SET name = %s, slug = %s WHERE id = %s RETURNING *", (name, slug, project_id))
        except psycopg.errors.UniqueViolation as exc:     # a project took it between the check and here
            raise BadRequest(f"a project with slug {slug!r} already exists") from exc
        row = cur.fetchone()
        if slug != project["slug"]:
            cur.execute("DELETE FROM project_slug_aliases WHERE slug = %s", (slug,))   # renamed back: no longer old
            cur.execute("INSERT INTO project_slug_aliases (slug, project_id) VALUES (%s, %s) "
                        "ON CONFLICT (slug) DO UPDATE SET project_id = EXCLUDED.project_id, created_at = now()",
                        (project["slug"], project_id))
        record_activity(cur, project_id, actor, "renamed", "project", project_id,
                        f"Project renamed: {', '.join(changed)}")
    return row


def valid_git_remote(value: str) -> str:
    """The remote, stripped, if it is `https://<host>/<owner>/<repo>.git` or `git@<host>:<owner>/<repo>.git` for a host in
    config.GIT_REMOTE_HOSTS (owner and repo: letters, digits, . _ -, never starting with - or ., at most 100 characters);
    '' (clear it) is allowed. Anything else is a BadRequest: a runner clones and pushes this, so it is never free text."""
    value = (value or "").strip()
    if not value:
        return ""
    hosts = "|".join(re.escape(h) for h in config.GIT_REMOTE_HOSTS)
    part = r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}"
    if re.fullmatch(rf"https://(?:{hosts})/{part}/{part}\.git", value) or \
            re.fullmatch(rf"git@(?:{hosts}):{part}/{part}\.git", value):
        return value
    raise BadRequest("git remote must look like https://github.com/<owner>/<repo>.git or git@github.com:<owner>/<repo>.git")


def set_details(project_id: int, actor: str, repo_name: str | None = None, repo_path: str | None = None,
                dev_prod_sync: bool | None = None, note: str | None = None, git_remote: str | None = None) -> dict:
    """Owner only: the project's repo (+ folder in it), the dev/prod sync flag, a short note and the git remote a runner
    clones from."""
    if not is_owner(actor):
        raise Forbidden("only the owner edits a project's details")
    fields, params = [], []
    if git_remote is not None:
        fields.append("git_remote = %s")
        params.append(valid_git_remote(git_remote))
    for col, val in (("repo", repo_name), ("repo_path", repo_path)):
        if val is not None:
            val = val.strip()
            if not _REPO_RE.match(val):
                raise BadRequest(f"{col.replace('_', ' ')} may only hold letters, digits and . _ / -")
            fields.append(f"{col} = %s")
            params.append(val)
    if dev_prod_sync is not None:
        fields.append("dev_prod_sync = %s")
        params.append(bool(dev_prod_sync))
    if note is not None:
        fields.append("note = %s")
        params.append(repo._one_line(note, 500))
    if not fields:
        return repo.get_project(project_id)
    with get_cursor() as cur:
        remote_moved = was_on = False
        if git_remote is not None:
            cur.execute("SELECT git_remote, merge_via_crewboss FROM projects WHERE id = %s FOR UPDATE", (project_id,))
            old = cur.fetchone()
            remote_moved = bool(old) and (old["git_remote"] or "") != valid_git_remote(git_remote)
            was_on = bool(old) and old["merge_via_crewboss"]
        if remote_moved:        # #829: the merge executor's authority is the repository's: a different remote turns the opt-in off
            fields += ["merge_via_crewboss = false", "merge_repo = ''", "merge_via_crewboss_at = NULL"]
        cur.execute(f"UPDATE projects SET {', '.join(fields)} WHERE id = %s RETURNING *", params + [project_id])
        row = cur.fetchone()
        if not row:
            raise NotFound(f"project {project_id} not found")
        if remote_moved and was_on:
            record_activity(cur, project_id, actor, "disabled", "project", project_id,
                            "The git remote changed: Crewboss no longer merges approved branches itself (opt in again for the new repository)")
        record_activity(cur, project_id, actor, "updated", "project", project_id,
                        f"Project '{row['name']}' details updated"
                        + (f" (git remote: {row['git_remote'] or 'cleared'})" if git_remote is not None else ""))
    return row


# --- external-agent projects (#574) ----------------------------------------------------------------------------------

EXTERNAL_AGENT = "claude-desktop"   # the token agent kind an external agent's token carries (config.AGENTS)


def set_external_agent(project_id: int, actor: str, on: bool) -> dict:
    """Owner only (#574): mark a project as worked by an EXTERNAL agent -- a Claude Code session that does not run on
    the sessions server (the owner's desktop). It then counts as having a session for routing (own outbox, its branch is
    excluded from the parent session's reach) but is never in the session plan, so no tmux session is started for it.
    Exclusive with session_roles: a project that runs its own tmux session must have them turned off first. Clearing
    the flag also revokes the project's external tokens (an agent that is no longer attached must not stay able to
    act). An account, the dashboard root and an archived project can't be external."""
    if not is_owner(actor):
        raise Forbidden("only the owner marks a project as worked by an external agent")
    with get_cursor() as cur:
        cur.execute("SELECT * FROM projects WHERE id = %s FOR UPDATE", (project_id,))
        project = cur.fetchone()
        if not project:
            raise NotFound(f"project {project_id} not found")
        on = bool(on)
        if on == bool(project.get("external_agent")):
            return project
        if on:
            if project.get("kind") == "account":
                raise BadRequest("an account can't be worked by an external agent; mark one of its projects")
            if roles.is_role(project, "overseer"):
                raise BadRequest("the dashboard project is worked by dash, not an external agent")
            if project.get("archived_at"):
                raise BadRequest("the project is archived")
            if project.get("session_roles"):
                raise BadRequest(f"{project['name']} runs its own Claude session(s) on the sessions server; "
                                 "a project is worked by a session OR an external agent, not both")
        cur.execute("UPDATE projects SET external_agent = %s WHERE id = %s RETURNING *", (on, project_id))
        row = cur.fetchone()
        if on:
            msg = f"Project '{row['name']}' is now worked by an external agent"
        else:
            cur.execute("DELETE FROM session_tokens WHERE project_id = %s AND agent = %s", (project_id, EXTERNAL_AGENT))
            msg = f"Project '{row['name']}' is no longer worked by an external agent (its tokens were revoked)"
        record_activity(cur, project_id, actor, "updated", "project", project_id, msg)
    return row


def list_external_tokens(project_id: int) -> list[dict]:
    """The project's external-agent tokens (never the token or its hash): id, name, created_at, last_used_at."""
    with get_cursor() as cur:
        cur.execute("SELECT id, name, created_at, last_used_at FROM session_tokens "
                    "WHERE project_id = %s AND agent = %s ORDER BY id", (project_id, EXTERNAL_AGENT))
        return cur.fetchall()


def issue_external_token(project_id: int, actor: str) -> str:
    """Owner only: a new bearer token for the project's external agent, returned ONCE (the database keeps only its
    hash, app.auth.issue_token). It is an 'all' session token bound to this project with agent 'claude-desktop', so it
    reaches exactly what routed_project_ids gives the project (itself + its unrouted branch) inside its account
    partition, and acts as 'claude-desktop:all'. The project must be flagged external. Nothing about the token is
    written to the activity log."""
    if not is_owner(actor):
        raise Forbidden("only the owner issues an external agent's token")
    from app.auth import issue_token
    with get_cursor() as cur:
        cur.execute("SELECT * FROM projects WHERE id = %s", (project_id,))
        project = cur.fetchone()
        if not project:
            raise NotFound(f"project {project_id} not found")
        if not project.get("external_agent") or project.get("archived_at"):
            raise BadRequest(f"{project['name']} is not worked by an external agent; turn that on first")
        record_activity(cur, project_id, actor, "created", "token", None,
                        f"Token issued for the external agent of '{project['name']}'")
    return issue_token("all", f"{project['short_name']}-desktop", project_id=project_id, agent=EXTERNAL_AGENT)


def revoke_external_tokens(project_id: int, actor: str, token_id: int | None = None) -> int:
    """Owner only: revoke the project's external-agent tokens (the next call with one is a 401): one by `token_id`
    (only if it is THIS project's external token), else all of them. Returns how many were revoked."""
    if not is_owner(actor):
        raise Forbidden("only the owner revokes an external agent's token")
    with get_cursor() as cur:
        cur.execute("SELECT name FROM projects WHERE id = %s", (project_id,))
        project = cur.fetchone()
        if not project:
            raise NotFound(f"project {project_id} not found")
        if token_id is None:
            cur.execute("DELETE FROM session_tokens WHERE project_id = %s AND agent = %s",
                        (project_id, EXTERNAL_AGENT))
        else:
            cur.execute("DELETE FROM session_tokens WHERE id = %s AND project_id = %s AND agent = %s",
                        (token_id, project_id, EXTERNAL_AGENT))
        n = cur.rowcount
        if n:
            record_activity(cur, project_id, actor, "deleted", "token", None,
                            f"Revoked {'one' if token_id is not None else 'all'} external agent token"
                            f"{'' if token_id is not None else 's'} for '{project['name']}'")
    return n


def list_environments(project_id: int) -> list[dict]:
    with get_cursor() as cur:
        cur.execute("SELECT * FROM project_environments WHERE project_id = %s "
                    "ORDER BY CASE env WHEN 'dev' THEN 0 ELSE 1 END", (project_id,))
        return cur.fetchall()


def _env_values(fields: dict) -> dict:
    vals = {k: repo._one_line(str(fields.get(k) or ""), 300) for k in ENV_FIELDS}
    return vals


def set_environment(project_id: int, env: str, actor: str, cur=None, **fields) -> dict:
    """Owner only: create or update a project's dev or prod environment."""
    if not is_owner(actor) and actor != "system":
        raise Forbidden("only the owner sets a project's environments")
    if env not in config.ENVIRONMENTS:
        raise BadRequest(f"environment must be one of {config.ENVIRONMENTS}")
    vals = _env_values(fields)
    sql = ("INSERT INTO project_environments (project_id, env, server, path, url, deploy_method, zabbix_host) "
           "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (project_id, env) DO UPDATE SET "
           "server = EXCLUDED.server, path = EXCLUDED.path, url = EXCLUDED.url, "
           "deploy_method = EXCLUDED.deploy_method, zabbix_host = EXCLUDED.zabbix_host, updated_at = now() "
           "RETURNING *")
    params = (project_id, env, vals["server"], vals["path"], vals["url"], vals["deploy_method"], vals["zabbix_host"])

    def _do(c):
        c.execute(sql, params)
        row = c.fetchone()
        record_activity(c, project_id, actor, "updated", "project", project_id, f"Environment '{env}' set")
        return row

    if cur is not None:
        return _do(cur)
    with get_cursor() as c:
        return _do(c)


def delete_environment(project_id: int, env: str, actor: str) -> None:
    if not is_owner(actor):
        raise Forbidden("only the owner removes a project's environments")
    with get_cursor() as cur:
        cur.execute("DELETE FROM project_environments WHERE project_id = %s AND env = %s", (project_id, env))
        record_activity(cur, project_id, actor, "updated", "project", project_id, f"Environment '{env}' removed")


def create_placed_project(slug: str, name: str, type_: str, actor: str, short_name: str,
                          session_roles: list[str], parent_slug: str = "", kind: str = "project",
                          repo_name: str = "", repo_path: str = "", dev_env: bool = False,
                          prod_server: str = "", dev_server: str = "", owner_user_id: int | None = None) -> dict:
    """The New-project form, all or nothing: EVERYTHING is validated first (parent, kind, repo, servers), then the
    project is created, placed in the tree and given its environments in ONE transaction, so a refusal never
    leaves a half-made project behind (which the sessions timer might otherwise start). Prod always (its server
    may stay blank); dev only when the 'Dev environment' box was ticked, then dev and prod are kept in sync."""
    if not is_owner(actor):
        raise Forbidden("only the owner creates projects from the form")
    repo_name, repo_path = (repo_name or "").strip(), (repo_path or "").strip()
    if not _REPO_RE.match(repo_name) or not _REPO_RE.match(repo_path):
        raise BadRequest("repo and folder may only hold letters, digits and . _ / -")
    for s in (prod_server, dev_server):
        if s and s not in config.KNOWN_SERVERS:
            raise BadRequest(f"unknown server {s!r}; pick one of {', '.join(config.KNOWN_SERVERS)}")
    with get_cursor() as cur:
        idx = _index(cur)
        parent_id = None
        if parent_slug:
            parent = next((p for p in idx.values() if p["slug"] == parent_slug), None)
            if parent is None:
                raise BadRequest(f"no parent project {parent_slug!r}")
            parent_id = parent["id"]
        validate_place(idx, None, parent_id, kind)
        row = repo.create_project(slug, name, type_, actor, short_name=short_name, session_roles=session_roles,
                                  cur=cur, owner_user_id=owner_user_id)
        if parent_id is not None:
            cur.execute("UPDATE projects SET parent_id = %s WHERE id = %s", (parent_id, row["id"]))
        cur.execute("UPDATE projects SET kind = %s, repo = %s, repo_path = %s, dev_prod_sync = %s "
                    "WHERE id = %s RETURNING *", (kind, repo_name, repo_path, bool(dev_env), row["id"]))
        row = cur.fetchone()
        if kind == "project":
            set_environment(row["id"], "prod", "system", cur=cur, server=prod_server)
            if dev_env:
                set_environment(row["id"], "dev", "system", cur=cur, server=dev_server)
    return row
