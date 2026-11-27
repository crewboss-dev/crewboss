"""Bearer-token auth for /api writes and /mcp.

A SESSION token is bound to (project, stream) — or (project, 'all'), one session handling every stream of its
project — and may ONLY read or write its own project (app/scope.py enforces it; another project's ids are a
404, never a 403, so their existence doesn't leak). A ROLE token (scanner, zabbix) is project-less and may only do
its one machine job. The 'satellite' and 'reporter' roles are bound to ONE project: a satellite may only read that
project's client snapshot and post client requests; a reporter may only POST that project's health reports (write-only,
task #404). Only a hash is ever stored — see app/tokens.py."""
from __future__ import annotations

import hashlib
import secrets

from fastapi import Header, HTTPException

from app import roles
from app.config import (AGENTS, CATALOG_IMPORT_KINDS, DEFAULT_AGENT, PROJECT_TOKEN_KINDS, RUNNER_KINDS, SESSION_KINDS,
                        TOKEN_KINDS)
from app.db import get_cursor


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# Wire prefixes. New tokens carry the Crewboss prefixes; the pre-rename ones (task #589) are STILL ACCEPTED, because live
# sessions, runners and scripts hold them. A session token is recognised by its hash, never by its prefix, so the old
# session prefix needs no code here at all; it is listed for the places that look at the shape (redaction, the launcher).
# TODO(#589 compat): drop the LEGACY_* tuples, the `mdd_` accepts elsewhere (sessions/crewboss-sessions.sh, app/deploys.py
# redaction) and tests/test_wire_compat.py once every session, runner and script was re-issued a Crewboss-prefixed token.
SESSION_PREFIX = "cb_"
LEGACY_SESSION_PREFIXES = ("mdd_",)


def new_token() -> str:
    return SESSION_PREFIX + secrets.token_urlsafe(32)


def issue_token(stream: str, name: str, project_id: int | None = None, agent: str = DEFAULT_AGENT,
                sources: list[str] | None = None) -> str:
    """`stream` is the token kind: a session stream, 'all' (every stream of one project), or a role such as
    'scanner'. A session token without project_id belongs to the legacy_token role's project; a 'satellite' token
    must name its project. `agent` is WHICH AI holds the token (attribution only; default 'claude') — it sets
    the actor the token's writes are recorded as ('<agent>:<stream>') and never changes the token's reach. `sources`
    (a 'reporter' token only, task #404) limits which report sources it may post for; None = any."""
    if stream in RUNNER_KINDS:
        raise ValueError(f"a {stream!r} token is a runner token: it is made by enrolment (app/devices.py), never issued here")
    if stream not in TOKEN_KINDS:
        raise ValueError(f"unknown token kind {stream!r}, must be one of {TOKEN_KINDS}")
    if agent not in AGENTS:
        raise ValueError(f"unknown agent {agent!r}, must be one of {AGENTS}")
    if stream not in PROJECT_TOKEN_KINDS:
        project_id = None  # machine role tokens are never project-bound
    elif stream not in SESSION_KINDS and project_id is None:
        raise ValueError(f"a {stream!r} token must be bound to a project")
    if sources and stream != "reporter":
        raise ValueError("only a 'reporter' token is limited to sources")
    token = new_token()
    with get_cursor() as cur:
        cur.execute(
            "INSERT INTO session_tokens (stream, name, token_hash, project_id, agent, sources) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (stream, name, hash_token(token), project_id, agent, list(sources) if sources else None),
        )
    return token


