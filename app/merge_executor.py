"""The merge executor (task #829): once a merge request has all its gates (tests green and an independent code-auditor pass, both
recorded for the EXACT commit, and a project Admin's approval with a fresh second factor), Crewboss itself moves the repository's default
branch to that commit on GitHub and records it. Runners never push main and never hold the key. The only credential is a fine-grained
token of a dedicated bot account (Contents: write on the enrolled repos only), a 0600 file read by app/github_client.py.

AUTHORITY IS THE GITHUB REPOSITORY'S, NOT THE PROJECT'S. The instance owner (fresh factor) keeps an allow-list of 'owner/name' repos the
executor may touch (merge_repos), each enrolled for one project. A project may opt in (projects.merge_via_crewboss) only for a repo on that list, only one opted-in
project per repo, and changing its git_remote turns the opt-in off. Each request stores the repo it was made against; at execute time the
executor refuses if the project's remote, the opt-in or the enrolment no longer says the same repo.

Per approved request of an opted-in project:
  1. claim: approved -> merging (merging_at), in one transaction (FOR UPDATE SKIP LOCKED). Only approvals made AFTER the opt-in
     (decided_at > merge_via_crewboss_at) and younger than 24 hours are claimed; older ones are sent back as expired (an Admin re-queues);
  2. re-check: gates recorded, decided_by is still an enabled Admin of the project, repo recorded = remote = opted-in = enrolled;
  3. the pushed branch head must still be the approved commit (a branch that moved after approval is a NEW commit: ask again);
  4. compare with the default branch: 'ahead' goes on; 'identical' / 'behind' is recorded as merged with no change; 'diverged' is SENT BACK;
  5. ONE ref update with force=false, so GitHub itself refuses anything that is not a fast-forward. Never a force-push, a merge commit
     or a branch deletion.
A refusal that retrying cannot change (a 4xx, a moved branch, a conflict) sends the request back at once (state sent_back + reason, the
task back to 'doing'). Network errors, 5xx and rate limits are retried with exponential backoff (30 s doubling to 10 min, 7 tries, about 25
minutes), then sent back; an Admin can re-queue a request sent back for a GitHub failure or an expired approval without a new commit.
A claim whose process died stays 'merging' for at most 3 minutes: recover() (start-up and every pass) returns it to approved, and a rerun is
idempotent (the compare says 'identical', so it is recorded as merged). run_once holds its own advisory lock: one process executes.
Nothing here names a repository, branch or login: all come from the project and GitHub."""
from __future__ import annotations

import logging
from contextlib import contextmanager
from urllib.parse import quote

import psycopg

from app import access, github_client, release_gates, repo, web_auth
from app.db import connect, get_cursor
from app.github_client import GitHubError
from app.repo import BadRequest, Conflict

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 7
BACKOFF_BASE_SECONDS = 30
BACKOFF_CAP_SECONDS = 600
APPROVAL_TTL_HOURS = 24
MERGING_STALE_SECONDS = 180
LOCK_KEY = 829001            # pg advisory lock: one process runs the merge pass (the deploy executor has its own, deploys.EXEC_LOCK_KEY)
ACTOR = "dashboard:merge-executor"


def backoff_seconds(attempts: int) -> int:
    """How long to wait after the `attempts`-th failed try: 30 s, 1, 2, 4, 8, then 10 min (about 25 minutes over the 6 waits)."""
    return min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2 ** max(0, attempts - 1))


class _Back(Exception):
    """The request cannot merge as it is: send it back with this reason. `kind` 'github' / 'expired' lets an Admin re-queue it."""

    def __init__(self, message: str, kind: str = ""):
        super().__init__(message)
        self.kind = kind


class _Retry(Exception):
    """A transient failure: try again later (with backoff)."""


def _tail(text: str) -> str:
    return " ".join((text or "").split())[:300]


def _fence(mr: dict, from_state: str) -> tuple[str, tuple]:
    """The WHERE tail that pins an outcome write to ITS claim: a claimed row ('merging') carries the claim's merging_at and attempt count,
    so a stalled first attempt cannot overwrite the row once recover() has returned it and a second attempt has re-claimed it."""
    if from_state == "merging":
        return "AND state = 'merging' AND merging_at IS NOT DISTINCT FROM %s AND merge_attempts = %s", (mr.get("merging_at"), mr["merge_attempts"])
    return "AND state = %s", (from_state,)


