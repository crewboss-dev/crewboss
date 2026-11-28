"""Environment configuration. No production credentials live here — this app never touches
prod databases or client systems."""
from __future__ import annotations

import os
import re

from app import front_door, instance

# Where a value below carries a fallback from `instance` (app/instance.py), instance.yaml supplies it: the environment
# variable still wins, and with no instance.yaml the fallback is a neutral generic default.

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://dashboard:dashboard@db:5432/dashboard"
)
APP_HOST = os.environ.get("APP_HOST", "0.0.0.0")
APP_PORT = int(os.environ.get("APP_PORT", "8000"))

# Streams a Claude session can own. "reviewer" (not "reviews") is deliberate: it names the role.
# "errors" is the standing session for production failures pushed from System Health (phase 3).
STREAMS = ("features", "bugs", "tweaks", "reviewer", "errors")

# Token kinds = the session streams above + NON-session roles. A role token authenticates exactly one
# machine job and nothing else: "scanner" (scanner/auto_rescan.py) may ONLY POST /api/catalog/import —
# it is not a Claude session, can't write tasks/knowledge/outbox, and has no MCP access.
# "zabbix" (the Zabbix poke webhook, media type 49) may ONLY POST /api/health/zabbix/poke.
# "satellite" (a client's mini dashboard, task #106) is bound to a PROJECT: it may ONLY read that project's client
# snapshot and post client requests (/api/satellite/*). "reporter" (task #404) is bound to a PROJECT too and is
# write-only: it may ONLY POST that project's app health report (/api/health/report) — no reads, nothing else.
# "digest" (task #828) is project-less and READ-ONLY: it may ONLY GET /api/summary (counts + titles for an external digest).
ROLE_KINDS = ("scanner", "zabbix", "satellite", "reporter", "digest")
PROJECT_BOUND_ROLES = ("satellite", "reporter")
# Which AI holds a session token (task #167). Attribution ONLY — it never changes a token's reach or isolation
# (that is project_id + stream). A write by a token is recorded as actor "<agent>:<stream>", so Claude Code and
# the Gemini CLI can both use the dashboard and each AI's work is honestly attributed. Legacy tokens are Claude's.
AGENTS = ("claude", "gemini", "claude-desktop")
DEFAULT_AGENT = "claude"
# A session token is bound to (project, stream) — or (project, "all"): ONE session that handles every stream
# of its project. "all" is a session kind, not a stream: nothing is ever filed under it.
ALL_STREAMS_ROLE = "all"
SESSION_KINDS = STREAMS + (ALL_STREAMS_ROLE,)
TOKEN_KINDS = SESSION_KINDS + ROLE_KINDS
# Runner principals (task #627, change #50): a device (a runner enrolled on a user's PC, token mdr_...) and a job (a token
# for one job, mdj_..., P2). They are NOT session or role tokens: kept out of SESSION_KINDS and TOKEN_KINDS on purpose, so
# no session/MCP surface and no token issuing can ever take one.
RUNNER_KINDS = ("device", "job", "ca")
# Kinds whose token carries a project: every session kind plus the project-bound roles.
PROJECT_TOKEN_KINDS = SESSION_KINDS + PROJECT_BOUND_ROLES
# Project CONNECTIONS this install seeds (#784): instance.yaml `connections:`, a list of {project, kind, other, note?} by slug. Inserted at start
# when missing (app/connections.py seed_from_config), never edited or deleted. A malformed list is a loud error.
def _connections(raw) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("instance.yaml: 'connections' must be a list of {project, kind, other, note} mappings")
    kinds = ("part_of", "depends_on", "integrates_with")
    out = []
    for item in raw:
        if not isinstance(item, dict) or not all(isinstance(item.get(k), str) and item[k].strip() for k in ("project", "kind", "other")):
            raise ValueError(f"instance.yaml: each connection needs string project, kind and other (got {item!r})")
        if item["kind"] not in kinds:
            raise ValueError(f"instance.yaml: connection kind must be one of {kinds}, not {item['kind']!r}")
        note = item.get("note") or ""
        if not isinstance(note, str):
            raise ValueError(f"instance.yaml: a connection note must be a string (got {note!r})")
        out.append({"project": item["project"].strip(), "kind": item["kind"], "other": item["other"].strip(), "note": note})
    return out


CONNECTIONS = _connections(instance.get("connections"))


# Extra MCP servers a runner job gets beside crewboss-job (#829): instance.yaml `job_mcp_servers:`, a list of {name, url, projects?}
# (projects = project slugs it applies to; omitted = every project). Off unless set: Crewboss is open source, so no instance's server
# (a knowledge-base MCP) is built in. The URL is sent to the runner in the claim; the runner writes it into the job's --strict-mcp-config.
def _job_mcp_servers(raw) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("instance.yaml: 'job_mcp_servers' must be a list of {name, url, projects} mappings")
    out = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not isinstance(item.get("url"), str):
            raise ValueError(f"instance.yaml: each job_mcp_servers entry needs a string name and url (got {item!r})")
        name, url, projects = item["name"].strip(), item["url"].strip(), item.get("projects") or []
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,30}", name) or name == "crewboss-job":
            raise ValueError(f"instance.yaml: job_mcp_servers name {name!r}: lower-case letters, digits, - _ (not crewboss-job)")
        if not re.fullmatch(r"https?://\S{1,300}", url):
            raise ValueError(f"instance.yaml: job_mcp_servers url for {name!r} must be http(s)://...")
        if not isinstance(projects, list) or not all(isinstance(x, str) for x in projects):
            raise ValueError(f"instance.yaml: job_mcp_servers projects for {name!r} must be a list of project slugs")
        out.append({"name": name, "url": url, "projects": [x.strip() for x in projects]})
    return out


JOB_MCP_SERVERS = _job_mcp_servers(instance.get("job_mcp_servers"))

