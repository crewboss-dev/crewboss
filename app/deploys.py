"""Deploy requests, approval and the server-side executor (task #627, change #50, phase P5). See docs/deploy-executor.md.

A PC never holds SSH material. A running job ASKS (job MCP request_deploy: a target name and a full commit sha); a person
approves a card on /<account>/approvals; only then does THIS process, the main Dashboard server, run the deploy:

  requested -> approved -> running -> done | failed | orphaned          (or requested -> refused)

* The executor takes the deploy lock for the target (locks row: kind 'deploy', resource 'target:<id>', job_id NULL,
  server_job_id = the request). A job token can't take that lock (app/jobs.py JOB_LOCK_KINDS). A second approved deploy for the
  same target stays `approved` and waits; the loop tries it again next tick.
* It signs a certificate from the account's existing CA (app/ssh_ca.py) for exactly that server's principal, valid only as
  long as one deploy may run (DEPLOY_TIMEOUT_SECONDS), with NO pty and NO port forwarding, and a FORCED COMMAND: the
  certificate itself can run that one command and nothing else, whatever the client asks for. The host key is the one
  pinned at the server's first check (StrictHostKeyChecking=yes). Servers that connect through a jump host are refused:
  a forced-command pass cannot open the jump hop.
* The command is `git pull --ff-only` in the target's repo path, then the target's post_cmd. It first checks the branch,
  and checks HEAD against the approved sha right after the pull and BEFORE post_cmd; a mismatch skips post_cmd and the
  request is `failed` with flagged = true. The dashboard re-checks the HEAD the box reported.
* The output tail is redacted (best effort) and capped; it is a record, not a place for secrets.
* If the app restarts mid-deploy the running request becomes `orphaned`, its lock is marked orphaned, and nothing retries
  it: a person clears the lock on the Machines page and asks again (the P2 orphan rule).
* The executor runs in the app process as an asyncio task that calls Executor.run_once in a thread (like the job
  sweeper), so one process runs one deploy at a time; the database lock is what keeps a second process or restart honest.
"""
from __future__ import annotations

import logging
import math
import os
import re
import shlex
import subprocess
import uuid
from contextlib import contextmanager
from pathlib import Path

from psycopg.errors import UniqueViolation

from app import config, jobs, repo, server_access, ssh_ca, tree
from app.db import connect, get_cursor
from app.repo import BadRequest, Conflict, Forbidden, NotFound

log = logging.getLogger("crewboss.deploys")

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
OPEN_STATES = ("requested", "approved", "running")
TAIL_CHARS = 4000
SUBJECT_MAX = 200

# Exit codes of the remote script (anything else is the post_cmd's own).
EXIT_NO_REPO, EXIT_WRONG_BRANCH, EXIT_PULL, EXIT_MISMATCH, EXIT_NOT_ON_BRANCH = 97, 96, 98, 99, 95
EXIT_TIMEOUT = 124   # coreutils `timeout`
JUMP_MSG = "targets that connect through a jump host are not supported by the executor"
EXEC_LOCK_KEY = 627005   # pg advisory lock: one process executes deploys
_EXIT_TEXT = {EXIT_NO_REPO: "the repo path does not exist on the server", EXIT_WRONG_BRANCH: "the repo is not on the target's branch",
              EXIT_PULL: "git fetch or git merge --ff-only failed (not a fast-forward, or the remote is unreachable)",
              EXIT_TIMEOUT: "the deploy timed out on the box and was stopped",
              EXIT_MISMATCH: "HEAD after the pull is not the approved commit; post_cmd was not run"}

