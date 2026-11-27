"""Merging to main (change #77, task #713): nothing reaches main until its tests are green, an INDEPENDENT code-auditor pass is
recorded on the job that did the work, and a project Admin approves (a fresh second factor; with two or more Admins, not the
requester). The dashboard records and gates: the merge itself is done by the job / runner afterwards and then marked merged.

A hotfix merge is the same gate with the flag set (and is flagged in the audit log); it never skips tests, the auditor, or the Admin.

GitHub branch protection (the second lock, so a stray `git push` to main is refused at GitHub too): protect_plan() builds the
exact API call as a CARD for a human admin (the call, a `gh` command and what it replaces); protect_apply() can send it only with that card's confirmation token, through
a client the caller passes. Nothing here calls GitHub by itself, and Crewboss's own merge credential is never used for it."""
from __future__ import annotations

import hashlib
import json
import re

from app import access, repo
from app.db import get_cursor
from app.repo import BadRequest, Conflict, Forbidden, NotFound

SHA_RE = re.compile(r"^[0-9a-f]{40}\Z")
# A branch name ends up in a GitHub API URL path: no '..', '//', trailing '/' or '.', '.lock' ending (git forbids them anyway; migration 0110 CHECK).
BRANCH_RE = re.compile(r"^(?!.*\.\.)(?!.*//)(?!.*\.lock\Z)[A-Za-z0-9][A-Za-z0-9._/-]{0,100}(?<![/.])\Z")
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}/[A-Za-z0-9._-]{1,100}\Z")


def repo_of(project: dict) -> str:
    """The GitHub repository ('owner/name', lower case) of a project's remote, or '' when it has none / is not GitHub. Stored on every
    merge request so the executor can tell the repo it was asked about from whatever the remote says later."""
    from app import github_client
    try:
        return github_client.repo_full_name(project.get("git_remote") or "").lower()
    except BadRequest:
        return ""


def request_merge(who, project: dict, branch: str, sha: str, job_id: int | None = None, hotfix: bool = False) -> dict:
    role = access.role_in(who, project["id"])
    if role not in (access.PROJECT_OWNER, "admin", "member"):
        raise Forbidden("only a project Member or above asks to merge")
    sha = (sha or "").strip().lower()
    if not SHA_RE.match(sha) or not BRANCH_RE.match((branch or "").strip()):
        raise BadRequest("a branch name and the full 40-character commit id are required")
    with get_cursor() as cur:
        cur.execute("INSERT INTO merge_requests (project_id, job_id, branch, sha, hotfix, requested_by, admins_at_request, merge_repo) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
                    (project["id"], job_id, branch.strip(), sha, hotfix, who.user_id, len(access.admin_user_ids(project["id"])),
                     repo_of(project)))
        row = cur.fetchone()
        repo.record_activity(cur, project["id"], f"user:{who.user_id}" if who.user_id else "owner", "requested", "merge", row["id"],
                             f"Asked to merge {branch.strip()} ({sha[:12]}) to main" + (" as a HOTFIX" if hotfix else ""))
    return dict(row)


def record_check(merge_id: int, kind: str, ok: bool, by: str, note: str = "") -> dict:
    """Record the tests result ('tests') or the independent code-auditor pass ('audit') on the merge request (and so on the job).
    A later failing result clears an earlier pass. `by` names who/what recorded it (e.g. the job actor)."""
    if kind not in ("tests", "audit"):
        raise BadRequest("kind: tests or audit")
    col = "tests_green_at" if kind == "tests" else "audit_passed_at"
    with get_cursor() as cur:
        cur.execute("SELECT state FROM merge_requests WHERE id = %s FOR UPDATE", (merge_id,))
        mr = cur.fetchone()
        if not mr:
            raise NotFound("merge request not found")
        if mr["state"] != "requested":
            raise Conflict(f"merge request {merge_id} is already {mr['state']}")
        extra = ", audit_by = %s, audit_note = %s" if kind == "audit" else ""
        params = ([by[:100], note[:500]] if kind == "audit" else []) + [merge_id]
        cur.execute(f"UPDATE merge_requests SET {col} = CASE WHEN %s THEN now() END{extra} WHERE id = %s RETURNING *",  # noqa: S608 - fixed columns
                    [ok, *params])
        return dict(cur.fetchone())