def _sent_back(cur, mr: dict, reason: str, kind: str = "", from_state: str = "merging") -> bool:
    fence, fparams = _fence(mr, from_state)
    cur.execute("UPDATE merge_requests SET state = 'sent_back', merge_note = %s, sent_back_kind = %s, next_attempt_at = NULL "
                f"WHERE id = %s {fence} RETURNING id", (reason[:500], kind, mr["id"], *fparams))
    if not cur.fetchone():
        return False
    repo.record_activity(cur, mr["project_id"], ACTOR, "sent_back", "merge", mr["id"],
                         f"Merge of {mr['branch']} ({mr['sha'][:12]}) sent back: {reason}")
    return True


def _task_of(mr: dict) -> int | None:
    if not mr.get("job_id"):
        return None
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT task_id FROM jobs WHERE id = %s", (mr["job_id"],))
        row = cur.fetchone()
    return row["task_id"] if row else None


def _tell_task(mr: dict, body: str, back: bool) -> None:
    task_id = _task_of(mr)
    if not task_id:
        return
    try:
        repo.comment_task(task_id, body, f"merge:{mr['id']}")
        if back:
            repo.update_task(task_id, {"status": "doing"}, f"merge:{mr['id']}")
    except Exception:  # noqa: BLE001 - a missing task never loses the merge record
        log.exception("merge executor: task note")


def _claim(merge_id: int) -> tuple[dict, dict] | None:
    """approved -> merging in one transaction. Only an approval made after the project opted in, younger than the approval TTL, and
    not waiting out a backoff. Never with web sign-in off (an approval needs a real second factor)."""
    if not web_auth.required():
        return None
    with get_cursor() as cur:
        cur.execute("SELECT m.* FROM merge_requests m JOIN projects p ON p.id = m.project_id "
                    "WHERE m.id = %s AND m.state = 'approved' AND p.merge_via_crewboss AND p.merge_via_crewboss_at IS NOT NULL "
                    "AND m.decided_at > p.merge_via_crewboss_at AND m.decided_at > now() - make_interval(hours => %s) "
                    "AND (m.next_attempt_at IS NULL OR m.next_attempt_at <= now()) FOR UPDATE OF m SKIP LOCKED",
                    (merge_id, APPROVAL_TTL_HOURS))
        mr = cur.fetchone()
        if not mr:
            return None
        cur.execute("SELECT * FROM projects WHERE id = %s", (mr["project_id"],))
        project = dict(cur.fetchone())
        cur.execute("UPDATE merge_requests SET state = 'merging', merging_at = now(), merge_attempts = merge_attempts + 1 "
                    "WHERE id = %s RETURNING *", (merge_id,))
        return dict(cur.fetchone()), project


def _check_authority(mr: dict, project: dict) -> str:
    """Everything that must STILL be true when the executor acts; _Back when not. Returns the repository's 'owner/name'."""
    if not release_gates.BRANCH_RE.match(mr["branch"] or ""):
        raise _Back("the branch name is not a plain git branch name")
    if mr["tests_green_at"] is None or mr["audit_passed_at"] is None or mr["decided_by"] is None or not mr["decided_at"]:
        raise _Back("the gates are not all recorded for this commit")
    if mr["decided_by"] not in access.admin_user_ids(mr["project_id"]):
        with get_cursor(commit=False) as cur:
            cur.execute("SELECT 1 FROM users WHERE id = %s AND role = 'owner' AND disabled_at IS NULL", (mr["decided_by"],))
            if not cur.fetchone():
                raise _Back("the person who approved it is no longer an Admin of this project: an Admin approves it again")
    try:
        full = github_client.repo_full_name(project.get("git_remote") or "")
    except BadRequest as exc:
        raise _Back(str(exc)) from None
    low = full.lower()
    if not mr.get("merge_repo"):
        raise _Back("this request was made without a GitHub repository on record: ask for the merge again")
    if low != mr["merge_repo"] or low != (project.get("merge_repo") or ""):
        raise _Back(f"the project's git remote now points at {full}, not {mr['merge_repo']} (the repository this merge was asked for "
                    "and the opt-in was made for). Nothing was changed")
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT 1 FROM merge_repos WHERE full_name = %s AND project_id = %s", (low, project["id"]))
        if not cur.fetchone():
            raise _Back(f"{full} is not on the instance owner's list of repositories Crewboss may merge to for this project")
    return full