_QUERY_KEYS = (r"key|pwd|passwd|password|auth|sig|signature|session|sid|token|access_token|refresh_token|client_secret|api_key|apikey")
_REDACT = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S), "[private key removed]"),
    # whole-line credentials in headers (Name: value or name=value), case-insensitive
    (re.compile(r"(?im)\b((?:proxy-)?authorization|set-cookie|cookie)(\s*[:=]\s*)[^\r\n]*"), r"\1\2[removed]"),
    # user:password@ inside any URL
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]{0,31}://[^\s/:@]+:)[^\s/@]+(@)"), r"\1[removed]\2"),
    (re.compile(r"(?i)\bbearer\s+\S+"), "Bearer [removed]"),
    # query / form parameters that carry a credential
    (re.compile(r"(?i)(?<![A-Za-z0-9_])((?:" + _QUERY_KEYS + r")=)[^&\s\"'<>]+"), r"\1[removed]"),
    # a YAML key whose value is on the NEXT line (`password:` / `password: |` then an indented line). Runs before the same-line rule below.
    # The key is exactly password / passwd / pwd, with an optional prefix ending in _ . or - (db_password) and an optional short suffix
    # (password_hash, password_2): `compass:` and `password_policy:` are not password keys and keep their next line.
    (re.compile(r"(?im)^([ \t]*(?:-[ \t]+)?[\"']?(?:[A-Za-z0-9_.-]{0,64}[_.-])?(?:password|passwd|pwd)"
                r"(?:[_-](?:value|new|old|hash|secret|[0-9]))?[\"']?[ \t]*:[ \t]*(?:[|>][+-]?[0-9]?[ \t]*)?\r?\n[ \t]+)[^\r\n]+"), r"\1[removed]"),
    # *_PW=value / PW: value (an env name that ends in PW; "pw" followed by = or : is a password, nothing else)
    (re.compile(r"(?i)(pw[ \t]*[=:][ \t]*)(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|\S+)"), r"\1[removed]"),
    # sshpass -p PASSWORD (also -p'PASSWORD'). The gap is bounded (40) so a long run of `sshpass ` words stays linear.
    (re.compile(r"(?i)\b(sshpass\b[^\r\n]{0,40}?\s-p[ \t]*)(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|\S+)"), r"\1[removed]"),
    # curl -u user:pass / --user user:pass (a colon in the value; uid:gid such as `docker run -u 1000:1000` is not a credential)
    # (`-ubob:pass` with no space, `--user=`, and a quoted value that holds spaces: -u 'bob:pa ss')
    (re.compile(r"(?<![\w-])((?:-u[ \t]*=?|--user(?:[ \t]+|=))[\"']?)(?!\d+:\d+(?:[\"'\s]|$))"
                r"(?:(?<=\")[^\":\r\n]{1,64}:[^\"\r\n]*|(?<=')[^':\r\n]{1,64}:[^'\r\n]*|[^\s:'\"]{1,64}:[^\s'\"]+)"), r"\1[removed]"),
    # name=value / name: value secrets, also JSON-quoted ("password": "x", "db_password":"x", 'k': 'v'); pwd/passwd names; names that end in
    # key/secret/token with no word boundary before it (STRIPE_KEY=, GITHUBTOKEN=, db_secret:). Anchored on the KEYWORD (the name's prefix is left
    # where it is) with bounded parts and a same-line separator: linear time on a long unbroken run, and it never eats the next line's token.
    (re.compile(r"(?i)((?:pass(?:word|wd)?|pwd|secret|token|api[_-]?key|[_-]key|authorization|credential)[A-Za-z0-9_.-]{0,64}[\"']?"
                r"[ \t]*[=:][ \t]*)(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|\S+)"), r"\1[removed]"),
    (re.compile(r"(?<=[A-Z0-9_])(KEY[ \t]*[=:][ \t]*)\S+"), r"\1[removed]"),      # an upper-case env name ending in KEY (MYKEY=, SSH_KEY=)
    # a 32-64 hex value after key / token / secret (a 64-hex there goes even when it carries a sha256: prefix) (kept apart from the commit-id rule below, which leaves a bare 40-hex alone)
    (re.compile(r"(?i)((?:key|token|secret)[\"']?[ \t]*[=:][ \t]*[\"']?(?:sha256:)?)[0-9a-f]{32,64}\b"), r"\1[removed]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[token removed]"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"), "[token removed]"),          # a Google API key
    (re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*"), "[token removed]"),
    # mysql -pPASSWORD (no space: that is how the client reads it). The gap is bounded (80): an unbounded lazy gap was quadratic on a long
    # run of `mysql ` words.
    (re.compile(r"(?i)\b((?:mysql|mariadb|mysqldump|mysqladmin)\b[^\r\n]{0,80}?\s)-p\S+"), r"\1-p[removed]"),
    # crypt(3) password hashes
    (re.compile(r"\$(?:6|5|1|y|7|2[abxy]?)\$[./A-Za-z0-9$,=-]{8,}"), "[hash removed]"),
    (re.compile(r"\$(?:apr1|argon2(?:id|i|d)|pbkdf2(?:-sha(?:1|256|512))?|scrypt|sha1|md5|P|H|S)\$[./A-Za-z0-9$,=+-]{8,}"), "[hash removed]"),
    (re.compile(r"\{(?:S?SHA(?:1|256|512)?|MD5|CRYPT|SHA(?:256|512)-CRYPT|ARGON2|PBKDF2(?:-SHA(?:1|256|512))?)\}[A-Za-z0-9+/=$.-]{8,}"), "[hash removed]"),
    (re.compile(r"\b(?:cbj|cbr|cb|mdj|mdd|mdt|sk|ghp|gho|xox[a-z])[_-][A-Za-z0-9_-]{16,}"), "[token removed]"),
    (re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/_-]{32,}={0,2}(?![A-Za-z0-9+/])"), None),   # long opaque blobs, but not a 40-hex sha
]
_INSTANCE = uuid.uuid4().hex   # this process; a running request owned by another instance is a dead one


def _blob(m) -> str:
    """A long opaque run is a secret, except a git commit id (40 hex) and an image digest (`sha256:` + 64 hex). A bare 32-hex (an md5 or a
    session id) and a bare 64-hex (a sha256 that is not labelled as one) go. Between 32 and 39 characters it must mix upper, lower and
    digits (a plain word or a path is left alone); from 40 on any such run goes."""
    t = m.group(0)
    if re.fullmatch(r"[0-9a-fA-F]{32}", t):
        return "[removed]"
    if re.fullmatch(r"[0-9a-fA-F]{64}", t):
        return t if m.string[max(m.start() - 7, 0):m.start()].lower() == "sha256:" else "[removed]"
    if SHA_RE.match(t) or re.fullmatch(r"[0-9a-f]{40,64}", t):
        return t
    if len(t) < 40 and not (re.search(r"[A-Z]", t) and re.search(r"[a-z]", t) and re.search(r"[0-9]", t)):
        return t
    return "[removed]"


def redact(text: str) -> str:
    """Best-effort scrub of output before it is stored or shown: key blocks, credential headers, URL passwords, credential query parameters,
    name=value secrets, bearer/known token shapes, AWS keys, JWTs, mysql -p, sshpass -p, curl -u, password hashes and long opaque strings
    (a plain 40-hex commit and a sha256:-prefixed image digest are kept; a bare 32-hex or 64-hex is not)."""
    for pat, repl in _REDACT:
        text = pat.sub(_blob, text) if repl is None else pat.sub(repl, text)
    return text


def tail(text: str) -> str:
    text = redact((text or "").replace("\x00", ""))
    return text[-TAIL_CHARS:]


def lock_resource(target_id: int) -> str:
    return f"target:{int(target_id)}"


# --- targets (set by a person) ---------------------------------------------------------------------------------------

def _account_of(project_id: int) -> int:
    account_id = tree.account_root_id(project_id)
    if account_id is None:
        raise NotFound("no such project")
    return account_id


def add_target(account_id: int, project_slug: str, name: str, server_id: int, repo_path: str, post_cmd: str = "",
               branch: str = "main", by: str = "", env: str = "prod", timeout_seconds: int | None = None) -> dict:
    """A person defines where a project deploys. The project and the server must both be this account's (another
    account's is NotFound). The shape of every field is also checked by the table."""
    try:
        project = repo.get_project_by_slug((project_slug or "").strip())
    except NotFound:
        project = None
    if project is None or tree.account_root_id(project["id"]) != account_id:
        raise NotFound(f"no such project {project_slug!r}")
    return _add_target_checked(account_id, project, int(server_id), name, repo_path, post_cmd, branch, by, env, timeout_seconds)


def clean_timeout(value) -> int | None:
    """The per-target deploy time limit in seconds: blank/None = the instance default; else 60 to 7200 (the table checks it too)."""
    if value is None or str(value).strip() == "":
        return None
    try:
        secs = int(str(value).strip())
    except ValueError:
        raise BadRequest("time limit: a whole number of seconds (60 to 7200), or blank for the default") from None
    if not 60 <= secs <= 7200:
        raise BadRequest("time limit: 60 to 7200 seconds, or blank for the default")
    return secs


def target_timeout(tgt: dict) -> int:
    """Seconds this target's deploy may run: its own limit, else the instance default."""
    return int(tgt.get("timeout_seconds") or config.DEPLOY_TIMEOUT_SECONDS)


def _add_target_checked(account_id: int, project: dict, server_id: int, name: str, repo_path: str, post_cmd: str,
                        branch: str, by: str, env: str, timeout_seconds: int | None = None) -> dict:
    server = server_access.get(account_id, int(server_id))
    if server.get("via_server_id"):
        raise BadRequest(JUMP_MSG)
    if not server_access.is_ready(server):       # a target never points at a box that cannot be deployed to (#810)
        raise BadRequest(f"{server['name']} isn't ready to deploy to: check access on the Machines page first")
    name, repo_path, branch = (name or "").strip(), (repo_path or "").strip(), (branch or "main").strip()
    post_cmd = (post_cmd or "").strip()
    env = clean_env(env)
    timeout_seconds = clean_timeout(timeout_seconds)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}", name):
        raise BadRequest("name: letters, digits, . _ - (up to 63)")
    if not re.fullmatch(r"/[A-Za-z0-9._/-]{1,200}", repo_path) or ".." in repo_path:
        raise BadRequest("repo path: an absolute path of letters, digits, . _ / - (no ..)")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,100}", branch):
        raise BadRequest("branch: letters, digits, . _ / -")
    if len(post_cmd) > 500 or re.search(r"[\r\n\x00-\x08]", post_cmd):
        raise BadRequest("post command: one line, up to 500 characters")
    try:
        with get_cursor() as cur:
            cur.execute("INSERT INTO deploy_targets (project_id, server_id, name, repo_path, post_cmd, branch, created_by, env, timeout_seconds) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
                        (project["id"], server["id"], name, repo_path, post_cmd, branch, by[:100], env, timeout_seconds))
            row = cur.fetchone()
            repo.record_activity(cur, account_id, by[:100] or "owner", "added", "deploy_target", row["id"],
                                 f"Added deploy target {name} for {project['slug']} on {server['name']}")
    except UniqueViolation:
        raise Conflict(f"{project['slug']} already has a deploy target named {name!r}") from None
    return dict(row)


ENVS = ("dev", "staging", "prod")


def clean_env(env: str) -> str:
    env = (env or "").strip().lower()
    env = "prod" if env == "production" else env
    if env not in ENVS:
        raise BadRequest("environment: dev, staging or production")
    return env


def set_target_env(account_id: int, target_id: int, env: str, by: str = "") -> None:
    """Tag a target dev or prod (a person; the chip and the filter on the Servers page read it)."""
    env = clean_env(env)
    with get_cursor() as cur:
        cur.execute("UPDATE deploy_targets t SET env = %s FROM commissioned_servers s "
                    "WHERE t.id = %s AND s.id = t.server_id AND s.account_id = %s RETURNING t.id",
                    (env, target_id, account_id))
        if not cur.fetchone():
            raise NotFound("no such deploy target")
        repo.record_activity(cur, account_id, by[:100] or "owner", "tagged", "deploy_target", target_id,
                             f"Deploy target {target_id} is now {env}")