def _job_merge(cur, job_id: int, fence: int, sha: str):
    """The merge request of THIS running job for THIS commit, made on first use. A job attests only for its own task's project and
    only for a commit it names in full; what it records is shown to the Admin as the job's claim, never as the Admin's own check."""
    from app import jobs
    sha = (sha or "").strip().lower()
    if not SHA_RE.match(sha):
        raise BadRequest("sha must be the full 40-character commit id")
    job = jobs._running(cur, job_id, fence)
    cur.execute("SELECT project_id FROM tasks WHERE id = %s", (job["task_id"],))
    task = cur.fetchone()
    if not task:
        raise NotFound("the job's task is gone")
    cur.execute("SELECT * FROM merge_requests WHERE job_id = %s AND sha = %s AND project_id = %s ORDER BY id DESC LIMIT 1 FOR UPDATE",
                (job_id, sha, task["project_id"]))
    mr = cur.fetchone()
    if mr is None:
        cur.execute("SELECT git_remote FROM projects WHERE id = %s", (task["project_id"],))
        remote = cur.fetchone()
        branch = (job.get("branch") or "job-branch")[:100]
        if not BRANCH_RE.match(branch):
            branch = "job-branch"
        cur.execute("INSERT INTO merge_requests (project_id, job_id, branch, sha, requested_by, admins_at_request, merge_repo) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *",
                    (task["project_id"], job_id, branch, sha, job["user_id"],
                     len(access.admin_user_ids(task["project_id"])), repo_of(remote or {})))
        mr = cur.fetchone()
    return job, mr


def job_record_check(job_id: int, fence: int, kind: str, sha: str, ok: bool, summary: str = "") -> dict:
    """The job MCP's record_check: kind 'tests' or 'auditor' (the independent code-auditor pass), for the job's own commit."""
    from app import jobs
    kind = {"auditor": "audit"}.get(kind, kind)
    if kind not in ("tests", "audit"):
        raise BadRequest("kind: tests or auditor")
    with get_cursor() as cur:
        job, mr = _job_merge(cur, job_id, fence, sha)
    return record_check(mr["id"], kind, bool(ok), jobs.actor(job_id), (summary or "")[:500])


def job_report_merge(job_id: int, fence: int, sha: str) -> dict:
    """The job reports it merged `sha`: accepted only for an APPROVED merge request of this job (mark_merged refuses the rest).
    On a project where Crewboss merges itself (projects.merge_via_crewboss) a job's report is NOT an attestation: it only gets the
    request's current state back, and only the merge executor (app/merge_executor.py) ever marks such a request merged."""
    with get_cursor() as cur:
        job, mr = _job_merge(cur, job_id, fence, sha)
        cur.execute("SELECT merge_via_crewboss FROM projects WHERE id = %s", (mr["project_id"],))
        by_crewboss = cur.fetchone()["merge_via_crewboss"]
    if by_crewboss:
        return {"id": mr["id"], "state": mr["state"], "note": "Crewboss merges this project's branches itself; nothing to report", "merge_note": mr["merge_note"]}
    return mark_merged(mr["id"])


def approve_merge(who, project_id: int, merge_id: int, factor_fresh: bool, approver_id: int | None = None) -> dict:
    if not access.is_admin(who, project_id):
        raise Forbidden("only a project Admin approves a merge to main")
    with get_cursor() as cur:
        cur.execute("SELECT * FROM merge_requests WHERE id = %s AND project_id = %s FOR UPDATE", (merge_id, project_id))
        mr = cur.fetchone()
        if not mr:
            raise NotFound("merge request not found")
        if mr["state"] != "requested":
            raise Conflict(f"merge request {merge_id} is already {mr['state']}")
        if mr["tests_green_at"] is None:
            raise Conflict("the tests are not green for this commit")
        if mr["audit_passed_at"] is None:
            raise Conflict("no independent code-auditor pass is recorded for this commit")
        if not factor_fresh:
            raise Forbidden("confirm your second factor first (it must be within the last 5 minutes)")
        if max(mr["admins_at_request"], len(access.admin_user_ids(project_id))) >= 2 and access.same_person(mr["requested_by"], who.user_id):
            raise Forbidden("this project has more than one Admin: another Admin approves your merge")
        cur.execute("UPDATE merge_requests SET state = 'approved', decided_by = %s, decided_at = now() WHERE id = %s RETURNING *",
                    (approver_id if approver_id is not None else who.user_id, merge_id))
        mr = cur.fetchone()
        repo.record_activity(cur, project_id, f"user:{who.user_id}" if who.user_id else "owner", "approved", "merge", merge_id,
                             f"Approved the merge of {mr['branch']} ({mr['sha'][:12]}) to main" + (" (HOTFIX)" if mr["hotfix"] else ""))
    return dict(mr)