def _check_identity(client, cache: dict | None) -> None:
    """Re-ask GitHub whose credential this is, right before a write: the token file can be swapped after the opt-in. Must equal the
    configured bot login and not be on the deny-list (github_client.credential_login), else _Back (an Admin re-queues once fixed). One
    GET /user per pass: the outcome is cached in `cache` (run_once's, per pass); a call outside a pass has no cache and asks every time."""
    if cache is not None and "outcome" in cache:
        outcome = cache["outcome"]
    else:
        try:
            outcome = ("ok", github_client.credential_login(client))
        except GitHubError as exc:
            outcome = ("retry" if exc.transient else "back", str(exc))
        except BadRequest as exc:
            outcome = ("back", str(exc))
        if cache is not None:
            cache["outcome"] = outcome
    if outcome[0] == "ok":
        return
    if outcome[0] == "retry":
        raise _Retry(f"could not confirm the GitHub credential's identity ({_tail(outcome[1])})")
    log.warning("merge executor: refusing to merge, the GitHub credential failed its identity check: %s", _tail(outcome[1]))
    raise _Back(f"the GitHub credential is not the configured bot account any more ({_tail(outcome[1])}). Nothing was changed. "
                "An Admin can re-queue it once the right token is mounted", "github")


def _attempt(mr: dict, project: dict, client, identity: dict | None = None) -> tuple[str, str, str]:
    """The GitHub work. Returns (target, landed sha, note); raises _Back / _Retry / BadRequest."""
    full = _check_authority(mr, project)
    target = (client.request("GET", f"/repos/{full}") or {}).get("default_branch") or ""
    if not target or not release_gates.BRANCH_RE.match(target):
        raise _Retry("GitHub did not name the repository's default branch")
    head = ((client.request("GET", f"/repos/{full}/branches/{quote(mr['branch'], safe='/')}") or {}).get("commit") or {}).get("sha", "")
    if head.lower() != mr["sha"]:
        raise _Back(f"{mr['branch']} is now at {head[:12] or 'nothing'}, not the approved {mr['sha'][:12]}: a changed branch is a "
                    "new commit. Ask for the merge again on it")
    cmp = client.request("GET", f"/repos/{full}/compare/{quote(target, safe='/')}...{mr['sha']}") or {}
    status = cmp.get("status")
    if status == "diverged":
        raise _Back(f"{target} has moved on and {mr['branch']} is not rebased on it (or the two conflict). Rebase the branch on "
                    f"{target}, run the tests again and ask for the merge again. Nothing was changed")
    if status in ("identical", "behind"):
        return target, mr["sha"], f"already on {target}: no change made"
    if status != "ahead":
        raise _Retry(f"unexpected compare status {status!r}")
    _check_identity(client, identity)
    done = client.request("PATCH", f"/repos/{full}/git/refs/heads/{quote(target, safe='/')}", json={"sha": mr["sha"], "force": False})
    landed = ((done or {}).get("object") or {}).get("sha", "")
    if landed.lower() != mr["sha"]:
        raise _Retry("GitHub did not confirm the new head of " + target)
    return target, landed, ""