# The project a session token with no project_id belongs to: the tokens issued before per-project sessions
# (the flat ~/.config/crewboss/tokens/<stream> files) belong to the `roles.legacy_token` project. Overridable for tests.
LEGACY_TOKEN_PROJECT = os.environ.get("CREWBOSS_LEGACY_TOKEN_PROJECT", instance.get_str("roles.legacy_token"))
# The OVERSEER (owner, 2026-10-01, task #129): this project's 'all' session (dash) may READ every active project
# (tasks, questions, health, knowledge, workflows) to plan across them. Its WRITES stay its own project's, except
# the global plan (its own table) and triage suggestions (Accept / Keep mine) on other projects' tasks. Every other
# session stays strictly single-project. Empty = no overseer.
OVERSEER_PROJECT = os.environ.get("CREWBOSS_OVERSEER_PROJECT", instance.get_str("roles.overseer"))
# The actor the overseer's cross-project triage suggestions are recorded as (shown as "dash (overseer)").
OVERSEER_ACTOR = "claude:overseer"
# Re-plan cooldown (minutes): no new plan_stale nudge within this long of a plan run or a delivered nudge; changes
# in the cooldown become ONE follow-up nudge after it. 0 = no cooldown.
# Drift guard for the shared chrome: a folder holding an upstream design system's `chrome.css` and `theme-tokens.css`, which
# this install tracks verbatim (tests/test_chrome_drift.py). Unset (the default) = the guard skips.
CHROME_UPSTREAM_PATH = os.environ.get("CHROME_UPSTREAM_PATH", instance.get_str("chrome_upstream_path"))
PLAN_STALE_COOLDOWN_MINUTES = int(os.environ.get("CREWBOSS_PLAN_STALE_COOLDOWN_MINUTES", "10"))
# The slug of the account the session launcher's boot fallback names (sessions.env CREWBOSS_FALLBACK_SESSION, first word): it is
# pinned like a role slug, so a rename can't cut the wiring. instance.yaml `fallback_session_slug`; empty = none.
FALLBACK_SESSION_SLUG = os.environ.get("CREWBOSS_FALLBACK_SESSION_SLUG", instance.get_str("fallback_session_slug"))
# A prefix dropped from a project's slug when its short name (the tmux session name) is derived, e.g. "acme-" so acme-erp -> erp.
# instance.yaml `slug_prefix`; empty = keep the slug as it is.
SLUG_PREFIX = os.environ.get("CREWBOSS_SLUG_PREFIX", instance.get_str("slug_prefix"))
# Zabbix item keys for the server readings that come from this install's own agent template (app/servers.py): a map of
# reboot / sec_updates / updates to a key. instance.yaml `server_health_keys`; empty = those readings are not shown.
SERVER_HEALTH_KEYS = instance.get_map("server_health_keys")
# The project a no-token read (the owner, the network) defaults to when it names none.
DEFAULT_PROJECT = instance.get_str("roles.default")
# The Crewboss repo a new runner PC clones to install the runner (runners page how-to, #806). HTTPS so a fresh Windows PC can
# sign in through Git Credential Manager without an SSH key. instance.yaml `runner_repo`; env CREWBOSS_RUNNER_REPO wins.
RUNNER_REPO = os.environ.get("CREWBOSS_RUNNER_REPO", instance.get_str("runner_repo", "https://github.com/crewboss-dev/crewboss.git"))
# A suggested runner PROFILE name for the how-to ("crewboss-runner enrol <code> --name <profile>", #975): this instance's `name` as a slug
# (a-z, 0-9, -; at most 32), or "work" when it has none.
import re as _re
PROFILE_HINT = _re.sub(r"[^a-z0-9]+", "-", (instance.get_str("name", "") or "").lower()).strip("-")[:32].strip("-") or "work"
# While that repo is private, the how-to adds a "sign git in to GitHub" step (gh auth login). instance.yaml `runner_repo_private: true`
# or env CREWBOSS_RUNNER_REPO_PRIVATE=1; off by default, so a public repo shows no sign-in step.
RUNNER_REPO_PRIVATE = (os.environ["CREWBOSS_RUNNER_REPO_PRIVATE"].strip().lower() in ("1", "true", "yes", "on")
                       if "CREWBOSS_RUNNER_REPO_PRIVATE" in os.environ else instance.get_bool("runner_repo_private"))
# Who may import a catalog scan over HTTP: the scanner role, or the features session (whose stream
# owns the workflow catalog) for a manual push.
CATALOG_IMPORT_KINDS = ("scanner", "features")

# task.stream -> the outbox/token "stream" short name; tasks.owner_stream holds the same short name
TASK_STREAM_TO_SHORT = {
    "feature": "features",
    "bug": "bugs",
    "tweak": "tweaks",
    "review": "reviewer",
    "error": "errors",
}

# Presentation metadata for stream chips, shared by every template (colour paired
# with a word, never colour alone).
STREAM_META = {
    "feature": {"label": "Feature", "classes": "bg-indigo/10 text-indigo"},
    "bug": {"label": "Bug", "classes": "bg-status-high/15 text-status-high"},
    "tweak": {"label": "Tweak", "classes": "bg-status-warn/15 text-charcoal dark:text-status-warn"},
    "review": {"label": "Review", "classes": "bg-status-ok/15 text-status-ok"},
    "error": {"label": "Error", "classes": "bg-status-disaster/15 text-status-disaster font-semibold"},
}
# The status flow (backlog -> ready -> doing -> review -> done). One colour per meaning, everywhere:
# neutral = not started, indigo = being worked, flame = needs YOU (review), green = done.
STATUS_META = {
    "backlog": {"label": "Backlog", "classes": "bg-charcoal/[.07] text-charcoal/70"},
    "ready": {"label": "Ready", "classes": "bg-charcoal/10 text-charcoal"},
    "doing": {"label": "In progress", "classes": "bg-indigo/10 text-indigo"},
    "review": {"label": "Review", "classes": "bg-flame/10 text-flame"},
    "done": {"label": "Done", "classes": "bg-status-ok/15 text-status-ok"},
}
STATUS_ORDER = ("backlog", "ready", "doing", "review", "done")
# What a request IS — decided by the AI's triage, confirmed by the owner (tasks.category). The owner's
# taxonomy: a Workflow is orchestration (the catalog); an AI Skill is a reusable AI capability built in
# code (Classify email, Extract invoice, Draft reply); a Rule is a deterministic WHEN/IF -> THEN policy at
# a step; an AI Instruction is a plain-language policy tuning a Skill's judgement. Rule / AI Skill /
# AI Instruction reuse the canvas's logic / AI kind accents so a chip and the step it lands on read alike.
_NEUTRAL = "bg-charcoal/[.06] text-charcoal/70"
CATEGORY_META = {
    "workflow": {"label": "Workflow", "classes": "bg-kind-trigger/10 text-kind-trigger"},
    "ai_skill": {"label": "AI Skill", "classes": "bg-kind-ai/10 text-kind-ai"},
    "rule": {"label": "Rule", "classes": "bg-kind-logic/15 text-charcoal"},
    "ai_instruction": {"label": "AI Instruction", "classes": "bg-kind-ai/10 text-charcoal"},
    "feature": {"label": "Feature", "classes": _NEUTRAL},
    "fix": {"label": "Fix", "classes": _NEUTRAL},
    "ops": {"label": "Ops", "classes": _NEUTRAL},
    "other": {"label": "Other", "classes": _NEUTRAL},
}
NEEDED_NOW_META = {
    "yes": {"label": "needed now"},
    "later": {"label": "not needed yet"},
    "no": {"label": "not needed"},
}
# MoSCoW as a LEFT STRIPE on every card: one hue (indigo; cream in dark mode) at falling strength, so the
# eye reads importance at a glance, always with its word next to it.
PRIORITY_META = {
    "must": {"label": "Must have", "short": "Must", "stripe": "bg-indigodeep"},
    "should": {"label": "Should have", "short": "Should", "stripe": "bg-indigo/50"},
    "could": {"label": "Could have", "short": "Could", "stripe": "bg-indigo/20"},
    "wont": {"label": "Won't have", "short": "Won't", "stripe": "bg-charcoal/10"},
}