REQUEUEABLE = ("github", "expired")


def requeue_merge(who, project_id: int, merge_id: int, factor_fresh: bool) -> dict:
    """An Admin re-queues a request the executor sent back for a GitHub failure (or an expired approval) WITHOUT a new commit: the same
    gates still hold for the same sha, and this is a fresh approval (fresh second factor, same two-person rule as approving)."""
    if not access.is_admin(who, project_id):
        raise Forbidden("only a project Admin re-queues a merge")
    with get_cursor() as cur:
        cur.execute("SELECT * FROM merge_requests WHERE id = %s AND project_id = %s FOR UPDATE", (merge_id, project_id))
        mr = cur.fetchone()
        if not mr:
            raise NotFound("merge request not found")
        if mr["state"] != "sent_back" or mr["sent_back_kind"] not in REQUEUEABLE:
            raise Conflict("only a merge sent back for a GitHub failure or an expired approval is re-queued; otherwise ask for the merge again")
        if mr["tests_green_at"] is None or mr["audit_passed_at"] is None:
            raise Conflict("the gates are no longer recorded for this commit")
        if not factor_fresh:
            raise Forbidden("confirm your second factor first (it must be within the last 5 minutes)")
        if max(mr["admins_at_request"], len(access.admin_user_ids(project_id))) >= 2 and access.same_person(mr["requested_by"], who.user_id):
            raise Forbidden("this project has more than one Admin: another Admin re-queues your merge")
        cur.execute("UPDATE merge_requests SET state = 'approved', decided_by = %s, decided_at = now(), merge_attempts = 0, "
                    "next_attempt_at = NULL, merge_note = '', sent_back_kind = '' WHERE id = %s RETURNING *", (who.user_id, merge_id))
        mr = cur.fetchone()
        repo.record_activity(cur, project_id, f"user:{who.user_id}" if who.user_id else "owner", "requeued", "merge", merge_id,
                             f"Re-queued the merge of {mr['branch']} ({mr['sha'][:12]})")
    return dict(mr)


def refuse_merge(who, project_id: int, merge_id: int) -> dict:
    if not access.is_admin(who, project_id):
        raise Forbidden("only a project Admin refuses a merge")
    with get_cursor() as cur:
        cur.execute("UPDATE merge_requests SET state = 'refused', decided_by = %s, decided_at = now() "
                    "WHERE id = %s AND project_id = %s AND state = 'requested' RETURNING *", (who.user_id, merge_id, project_id))
        row = cur.fetchone()
        if not row:
            raise NotFound("no such open merge request")
    return dict(row)


def mark_merged(merge_id: int) -> dict:
    """The job merged an APPROVED request. Anything not approved is refused: this is the check that nothing merges unapproved."""
    with get_cursor() as cur:
        # The opt-in is part of the UPDATE itself: a project where Crewboss merges is never marked merged by anyone else, however it is called.
        cur.execute("UPDATE merge_requests m SET state = 'merged' FROM projects p WHERE m.id = %s AND m.state = 'approved' "
                    "AND p.id = m.project_id AND NOT p.merge_via_crewboss RETURNING m.*", (merge_id,))
        row = cur.fetchone()
        if not row:
            raise Conflict("only an approved merge request can be marked merged (never one that Crewboss itself merges)")
    return dict(row)


