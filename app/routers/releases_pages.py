"""Releases and Team (change #77, task #713): /<slug>/releases and /<slug>/team.

Releases: the project's deploy targets (dev | staging | production), who asked for what, the Admin's Approve / Refuse (production
needs a fresh second factor; with two or more Admins, not the requester), hotfix and rollback, and merge requests to main (tests
green + a recorded code-auditor pass + an Admin). The rules are app/deploys.py and app/release_gates.py; the page only asks.

Team: the project's Owner (its creator, or the instance owner) adds people, sets their role (admin / member / viewer), transfers
ownership and chooses the release policy. Every POST here sits behind project_from_path (a person who can change the project; the
Team POSTs also need to be its creator / the instance owner: pages.slug_policy 'manage')."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import access, deploys, local_auth as la, logic, merge_executor, release_gates, repo
from app.db import get_cursor
from app.repo import BadRequest, Conflict, Forbidden, NotFound
from app.routers.pages import FLASH_COOKIE, ProjectDep, render
from app.routers.runners_pages import _user

project_router = APIRouter(prefix="/{slug}")


def _back(project: dict, section: str, message: str):
    resp = RedirectResponse(f"/{project['slug']}/{section}", status_code=303)
    resp.set_cookie(FLASH_COOKIE, message, max_age=60, samesite="lax", httponly=True)    # fixed ASCII text only
    return resp


def _targets(project_id: int) -> list[dict]:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT t.id, t.name, t.env, t.branch, t.enabled, t.repo_path, t.post_cmd, s.name AS server_name FROM deploy_targets t "
                    "JOIN commissioned_servers s ON s.id = t.server_id WHERE t.project_id = %s ORDER BY t.env, lower(t.name)",
                    (project_id,))
        return [dict(r) for r in cur.fetchall()]


def _with_logic(project: dict, rows: list[dict]) -> list[dict]:
    """Each release row gets `logic`: what its commit changed in the logic map ({added, removed, changed}), or None without a report (#715)."""
    found = logic.release_summaries(project["id"], [r["sha"] for r in rows[:15]])
    for r in rows:
        r["logic"] = found.get(r["sha"].lower())
    return rows


@project_router.get("/releases", response_class=HTMLResponse)
def releases_page(request: Request, project: dict = ProjectDep):
    return _render_releases(request, project)


def _render_releases(request: Request, project: dict, protect_card: dict | None = None, protect_error: str = "",
                     target_error: str = "", target_form: dict | None = None, status: int = 200):
    who = access.of_request(request)
    manage = deploys.can_manage_targets(who, project)
    targets = _targets(project["id"])
    for t in targets:
        t["previous"] = deploys.previous_release(t["id"]) if t["env"] == "prod" else None
        t["state_warning"] = release_gates.state_warning(project, t["id"])      # #726
    resp = render(request, "pages/releases.html", "releases", can_manage=manage,
                  pickable=deploys.pickable_servers(who, project) if manage else [], envs=deploys.ENVS,
                  target_error=target_error, target_form=target_form or {}, targets=targets, requests=_with_logic(project, deploys.list_for_project(project["id"])),
                  merges=release_gates.list_merges(project["id"]), is_admin=access.is_admin(who, project["id"]),
                  can_work=access.can_edit(who, project["id"]), policy=project.get("release_policy") or "standard",
                  admins=len(access.admin_user_ids(project["id"])),
                  is_project_owner=access.role_in(who, project["id"]) == access.PROJECT_OWNER,
                  protect_card=protect_card, protect_error=protect_error, merge_by_crewboss=bool(project.get("merge_via_crewboss")),
                  merge_repos=merge_executor.list_repos(), is_instance_owner=who.is_owner,
                  project_repo=release_gates.repo_of(project))
    resp.status_code = status
    return resp


@project_router.post("/releases/targets", dependencies=[Depends(access.require_fresh())])
def add_target(request: Request, server_id: int = Form(...), name: str = Form(""), repo_path: str = Form(...),
               post_cmd: str = Form(""), branch: str = Form("main"), env: str = Form("dev"),
               timeout_seconds: str = Form(""), project: dict = ProjectDep):
    """"Add a place to deploy" (#810): the instance owner on any ready machine of the account, a project Owner on the machines
    pinned to the project. The target's name is its environment unless one is given (the form has no name box)."""
    _user(request)          # a real signed-in person (these run commands on a server), like the old /servers/targets routes
    who = access.of_request(request)
    try:
        deploys.add_target_from_project(who, project, server_id, name.strip() or env.strip().lower().replace("production", "prod"),
                                        repo_path, post_cmd, branch, env, timeout_seconds)
    except (BadRequest, Conflict, NotFound) as exc:
        return _render_releases(request, project, target_error=str(exc), status=400,
                                target_form={"server_id": server_id, "repo_path": repo_path, "post_cmd": post_cmd,
                                             "branch": branch, "env": env, "timeout_seconds": timeout_seconds})
    return _back(project, "releases", "Deploy target added.")


@project_router.post("/releases/targets/{target_id:int}/env", dependencies=[Depends(access.require_fresh())])
def set_target_env(request: Request, target_id: int, env: str = Form(""), project: dict = ProjectDep):
    _user(request)
    deploys.set_target_env_for(access.of_request(request), project, target_id, env)
    return _back(project, "releases", "Target tagged.")        # fixed text: never echo form input into the cookie


@project_router.post("/releases/targets/{target_id:int}/{action}", dependencies=[Depends(access.require_fresh())])
def target_action(request: Request, target_id: int, action: str, project: dict = ProjectDep):
    _user(request)
    if action not in ("enable", "disable"):
        raise NotFound("no such action")
    deploys.set_target_enabled_for(access.of_request(request), project, target_id, action == "enable")
    return _back(project, "releases", "Target enabled." if action == "enable" else "Target disabled.")


@project_router.post("/releases/deploy")
def request_release(request: Request, target_id: int = Form(...), sha: str = Form(...), hotfix: str = Form(""),
                    reason: str = Form(""), tag: str = Form(""), project: dict = ProjectDep):
    row = deploys.request_release(access.of_request(request), project, target_id, sha, hotfix=hotfix == "1", reason=reason, tag=tag)
    return _back(project, "releases", "Deploy started." if row["state"] == "approved" else "Requested. A project Admin approves production.")


@project_router.post("/releases/rollback")
def request_rollback(request: Request, target_id: int = Form(...), project: dict = ProjectDep):
    deploys.request_rollback(access.of_request(request), project, target_id)
    return _back(project, "releases", "Rollback requested. A project Admin approves it.")


@project_router.post("/releases/deploys/{request_id:int}/approve", dependencies=[Depends(access.require_fresh())])
def approve_deploy(request: Request, request_id: int, project: dict = ProjectDep):
    who = access.of_request(request)
    deploys.approve_checked(who, project["id"], request_id, True, who.user_id)
    return _back(project, "releases", "Approved. The dashboard will deploy it shortly.")


@project_router.post("/releases/deploys/{request_id:int}/refuse")
def refuse_deploy(request: Request, request_id: int, project: dict = ProjectDep):
    deploys.refuse_checked(access.of_request(request), project["id"], request_id)
    return _back(project, "releases", "Refused.")


@project_router.post("/releases/merges")
def request_merge(request: Request, branch: str = Form(...), sha: str = Form(...), hotfix: str = Form(""), project: dict = ProjectDep):
    release_gates.request_merge(access.of_request(request), project, branch, sha, hotfix=hotfix == "1")
    return _back(project, "releases", "Merge requested.")


SHORT = access.STEPUP_SHORT_SECONDS          # approving a merge, the opt-in and the repo list need a passkey tap in the last 5 minutes


@project_router.post("/releases/merges/{merge_id:int}/approve", dependencies=[Depends(access.require_fresh(SHORT))])
def approve_merge(request: Request, merge_id: int, project: dict = ProjectDep):
    release_gates.approve_merge(access.of_request(request), project["id"], merge_id, access.factor_fresh(request))
    if project.get("merge_via_crewboss"):
        # The approval is already saved; a GitHub problem is the executor's to retry or send back, never this request's failure.
        result = merge_executor.execute(merge_id, github_client_for_request())
        if result["state"] == "merged":
            return _back(project, "releases", "Merge approved and merged.")
        return _back(project, "releases", "Merge approved but sent back: see the reason below." if result["state"] == "sent_back"
                     else "Merge approved. Crewboss merges it shortly.")
    return _back(project, "releases", "Merge approved. The job merges it now.")


@project_router.post("/releases/merges/{merge_id:int}/requeue", dependencies=[Depends(access.require_fresh(SHORT))])
def requeue_merge(request: Request, merge_id: int, project: dict = ProjectDep):
    """An Admin puts a merge the executor sent back for a GitHub failure (or an expired approval) back in the queue: no new commit."""
    release_gates.requeue_merge(access.of_request(request), project["id"], merge_id, access.factor_fresh(request))
    return _back(project, "releases", "Merge re-queued. Crewboss merges it shortly.")


@project_router.post("/releases/merges/{merge_id:int}/refuse")
def refuse_merge(request: Request, merge_id: int, project: dict = ProjectDep):
    release_gates.refuse_merge(access.of_request(request), project["id"], merge_id)
    return _back(project, "releases", "Merge refused.")


# --- the merge executor and the protection card ----------------------------------------------------------------------------------
# Enrolment is the INSTANCE owner's (a list of repos); the opt-in is the project Owner's, for an enrolled repo only. Branch protection
# is NEVER applied by Crewboss: the card shows the call and a `gh` command for a human admin to run once with their own credential.

def github_client():
    """The client the executor and the card use. Tests replace this with a fake; nothing else calls GitHub."""
    from app import github_client as gh
    return gh.default_client()


def github_client_for_request():
    return github_client()


def _owner_only(request: Request, project: dict) -> None:
    if access.role_in(access.of_request(request), project["id"]) != access.PROJECT_OWNER:
        raise Forbidden("only the project's Owner (or the instance owner) does this")


def _instance_owner_only(request: Request) -> None:
    if not access.of_request(request).is_owner:
        raise Forbidden("only the instance owner chooses which repositories Crewboss may merge to")


@project_router.post("/releases/merge-repos", dependencies=[Depends(access.require_fresh(SHORT))])
def merge_repo_add(request: Request, full_name: str = Form(""), project: dict = ProjectDep):
    _instance_owner_only(request)
    merge_executor.enrol_repo(full_name, project["id"], f"user:{access.of_request(request).user_id}")
    return _back(project, "releases", "Repository enrolled for this project.")


@project_router.post("/releases/merge-repos/remove", dependencies=[Depends(access.require_fresh(SHORT))])
def merge_repo_remove(request: Request, full_name: str = Form(""), project: dict = ProjectDep):
    _instance_owner_only(request)
    merge_executor.unenrol_repo(full_name, f"user:{access.of_request(request).user_id}")
    return _back(project, "releases", "Repository removed. Projects merging into it are switched off.")


@project_router.post("/releases/merge-executor", dependencies=[Depends(access.require_fresh(SHORT))])
def merge_executor_toggle(request: Request, enable: str = Form(""), project: dict = ProjectDep):
    """The project Owner turns "Crewboss merges approved branches itself" on or off (second factor within 5 minutes). On needs an enrolled
    repo, a working bot credential (GET /user) that is not a person's account, and web sign-in on."""
    _owner_only(request, project)
    by = f"user:{access.of_request(request).user_id}"
    if enable == "1":
        try:
            merge_executor.enable(project, by, github_client_for_request())
        except BadRequest as exc:
            return _render_releases(request, project, protect_error=str(exc), status=400)
        except Conflict as exc:
            return _render_releases(request, project, protect_error=str(exc), status=409)
        return _back(project, "releases", "Crewboss now merges approved branches.")
    merge_executor.set_enabled(project["id"], False, by)
    return _back(project, "releases", "Crewboss no longer merges branches.")


@project_router.post("/releases/protect/plan", response_class=HTMLResponse, dependencies=[Depends(access.require_fresh(SHORT))])
def protect_plan(request: Request, mode: str = Form(""), project: dict = ProjectDep):
    """The card for the repository's REAL default branch: what it would replace and the command a human admin runs. Sends nothing.
    It reads GitHub with the bot credential, so: the project Owner, a second factor within 5 minutes, and only a repository the instance
    owner enrolled for THIS project (never whatever a project's git remote says)."""
    from urllib.parse import quote
    from app import github_client as gh
    _owner_only(request, project)
    try:
        full = gh.repo_full_name(project.get("git_remote") or "")
        with get_cursor(commit=False) as cur:
            cur.execute("SELECT 1 FROM merge_repos WHERE full_name = %s AND project_id = %s", (full.lower(), project["id"]))
            if not cur.fetchone():
                raise BadRequest(f"{full} is not enrolled for this project: the instance owner adds it to the repositories Crewboss may "
                                 "use first")
        client = github_client_for_request()
        branch = str((client.request("GET", f"/repos/{full}") or {}).get("default_branch") or "")
        if not branch:
            raise BadRequest("GitHub did not name the repository's default branch")
        login = gh.credential_login(client) if mode == "merge-path" else ""
        try:
            existing = client.request("GET", f"/repos/{full}/branches/{quote(branch, safe='/')}/protection")
        except BadRequest:            # no protection (404) or this credential may not read it: the card says so
            existing = None
        card = release_gates.protect_plan(full, branch, push_login=login, existing=existing)
    except BadRequest as exc:
        return _render_releases(request, project, protect_error=str(exc))
    card["mode"] = mode
    return _render_releases(request, project, protect_card=card)


# --- team ------------------------------------------------------------------------------------------------------------

@project_router.get("/team", response_class=HTMLResponse)
def team_page(request: Request, project: dict = ProjectDep):
    who = access.of_request(request)
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT id, username, display_name FROM users WHERE id = %s", (project.get("owner_user_id"),))
        creator = cur.fetchone()
    return render(request, "pages/team.html", "team", members=access.members(project["id"]), creator=creator,
                  can_manage=access.owns(who, project), roles=access.SHARE_ROLES,
                  policy=project.get("release_policy") or "standard", policies=deploys.POLICIES)


def _user_by_name(username: str) -> dict:
    user = la.get_user_by_username(username or "")
    if not user or user["disabled_at"]:
        raise NotFound("no such user")
    return user


@project_router.post("/team/members", dependencies=[Depends(access.require_fresh())])
def set_member(request: Request, username: str = Form(...), role: str = Form("member"), project: dict = ProjectDep):
    access.share(project["id"], _user_by_name(username)["id"], role, access.of_request(request).user_id)
    return _back(project, "team", "Saved.")


@project_router.post("/team/members/remove", dependencies=[Depends(access.require_fresh())])
def remove_member(request: Request, username: str = Form(...), project: dict = ProjectDep):
    access.unshare(project["id"], _user_by_name(username)["id"])
    return _back(project, "team", "Removed.")


@project_router.post("/team/transfer", dependencies=[Depends(access.require_fresh())])
def transfer(request: Request, username: str = Form(...), project: dict = ProjectDep):
    access.transfer_ownership(project["id"], _user_by_name(username)["id"])
    return _back(project, "team", "Ownership transferred. You stay on as an Admin.")


@project_router.post("/team/policy", dependencies=[Depends(access.require_fresh())])
def release_policy(request: Request, policy: str = Form(...), project: dict = ProjectDep):
    deploys.set_release_policy(access.of_request(request), project, policy)
    return _back(project, "team", "Release policy saved.")