# --- Project environments (task #132) -----------------------------------------------------------------------
# Where a project's dev / prod can live: the servers your install knows about. LATER this list comes from
# the ERP's Guacamole connections (one entry per connection the owner can open), so a client project can pick its
# own servers; until then an owner-maintained list (comma-separated CREWBOSS_KNOWN_SERVERS overrides it).
# Your machine names (never a raw IP), from instance.yaml `servers`. An environment's Zabbix
# host is recorded separately (project_environments.zabbix_host = the name Zabbix and the Health page use).
_DEFAULT_SERVERS = tuple(instance.get_list("servers"))
KNOWN_SERVERS = tuple(s.strip() for s in (os.environ.get("CREWBOSS_KNOWN_SERVERS") or ",".join(_DEFAULT_SERVERS)).split(",")
                      if s.strip())
# ERP_URL: the ERP's base url, used by app/erp_health.py (the menu drawer no longer uses it: see MENU_LINKS). No global Admin link (#577, owner 10-27): each
# project's admin site is reached from that project's own pages (Health, drift), never from the drawer.
_erp_base = (os.environ.get("CREWBOSS_ERP_URL") or instance.get_str("erp_url")).rstrip("/")
ERP_URL = _erp_base + "/" if _erp_base else ""


def parse_menu_links(raw) -> list[dict]:
    """The `menu_links` list of instance.yaml -> [{"label", "url"}] for the menu drawer. A link needs a non-empty label (at most
    40 characters) and an http(s) url with a host; anything else is skipped with a warning. Never raises."""
    import logging
    from urllib.parse import urlsplit
    log = logging.getLogger(__name__)
    if raw is None:
        return []
    if not isinstance(raw, list):
        log.warning("instance.yaml menu_links must be a list; ignoring it")
        return []
    out = []
    for i, item in enumerate(raw):
        label = item.get("label") if isinstance(item, dict) else None
        url = item.get("url") if isinstance(item, dict) else None
        label = label.strip() if isinstance(label, str) else ""
        url = url.strip() if isinstance(url, str) else ""
        try:
            parts = urlsplit(url) if url else None
        except ValueError:
            parts = None
        if not label or len(label) > 40 or not parts or parts.scheme not in ("http", "https") or not parts.netloc:
            log.warning("instance.yaml menu_links[%d] skipped: needs a label (1-40 characters) and an http(s) url", i)
            continue
        out.append({"label": label, "url": url})
    return out


# Extra links in the menu drawer (instance.yaml `menu_links`); none = the drawer shows no such section.
MENU_LINKS = parse_menu_links(instance.get("menu_links"))
PROJECT_KINDS = ("account", "project")
ENVIRONMENTS = ("dev", "prod")

# The dashboard's public base URL, for links handed to a session (e.g. an owner question's task link).
# Precedence (#976): the env var, then the padlocked address the Front door card switched on (front_door.json), then instance.yaml.
PUBLIC_URL = (os.environ.get("CREWBOSS_PUBLIC_URL") or front_door.public_url()
              or instance.get_str("public_url", "http://localhost:8931")).rstrip("/")
# The LAN address people on the local network use ("" when this is not a LAN install). Shown on the Front door card.
LAN_URL = front_door.lan_url()
# Passkeys are an optional upgrade (#976): `auto` offers them where there is a padlock (or on localhost), `off` hides them everywhere.
AUTH_PASSKEYS = (os.environ.get("CREWBOSS_AUTH_PASSKEYS") or instance.get_str("auth_passkeys", "auto")).strip().lower()
if AUTH_PASSKEYS not in ("auto", "off"):
    raise ValueError(f"auth_passkeys must be 'auto' or 'off' (instance.yaml key 'auth_passkeys' or CREWBOSS_AUTH_PASSKEYS), not {AUTH_PASSKEYS!r}")

# --- Server manifest push (#726) ------------------------------------------------------------------------------------
# Optional: after a manifest is regenerated and its content changed, POST it to this URL, signed with HMAC-SHA256 over
# "<timestamp>.<body>" using the secret in the 0600 file. Off unless BOTH are set. No vendor-specific code: any consumer can listen.
MANIFEST_WEBHOOK_URL = os.environ.get("CREWBOSS_MANIFEST_WEBHOOK_URL", "").strip()
MANIFEST_WEBHOOK_SECRET_FILE = os.environ.get("CREWBOSS_MANIFEST_WEBHOOK_SECRET_FILE", "/run/secrets/manifest-webhook-secret")

# --- System Health (phase 3) -------------------------------------------------------------------------
# Production health comes from Zabbix over a READ-ONLY API token (a user whose role allows *.get only, on the host groups
# below). The token lives in a 0600 file mounted into the container, never in the environment or the DB.
# No URL or no token file = health is simply "not connected" (the tests, a fresh checkout). instance.yaml `zabbix_url`.
ZABBIX_API_URL = os.environ.get("CREWBOSS_ZABBIX_URL") or instance.get_str("zabbix_url")
ZABBIX_TOKEN_FILE = os.environ.get("CREWBOSS_ZABBIX_TOKEN_FILE", "/run/secrets/zabbix-api-token")
# Host group ids watched (instance.yaml `zabbix_groupids`). Comma-separated.
ZABBIX_GROUPIDS = tuple(g.strip() for g in os.environ.get("CREWBOSS_ZABBIX_GROUPIDS", ",".join(instance.get_list("zabbix_groupids"))).split(",") if g.strip())
# The project whose Health page shows Zabbix and whose errors session gets new faults.
HEALTH_PROJECT = os.environ.get("CREWBOSS_HEALTH_PROJECT", instance.get_str("roles.health"))
# Background catch-up interval (seconds); the poke makes it near real time, this covers a missed poke. 0 = off.
HEALTH_POLL_SECONDS = int(os.environ.get("CREWBOSS_HEALTH_POLL_SECONDS", "120"))
# New problems at or above this Zabbix severity go to the errors session (3 = Average). Lower ones only show.
HEALTH_ESCALATE_MIN_SEVERITY = int(os.environ.get("CREWBOSS_HEALTH_ESCALATE_MIN_SEVERITY", "3"))

