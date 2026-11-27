"""Pydantic request bodies for /api writes."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class TaskCreate(BaseModel):
    # None = the token's own project (the only one a session token may name)
    project: str | None = None
    title: str
    body: str = ""
    priority: str | None = None
    stream: str
    subprojects: list[str] = Field(default_factory=list)
    links: dict = Field(default_factory=dict)
    needs_owner: bool = False
    # Tasks v2: who does it ('ai' = the stream's session, default; 'owner' = the owner's "Assigned to
    # me"), an optional checklist (e.g. test steps for a testing handoff) and a starting status.
    assignee: str | None = None
    subtasks: list[str] = Field(default_factory=list)
    status: str | None = None
    # Planning dates (#915; docs/planning-dates.md). Dates are ISO YYYY-MM-DD strings, validated in app/planning.py.
    due_on: str | None = None
    lead_days: int | None = None
    start_by: str | None = None
    milestone: str | None = None        # the id or name of a milestone of the task's own project
    waiting_on: str | None = None
    waiting_for: str | None = None
    waiting_until: str | None = None


class TaskPatch(BaseModel):
    # project: a slug — moves the task to that (active) project
    project: str | None = None
    title: str | None = None
    body: str | None = None
    priority: str | None = None
    status: str | None = None
    stream: str | None = None
    subprojects: list[str] | None = None
    links: dict | None = None
    due_on: str | None = None
    lead_days: int | None = None
    start_by: str | None = None
    milestone: str | None = None
    waiting_on: str | None = None
    waiting_for: str | None = None
    waiting_until: str | None = None
    clear: list[str] | None = None      # any of due_on, lead_days, start_by, milestone, waiting


class MilestoneCreate(BaseModel):
    project: str | None = None
    name: str
    due_on: str
    note: str = ""


class MilestonePatch(BaseModel):
    name: str | None = None
    due_on: str | None = None
    note: str | None = None
    archived: bool | None = None


class CommentCreate(BaseModel):
    body: str
    # Derived from the token server-side; if sent it must match (kept so old callers don't 422).
    author: str | None = None


class KnowledgeCreate(BaseModel):
    project: str | None = None
    kind: str = "note"
    title: str
    # Required on every new entry (repo.clean_summary: <= 200 chars); "" is rejected with a 400 there.
    summary: str = ""
    body: str = ""
    subprojects: list[str] = Field(default_factory=list)
    source: str = ""
    needs_review: bool = False
    target_value: float | None = None
    target_period: str | None = None
    target_unit: str | None = None


class KnowledgePatch(BaseModel):
    title: str | None = None
    summary: str | None = None
    body: str | None = None
    kind: str | None = None
    subprojects: list[str] | None = None
    source: str | None = None
    needs_review: bool | None = None
    target_value: float | None = None
    target_period: str | None = None
    target_unit: str | None = None


class KnowledgeArchive(BaseModel):
    reason: str


class ProjectCreate(BaseModel):
    slug: str
    name: str
    type: str = "internal"
    # names the project's sessions (<short>-<role>); required
    short_name: str
    # ['all'] = one session for every stream (default); or some of features/reviewer/bugs/tweaks/errors
    session_roles: list[str] | None = None


class DeliveredBody(BaseModel):
    delivered_via: str = "hook"


class OwnerPromptBody(BaseModel):
    prompt: str = Field(default="", max_length=2000)   # a longer prompt is never a decision quote: 422


class SessionStopping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phase: str  # notice | refresh | cancel | stopped — no free text: the message and the session come from the token


class HeartbeatBody(BaseModel):
    state: str = "online"
    # the channel says which project it runs for; it must be the token's own
    project: str | None = None


class LinkWorkflowBody(BaseModel):
    workflow_id: str
    step: str | None = None


class SuggestTriageBody(BaseModel):
    rationale: str
    business_case: str = ""
    priority: str | None = None
    rank_hint: str | None = None
    stream: str | None = None
    category: str | None = None
    needed_now: str | None = None
    needed_reason: str = ""
    assignee: str | None = None


# --- tasks v2. extra="forbid": authorship (created_by / done_by / by) is derived from the token and a
# body that tries to set it is refused outright (422), not silently ignored.

class AssignBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    assignee: str


class SubtasksAdd(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[str]


class SubtaskPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    done: bool | None = None
    text: str | None = None


class SubtaskChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: str
    subtask_id: int | None = None
    text: str | None = None
    position: int | None = None


class ProposeSubtasksBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    changes: list[SubtaskChange]
    rationale: str


class OutboxPush(BaseModel):
    """Generic outbox insert for non-owner producers, e.g. System Health (phase 3) pushing
    production failures to the errors stream with kind='health_error'."""
    stream: str
    kind: str = "health_error"
    message: str
    project: str | None = None
    task_id: int | None = None
    dedupe_key: str | None = None


class ZabbixPoke(BaseModel):
    """What Zabbix media type 49 sends. Only the shape is checked; nothing in it is stored or acted on (the
    sync asks Zabbix). Extra fields are ignored."""
    model_config = ConfigDict(extra="ignore")
    eventid: str | None = Field(default=None, pattern=r"^\d{1,20}$")


class SatelliteRequest(BaseModel):
    """A feature request pushed up from a client's mini dashboard. request_id is the satellite's own id for it,
    so a retried push never makes a second task."""
    title: str
    body: str = ""
    requested_by: str
    request_id: str


class SshCertRequest(BaseModel):
    # One 'ssh-ed25519 AAAA… [comment]' line: the session's throw-away key to sign (app/ssh_ca.parse_ed25519).
    public_key: str = Field(max_length=400)