def set_target_enabled(account_id: int, target_id: int, enabled: bool, by: str = "") -> None:
    with get_cursor() as cur:
        cur.execute("UPDATE deploy_targets t SET enabled = %s FROM commissioned_servers s "
                    "WHERE t.id = %s AND s.id = t.server_id AND s.account_id = %s RETURNING t.id",
                    (bool(enabled), target_id, account_id))
        if not cur.fetchone():
            raise NotFound("no such deploy target")
        repo.record_activity(cur, account_id, by[:100] or "owner", "enabled" if enabled else "disabled", "deploy_target",
                             target_id, f"{'Enabled' if enabled else 'Disabled'} deploy target {target_id}")


def list_targets(account_id: int) -> list[dict]:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT t.*, p.slug AS project_slug, s.name AS server_name, s.address, s.login_user "
                    "FROM deploy_targets t JOIN projects p ON p.id = t.project_id "
                    "JOIN commissioned_servers s ON s.id = t.server_id WHERE s.account_id = %s ORDER BY p.slug, lower(t.name)",
                    (account_id,))
        return [dict(r) for r in cur.fetchall()]


# --- release governance (change #77, task #713) ---------------------------------------------------------------------------
# Dev first, production by PROMOTION only, an Admin approves. The rules, in one place:
#   * a target is dev | staging | prod ('production' is an alias of prod). A runner is never a production target: targets are
#     commissioned servers, and a runner only ever previews (a 'preview' row, below).
#   * dev / staging: a Member or above deploys at once (no approval) to a server the owner assigned them (the instance owner: any).
#   * prod: promote-only. The commit must have a successful dev or staging deploy recorded for this project; under the project's
#     release_policy 'production_only' (an app with no dev environment, e.g. a website) a successful runner PREVIEW run of the
#     exact commit stands in. A commit that never ran there is refused. A Member REQUESTS; an Admin (project Owner/Admin, or the
#     instance owner) approves, with a second factor shown in the last 5 minutes. With 2+ Admins the approver is not the requester;
#     a solo Admin may approve their own.
#   * hotfix: an explicit flag with a reason. Still branch -> tests -> Admin approval; the dev/staging run may be skipped, the
#     skip is written to the audit log, and approving it opens a follow-up task that merges the fix back into dev and main.
#   * rollback: put back the previous release of a target (the last successful deploy before the current one). It already ran in
#     production, so it needs no dev run, but it takes the same Admin approval, factor and two-person rule.
# Every request and every approval records the real user (server_jobs.requested_by / decided_by, activity.user_id).

PROD_ENV = "prod"
DEV_LIKE = ("dev", "staging")
POLICIES = ("standard", "production_only")


def set_release_policy(who, project: dict, policy: str) -> dict:
    """Only the project's Owner or the instance owner (change #77); audited."""
    from app import access
    if access.role_in(who, project["id"]) != access.PROJECT_OWNER:
        raise NotFound("project not found")
    if policy not in POLICIES:
        raise BadRequest(f"release policy: {' or '.join(POLICIES)}")
    with get_cursor() as cur:
        cur.execute("UPDATE projects SET release_policy = %s WHERE id = %s RETURNING *", (policy, project["id"]))
        row = cur.fetchone()
        repo.record_activity(cur, project["id"], f"user:{who.user_id}" if who.user_id else "owner", "release policy",
                             "project", project["id"], f"Release policy of {project['name']} set to {policy}")
    return dict(row)


def _ran_before(cur, project: dict, sha: str) -> bool:
    """Has this exact commit run somewhere it is allowed to be promoted from? dev/staging deploys that succeeded; for a
    production_only project also a successful runner preview run."""
    envs = list(DEV_LIKE) + (["preview"] if project.get("release_policy") == "production_only" else [])
    cur.execute("SELECT 1 FROM server_jobs WHERE project_id = %s AND sha = %s AND env = ANY(%s) AND state = 'done' LIMIT 1",
                (project["id"], sha, envs))
    return cur.fetchone() is not None


def record_preview(job_id: int, fence: int, sha: str) -> dict:
    """A RUNNING job reports that the runner built `sha`, ran the tests and started the local preview (the #707 live preview).
    Recorded as a deploy-like row (env 'preview', no target, done) so promotion checks one table. It only counts for a project
    whose release_policy is production_only."""
    sha = (sha or "").strip().lower()
    if not SHA_RE.match(sha):
        raise BadRequest("sha must be the full 40-character commit id")
    with get_cursor() as cur:
        job = jobs._running(cur, job_id, fence)
        cur.execute("SELECT project_id FROM tasks WHERE id = %s", (job["task_id"],))
        task = cur.fetchone()
        if not task:
            raise NotFound("the job's task is gone")
        cur.execute("INSERT INTO server_jobs (account_id, project_id, target_name, job_id, task_id, device_id, requested_by, sha, "
                    "state, env, started_at, finished_at, decided_at) VALUES (%s, %s, 'preview', %s, %s, %s, %s, %s, 'done', "
                    "'preview', now(), now(), now()) RETURNING *",
                    (job["account_id"], task["project_id"], job_id, job["task_id"], job["device_id"], job["user_id"], sha))
        row = cur.fetchone()
        repo.record_activity(cur, task["project_id"], jobs.actor(job_id), "previewed", "server_job", row["id"],
                             f"Runner preview of {sha[:12]} ran green")
    return dict(row)


def add_target_assigned(who, project: dict, server_id: int, name: str, repo_path: str, post_cmd: str = "",
                        branch: str = "main", env: str = "dev", timeout_seconds: int | None = None) -> dict:
    """The project's OWNER (its creator; the instance owner uses the Servers page) defines where the project deploys: repo path,
    post command, branch. A Member or Admin never does, because those commands run on a server and dev deploys need no approval.
    The server must be PINNED to this project by the instance owner (any other is NotFound, like a missing one). The same field
    checks as add_target; the server stays in the owner's account (its CA, its executor)."""
    from app import access
    env = clean_env(env)
    role = access.role_in(who, project["id"])
    if who.is_owner or role is None:
        raise NotFound("no such server")
    if role != access.PROJECT_OWNER:
        raise Forbidden("only the project's Owner defines deploy targets")
    if not access.server_assigned_to_project(project["id"], int(server_id)):
        raise NotFound("no such server")
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT account_id FROM commissioned_servers WHERE id = %s", (int(server_id),))
        srv = cur.fetchone()
    if not srv:
        raise NotFound("no such server")
    # One instance-wide executor runs one deploy at a time: a project Owner (not the instance owner) may not hold it longer than the
    # instance default. Anything longer is capped to the default; only the instance owner (Servers page / Releases tab) sets more.
    secs = clean_timeout(timeout_seconds)
    if secs is not None and secs > config.DEPLOY_TIMEOUT_SECONDS:
        secs = config.DEPLOY_TIMEOUT_SECONDS
    return _add_target_checked(srv["account_id"], project, int(server_id), name, repo_path, post_cmd, branch,
                               f"user:{who.user_id}", env, secs)


def can_manage_targets(who, project: dict) -> bool:
    """Who adds, tags and switches a project's deploy targets: the instance owner and the project's Owner (a Member or Admin never:
    a target's commands run on a server, and dev deploys need no approval). The controls live on the project's Releases tab (#810)."""
    from app import access
    return access.role_in(who, project["id"]) == access.PROJECT_OWNER