# --- ERP health feed (task #196, change #15) ----------------------------------------------------------
# The ERP keeps its sensors (system_events, ops.record, sync_state); the dashboard reads ONE read-only JSON feed,
# GET <ERP_HEALTH_URL>/api/health (contract: docs/health-report-contract.md), and
# never connects to the ERP's database. ERP_HEALTH_URL is the ERP's BASE url (scheme://host[:port], no path); the
# feed paths are appended. Two bearer tokens, each in its own 0600 file mounted into the container (never in the
# environment, the DB or a log), read at USE time so dropping one in needs no restart:
#   read    (scope health:read)    -> the feed + the AI call log;
#   resolve (scope health:resolve) -> POST /api/health/events/{id}/resolve, only from the Health page's button.
# No URL or no read token = "ERP feed not connected" and the loop does nothing.
ERP_HEALTH_URL = (os.environ.get("CREWBOSS_ERP_HEALTH_URL") or "").strip().rstrip("/")
ERP_HEALTH_READ_TOKEN_FILE = os.environ.get("CREWBOSS_ERP_HEALTH_READ_TOKEN_FILE", "/run/secrets/erp-health-read-token")
ERP_HEALTH_RESOLVE_TOKEN_FILE = os.environ.get("CREWBOSS_ERP_HEALTH_RESOLVE_TOKEN_FILE",
                                               "/run/secrets/erp-health-resolve-token")
# Pull interval (seconds), next to the Zabbix loop. 0 = off (the test suite).
ERP_HEALTH_POLL_SECONDS = int(os.environ.get("CREWBOSS_ERP_HEALTH_POLL_SECONDS", "120"))
# A snapshot older than this (or a failed fetch) shows as stale; no fresh snapshot for longer than DOWN escalates
# once to the errors session.
ERP_HEALTH_STALE_MINUTES = int(os.environ.get("CREWBOSS_ERP_HEALTH_STALE_MINUTES", "10"))
ERP_HEALTH_DOWN_MINUTES = int(os.environ.get("CREWBOSS_ERP_HEALTH_DOWN_MINUTES", "15"))
# Code-fault filtering (#828): the ERP tags each event kind "code_fault" | "ops". Off (default) = every event is tracked and
# escalated as before. On (instance.yaml `erp_health_code_faults_only: true`, env CREWBOSS_ERP_HEALTH_CODE_FAULTS_ONLY) = only
# code_fault events are; a missing or unknown kind counts as ops, and ops events stay with the ERP's own monitoring.
ERP_HEALTH_CODE_FAULTS_ONLY = (os.environ["CREWBOSS_ERP_HEALTH_CODE_FAULTS_ONLY"].strip().lower() in ("1", "true", "yes", "on")
                               if "CREWBOSS_ERP_HEALTH_CODE_FAULTS_ONLY" in os.environ
                               else instance.get_bool("erp_health_code_faults_only"))
# A heartbeat (sync_state row) older than this is red, as on the ERP's own page.
ERP_HEARTBEAT_RED_MINUTES = int(os.environ.get("CREWBOSS_ERP_HEARTBEAT_RED_MINUTES", "120"))

# Workflows page filters (owner's discovery-tool brief: filter by trigger source and downstream impact).
TRIGGER_META = {
    "button": "Button (someone clicks)",
    "timer": "Schedule",
    "webhook": "Webhook",
    "event": "Event",
    "api": "API call",
}
# What a workflow CHANGES, from its side_effects ("db:<table>" or an outside system). One workflow can have several.
IMPACT_META = {
    "data": "Changes ERP data",
    "money": "Billing & money",
    "cases": "Cases & tickets",
    "notify": "Notifies people",
    "external": "Calls outside systems",
    "none": "Read only",
}
_MONEY = {"xero", "stripe", "crayon", "pingco", "db:invoices", "db:case_charges"}
_CASES = {"db:cases", "syncro"}
_NOTIFY = {"graph", "push", "webpush", "db:notifications", "db:sla_notifications"}


def workflow_impacts(side_effects) -> set[str]:
    effects = set(side_effects or [])
    if not effects:
        return {"none"}
    out = set()
    if any(e.startswith("db:") for e in effects):
        out.add("data")
    if any(not e.startswith("db:") for e in effects):
        out.add("external")
    if effects & _MONEY:
        out.add("money")
    if effects & _CASES:
        out.add("cases")
    if effects & _NOTIFY:
        out.add("notify")
    return out


WORKFLOW_GROUPS = (
    "Sales", "Service", "Monitoring", "Billing", "Email and AiOS",
    "Remote access", "Documents and Sign", "Platform", "Infrastructure",
)

# The dashboard's own origins (host[:port], as a browser puts it in the Origin header). The same-origin
# guard (app/origin_guard.py) rejects any browser write outside /api and /mcp that comes from anywhere
# else. CREWBOSS_ORIGIN_HOSTS REPLACES the list (comma-separated) without a code change.
_DEFAULT_ORIGIN_HOSTS = tuple(instance.get_list("origin_hosts", ("127.0.0.1:8931", "localhost:8931")))
# The LAN hosts and the padlocked domain from front_door.json are ADDED to whichever list is in force (#976), so the card's own pages pass the guard.
ORIGIN_HOSTS = tuple(dict.fromkeys(
    h.strip().lower()
    for h in (*(os.environ.get("CREWBOSS_ORIGIN_HOSTS") or ",".join(_DEFAULT_ORIGIN_HOSTS)).split(","), *front_door.origin_hosts())
    if h.strip()
))

# Materials (task #295, owner 2026-10-13): shared resources (logos, email/document templates, fonts…) per account or
# project, inherited down the tree. Files live in a plain folder (bind-mounted from the host that runs the sessions, so AI sessions there
# read them straight from disk); the database holds what each one is for. MATERIALS_HOST_DIR is the same folder as
# seen on the host — what get_material hands a session.
MATERIALS_DIR = os.environ.get("CREWBOSS_MATERIALS_DIR", "/data/materials")
MATERIALS_HOST_DIR = os.environ.get("CREWBOSS_MATERIALS_HOST_DIR", MATERIALS_DIR)
MATERIALS_MAX_BYTES = int(os.environ.get("CREWBOSS_MATERIALS_MAX_BYTES", str(15 * 1024 * 1024)))
# Assets (task #778): content-addressed files of skill attachments and (from P3) the imported materials. Defaults to the
# materials folder so existing files keep their path and the host bind mount stays valid.
ASSETS_DIR = os.environ.get("CREWBOSS_ASSETS_DIR", MATERIALS_DIR)
ASSET_ATTACHMENT_MAX_BYTES = int(os.environ.get("CREWBOSS_ASSET_ATTACHMENT_MAX_BYTES", str(1024 * 1024)))

# Servers tab (task #300): an optional estate-docs repo, mounted read-only — servers.yml + infra policy docs.
ESTATE_DIR = os.environ.get("CREWBOSS_ESTATE_DIR", "/data/estate")
# Where projects' repos are checked out on THIS server, for the Environment tab's "Suggest from the repo" (#810): <REPOS_DIR>/<project repo>/<folder>.
# Read-only and optional: a repo that is not here simply cannot be suggested from, and the person adds the rows by hand.
REPOS_DIR = os.environ.get("CREWBOSS_REPOS_DIR", instance.get_str("repos_dir", "~/repos"))