DEVICE_PREFIX = "cbr_"   # a runner device token (task #627, app/devices.py)
JOB_PREFIX = "cbj_"      # a job token (task #627, app/jobs.py): minted at claim, scoped to that ONE job
CA_PREFIX = "cbt_"       # a CA turn token (task #702, app/ca.py): minted at claim, scoped to that ONE chat turn of ONE user
LEGACY_DEVICE_PREFIXES = ("mdr_",)   # TODO(#589 compat): enrolled runners still hold these
LEGACY_JOB_PREFIXES = ("mdj_",)      # TODO(#589 compat): a job token lives only as long as its job; drop with the rest
DEVICE_PREFIXES = (DEVICE_PREFIX, *LEGACY_DEVICE_PREFIXES)
JOB_PREFIXES = (JOB_PREFIX, *LEGACY_JOB_PREFIXES)
RUNNER_PREFIXES = DEVICE_PREFIXES + JOB_PREFIXES + (CA_PREFIX,)


def _resolve_device(token: str) -> dict | None:
    """The principal for a live device token (hash match, not revoked, not expired, its user not disabled), else
    None. stream='device' is in RUNNER_KINDS, so no session or MCP surface accepts it; it carries the user and the ONE
    account it was enrolled for (user_id, account_id, device_id) and no project."""
    with get_cursor() as cur:
        cur.execute(
            "SELECT d.id, d.user_id, d.account_id, d.name, d.mode FROM device_tokens d JOIN users u ON u.id = d.user_id "
            "WHERE d.token_hash = %s AND d.revoked_at IS NULL AND d.expires_at > now() AND u.disabled_at IS NULL",
            (hash_token(token),))
        row = cur.fetchone()
        if not row:
            return None
        cur.execute("UPDATE device_tokens SET last_seen = now() WHERE id = %s", (row["id"],))
    return {"id": row["id"], "stream": "device", "name": row["name"], "project_id": None, "project": None,
            "agent": DEFAULT_AGENT, "sources": None, "user_id": row["user_id"], "account_id": row["account_id"],
            "device_id": row["id"], "mode": row["mode"]}


def _resolve_job(token: str) -> dict | None:
    """The principal for a job token: its hash matches a RUNNING job whose device is still live (not revoked, not expired)
    and whose user is not disabled. A token is cleared when its job finishes, is requeued or is revoked with its device, so
    an old token resolves to nothing. stream='job' is in RUNNER_KINDS: no session or MCP surface accepts it. It carries the
    ONE job."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT j.id, j.account_id, j.user_id, j.device_id, j.fence FROM jobs j "
                    "JOIN device_tokens d ON d.id = j.device_id JOIN users u ON u.id = j.user_id "
                    "WHERE j.token_hash = %s AND j.state = 'running' AND d.revoked_at IS NULL AND d.expires_at > now() "
                    "AND u.disabled_at IS NULL", (hash_token(token),))
        row = cur.fetchone()
    if not row:
        return None
    return {"id": row["id"], "stream": "job", "name": f"job-{row['id']}", "project_id": None, "project": None,
            "agent": DEFAULT_AGENT, "sources": None, "job_id": row["id"], "account_id": row["account_id"],
            "user_id": row["user_id"], "device_id": row["device_id"], "fence": row["fence"]}


def _resolve_turn(token: str) -> dict | None:
    """The principal for a CA turn token: its hash matches a RUNNING turn whose device is still live and whose user is not
    disabled. The token is cleared when the turn ends, is cancelled or fails, so an old one resolves to nothing. stream='ca' is
    in RUNNER_KINDS: no session or MCP surface but /runner/ca-mcp accepts it. It carries the ONE turn, its user and account."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT t.id, t.conversation_id, t.user_id, t.account_id, t.device_id, t.fence FROM ca_turns t "
                    "JOIN device_tokens d ON d.id = t.device_id JOIN users u ON u.id = t.user_id "
                    "WHERE t.token_hash = %s AND t.state = 'running' AND d.revoked_at IS NULL AND d.expires_at > now() "
                    "AND u.disabled_at IS NULL", (hash_token(token),))
        row = cur.fetchone()
    if not row:
        return None
    return {"id": row["id"], "stream": "ca", "name": f"ca-turn-{row['id']}", "project_id": None, "project": None,
            "agent": DEFAULT_AGENT, "sources": None, "turn_id": row["id"], "conversation_id": row["conversation_id"],
            "account_id": row["account_id"], "user_id": row["user_id"], "device_id": row["device_id"], "fence": row["fence"]}