def list_merges(project_id: int, limit: int = 30) -> list[dict]:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT m.*, ru.display_name AS requested_name, du.display_name AS decided_name FROM merge_requests m "
                    "LEFT JOIN users ru ON ru.id = m.requested_by LEFT JOIN users du ON du.id = m.decided_by "
                    "WHERE m.project_id = %s ORDER BY (m.state = 'requested') DESC, m.id DESC LIMIT %s", (project_id, limit))
        return [dict(r) for r in cur.fetchall()]


# --- GitHub branch protection --------------------------------------------------------------------------------------------

def protection_payload(push_login: str = "") -> dict:
    """The standard card (pull requests only), or with `push_login` the merge-path card: nobody but that identity (the account the
    merge executor's GitHub credential belongs to) may update the branch, and only by fast-forward (linear history, no force-push).
    A normal bot USER goes in `restrictions.users`; a GitHub App identity ('slug[bot]') goes in `restrictions.apps` (by slug)."""
    if push_login:
        is_app = push_login.endswith("[bot]")
        return {"required_status_checks": None, "enforce_admins": True, "required_pull_request_reviews": None,
                "restrictions": {"users": [] if is_app else [push_login], "teams": [], "apps": [push_login[:-5]] if is_app else []},
                "required_linear_history": True, "allow_force_pushes": False, "allow_deletions": False}
    return {"required_status_checks": {"strict": True, "contexts": []},
            "enforce_admins": True,
            "required_pull_request_reviews": {"required_approving_review_count": 1, "dismiss_stale_reviews": True},
            "restrictions": None, "allow_force_pushes": False, "allow_deletions": False}


def describe_protection(existing: dict | None) -> str:
    """One plain sentence on what a branch already has that the PUT would REPLACE (the call replaces the whole protection)."""
    if not existing:
        return "No existing protection could be read, so check what the branch has now before running it (the call replaces all of it)."
    parts = []
    if existing.get("required_status_checks") is not None:
        parts.append("required status checks")
    if existing.get("required_pull_request_reviews") is not None:
        parts.append("required pull-request reviews")
    if existing.get("restrictions") is not None:
        parts.append("push restrictions")
    if (existing.get("required_signatures") or {}).get("enabled"):
        parts.append("required signed commits")
    if (existing.get("required_conversation_resolution") or {}).get("enabled"):
        parts.append("required conversation resolution")
    return ("It replaces the protection the branch has now (" + ", ".join(parts) + ")." if parts
            else "The branch has no protection today, so nothing is replaced.")


def protect_plan(repo_full_name: str, branch: str = "main", push_login: str = "", existing: dict | None = None) -> dict:
    """The card for protecting `branch` (the repository's real default branch, read by the caller): exactly what a human admin runs, and
    what it replaces. Crewboss itself NEVER sends it: the executor's credential merges only, it does not administer the repository."""
    if not REPO_RE.match(repo_full_name or "") or not BRANCH_RE.match(branch or ""):
        raise BadRequest("repo must look like owner/name and the branch be a plain name")
    if push_login and not re.match(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}(\[bot\])?$", push_login):
        raise BadRequest("that is not a GitHub login")
    call = {"method": "PUT", "path": f"/repos/{repo_full_name}/branches/{branch}/protection", "json": protection_payload(push_login)}
    token = hashlib.sha256(json.dumps(call, sort_keys=True).encode()).hexdigest()[:32]
    kind = "an app" if push_login.endswith("[bot]") else "the account"
    card = (f"Protect {branch} on {repo_full_name}: only {push_login} ({kind} Crewboss merges with) may change it, fast-forward only, "
            "no force-push, no deletion, admins included. Push restrictions exist on organisation repositories only; "
            "a GitHub App identity is listed under apps, a normal bot account under users. " if push_login else
            f"Protect {branch} on {repo_full_name}: pull requests only (1 approval), up-to-date checks, no force-push, "
            "no deletion, admins included. ") + describe_protection(existing)
    command = (f"gh api -X PUT repos/{repo_full_name}/branches/{branch}/protection --input - <<'JSON'\n"
               f"{json.dumps(call['json'], indent=2)}\nJSON")
    return {"card": card, "call": call, "confirm_token": token, "command": command, "branch": branch}