# Server access (task #367): each account's SSH certificate authority signs one-hour, one-server passes. Its private
# key is stored encrypted; the Fernet key that opens it lives in a 0600 file mounted into the container (never in the
# environment, the DB or a log), read at USE time. No file = adding servers works, signing and checks refuse.
SSH_CA_KEY_FILE = os.environ.get("CREWBOSS_SSH_CA_KEY_FILE", "/run/secrets/ssh-ca-enc-key")
# A pass is valid for this long (minutes), backdated a few minutes for clock skew between here and the server.
SSH_CERT_MINUTES = int(os.environ.get("CREWBOSS_SSH_CERT_MINUTES", "60"))
SSH_CERT_BACKDATE_MINUTES = 5
# Check access: ssh's own connect timeout, and the whole check's (seconds).
SSH_CONNECT_TIMEOUT = int(os.environ.get("CREWBOSS_SSH_CONNECT_TIMEOUT", "10"))
SSH_CHECK_TIMEOUT = int(os.environ.get("CREWBOSS_SSH_CHECK_TIMEOUT", "30"))

# Runners (task #627): the runner identity key (an ed25519 key, a 0600 file made once on the host with
# `python -m app.runner_identity keygen` and mounted read-only; app/runner_identity.py), the base URL runners are told to connect to (default: this dashboard's public
# URL), and how long a device token lives (days) before the runner must be enrolled again.
RUNNER_IDENTITY_KEY_FILE = os.environ.get("CREWBOSS_RUNNER_IDENTITY_KEY_FILE", "/run/secrets/runner-identity-key")
RUNNER_BASE_URL = os.environ.get("CREWBOSS_RUNNER_BASE_URL", "").strip().rstrip("/") or PUBLIC_URL
RUNNER_DEVICE_TOKEN_DAYS = int(os.environ.get("CREWBOSS_RUNNER_DEVICE_TOKEN_DAYS", "90"))
# Runner WebSocket (task #627, P3): how often the server re-challenges a connected runner (seconds), how long the runner has to
# answer a challenge, how often a live socket re-checks that its device is still live (the backstop for a revoke that didn't
# signal, a disabled user or an expired token), and extra Host names /runner/ws accepts (comma-separated; the MCP allowlist
# and this dashboard's own host are always accepted).
RUNNER_WS_CHALLENGE_SECONDS = float(os.environ.get("CREWBOSS_RUNNER_WS_CHALLENGE_SECONDS", "20"))
RUNNER_WS_PROOF_TIMEOUT_SECONDS = float(os.environ.get("CREWBOSS_RUNNER_WS_PROOF_TIMEOUT_SECONDS", "10"))
RUNNER_WS_RECHECK_SECONDS = float(os.environ.get("CREWBOSS_RUNNER_WS_RECHECK_SECONDS", "5"))
RUNNER_ALLOWED_HOSTS = os.environ.get("CREWBOSS_RUNNER_ALLOWED_HOSTS", "")
# Jobs (task #627, P2): a running job's lease (seconds, renewed by each heartbeat), how often the sweeper looks for expired
# leases (0 = off), the wall-vs-monotonic drift (seconds) it reads as a clock jump or a sleeping server host (one lease of
# grace is then given instead of expiring anything), and how many times a job is claimed before it is failed for a human.
JOB_LEASE_SECONDS = int(os.environ.get("CREWBOSS_JOB_LEASE_SECONDS", "90"))
JOB_SWEEP_SECONDS = int(os.environ.get("CREWBOSS_JOB_SWEEP_SECONDS", "15"))
JOB_CLOCK_JUMP_SECONDS = int(os.environ.get("CREWBOSS_JOB_CLOCK_JUMP_SECONDS", "30"))
JOB_MAX_ATTEMPTS = int(os.environ.get("CREWBOSS_JOB_MAX_ATTEMPTS", "3"))
# The longest a runner may let one job run (seconds, carried in the claim payload as max_seconds): a hung agent can't hold a lease forever.
JOB_MAX_SECONDS = int(os.environ.get("CREWBOSS_JOB_MAX_SECONDS", "14400"))

# CA, the Crewboss Assistant (task #702, v1). The model its turns run on (instance.yaml `ca_model`, default sonnet; the runner
# passes it to `claude --model`), the timezone reminders are spoken in (instance.yaml `timezone`, default UTC), how often the
# scheduler thread looks for due reminders and dead turns (0 = off), a turn's lease (seconds, kept alive by the runner), the
# longest a turn may run, and how long a queued turn waits for a runner before it fails.
CA_MODEL = os.environ.get("CREWBOSS_CA_MODEL", "").strip() or instance.get_str("ca_model", "sonnet")
# The install's local time zone (instance.yaml `timezone`, default UTC): "today" and "this week" on the Board, the morning re-plan
# and the times shown for servers are in it. Env CREWBOSS_TIMEZONE wins.
LOCAL_TZ = (os.environ.get("CREWBOSS_TIMEZONE") or "").strip() or instance.get_str("timezone", "UTC")
CA_TIMEZONE = os.environ.get("CREWBOSS_CA_TIMEZONE", "").strip() or LOCAL_TZ
CA_TICK_SECONDS = int(os.environ.get("CREWBOSS_CA_TICK_SECONDS", "20"))
CA_TURN_LEASE_SECONDS = int(os.environ.get("CREWBOSS_CA_TURN_LEASE_SECONDS", "90"))
CA_TURN_MAX_SECONDS = int(os.environ.get("CREWBOSS_CA_TURN_MAX_SECONDS", "900"))
TASK_SUMMARY_MODEL = os.environ.get("CREWBOSS_TASK_SUMMARY_MODEL", "").strip() or instance.get_str("task_summary_model", "haiku")   # the cheap model that writes a task's summary (#91): haiku or sonnet
CA_QUEUE_WAIT_SECONDS = int(os.environ.get("CREWBOSS_CA_QUEUE_WAIT_SECONDS", "90"))
# CA uploads (task #727): the bytes live on the server's own disk in this folder (bind-mounted, like the materials folder: add it to the
# backups); the database holds the metadata. Stripped (file deleted, a stub kept) once the linked task has been closed this many days,
# or, for a chat not linked to a task, once the chat has been idle this many days.
CA_UPLOADS_DIR = os.environ.get("CREWBOSS_CA_UPLOADS_DIR") or instance.get_str("ca_uploads_dir", "/data/ca-uploads")
CA_UPLOAD_TASK_CLOSED_DAYS = int(os.environ.get("CREWBOSS_CA_UPLOAD_TASK_CLOSED_DAYS", "30"))
CA_UPLOAD_IDLE_DAYS = int(os.environ.get("CREWBOSS_CA_UPLOAD_IDLE_DAYS", "90"))
# Per-person ceiling on CA files kept on the server disk (review 727 #2): an upload past it is refused, in MB.
CA_UPLOAD_USER_QUOTA_MB = int(os.environ.get("CREWBOSS_CA_UPLOAD_USER_QUOTA_MB", "1024") or "1024")
# CA speech-to-text (task #727): a swappable provider chosen in instance.yaml (`speech: {provider: aws_transcribe, region: ...}`).
# No provider = the mic and its device menu are hidden. The provider's credentials are a 0600 file mounted into the container
# (never the environment, the DB or a log), read at use time. See app/speech.py and instance.example.yaml.
SPEECH_PROVIDER = (os.environ.get("CREWBOSS_SPEECH_PROVIDER") or instance.get_str("speech.provider", "")).strip().lower()
SPEECH_REGION = (os.environ.get("CREWBOSS_SPEECH_REGION") or instance.get_str("speech.region", "us-east-1")).strip()
SPEECH_LANGUAGE = (os.environ.get("CREWBOSS_SPEECH_LANGUAGE") or instance.get_str("speech.language", "en-US")).strip()
SPEECH_CREDENTIALS_FILE = os.environ.get("CREWBOSS_SPEECH_CREDENTIALS_FILE", "/run/secrets/speech-credentials")