def resolve_token(token: str) -> dict | None:
    """{id, stream, name, project_id, agent, project} for a valid token (touching last_used_at), else None. For
    a session token `project` is its project row (a legacy token's is LEGACY_TOKEN_PROJECT); a session token
    whose project can't be found resolves to None (fail closed). `agent` is which AI holds it (default 'claude').
    A runner token is told apart by its prefix: cbr_ (or the old mdr_) resolves against device_tokens only (stream
    'device'); cbj_ (or the old mdj_) resolves against jobs only (stream 'job', scoped to that one job)."""
    if token.startswith(DEVICE_PREFIXES):
        return _resolve_device(token)
    if token.startswith(JOB_PREFIXES):
        return _resolve_job(token)
    if token.startswith(CA_PREFIX):
        return _resolve_turn(token)
    with get_cursor() as cur:
        cur.execute("SELECT id, stream, name, project_id, agent, sources FROM session_tokens WHERE token_hash = %s",
                    (hash_token(token),))
        row = cur.fetchone()
        if not row:
            return None
        cur.execute("UPDATE session_tokens SET last_used_at = now() WHERE id = %s", (row["id"],))
        row = dict(row)
        row["agent"] = row.get("agent") or DEFAULT_AGENT
        row["project"] = None
        if row["stream"] in PROJECT_TOKEN_KINDS:
            if row["project_id"] is not None:
                cur.execute("SELECT * FROM projects WHERE id = %s", (row["project_id"],))
            elif row["stream"] in SESSION_KINDS:
                cur.execute("SELECT * FROM projects WHERE slug = %s", (roles.slug("legacy_token"),))
            else:
                return None  # a project-bound role with no project: fail closed
            project = cur.fetchone()
            if not project:
                return None
            if row["agent"] == "claude-desktop" and project.get("archived_at"):
                return None  # an external agent's token is dead once its project is archived (#574)
            if project.get("owner_user_id"):       # a disabled person's sessions stop with them (#713)
                cur.execute("SELECT disabled_at FROM users WHERE id = %s", (project["owner_user_id"],))
                owner = cur.fetchone()
                if owner is None or owner["disabled_at"]:
                    return None
            row["project"] = project
            row["project_id"] = project["id"]
    return row


def bind_audit_user(row: dict) -> dict:
    """Name the person a token acts for in this request's audit trail (task #713): a session token acts for the owner of its
    project, a runner (device or job) token for the user it was enrolled by. Returns the row."""
    from app import access
    if row.get("stream") in RUNNER_KINDS:
        access.set_current_user(row.get("user_id"))
    elif row.get("project") is not None:
        access.set_current_user(access.of_session(row).user_id)
    return row


def resolve_session_token(token: str) -> dict | None:
    """Like resolve_token, but only a SESSION token counts — a role token (scanner) is None here, so it can
    never act as a Claude session (REST writes, MCP tools)."""
    row = resolve_token(token)
    return row if row and row["stream"] in SESSION_KINDS else None


MISSING_TOKEN = ("missing Authorization: Bearer <token>. Every /api call needs a token (task #368); "
                 "the owner uses the dashboard pages, not /api")


def _bearer_row(authorization: str | None) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail=MISSING_TOKEN)
    row = resolve_token(authorization.split(" ", 1)[1].strip())
    if not row:
        raise HTTPException(status_code=401, detail="invalid or revoked token")
    return row


async def require_session(authorization: str | None = Header(default=None)) -> dict:
    """FastAPI dependency: require a valid Bearer SESSION token. Returns the session row (with its project and
    its agent). The caller's actor string for activity/audit purposes is actor_for(session) = f"{agent}:{stream}"."""
    row = _bearer_row(authorization)
    if row["stream"] in RUNNER_KINDS or row["stream"] not in SESSION_KINDS:
        raise HTTPException(status_code=403, detail=f"a {row['stream']!r} token can't do this")
    return bind_audit_user(row)