def pickable_servers(who, project: dict) -> list[dict]:
    """The READY machines this person may point the project at: the instance owner, every ready server of the project's account; the
    project's Owner (a designer), only the ones the instance owner pinned to this project."""
    account_id = _account_of(project["id"])
    rows = server_access.list_for([account_id])
    if not who.is_owner:
        from app import access
        rows = [r for r in rows if access.server_assigned_to_project(project["id"], r["id"])]
    return [r for r in rows if server_access.is_ready(r)]


def add_target_from_project(who, project: dict, server_id: int, name: str, repo_path: str, post_cmd: str = "",
                            branch: str = "main", env: str = "dev", timeout_seconds: int | None = None) -> dict:
    """"Add a place to deploy", from the project's Releases tab. The instance owner may use any ready server of the account; a
    project Owner only the servers pinned to the project (add_target_assigned, unchanged); nobody else. A server that isn't
    ready is refused with the reason, so a target never points at a box that can't be reached."""
    if not can_manage_targets(who, project):
        raise Forbidden("only the project's Owner defines deploy targets")
    if not who.is_owner:
        return add_target_assigned(who, project, server_id, name, repo_path, post_cmd, branch, env, timeout_seconds)
    account_id = _account_of(project["id"])
    server = server_access.get(account_id, int(server_id))
    if not server_access.is_ready(server):
        raise BadRequest(f"{server['name']} isn't ready to deploy to: check access on the Machines page first")
    return _add_target_checked(account_id, project, server["id"], name, repo_path, post_cmd, branch,
                               f"user:{who.user_id}" if who.user_id else "owner", clean_env(env), timeout_seconds)


def _own_target(project: dict, target_id: int) -> int:
    """The account id, if the target is this project's (another project's target is NotFound like a missing one)."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT 1 FROM deploy_targets WHERE id = %s AND project_id = %s", (int(target_id), project["id"]))
        if not cur.fetchone():
            raise NotFound("no such deploy target")
    return _account_of(project["id"])


def set_target_env_for(who, project: dict, target_id: int, env: str) -> None:
    if not can_manage_targets(who, project):
        raise Forbidden("only the project's Owner changes deploy targets")
    set_target_env(_own_target(project, target_id), target_id, env, f"user:{who.user_id}" if who.user_id else "owner")


def set_target_enabled_for(who, project: dict, target_id: int, enabled: bool) -> None:
    if not can_manage_targets(who, project):
        raise Forbidden("only the project's Owner changes deploy targets")
    set_target_enabled(_own_target(project, target_id), target_id, enabled, f"user:{who.user_id}" if who.user_id else "owner")


def previous_release(target_id: int) -> dict | None:
    """The release a rollback of this target restores: the last successful production deploy BEFORE the current one (a different
    commit). None when there is no earlier release."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT id, sha, tag, finished_at FROM server_jobs WHERE target_id = %s AND env = 'prod' AND state = 'done' "
                    "ORDER BY finished_at DESC, id DESC",
                    (target_id,))
        rows = cur.fetchall()
    if not rows:
        return None
    current = rows[0]["sha"]
    return next((dict(r) for r in rows[1:] if r["sha"] != current), None)


def request_release(who, project: dict, target_id: int, sha: str, hotfix: bool = False, reason: str = "", tag: str = "",
                    rollback: bool = False) -> dict:
    """A Member or above asks to deploy `sha` to one of the project's targets. dev/staging run at once (recorded as approved by
    the requester); prod waits for an Admin (see the block comment). Raises NotFound for a target they may not use,
    Forbidden for a viewer, BadRequest for a commit that never ran on dev."""
    from app import access
    sha = (sha or "").strip().lower()
    if not SHA_RE.match(sha):
        raise BadRequest("sha must be the full 40-character commit id")
    role = access.role_in(who, project["id"])
    if role == "viewer":
        raise Forbidden("a viewer can't deploy")
    if role is None:
        raise NotFound("no such deploy target")
    reason = " ".join((reason or "").split())[:300]
    with get_cursor() as cur:
        cur.execute("SELECT t.*, s.account_id AS server_account FROM deploy_targets t "
                    "JOIN commissioned_servers s ON s.id = t.server_id WHERE t.id = %s AND t.project_id = %s AND t.enabled",
                    (target_id, project["id"]))
        tgt = cur.fetchone()
        if not tgt or (not who.is_owner and not access.server_assigned_to_project(project["id"], tgt["server_id"])):
            raise NotFound("no such deploy target")
        env = tgt["env"]
        if hotfix and env != PROD_ENV:
            raise BadRequest("a hotfix is a production deploy")
        if env == PROD_ENV and not rollback and not _ran_before(cur, project, sha):
            if not hotfix:
                where = "dev or staging" if project.get("release_policy") != "production_only" else "dev, staging or a runner preview"
                raise BadRequest(f"{sha[:12]} has not run on {where} yet: production takes only a commit that did "
                                 "(promote it, or mark a real emergency as a hotfix)")
            if len(reason) < 8:
                raise BadRequest("a hotfix needs a reason (what is broken, why it can't wait)")
        row, fresh = _create_request(cur, tgt["server_account"], project["id"], tgt, sha, "", f"user:{who.user_id}",
                                     user_id=who.user_id, rollback=rollback)
        if not fresh:
            return dict(row)
        cur.execute("UPDATE server_jobs SET env = %s, hotfix = %s, hotfix_reason = %s, rollback = %s, tag = %s WHERE id = %s",
                    (env, hotfix, reason if hotfix else "", rollback, (tag or "")[:80], row["id"]))
        if hotfix:
            repo.record_activity(cur, project["id"], f"user:{who.user_id}", "hotfix", "server_job", row["id"],
                                 f"HOTFIX requested for {sha[:12]}, dev/staging skipped: {reason}")
        if env != PROD_ENV:
            cur.execute("UPDATE server_jobs SET state = 'approved', decided_by = %s, decided_at = now(), auto_approved = true "
                        "WHERE id = %s", (who.user_id, row["id"]))
            repo.record_activity(cur, project["id"], f"user:{who.user_id}", "approved", "server_job", row["id"],
                                 f"{env} deploy of {sha[:12]} to {tgt['name']} approved by its requester")
        cur.execute("SELECT * FROM server_jobs WHERE id = %s", (row["id"],))
        return dict(cur.fetchone())


def request_rollback(who, project: dict, target_id: int) -> dict:
    """One tap: put the target back on its previous release. Same Admin approval, factor and two-person rule as any production
    deploy; no dev run needed (it already ran in production)."""
    prev = previous_release(target_id)
    if prev is None:
        raise BadRequest("there is no earlier release of this target to go back to")
    return request_release(who, project, target_id, prev["sha"], tag=prev.get("tag") or "", rollback=True)