# The merge executor's GitHub credential (task #829): a fine-grained token of a NEW dedicated bot account (Contents: write on the enrolled
# repos only, never a person's account), in a 0600 file mounted into the container (never the environment, the DB or a log).
# The bot login (CREWBOSS_GITHUB_BOT_LOGIN, or instance.yaml `merge_executor.bot_login`) is REQUIRED to turn the executor on: it is the only
# login Crewboss accepts for the token. CREWBOSS_GITHUB_DENY_LOGINS (comma separated) or instance.yaml `merge_executor.deny_logins` (a YAML
# list or a comma separated string) names human accounts it must never be. Entries are lower-cased. See app/github_client.py.
GITHUB_TOKEN_FILE = os.environ.get("CREWBOSS_GITHUB_TOKEN_FILE", "/run/secrets/github-token")
GITHUB_BOT_LOGIN = (os.environ.get("CREWBOSS_GITHUB_BOT_LOGIN") or instance.get_str("merge_executor.bot_login", "")).strip().lower()
GITHUB_DENY_LOGINS = frozenset(
    x.strip().lower()
    for item in ([os.environ["CREWBOSS_GITHUB_DENY_LOGINS"]] if os.environ.get("CREWBOSS_GITHUB_DENY_LOGINS")
                 else instance.get_list("merge_executor.deny_logins"))
    for x in item.split(",") if x.strip())

# Hosts a project's git_remote may point at (task #627, P4b). Only https://<host>/<owner>/<repo>.git and git@<host>:<owner>/<repo>.git
# are accepted; github.com only for now. A runner clones and pushes this URL, so it is never free text.
GIT_REMOTE_HOSTS = ("github.com",)

# Catalog export for the ERP (#393): on every import of EXPORT_PROJECT's catalog, <EXPORTS_DIR>/catalog.json gets the
# workflows + AI skills with each step's resolved integration ids (app/catalog_export.py). The folder is the checkout's
# exports/ bind-mounted in (docker-compose.yml); unset (the tests, a bare checkout) = nothing is written.
EXPORTS_DIR = os.environ.get("CREWBOSS_EXPORTS_DIR", "")
EXPORT_PROJECT = os.environ.get("CREWBOSS_EXPORT_PROJECT", instance.get_str("roles.export"))

# No drift (#402): app/drift.py checks each linked admin site's catalog against its running app (and, from Zabbix, that
# every server timer has a diagram) and shows what doesn't match on the Health page. The reviewed config is
# CREWBOSS_HOME/drift.yaml; a project's repo .git is mounted read-only at its own path (docker-compose.yml). 0 = off (the tests).
DRIFT_FILE = os.environ.get("CREWBOSS_DRIFT_FILE", instance.path("drift.yaml"))
DRIFT_POLL_SECONDS = int(os.environ.get("CREWBOSS_DRIFT_POLL_SECONDS", "300"))

# Pushed app health (task #404): each server runs its own health checks and POSTs batches of reports to
# /api/health/report with its project's 'reporter' token, spooling while the link or the dashboard is down
# (docs/health-report-contract.md). CREWBOSS_HOME/health_feeds.yaml (reviewed) says, per project, whether the dashboard reads the
# pushed reports (push) or still fetches the ERP feed (pull), and how often a report is expected; no report OBSERVED
# for DEADMAN_FACTOR x that = "<project> stopped reporting". History is kept HISTORY_DAYS (by observed_at).
HEALTH_FEEDS_FILE = os.environ.get("CREWBOSS_HEALTH_FEEDS_FILE", instance.path("health_feeds.yaml"))
HEALTH_REPORT_MAX_BYTES = int(os.environ.get("CREWBOSS_HEALTH_REPORT_MAX_BYTES", str(2 * 1024 * 1024)))
HEALTH_REPORT_MAX_BATCH = int(os.environ.get("CREWBOSS_HEALTH_REPORT_MAX_BATCH", "500"))
HEALTH_REPORT_DEADMAN_FACTOR = int(os.environ.get("CREWBOSS_HEALTH_REPORT_DEADMAN_FACTOR", "3"))
HEALTH_REPORT_HISTORY_DAYS = int(os.environ.get("CREWBOSS_HEALTH_REPORT_HISTORY_DAYS", "7"))

# Phone notifications (#487, owner 2026-10-19: "I'll be installing dash dashboard as a PWA app on my phone and that's
# how I'd be getting notifications on internal device failures"; the rule is q292). app/push.py sends Web Push (VAPID)
# to the subscribed devices for estate problems (health_problems, every source). The VAPID PRIVATE key lives in a 0600
# file mounted into the container (never in the environment, the DB or a log), read at USE time: no file = push is off
# and the avatar menu says so. SUB is the claim the push services may use to contact whoever runs the sender.
VAPID_KEY_FILE = os.environ.get("CREWBOSS_VAPID_KEY_FILE", "/run/secrets/vapid-private.pem")
VAPID_SUB = os.environ.get("CREWBOSS_VAPID_SUB", instance.get_str("vapid_sub", "mailto:admin@example.com"))
# Sweep interval (seconds). 0 = off (the tests drive push.sweep() themselves).
PUSH_SWEEP_SECONDS = int(os.environ.get("CREWBOSS_PUSH_SWEEP_SECONDS", "30"))
# Planning reminders (#915): a pass every PLANNING_REMINDER_SECONDS (0 = off; the tests drive it) sends ONE push for everything
# whose start_by arrived or whose waiting_until passed, never before PLANNING_REMINDER_HOUR local time.
PLANNING_REMINDER_SECONDS = int(os.environ.get("CREWBOSS_PLANNING_REMINDER_SECONDS", "900"))
PLANNING_REMINDER_HOUR = int(os.environ.get("CREWBOSS_PLANNING_REMINDER_HOUR", "7"))
# The rule (q292): High/Disaster (>= 4) at once; Average (3) only once it has been open this long; Warning and below
# never. Problems that qualify within GROUP_SECONDS of a push join it (one notification); a problem key (a Zabbix
# trigger, a drift finding) that comes back pushes again only after REPEAT_MINUTES, and only if still open then.
PUSH_AVERAGE_MINUTES = int(os.environ.get("CREWBOSS_PUSH_AVERAGE_MINUTES", "15"))
PUSH_GROUP_SECONDS = int(os.environ.get("CREWBOSS_PUSH_GROUP_SECONDS", "120"))
PUSH_REPEAT_MINUTES = int(os.environ.get("CREWBOSS_PUSH_REPEAT_MINUTES", "60"))
# One quiet "resolved" notification when every problem of a push has cleared. 0 = never.
PUSH_RESOLVED = os.environ.get("CREWBOSS_PUSH_RESOLVED", "1") not in ("", "0", "false", "no")