def execute(merge_id: int, client=None, identity: dict | None = None) -> dict:
    """Run one approved merge request. Returns {"state", "note", "target"}: 'merged', 'sent_back', 'approved' (will retry later), or
    'skipped' (not claimable now). Never raises: whatever goes wrong, the request is sent back, retried, or (if even that fails) left
    'merging' for recover() to return."""
    claimed = _claim(merge_id)
    if claimed is None:
        return {"state": "skipped", "note": "", "target": ""}
    mr, project = claimed
    try:
        try:
            target, landed, note = _attempt(mr, project, client or github_client.default_client(), identity)
            return _merged(mr, target, landed, note)
        except _Back as exc:
            return _back(mr, str(exc), exc.kind)
        except _Retry as exc:
            return _retry(mr, str(exc))
        except BadRequest as exc:
            text = str(exc)
            if "fast forward" in text.lower() or "fast-forward" in text.lower():
                return _back(mr, "GitHub refused it as not a fast-forward: the default branch moved while this ran. Rebase on it, run "
                                 "the tests again and ask again. Nothing was changed")
            if getattr(exc, "status", 0) and not getattr(exc, "transient", True):
                return _back(mr, f"GitHub refused it and retrying will not change that ({_tail(text)}). An Admin can re-queue it once "
                                 "the credential or the repository settings are fixed", "github")
            return _retry(mr, _tail(text))
        except Exception as exc:  # noqa: BLE001 - never leave a request stuck in 'merging'
            log.exception("merge executor %s", merge_id)
            return _retry(mr, f"executor error: {type(exc).__name__}")
    except Exception:  # noqa: BLE001 - even recording the outcome failed: recover() returns the claim to approved
        log.exception("merge executor %s: recording the outcome", merge_id)
        return {"state": "merging", "note": "the outcome could not be recorded; the claim is returned shortly", "target": ""}


def _superseded() -> dict:
    """An outcome write whose claim is no longer the row's (it was recovered and re-claimed, or already decided): nothing was changed."""
    log.warning("merge executor: an outcome was dropped because its claim is no longer current")
    return {"state": "skipped", "note": "this attempt was superseded by a newer one; nothing was recorded", "target": ""}


def _back(mr: dict, reason: str, kind: str = "") -> dict:
    with get_cursor() as cur:
        done = _sent_back(cur, mr, reason, kind)
    if not done:
        return _superseded()
    _tell_task(mr, f"Merge of {mr['branch']} ({mr['sha'][:12]}) was sent back: {reason}.", True)
    return {"state": "sent_back", "note": reason, "target": ""}


def _retry(mr: dict, note: str) -> dict:
    """Back to approved to wait out the backoff; sent back (re-queueable) after MAX_ATTEMPTS."""
    final = mr["merge_attempts"] >= MAX_ATTEMPTS
    with get_cursor() as cur:
        if final:
            reason = f"GitHub failed {MAX_ATTEMPTS} times over about 25 minutes ({note}). An Admin can re-queue it"
            final = _sent_back(cur, mr, reason, "github")
            if not final:
                return _superseded()
        else:
            fence, fparams = _fence(mr, "merging")
            cur.execute("UPDATE merge_requests SET state = 'approved', merge_note = %s, "
                        f"next_attempt_at = now() + make_interval(secs => %s) WHERE id = %s {fence} RETURNING id",
                        (note[:500], backoff_seconds(mr["merge_attempts"]), mr["id"], *fparams))
            if not cur.fetchone():
                return _superseded()
    if final:
        _tell_task(mr, f"Merge of {mr['sha'][:12]} was sent back: GitHub failed {MAX_ATTEMPTS} times ({note}).", True)
    return {"state": "sent_back" if final else "approved", "note": note, "target": ""}


def _merged(mr: dict, target: str, landed: str, note: str = "") -> dict:
    with get_cursor() as cur:
        fence, fparams = _fence(mr, "merging")
        cur.execute("UPDATE merge_requests SET state = 'merged', merged_at = now(), merged_sha = %s, merged_by = %s, merge_target = %s, "
                    f"merge_note = %s WHERE id = %s {fence} RETURNING id", (landed, ACTOR, target, note, mr["id"], *fparams))
        done = cur.fetchone()
        if done:
            cur.execute("SELECT display_name FROM users WHERE id = %s", (mr["decided_by"],))
            who = cur.fetchone()
            repo.record_activity(cur, mr["project_id"], ACTOR, "merged", "merge", mr["id"],
                                 f"Merged {mr['branch']} to {target} at {landed[:12]}, approved by {(who or {}).get('display_name') or 'an Admin'}"
                                 + (f" ({note})" if note else ""))
    if not done:
        return _superseded()
    _tell_task(mr, f"Merged {mr['branch']} to {target} at {landed[:12]} ({note or 'fast-forward, approved by an Admin'}).", False)
    return {"state": "merged", "note": note, "target": target}