async def require_device(authorization: str | None = Header(default=None)) -> dict:
    """FastAPI dependency for the runner's own endpoints (claim, heartbeat): a live DEVICE token (cbr_, or the old mdr_) and nothing else."""
    row = _bearer_row(authorization)
    if row["stream"] != "device":
        raise HTTPException(status_code=403, detail=f"a {row['stream']!r} token can't do this")
    return bind_audit_user(row)


# There is no optional_session any more (task #368): "no Authorization = the owner" let any caller on the gated
# network read every account's data through /api. Every /api read now takes require_session like the writes; the
# owner's HTML pages never call /api.


async def require_catalog_importer(authorization: str | None = Header(default=None)) -> dict:
    """FastAPI dependency for POST /api/catalog/import: the scanner role, or the features session (its own
    project only — see api_import_catalog)."""
    row = _bearer_row(authorization)
    if row["stream"] not in CATALOG_IMPORT_KINDS:
        raise HTTPException(status_code=403,
                            detail=f"catalog import needs a {' or '.join(CATALOG_IMPORT_KINDS)} token")
    return row


async def require_zabbix_poke(authorization: str | None = Header(default=None)) -> dict:
    """FastAPI dependency for POST /api/health/zabbix/poke: ONLY the 'zabbix' role token (media type 49)."""
    row = _bearer_row(authorization)
    if row["stream"] != "zabbix":
        raise HTTPException(status_code=403, detail="the Zabbix poke needs a 'zabbix' token")
    return row


def actor_for(session: dict) -> str:
    """The actor a session token's writes are recorded as: '<agent>:<stream>' (default agent 'claude'), so the
    dashboard attributes each write to the AI that made it."""
    return f"{session.get('agent', DEFAULT_AGENT)}:{session['stream']}"


async def require_satellite(authorization: str | None = Header(default=None)) -> dict:
    """FastAPI dependency for /api/satellite/*: ONLY a 'satellite' token, bound to one project (its `project`
    row). Everything the satellite reads or writes is that project's, never one named in the request."""
    row = _bearer_row(authorization)
    if row["stream"] != "satellite" or not row.get("project"):
        raise HTTPException(status_code=403, detail="this needs a 'satellite' token")
    return row


async def require_digest(authorization: str | None = Header(default=None)) -> dict:
    """FastAPI dependency for GET /api/summary (task #828): ONLY a 'digest' role token (project-less, read-only). A session
    token or any other role is refused with a 403, as everywhere else."""
    row = _bearer_row(authorization)
    if row["stream"] != "digest":
        raise HTTPException(status_code=403, detail="the summary needs a 'digest' token")
    return row


async def require_manifest_server(server_id: int, authorization: str | None = Header(default=None)) -> int:
    """FastAPI dependency for GET /api/servers/{server_id}/manifest (#726): ONLY a per-server manifest token (cbm_, read-only, hashed at
    rest). No token = 401; a token of ANOTHER server = the same 404 a missing server gets. Session and role tokens never work here."""
    from app import manifest
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail=MISSING_TOKEN)
    owns = manifest.server_for_token(authorization.split(" ", 1)[1].strip())
    if owns is None:
        raise HTTPException(status_code=401, detail="invalid or revoked token")
    if owns != server_id:
        raise HTTPException(status_code=404, detail="no such server")
    return owns


async def require_reporter(authorization: str | None = Header(default=None)) -> dict:
    """FastAPI dependency for POST /api/health/report (task #404): ONLY a 'reporter' token, bound to one project (its
    `project` row) and optionally to named sources (`sources`). Write-only: no other route accepts it."""
    row = _bearer_row(authorization)
    if row["stream"] != "reporter" or not row.get("project"):
        raise HTTPException(status_code=403, detail="this needs a 'reporter' token")
    return row