# --- Platform keys (#562; the ERP's contract docs/integrations/dashboard-platform-keys.md, #547/#563) ---------------
# The keys that run the service for every tenant are SET here and written ONE WAY into each project's own key store,
# through its signed door (PUT/POST/GET <url>/platform/keys…, key id dash-keys-1). The Dashboard reads STATUS only:
# never a value, and it stores none. One store per project, ONE writer credential per project, never reused (the
# contract's "A new project with its own key store"): a new store is a row here, not code. The credential is a 0600
# file mounted into the container (never in the environment, the DB or a log), read at USE time, so dropping one in
# needs no restart. No file = the page says "not connected" and the reminder loop skips that store.
# Every store is one section of ONE page, the Dashboard project's Platform keys (/<dashboard>/keys#<slug>; #577, the
# owner's #547: platform keys are global, set from the Dashboard). The old /<slug>/keys redirects there.
#   slug        the dashboard project the store belongs to (its section anchor; reminders and stage rows key off it)
#   name        the product, as its section heads it ("Acme ERP · app01 · admin.example.com")
#   url         the store's admin host (scheme://host, no path)
#   key_file    the writer credential (on the host: ~/.config/crewboss/<key_id>)
#   server      a name for the store's server, shown on the page and in reminders
def _platform_key_stores() -> dict:
    """The stores from instance.yaml (`platform_key_stores:`, slug -> name/url/key_id/key_file/server). The two
    environment variables override the FIRST store's url and writer-credential file, as they always did the only one."""
    stores = {}
    for n, (slug, raw) in enumerate((instance.get_map("platform_key_stores")).items()):
        raw = dict(raw or {})
        url, key_file = raw.get("url", ""), raw.get("key_file", "")
        if n == 0:
            url = os.environ.get("CREWBOSS_PLATFORM_KEYS_URL") or url
            key_file = os.environ.get("CREWBOSS_PLATFORM_KEYS_FILE", key_file)
        stores[str(slug)] = {"name": raw.get("name", str(slug)), "url": str(url).rstrip("/"),
                             "key_id": raw.get("key_id", ""), "key_file": key_file, "server": raw.get("server", "")}
    return stores


PLATFORM_KEY_STORES = _platform_key_stores()
# How often the reminder loop reads each store's status (seconds; the door allows 30 calls a minute across every
# route, so this stays far below). A stage change (ok -> due_30 -> due_7 -> overdue) raises a Health reminder and a
# phone notification once. 0 = off (the tests drive platform_keys.sync() themselves).
PLATFORM_KEYS_POLL_SECONDS = int(os.environ.get("CREWBOSS_PLATFORM_KEYS_POLL_SECONDS", "900"))
# The Dashboard's own project: its Integrations page also lists the Dashboard's own links (app/integrations_dashboard.py).
DASHBOARD_PROJECT = os.environ.get("CREWBOSS_DASHBOARD_PROJECT", instance.get_str("roles.dashboard"))

# --- Dashboard sign-in (task #394) -------------------------------------------------------------------------------
# Every page needs a signed-in super admin (app/web_auth.py): Microsoft (Entra) OIDC against the owner's tenant, then
# the account's object id must be in CREWBOSS_SUPERADMIN_OIDS, a server setting no page can change (#503; groups grant
# nothing). "required" (the default) fails CLOSED: pages answer 503 until the tenant, client id and client secret
# file are set; an empty allowlist lets nobody in. "off" is the explicit opt-out (the tests, and a box where NetBird
# alone is accepted).
WEB_AUTH = os.environ.get("CREWBOSS_WEB_AUTH", "required").strip().lower()
OIDC_TENANT_ID = os.environ.get("CREWBOSS_OIDC_TENANT_ID", "").strip()
OIDC_CLIENT_ID = os.environ.get("CREWBOSS_OIDC_CLIENT_ID", "").strip()
OIDC_CLIENT_SECRET_FILE = os.environ.get("CREWBOSS_OIDC_CLIENT_SECRET_FILE", "/run/secrets/entra-client-secret")
SUPERADMIN_OIDS = frozenset(o.strip().lower() for o in os.environ.get("CREWBOSS_SUPERADMIN_OIDS", "").split(",") if o.strip())
# A LOCAL session (passkey / password + code) ends this long after sign-in, however busy: 30 days per browser (#809: sign in with an
# identifier + passkey about once a month, like most systems). Env-overridable. Sign out everywhere, a disabled user or a changed
# session epoch end a session at once whatever this says.
WEB_SESSION_MINUTES = int(os.environ.get("CREWBOSS_WEB_SESSION_MINUTES", "43200"))
# A Microsoft (OIDC) session keeps the owner's original cap (q296: "a 120-minute session cap"); #809 only changed local sign-in.
OIDC_SESSION_MINUTES = int(os.environ.get("CREWBOSS_OIDC_SESSION_MINUTES", "120"))
# How long a started sign-in may take at Microsoft before it has to start again.
OIDC_FLOW_MINUTES = int(os.environ.get("CREWBOSS_OIDC_FLOW_MINUTES", "10"))

# --- Local sign-in (task #706) ---------------------------------------------------------------------------------------
# `auth: local | oidc` in instance.yaml (env CREWBOSS_AUTH wins). Unset = local, EXCEPT that an install with OIDC already
# configured (client id + a client secret file) keeps OIDC, so merging this never changes a running install:
# app/web_auth.py mode() decides, reading these live. The explicit value is checked here, at start-up.
AUTH_SETTING = (os.environ.get("CREWBOSS_AUTH") or instance.get_str("auth")).strip().lower()
if AUTH_SETTING not in ("", "local", "oidc"):
    raise ValueError(f"auth must be 'local' or 'oidc' (instance.yaml key 'auth' or CREWBOSS_AUTH), not {AUTH_SETTING!r}")