def recover() -> int:
    """Claims whose process died (still 'merging' after MERGING_STALE_SECONDS) go back to approved. A rerun is idempotent: the
    compare says 'identical' for a push that did land, and the request is recorded as merged. Returns how many."""
    with get_cursor() as cur:
        cur.execute("UPDATE merge_requests SET state = 'approved', next_attempt_at = NULL, "
                    "merge_note = 'the executor stopped while merging this; it is being retried' "
                    "WHERE state = 'merging' AND COALESCE(merging_at, '-infinity') < now() - make_interval(secs => %s) "
                    "RETURNING id, project_id, branch, sha", (MERGING_STALE_SECONDS,))
        rows = cur.fetchall()
        for r in rows:
            repo.record_activity(cur, r["project_id"], ACTOR, "recovered", "merge", r["id"],
                                 f"Merge of {r['branch']} ({r['sha'][:12]}) was left merging by a stopped process: back to approved")
    return len(rows)


PRE_OPTIN_NOTE = "approved before Crewboss merging was on; ask again"


def _send_back_approved(where: str, params: tuple, reason: str, kind: str, told: str) -> int:
    with get_cursor() as cur:
        cur.execute("SELECT m.* FROM merge_requests m JOIN projects p ON p.id = m.project_id "
                    f"WHERE m.state = 'approved' AND p.merge_via_crewboss AND {where} FOR UPDATE OF m SKIP LOCKED", params)
        rows = [dict(r) for r in cur.fetchall()]
        done = [r for r in rows if _sent_back(cur, r, reason, kind, from_state="approved")]
    for r in done:
        _tell_task(r, f"Merge of {r['branch']} ({r['sha'][:12]}) was sent back: {told}.", True)
    return len(done)


def send_back_pre_optin(project_id: int | None = None) -> int:
    """Approvals decided before (or at) the project's opt-in are never claimed, so they would sit 'approved' for ever. They are sent back
    with a note to ask again (a plain sent_back: a new request, not a re-queue). Called when the executor is turned on, and every pass."""
    return _send_back_approved("m.decided_at <= p.merge_via_crewboss_at AND (%s::int IS NULL OR m.project_id = %s)", (project_id, project_id),
                               PRE_OPTIN_NOTE, "", PRE_OPTIN_NOTE)


def expire() -> int:
    """Approvals (made after the opt-in) older than APPROVAL_TTL_HOURS on opted-in projects are sent back (an Admin re-queues them with a
    fresh factor); approvals from before the opt-in are sent back too (send_back_pre_optin)."""
    n = _send_back_approved("m.decided_at > p.merge_via_crewboss_at AND m.decided_at <= now() - make_interval(hours => %s)", (APPROVAL_TTL_HOURS,),
                            f"the approval is older than {APPROVAL_TTL_HOURS} hours: an Admin re-queues it (it needs no new commit)", "expired",
                            f"its approval expired after {APPROVAL_TTL_HOURS} hours")
    return n + send_back_pre_optin()


@contextmanager
def _run_lock():
    """True when this process holds the merge pass's advisory lock (session level, own connection), else False."""
    conn = connect()
    try:
        got = conn.execute("SELECT pg_try_advisory_lock(%s) AS g", (LOCK_KEY,)).fetchone()["g"]
        conn.commit()
        try:
            yield bool(got)
        finally:
            if got:
                conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
                conn.commit()
    finally:
        conn.close()


def run_once(client=None) -> int:
    """One pass: recover dead claims, expire old approvals, then run every approved request of an opted-in project that is due.
    One process at a time. Returns how many it ran."""
    with _run_lock() as got:
        if not got:
            return 0
        recover()
        expire()
        with get_cursor(commit=False) as cur:
            cur.execute("SELECT m.id FROM merge_requests m JOIN projects p ON p.id = m.project_id "
                        "WHERE m.state = 'approved' AND p.merge_via_crewboss ORDER BY m.id")
            ids = [r["id"] for r in cur.fetchall()]
        identity: dict = {}          # one GET /user for the whole pass
        return sum(1 for merge_id in ids if execute(merge_id, client, identity)["state"] != "skipped")


# --- enrolment (the instance owner's list) and the per-project opt-in ---------------------------------------------------------