def protect_apply(client, plan: dict, confirm_token: str) -> dict:
    """Send the planned call through `client.request(method, path, json=...)` ONLY with the card's own token. NOT used by the Releases
    page any more (a human admin runs the card's command with their own credential, once); kept for a caller that holds an admin client."""
    expected = hashlib.sha256(json.dumps(plan["call"], sort_keys=True).encode()).hexdigest()[:32]
    if not confirm_token or confirm_token != expected or plan.get("confirm_token") != expected:
        raise Forbidden("the confirmation does not match this card")
    call = plan["call"]
    return client.request(call["method"], call["path"], json=call["json"])


# --- undeclared state (#726) ---------------------------------------------------------------------------------------------
# A deploy whose project says nothing about what it keeps, while its compose file shows a database or a volume, gets a WARNING
# ("state not declared for backup"). A project can opt in to make it a block (projects.require_state_declared). Best effort: the compose
# files are read from the project's clone under REPOS_DIR when it is on this server; no clone, no warning.
STATE_WARNING = "state not declared for backup"
_DB_IMAGES = ("postgres", "mysql", "mariadb", "mongo", "redis", "timescale", "clickhouse", "influxdb", "couchdb", "cassandra")
_COMPOSE_NAMES = ("docker-compose*.yml", "docker-compose*.yaml", "compose*.yml", "compose*.yaml")


MAX_COMPOSE_BYTES = 256_000
MAX_SERVICES = 60
MAX_VOLUMES = 40
MAX_NAME = 200


def _compose_file(path, found: list) -> None:
    import yaml
    if not path.is_file() or path.stat().st_size > MAX_COMPOSE_BYTES:
        return
    with open(path, "rb") as fh:
        raw = fh.read(MAX_COMPOSE_BYTES + 1)
    doc = yaml.safe_load(raw.decode("utf-8", "replace"))
    if not isinstance(doc, dict):
        return
    services = doc.get("services")
    if isinstance(services, dict):
        for name, svc in list(services.items())[:MAX_SERVICES]:
            if not isinstance(svc, dict):
                continue
            image = svc.get("image")
            image = image.lower().rsplit("/", 1)[-1][:MAX_NAME] if isinstance(image, str) else ""
            if any(image.startswith(d) for d in _DB_IMAGES):
                found.append(f"database {image.split(':')[0]} ({str(name)[:60]})")
            vols = svc.get("volumes")
            for vol in (vols[:MAX_VOLUMES] if isinstance(vols, list) else []):
                src = vol if isinstance(vol, str) else (vol.get("source") if isinstance(vol, dict) else None)
                if not isinstance(src, str):
                    continue
                src = src.split(":")[0][:MAX_NAME]
                if src and not src.startswith(("./", "../", "/dev/", "/var/run/", "/run/")) and src != "/etc/localtime":
                    found.append(f"volume {src}")
    top = doc.get("volumes")
    if isinstance(top, dict):
        for vol in list(top)[:MAX_VOLUMES]:
            if isinstance(vol, str):
                found.append(f"volume {vol[:MAX_NAME]}")


def compose_state(project: dict) -> list[str]:
    """What the project's compose files say is stateful: ['database postgres (db)', 'volume pgdata', ...]. Best effort and bounded: only strings are
    read (nothing is str()'d, so a YAML alias cannot expand), size and counts are capped, and any odd file is skipped. [] when nothing is found,
    the clone is not here or anything goes wrong: this must never break a page."""
    found: list[str] = []
    try:
        from app import environment
        base = environment.repo_dir(project)
        if base is None:
            return []
        seen_files: set = set()
        for pattern in _COMPOSE_NAMES:
            for path in sorted(base.glob(pattern))[:10]:
                if path in seen_files:
                    continue
                seen_files.add(path)
                try:
                    _compose_file(path, found)
                except Exception:  # noqa: BLE001 - one bad file never hides the rest
                    continue
    except Exception:  # noqa: BLE001
        return []
    return list(dict.fromkeys(found))


def state_warning(project: dict, target_id: int | None = None) -> dict | None:
    """{warning, found, block} when the project has not declared its state but its compose files show some; else None."""
    from app import stateful
    if stateful.declared(stateful.effective(project["id"], target_id)):
        return None
    found = compose_state(project)
    if not found:
        return None
    return {"warning": STATE_WARNING, "found": found[:6], "block": bool(project.get("require_state_declared"))}