# A local session also ends after this long without a request (the absolute cap is WEB_SESSION_MINUTES above). Default 30 days = no
# separate idle limit (#809); set it lower in the environment to bring one back.
WEB_IDLE_MINUTES = int(os.environ.get("CREWBOSS_WEB_IDLE_MINUTES", "43200"))
# Step-up (#809): a sensitive action (a key, a token, a role, a server, a deploy or merge approval) needs a passkey tap this recent. Sign-in
# by passkey counts as a tap. Hours; 12 by default. The most sensitive actions ask for 5 minutes regardless.
WEB_STEPUP_HOURS = float(os.environ.get("CREWBOSS_WEB_STEPUP_HOURS", "12"))
# A session that may only enrol a second factor (first run) lives this long.
ENROLMENT_SESSION_MINUTES = int(os.environ.get("CREWBOSS_ENROLMENT_SESSION_MINUTES", "30"))
# Where the key that encrypts TOTP secrets lives (made 0600 on first use). Under the instance folder so it survives
# image rebuilds; mount that folder writable, or point this at a mounted secret.
# Peers allowed to say who the real client is (X-Forwarded-For). Default: loopback ONLY. The installer writes the actual proxy's
# address into instance.yaml (trusted_proxies); an install whose proxy reaches the app from another address (a docker gateway, a mesh
# peer) lists that address there. Anyone else's header is ignored, so a client cannot choose its own rate-limit bucket.
TRUSTED_PROXIES = tuple(p.strip() for p in (os.environ.get("CREWBOSS_TRUSTED_PROXIES", "").split(",") if os.environ.get("CREWBOSS_TRUSTED_PROXIES")
                        else instance.get_list("trusted_proxies", ("127.0.0.0/8", "::1/128"))) if p.strip())
LOCAL_AUTH_KEY_FILE = os.environ.get("CREWBOSS_LOCAL_AUTH_KEY_FILE", instance.path(os.path.join("secrets", "local-auth.key")))

# --- Public certificate check (task #707, app/tls_check.py) --------------------------------------------------------------
# When public_url is https and not localhost, the app checks (at start-up and every TLS_CHECK_INTERVAL_SECONDS) that the
# certificate served for it is publicly trusted, name-matching and not about to expire. `tls_check:` in instance.yaml (env
# CREWBOSS_TLS_CHECK): `warn` (the default: log + banner), `enforce` (also refuse to START; the installer writes it for new
# installs) or `off`. `tls_check_connect` (host:port) makes the check dial that address instead of the public name (the
# bundled proxy's), still verifying the certificate for the public host name.
_tls_yaml = instance.get("tls_check", "warn")
if _tls_yaml is False:       # YAML 1.1 reads an unquoted `off` as False
    _tls_yaml = "off"
if not isinstance(_tls_yaml, str):
    raise ValueError(f"{instance.INSTANCE_FILE}: 'tls_check' must be off, warn or enforce, not {_tls_yaml!r}")
TLS_CHECK = (os.environ.get("CREWBOSS_TLS_CHECK") or _tls_yaml).strip().lower()
if TLS_CHECK not in ("off", "warn", "enforce"):
    raise ValueError(f"tls_check must be 'off', 'warn' or 'enforce' (instance.yaml key 'tls_check' or CREWBOSS_TLS_CHECK), not {TLS_CHECK!r}")
TLS_CHECK_CONNECT = (os.environ.get("CREWBOSS_TLS_CHECK_CONNECT") or instance.get_str("tls_check_connect")).strip()
TLS_CHECK_INTERVAL_SECONDS = int(os.environ.get("CREWBOSS_TLS_CHECK_INTERVAL_SECONDS", str(12 * 3600)))   # 0 = never
TLS_CHECK_TIMEOUT_SECONDS = float(os.environ.get("CREWBOSS_TLS_CHECK_TIMEOUT_SECONDS", "4"))
TLS_CHECK_GRACE_SECONDS = int(os.environ.get("CREWBOSS_TLS_CHECK_GRACE_SECONDS", "150"))   # `enforce`: how long start-up waits for a first good certificate
TLS_MIN_DAYS = 7


if __name__ == "__main__":
    import sys

    from app import config_dump

    if sys.argv[1:] == ["dump"]:
        print(config_dump.dump(globals()))
    else:
        sys.exit("usage: python -m app.config dump   (the effective configuration, secrets redacted)")

# Deploy executor (task #627, P5; app/deploys.py): how often it looks for approved deploys (0 = off), the longest one deploy
# may run (seconds; also the certificate's validity, rounded up to minutes), and the most open requests one job may have.
# ONE-EXECUTOR RULE: run the executor (DEPLOY_POLL_SECONDS > 0) in one app process; a pg advisory lock makes a second process skip its
# passes, so extra replicas are harmless but idle. Give a replica CREWBOSS_DEPLOY_POLL_SECONDS=0 to be explicit.
DEPLOY_POLL_SECONDS = int(os.environ.get("CREWBOSS_DEPLOY_POLL_SECONDS", "5"))
# Server probe (#810): every SERVER_PROBE_SECONDS an `ssh-keyscan` (no login, no pass signed) per direct server, compared with the pinned
# host key; 0 switches the sweeper off (the tests do, and drive server_access.probe_all with the scan mocked).
SERVER_PROBE_SECONDS = int(os.environ.get("CREWBOSS_SERVER_PROBE_SECONDS", "300"))
SERVER_PROBE_TIMEOUT = int(os.environ.get("CREWBOSS_SERVER_PROBE_TIMEOUT", "5"))
DEPLOY_TIMEOUT_SECONDS = int(os.environ.get("CREWBOSS_DEPLOY_TIMEOUT_SECONDS", "600"))
DEPLOY_MAX_OPEN_PER_JOB = int(os.environ.get("CREWBOSS_DEPLOY_MAX_OPEN_PER_JOB", "5"))
# Server reads (#855): a job's look at a server's logs, through one-command passes. See app/server_reads.py and docs/server-reads.md.
SERVER_READ_HELPER = os.environ.get("CREWBOSS_SERVER_READ_HELPER", "/usr/local/bin/crewboss-read")
SERVER_READ_TIMEOUT_SECONDS = int(os.environ.get("CREWBOSS_SERVER_READ_TIMEOUT_SECONDS", "30"))
SERVER_READ_MAX_BYTES = int(os.environ.get("CREWBOSS_SERVER_READ_MAX_BYTES", "200000"))
SERVER_READ_PER_JOB_HOUR = int(os.environ.get("CREWBOSS_SERVER_READ_PER_JOB_HOUR", "60"))
SERVER_READ_PATH_PREFIXES = ("/var/log/", "/opt/", "/srv/")      # a file read must sit under one of these (the table CHECK repeats them)
SERVER_READ_SSH_MAX_BYTES = int(os.environ.get("CREWBOSS_SERVER_READ_SSH_MAX_BYTES", "2000000"))   # Crewboss reads at most this from the ssh child