def approve_checked(who, project_id: int, server_job_id: int, factor_fresh: bool, approver_id: int | None = None) -> dict:
    """An Admin approves a requested deploy of this project (the instance owner always may). Production needs a fresh second
    factor, and with two or more Admins someone other than the requester. Records the real user; a hotfix opens the follow-up
    task that merges it back."""
    from app import access
    approver_id = approver_id if approver_id is not None else who.user_id
    if not access.is_admin(who, project_id):
        raise Forbidden("only a project Admin approves a release")
    with get_cursor() as cur:
        cur.execute("SELECT * FROM server_jobs WHERE id = %s AND project_id = %s FOR UPDATE", (server_job_id, project_id))
        sj = cur.fetchone()
        if not sj:
            raise NotFound(f"deploy request {server_job_id} not found")
        if sj["state"] != "requested":
            raise Conflict(f"deploy request {server_job_id} is already {sj['state']}")
        if sj["env"] == PROD_ENV:
            if not factor_fresh:
                raise Forbidden("confirm your second factor first (it must be within the last 5 minutes)")
            requester = sj["requested_by"]
            if requester is None and sj["job_id"]:
                cur.execute("SELECT user_id FROM jobs WHERE id = %s", (sj["job_id"],))
                j = cur.fetchone()
                requester = j["user_id"] if j else None
            admins = max(sj["admins_at_request"], len(access.admin_user_ids(project_id)))    # counted when it was asked, too
            if admins >= 2 and access.same_person(requester, approver_id):
                raise Forbidden("this project has more than one Admin: another Admin approves your production deploy")
        cur.execute("UPDATE server_jobs SET state = 'approved', decided_by = %s, decided_at = now() WHERE id = %s RETURNING *",
                    (approver_id, server_job_id))
        sj = cur.fetchone()
        actor = f"user:{approver_id}" if approver_id else "owner"
        repo.record_activity(cur, project_id, actor, "approved", "server_job", server_job_id,
                             f"Approved the {'HOTFIX ' if sj['hotfix'] else 'rollback ' if sj['rollback'] else ''}"
                             f"{sj['env']} deploy of {sj['sha'][:12]} to {sj['target_name']}")
        if sj["hotfix"]:
            task = repo.create_task(project_id, f"Merge hotfix {sj['sha'][:12]} back into dev and main",
                                    f"Production got {sj['sha']} as a hotfix (dev/staging skipped). Reason: {sj['hotfix_reason']}\n"
                                    "Merge it back so dev and main don't drift: branch, tests, then the usual Admin approval.",
                                    "must", "bug", [], {}, "owner", assignee="ai")
            cur.execute("UPDATE server_jobs SET followup_task_id = %s WHERE id = %s RETURNING *", (task["id"], server_job_id))
            sj = cur.fetchone()
    return dict(sj)


def refuse_checked(who, project_id: int, server_job_id: int, approver_id: int | None = None) -> dict:
    from app import access
    if not access.is_admin(who, project_id):
        raise Forbidden("only a project Admin refuses a release")
    with get_cursor() as cur:
        cur.execute("SELECT state FROM server_jobs WHERE id = %s AND project_id = %s FOR UPDATE", (server_job_id, project_id))
        sj = cur.fetchone()
        if not sj:
            raise NotFound(f"deploy request {server_job_id} not found")
        if sj["state"] != "requested":
            raise Conflict(f"deploy request {server_job_id} is already {sj['state']}")
        approver_id = approver_id if approver_id is not None else who.user_id
        cur.execute("UPDATE server_jobs SET state = 'refused', decided_by = %s, decided_at = now() WHERE id = %s RETURNING *",
                    (approver_id, server_job_id))
        row = cur.fetchone()
        repo.record_activity(cur, project_id, f"user:{approver_id}" if approver_id else "owner", "refused", "server_job",
                             server_job_id, f"Refused the deploy of {row['sha'][:12]} to {row['target_name']}")
    return dict(row)