def _repo_key(full_name: str) -> str:
    full = (full_name or "").strip().lower()
    if not release_gates.REPO_RE.match(full):
        raise BadRequest("a repository looks like owner/name")
    return full


def list_repos() -> list[dict]:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT r.full_name, r.added_by, r.added_at, r.project_id, p.slug AS project_slug, "
                    "(p.merge_via_crewboss AND p.merge_repo = r.full_name) AS in_use "
                    "FROM merge_repos r JOIN projects p ON p.id = r.project_id ORDER BY r.full_name")
        return [dict(r) for r in cur.fetchall()]


def enrol_repo(full_name: str, project_id: int, by: str) -> str:
    """The instance owner allows the executor to merge into this repository FOR THIS PROJECT (the caller has checked who and the fresh
    factor). A repository belongs to one project: enrolling it for another means removing it first."""
    full = _repo_key(full_name)
    with get_cursor() as cur:
        cur.execute("INSERT INTO merge_repos (full_name, project_id, added_by) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING RETURNING full_name",
                    (full, project_id, by[:100]))
        if not cur.fetchone():
            cur.execute("SELECT project_id FROM merge_repos WHERE full_name = %s", (full,))
            if cur.fetchone()["project_id"] != project_id:
                raise Conflict(f"{full} is already enrolled for another project: remove it there first")
    return full


def unenrol_repo(full_name: str, by: str) -> str:
    """Removing a repository also turns off the opt-in of every project that used it; their approved requests are then never claimed."""
    full = _repo_key(full_name)
    with get_cursor() as cur:
        cur.execute("DELETE FROM merge_repos WHERE full_name = %s RETURNING full_name", (full,))
        if cur.fetchone():
            cur.execute("UPDATE projects SET merge_via_crewboss = false, merge_repo = '', merge_via_crewboss_at = NULL "
                        "WHERE merge_repo = %s AND merge_via_crewboss RETURNING id", (full,))
            for p in cur.fetchall():
                repo.record_activity(cur, p["id"], by, "disabled", "project", p["id"],
                                     f"Crewboss no longer merges approved branches itself: {full} was removed from the enrolled repositories")
    return full


def enable(project: dict, by: str, client=None) -> str:
    """Turn the executor on for a project. Refused unless: web sign-in is on (approvals need a real second factor), the project's remote
    is a GitHub repo the instance owner enrolled, no other project already uses it, a bot login is configured (it REFUSES without one), a token file is
    configured, GET /user answers, and the login is exactly that bot and not on config.GITHUB_DENY_LOGINS. Returns the bot's login."""
    if not web_auth.required():
        raise BadRequest("the merge executor needs web sign-in on (CREWBOSS_WEB_AUTH): its approvals rely on a real second factor")
    full = github_client.repo_full_name(project.get("git_remote") or "").lower()
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT 1 FROM merge_repos WHERE full_name = %s AND project_id = %s", (full, project["id"]))
        if not cur.fetchone():
            raise BadRequest(f"{full} is not enrolled for this project: the instance owner adds it to the repositories Crewboss may merge "
                             "to first")
    login = github_client.credential_login(client)
    try:
        with get_cursor() as cur:
            cur.execute("UPDATE projects SET merge_via_crewboss = true, merge_repo = %s, merge_via_crewboss_at = now() WHERE id = %s",
                        (full, project["id"]))
            repo.record_activity(cur, project["id"], by, "enabled", "project", project["id"],
                                 f"Crewboss merges approved branches into {full} itself (GitHub account {login})")
    except psycopg.errors.UniqueViolation:
        raise Conflict(f"another project already has Crewboss merging into {full}") from None
    send_back_pre_optin(project["id"])          # approvals decided before this opt-in would otherwise sit approved for ever
    return login


def set_enabled(project_id: int, enabled: bool, by: str) -> None:
    """Switch the opt-in OFF (enable() is the only way on). Clears the repo and the opt-in time."""
    if enabled:
        raise BadRequest("use enable() to turn the merge executor on")
    with get_cursor() as cur:
        cur.execute("UPDATE projects SET merge_via_crewboss = false, merge_repo = '', merge_via_crewboss_at = NULL WHERE id = %s", (project_id,))
        repo.record_activity(cur, project_id, by, "disabled", "project", project_id, "Crewboss no longer merges approved branches itself")