def promotion_evidence(project_id: int, sha: str) -> list[dict]:
    """What lets this commit be promoted, for the approver to SEE: each dev/staging deploy or runner preview of the exact commit,
    with who ran it and, for a preview, which device attested it (the runner's own claim)."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT s.env, s.state, s.finished_at, s.target_name, d.name AS device_name, u.display_name AS by_name "
                    "FROM server_jobs s LEFT JOIN device_tokens d ON d.id = s.device_id "
                    "LEFT JOIN users u ON u.id = COALESCE(s.decided_by, s.requested_by) "
                    "WHERE s.project_id = %s AND s.sha = %s AND s.env IN ('dev', 'staging', 'preview') AND s.state = 'done' "
                    "ORDER BY s.id", (project_id, sha))
        return [dict(r) for r in cur.fetchall()]


def list_for_project(project_id: int, limit: int = 40) -> list[dict]:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT s.*, ru.display_name AS requested_name, du.display_name AS decided_name FROM server_jobs s "
                    "LEFT JOIN users ru ON ru.id = s.requested_by LEFT JOIN users du ON du.id = s.decided_by "
                    "WHERE s.project_id = %s ORDER BY (s.state IN ('requested', 'approved', 'running')) DESC, s.id DESC LIMIT %s",
                    (project_id, limit))
        rows = [dict(r) for r in cur.fetchall()]
    proj = None
    for r in rows:
        r["evidence"] = promotion_evidence(project_id, r["sha"]) if (r["env"] == PROD_ENV and r["state"] == "requested") else []
        r["state_warning"] = None
        if r["state"] in ("requested", "approved") and r.get("target_id"):     # #726: shown beside the request, never a block here
            from app import release_gates
            proj = proj or repo.get_project(project_id)
            r["state_warning"] = release_gates.state_warning(proj, r["target_id"])
    return rows


# --- a job asks -------------------------------------------------------------------------------------------------------

def request_deploy(job_id: int, fence: int, target: str, sha: str, subject: str = "") -> dict:
    """A RUNNING job (fence checked) asks to deploy `sha` to one of its project's enabled targets. Creates a `requested`
    server_job and sends a push notification; runs nothing. The job's own ids and the sha are all that is recorded as
    fact (the subject is the job's claim, kept short and shown labelled). Asking again for the same open target + sha
    returns the existing request."""
    sha = (sha or "").strip().lower()
    if not SHA_RE.match(sha):
        raise BadRequest("sha must be the full 40-character commit id")
    subject = " ".join((subject or "").split())[:SUBJECT_MAX]
    created = None
    with get_cursor() as cur:
        job = jobs._running(cur, job_id, fence)
        cur.execute("SELECT project_id FROM tasks WHERE id = %s", (job["task_id"],))
        row = cur.fetchone()
        if not row:
            raise NotFound("the job's task is gone")
        project_id = row["project_id"]
        cur.execute("SELECT t.* FROM deploy_targets t JOIN commissioned_servers s ON s.id = t.server_id "
                    "WHERE t.project_id = %s AND lower(t.name) = lower(%s) AND t.enabled AND s.account_id = %s",
                    (project_id, (target or "").strip(), job["account_id"]))
        tgt = cur.fetchone()
        if not tgt:
            log.warning("job %s: deploy target %r refused (not an enabled target of its project)", job_id, target)
            raise NotFound(f"no deploy target {target!r} for this project")
        existing = _open_request(cur, tgt["id"], sha)
        if existing:
            return {"server_job_id": existing["id"], "state": existing["state"], "target": tgt["name"], "sha": sha}
        _require_promoted(cur, project_id, tgt, sha)
        cur.execute("SELECT count(*) AS n FROM server_jobs WHERE job_id = %s AND state = ANY(%s)",
                    (job_id, list(OPEN_STATES)))
        if cur.fetchone()["n"] >= config.DEPLOY_MAX_OPEN_PER_JOB:
            raise Conflict("this job already has too many open deploy requests")
        created, fresh = _create_request(cur, job["account_id"], project_id, tgt, sha, subject, jobs.actor(job_id),
                                         job_id=job_id, task_id=job["task_id"], device_id=job["device_id"])
        if not fresh:
            return {"server_job_id": created["id"], "state": created["state"], "target": tgt["name"], "sha": sha}
        account_slug = repo.get_project(job["account_id"])["slug"]
    _notify(created, account_slug)
    return {"server_job_id": created["id"], "state": "requested", "target": created["target_name"], "sha": sha}


def _open_request(cur, target_id: int, sha: str):
    cur.execute("SELECT id, state FROM server_jobs WHERE target_id = %s AND sha = %s AND state = ANY(%s)",
                (target_id, sha, list(OPEN_STATES)))
    return cur.fetchone()


def _require_promoted(cur, project_id: int, tgt: dict, sha: str) -> None:
    """Production is promote-only for EVERY way of asking (a job, a person on the approvals page): the commit must have run on
    dev/staging (or a runner preview, for a production_only project). Hotfix and rollback go through request_release."""
    if (tgt.get("env") or "prod") != PROD_ENV:
        return
    cur.execute("SELECT * FROM projects WHERE id = %s", (project_id,))
    project = cur.fetchone()
    if not _ran_before(cur, project, sha):
        raise BadRequest(f"{sha[:12]} has not run on dev or staging yet: production takes only a commit that did")


def _create_request(cur, account_id: int, project_id: int, tgt: dict, sha: str, subject: str, actor: str,
                    job_id=None, task_id=None, device_id=None, user_id=None, rollback: bool = False) -> tuple[dict, bool]:
    """Insert a `requested` row. Two asks for the same open target + sha at once hit the partial unique index: the loser
    gets the existing request back ((row, False)). The insert is in a savepoint, so the caller's transaction survives.
    A production release (not a rollback) first passes the logic map gate (app/logic.py); an override it uses is used up here."""
    cur.execute("SELECT * FROM projects WHERE id = %s", (project_id,))
    proj = cur.fetchone()
    if proj and proj.get("require_state_declared"):        # #726: a project that opted in refuses a deploy whose state is not declared
        from app import release_gates
        warn = release_gates.state_warning(proj, tgt["id"])
        if warn:
            raise BadRequest(f"{warn['warning']}: this project's compose file shows {', '.join(warn['found'][:3])}. "
                             "Declare what must be backed up in Where it lives, or tick 'keeps no state'")
    from app import logic
    override_id = logic.gate(cur, project_id, sha) if (tgt.get("env") or "prod") == PROD_ENV and not rollback else None
    try:
        with cur.connection.transaction():
            cur.execute("INSERT INTO server_jobs (account_id, project_id, target_id, target_name, job_id, task_id, "
                        "device_id, requested_by, sha, subject, env) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
                        (account_id, project_id, tgt["id"], tgt["name"], job_id, task_id, device_id, user_id, sha, subject,
                         tgt.get("env") or "prod"))
            row = cur.fetchone()
    except UniqueViolation:
        return _open_request(cur, tgt["id"], sha), False
    if override_id is not None:
        logic.use_override(cur, override_id, row["id"])
        repo.record_activity(cur, project_id, actor, "overrode", "server_job", row["id"],
                             f"Release of {sha[:12]} went past the logic map gate on a recorded one-release override")
    from app import access
    cur.execute("UPDATE server_jobs SET admins_at_request = %s WHERE id = %s RETURNING *",
                (len(access.admin_user_ids(project_id)), row["id"]))
    row = cur.fetchone()
    repo.record_activity(cur, account_id, actor, "requested", "server_job", row["id"],
                         f"{actor} asked to deploy {sha[:12]} to {tgt['name']}")
    return row, True


def request_by_person(account_id: int, user_id: int, target_id: int, sha: str) -> dict:
    """A person (Entra session, checked by the page) asks for a deploy of a sha already on the target's branch. It still
    needs an approval card. Records who asked; no job."""
    sha = (sha or "").strip().lower()
    if not SHA_RE.match(sha):
        raise BadRequest("sha must be the full 40-character commit id")
    with get_cursor() as cur:
        cur.execute("SELECT t.* FROM deploy_targets t JOIN commissioned_servers s ON s.id = t.server_id "
                    "WHERE t.id = %s AND t.enabled AND s.account_id = %s", (target_id, account_id))
        tgt = cur.fetchone()
        if not tgt:
            raise NotFound("no such deploy target")
        _require_promoted(cur, tgt["project_id"], tgt, sha)
        row, _ = _create_request(cur, account_id, tgt["project_id"], tgt, sha, "", f"user:{user_id}", user_id=user_id)
    return dict(row)


def ask_again(account_id: int, server_job_id: int, user_id: int) -> dict:
    """Ask again on a failed, refused or orphaned card: a new `requested` row for the same target and sha, asked by this
    person. The old row stays as the record. An open request for the same target + sha is returned instead."""
    with get_cursor() as cur:
        cur.execute("SELECT * FROM server_jobs WHERE id = %s AND account_id = %s", (server_job_id, account_id))
        old = cur.fetchone()
        if not old:
            raise NotFound(f"deploy request {server_job_id} not found")
        if old["state"] not in ("failed", "refused", "orphaned"):
            raise Conflict(f"deploy request {server_job_id} is {old['state']}, not failed or refused")
        cur.execute("SELECT * FROM deploy_targets WHERE id = %s AND enabled", (old["target_id"],))
        tgt = cur.fetchone()
        if not tgt:
            raise NotFound("that deploy target is gone or disabled")
        if not (old["hotfix"] or old["rollback"]):
            _require_promoted(cur, old["project_id"], tgt, old["sha"])      # asking again is asking: same promote-only rule
        row, fresh = _create_request(cur, account_id, old["project_id"], tgt, old["sha"], old["subject"], f"user:{user_id}",
                                     task_id=old["task_id"], user_id=user_id, rollback=bool(old["rollback"]))
        if fresh:   # a hotfix stays a hotfix (flagged, and its approval opens the merge-back task); a rollback stays one
            cur.execute("UPDATE server_jobs SET hotfix = %s, hotfix_reason = %s, rollback = %s, tag = %s WHERE id = %s RETURNING *",
                        (old["hotfix"], old["hotfix_reason"], old["rollback"], old["tag"], row["id"]))
            row = cur.fetchone()
    return dict(row)


def _notify(sj: dict, account_slug: str) -> None:
    """The phone push: ids and the target name only. Best effort; a failed push never fails the request."""
    try:
        from app import push
        push.deliver({"title": "Deploy waiting for approval", "body": f"{sj['target_name']} · {sj['sha'][:12]}",
                      "url": f"{config.PUBLIC_URL}/{account_slug}/approvals", "tag": f"mdd-deploy-{sj['id']}",
                      "renotify": True})
    except Exception:  # noqa: BLE001
        log.exception("deploy push")


# --- a person decides -------------------------------------------------------------------------------------------------

def list_for(account_id: int, limit: int = 50) -> list[dict]:
    """The account's deploy requests, open ones first then newest, with who/what asked."""
    with get_cursor(commit=False) as cur:
        cur.execute(
            "SELECT s.*, p.slug AS project_slug, k.title AS task_title, d.name AS device_name, u.display_name AS decided_name, ru.display_name AS requested_name, "
            "(SELECT l.state FROM locks l WHERE l.account_id = s.account_id AND l.kind = 'deploy' "
            "   AND l.resource = 'target:' || s.target_id) AS lock_state "
            "FROM server_jobs s JOIN projects p ON p.id = s.project_id LEFT JOIN tasks k ON k.id = s.task_id "
            "LEFT JOIN device_tokens d ON d.id = s.device_id LEFT JOIN users u ON u.id = s.decided_by LEFT JOIN users ru ON ru.id = s.requested_by "
            "WHERE s.account_id = %s ORDER BY (s.state IN ('requested', 'approved', 'running')) DESC, s.id DESC LIMIT %s",
            (account_id, limit))
        return [dict(r) for r in cur.fetchall()]


def _decide(account_id: int, server_job_id: int, user_id: int, to_state: str) -> dict:
    with get_cursor() as cur:
        cur.execute("SELECT * FROM server_jobs WHERE id = %s AND account_id = %s FOR UPDATE", (server_job_id, account_id))
        sj = cur.fetchone()
        if not sj:
            raise NotFound(f"deploy request {server_job_id} not found")
        if sj["state"] != "requested":
            raise Conflict(f"deploy request {server_job_id} is already {sj['state']}")
        cur.execute("UPDATE server_jobs SET state = %s, decided_by = %s, decided_at = now() WHERE id = %s RETURNING *",
                    (to_state, user_id, server_job_id))
        sj = cur.fetchone()
        repo.record_activity(cur, account_id, f"user:{user_id}", to_state, "server_job", server_job_id,
                             f"{to_state.capitalize()} the deploy of {sj['sha'][:12]} to {sj['target_name']}")
    return dict(sj)


def approve(account_id: int, server_job_id: int, user_id: int) -> dict:
    """LOW LEVEL, no release rules: records who approved. No page calls this any more; the approvals page goes through
    approve_for_account -> approve_checked (Admin, fresh factor, two-person rule, hotfix follow-up)."""
    return _decide(account_id, server_job_id, user_id, "approved")


def refuse(account_id: int, server_job_id: int, user_id: int) -> dict:
    return _decide(account_id, server_job_id, user_id, "refused")


def _project_of(account_id: int, server_job_id: int) -> int:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT project_id FROM server_jobs WHERE id = %s AND account_id = %s", (server_job_id, account_id))
        row = cur.fetchone()
    if not row:
        raise NotFound(f"deploy request {server_job_id} not found")
    return row["project_id"]


def approve_for_account(who, account_id: int, server_job_id: int, factor_fresh: bool, approver_id: int) -> dict:
    """The approvals page's Approve: the SAME rules as the Releases page (approve_checked)."""
    return approve_checked(who, _project_of(account_id, server_job_id), server_job_id, factor_fresh, approver_id)


def refuse_for_account(who, account_id: int, server_job_id: int, approver_id: int) -> dict:
    return refuse_checked(who, _project_of(account_id, server_job_id), server_job_id, approver_id)


# --- the executor -----------------------------------------------------------------------------------------------------

def remote_script(repo_path: str, branch: str, sha: str, post_cmd: str, timeout_seconds: int | None = None) -> str:
    """The one line the certificate is allowed to run. HEAD can only become the approved sha: fetch the branch, require the
    sha to be on it already, then fast-forward to exactly that sha (never `git pull`, which would take whatever is newest).
    Markers are printed BEFORE post_cmd, so its own output can't spoof them (the dashboard reads the first of each). The
    whole script runs under coreutils `timeout`, so the box stops it when the dashboard gives up."""
    q = shlex.quote
    parts = [f"cd {q(repo_path)} || exit {EXIT_NO_REPO}",
             f'[ "$(git rev-parse --abbrev-ref HEAD)" = {q(branch)} ] || exit {EXIT_WRONG_BRANCH}',
             'echo "@@before $(git rev-parse HEAD)"',
             f"git fetch -q origin {q(branch)} || exit {EXIT_PULL}",
             f"git merge-base --is-ancestor {q(sha)} {q('origin/' + branch)} || exit {EXIT_NOT_ON_BRANCH}",
             f"git merge -q --ff-only {q(sha)} || exit {EXIT_PULL}",
             'echo "@@after $(git rev-parse HEAD)"',
             f'[ "$(git rev-parse HEAD)" = {q(sha)} ] || exit {EXIT_MISMATCH}']
    if post_cmd:
        parts.append(post_cmd)
    script = "; ".join(parts)
    limit = timeout_seconds if timeout_seconds is not None else max(10, config.DEPLOY_TIMEOUT_SECONDS - 10)
    return f"timeout {int(limit)} sh -c {q(script)}"


def _markers(out: str) -> dict:
    found = {}
    for line in out.splitlines():
        m = re.match(r"@@(before|after) ([0-9a-f]{40})\s*$", line)
        if m and m.group(1) not in found:
            found[m.group(1)] = m.group(2)
    return found


def _ssh(argv: list[str], timeout: int) -> subprocess.CompletedProcess:
    """The one place the executor starts ssh (the tests replace it; they never reach a host)."""
    return server_access._run(argv, timeout)


def cert_minutes(timeout_seconds: int | None = None) -> int:
    return max(1, math.ceil((timeout_seconds or config.DEPLOY_TIMEOUT_SECONDS) / 60) + 1)


def _run_deploy(sj: dict) -> dict:
    """Do one approved deploy: sign, connect, run. Returns {ok, flagged, head_before, head_after, output, error}."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT * FROM deploy_targets WHERE id = %s", (sj["target_id"],))
        tgt = cur.fetchone()
    if not tgt or not tgt["enabled"]:
        return {"ok": False, "error": "the deploy target was removed or disabled"}
    account = repo.get_project(sj["account_id"])
    row = server_access.get(sj["account_id"], tgt["server_id"])
    if row["status"] != "commissioned" or not row["host_key"]:
        return {"ok": False, "error": f"server {row['name']} is not commissioned (no pinned host key)"}
    if row.get("via_server_id"):
        return {"ok": False, "error": JUMP_MSG}
    if not server_access.is_ready(row):       # #810: Needs setup / Not answering refuse, with the reason on the request
        return {"ok": False, "error": server_access.state_reason(row)}
    limit = target_timeout(tgt)       # #829: a target may allow longer than the instance default
    command = remote_script(tgt["repo_path"], tgt["branch"], sj["sha"], tgt["post_cmd"], max(10, limit - 10))
    try:
        with ssh_ca.workdir() as d:
            key, kh = os.path.join(d, "key"), os.path.join(d, "known_hosts")
            ssh_ca.keygen(["-q", "-t", "ed25519", "-N", "", "-C", f"dashboard-deploy-{sj['id']}", "-f", key])
            signed = ssh_ca.sign(account, row["id"], Path(key + ".pub").read_text(encoding="utf-8"), f"deploy:{sj['id']}",
                                 minutes=cert_minutes(limit), force_command=command, pty=False, port_forwarding=False)
            ssh_ca.write_private(key + "-cert.pub", signed["cert"] + "\n")
            ssh_ca.write_private(kh, server_access.known_hosts(row) + "\n")
            argv = server_access.ssh_argv(row, key, key + "-cert.pub", kh, True, command)
            try:
                res = _ssh(argv, limit)
            except subprocess.TimeoutExpired as exc:
                partial = exc.stdout if exc.stdout is not None else exc.output
                if isinstance(partial, bytes):
                    partial = partial.decode("utf-8", "replace")
                return {"ok": False, "orphan_lock": True, "output": partial or "",
                        "error": f"no result within {limit} s; the box may still be running it: "
                                 "check it, then clear the lock on the Machines page"}
    except (ssh_ca.CaUnavailable, RuntimeError, OSError, BadRequest) as exc:
        return {"ok": False, "error": f"could not start the deploy: {str(exc)[:200]}"}
    if res.returncode == 255 and "@@before" not in (res.stdout or "") and "Permission denied (publickey" in (res.stderr or ""):
        # ssh itself was refused (exit 255, and the remote script never ran: it prints @@before first). The paste line is gone from
        # authorized_keys (a deploy is the only login now that probes don't log in): Needs setup. The SAME words from the script's own
        # `git fetch` (a deploy key GitHub refused) are a failed deploy, never a server needing setup.
        why = server_access._failure(row, res, True)
        server_access.mark_needs_setup(row["id"], why)
        return {"ok": False, "error": why, "output": res.stderr or ""}
    out = (res.stdout or "") + (("\n" + res.stderr) if res.stderr else "")
    marks = _markers(res.stdout or "")
    result = {"head_before": marks.get("before", ""), "head_after": marks.get("after", ""), "output": out}
    if res.returncode == 0 and result["head_after"] == sj["sha"]:
        return {**result, "ok": True}
    flagged = bool(result["head_after"]) and result["head_after"] != sj["sha"]
    if res.returncode == EXIT_NOT_ON_BRANCH and not result["head_after"]:
        return {**result, "ok": False, "flagged": False,
                "error": f"{sj['sha'][:8]} is not on {tgt['branch']} yet (merge it first)"}
    if flagged:
        why = f"HEAD after the deploy is {result['head_after'][:12]}, not the approved {sj['sha'][:12]}"
    elif res.returncode == 0:
        why = "the server did not report its HEAD after the pull"
        flagged = True
    else:
        why = _EXIT_TEXT.get(res.returncode) or f"the command exited {res.returncode}"
    return {**result, "ok": False, "flagged": flagged, "error": why}


class Executor:
    """Runs approved deploys, one at a time per call. A request whose target lock is held stays `approved`."""

    def claim(self, sj_id: int) -> dict | None:
        """approved -> running, with the target's deploy lock, in ONE transaction. None = not claimable (state moved on, the
        lock is held or orphaned, or the target is gone: the last is failed here)."""
        with get_cursor() as cur:
            cur.execute("SELECT * FROM server_jobs WHERE id = %s AND state = 'approved' FOR UPDATE SKIP LOCKED", (sj_id,))
            sj = cur.fetchone()
            if not sj:
                return None
            if sj["target_id"] is None:
                self._end(cur, sj, "failed", error="the deploy target was removed")
                return None
            cur.execute("INSERT INTO locks (account_id, kind, resource, job_id, fence, server_job_id) "
                        "VALUES (%s, 'deploy', %s, NULL, 0, %s) ON CONFLICT DO NOTHING RETURNING 1",
                        (sj["account_id"], lock_resource(sj["target_id"]), sj_id))
            if not cur.fetchone():
                return None   # waits: another deploy (or an orphaned lock a person must clear) holds the target
            cur.execute("UPDATE server_jobs SET state = 'running', started_at = now(), owner = %s WHERE id = %s RETURNING *",
                        (_INSTANCE, sj_id))
            sj = cur.fetchone()
            repo.record_activity(cur, sj["account_id"], "dashboard:deploy", "started", "server_job", sj_id,
                                 f"Deploying {sj['sha'][:12]} to {sj['target_name']}")
            return dict(sj)

    def _end(self, cur, sj: dict, state: str, **fields) -> None:
        cur.execute("UPDATE server_jobs SET state = %s, finished_at = now(), head_before = %s, head_after = %s, "
                    "output_tail = %s, error = %s, flagged = %s WHERE id = %s",
                    (state, fields.get("head_before", ""), fields.get("head_after", ""), tail(fields.get("output", "")),
                     (fields.get("error") or "")[:500], bool(fields.get("flagged")), sj["id"]))
        if fields.get("orphan_lock"):   # the box may still be running it: a person checks, then clears the lock
            cur.execute("UPDATE locks SET state = 'orphaned' WHERE server_job_id = %s", (sj["id"],))
        else:
            cur.execute("DELETE FROM locks WHERE server_job_id = %s AND state = 'held'", (sj["id"],))
        repo.record_activity(cur, sj["account_id"], "dashboard:deploy", state, "server_job", sj["id"],
                             f"Deploy of {sj['sha'][:12]} to {sj['target_name']} {state}"
                             + (f": {fields['error']}" if fields.get("error") else ""))

    def finish(self, sj: dict, result: dict) -> None:
        state = "done" if result["ok"] else "failed"
        with get_cursor() as cur:
            cur.execute("SELECT state FROM server_jobs WHERE id = %s FOR UPDATE", (sj["id"],))
            if cur.fetchone()["state"] != "running":   # recovered as orphaned meanwhile: leave that verdict alone
                return
            self._end(cur, sj, state, **result)
        if state == "done" and sj.get("target_id"):       # #726: the server's manifest follows what is now deployed
            from app import manifest
            manifest.regenerate_for_target(sj["target_id"], "deploy")
        self._comment(sj, state, result)

    def _comment(self, sj: dict, state: str, result: dict) -> None:
        if not sj.get("task_id"):
            return
        body = f"Deploy of {sj['sha'][:12]} to {sj['target_name']}: {state}."
        if result.get("error"):
            body += f" {result['error']}."
        if result.get("flagged"):
            body += " FLAGGED: HEAD did not match the approved commit."
        if result.get("head_before") or result.get("head_after"):
            body += f" HEAD {result.get('head_before', '')[:12] or '?'} -> {result.get('head_after', '')[:12] or '?'}."
        try:
            repo.comment_task(sj["task_id"], body, f"deploy:{sj['id']}")
        except Exception:  # noqa: BLE001 — a missing task never loses the record
            log.exception("deploy comment")

    def run_one(self, sj_id: int) -> str | None:
        """Claim and run one approved request. Returns its final state, or None when it could not be claimed (waiting)."""
        sj = self.claim(sj_id)
        if sj is None:
            return None
        try:
            result = _run_deploy(sj)
        except Exception as exc:  # noqa: BLE001 — never leave a lock held on a bug
            log.exception("deploy %s", sj_id)
            result = {"ok": False, "error": f"executor error: {type(exc).__name__}"}
        self.finish(sj, result)
        return "done" if result["ok"] else "failed"

    def run_once(self) -> dict:
        """One pass over the approved requests, oldest first. Returns {ran, waiting} (+ skipped when another process is
        executing). A session advisory lock keeps it to ONE executing process: a second app process does nothing."""
        out = {"ran": 0, "waiting": 0}
        with _exec_lock() as got:
            if not got:
                return {**out, "skipped": True}
            recover()
            with get_cursor(commit=False) as cur:
                cur.execute("SELECT id FROM server_jobs WHERE state = 'approved' ORDER BY id")
                ids = [r["id"] for r in cur.fetchall()]
            for sj_id in ids:
                if self.run_one(sj_id) is None:
                    out["waiting"] += 1
                else:
                    out["ran"] += 1
        return out


@contextmanager
def _exec_lock():
    """Yield True when this process holds the executor's advisory lock (session level, on its own connection, released on
    exit or if the process dies), else False."""
    conn = connect()
    try:
        got = conn.execute("SELECT pg_try_advisory_lock(%s) AS g", (EXEC_LOCK_KEY,)).fetchone()["g"]
        conn.commit()
        try:
            yield bool(got)
        finally:
            if got:
                conn.execute("SELECT pg_advisory_unlock(%s)", (EXEC_LOCK_KEY,))
                conn.commit()
    finally:
        conn.close()


def recover(startup: bool = False) -> int:
    """Running requests whose process is gone become `orphaned`, and their lock is marked orphaned (a person clears it;
    nothing retries). startup=True (app start): if no other process holds the executor lock, every running request is a
    dead process's and is orphaned; if another process IS executing, its live deploy is left alone. Otherwise only a
    request running longer than a deploy may (timeout + a minute) is orphaned, whichever process owns it. Returns how many."""
    n = 0
    with _exec_lock() as free:
        dead = startup and free
        with get_cursor() as cur:
            cur.execute("SELECT * FROM server_jobs WHERE state = 'running' ORDER BY id FOR UPDATE SKIP LOCKED")
            for sj in cur.fetchall():
                cur.execute("SELECT timeout_seconds FROM deploy_targets WHERE id = %s", (sj["target_id"],))
                tgt = cur.fetchone() or {}
                cur.execute("SELECT (now() - %s) > make_interval(secs => %s) AS late",
                            (sj["started_at"], target_timeout(tgt) + 60))
                late = cur.fetchone()["late"]
                if not (dead or late):
                    continue
                why = ("the dashboard restarted while this deploy was running: it may have half-run. Check the box, clear "
                       "the lock on the Machines page, and ask again" if dead
                       else "the deploy ran longer than allowed and its result was lost: check the box")
                cur.execute("UPDATE server_jobs SET state = 'orphaned', finished_at = now(), error = %s WHERE id = %s",
                            (why[:500], sj["id"]))
                cur.execute("UPDATE locks SET state = 'orphaned' WHERE server_job_id = %s", (sj["id"],))
                repo.record_activity(cur, sj["account_id"], "dashboard:deploy", "orphaned", "server_job", sj["id"],
                                     f"Deploy of {sj['sha'][:12]} to {sj['target_name']} orphaned: {why}")
                log.warning("server_job %s orphaned: %s", sj["id"], why)
                n += 1
    return n
