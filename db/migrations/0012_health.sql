-- Phase 3: System Health. The dashboard pulls production health from Zabbix (a read-only API token) and keeps
-- its own copy of the problems it has seen, so the Health page and the global home render without a live call.

-- One row per Zabbix PROBLEM event (value=1) in the watched host groups. resolved_at is set when the event's
-- recovery is seen (r_eventid) or when it drops out of problem.get's open set. baseline = it was already open
-- the first time the dashboard synced, so it was recorded but never escalated.
CREATE TABLE IF NOT EXISTS health_problems (
    source        TEXT NOT NULL DEFAULT 'zabbix',
    eventid       BIGINT NOT NULL,
    objectid      BIGINT,
    host          TEXT NOT NULL DEFAULT '',
    name          TEXT NOT NULL,
    severity      SMALLINT NOT NULL DEFAULT 0,
    started_at    TIMESTAMPTZ NOT NULL,
    resolved_at   TIMESTAMPTZ,
    baseline      BOOLEAN NOT NULL DEFAULT false,
    escalated_at  TIMESTAMPTZ,
    seen_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (source, eventid)
);
CREATE INDEX IF NOT EXISTS health_problems_open ON health_problems (source) WHERE resolved_at IS NULL;
CREATE INDEX IF NOT EXISTS health_problems_resolved ON health_problems (resolved_at DESC) WHERE resolved_at IS NOT NULL;

-- Per-source sync state: the event bookmark (highest PROBLEM eventid seen) and how the last sync went.
CREATE TABLE IF NOT EXISTS health_sources (
    source        TEXT PRIMARY KEY,
    bookmark      BIGINT,
    last_sync_at  TIMESTAMPTZ,
    last_ok_at    TIMESTAMPTZ,
    last_error    TEXT,
    last_poke_at  TIMESTAMPTZ
);

-- A 'zabbix' ROLE token: the Zabbix poke webhook (media type 49) may ONLY call POST /api/health/zabbix/poke.
ALTER TABLE session_tokens DROP CONSTRAINT IF EXISTS session_tokens_stream_check;
ALTER TABLE session_tokens ADD CONSTRAINT session_tokens_stream_check
    CHECK (stream IN ('features', 'bugs', 'tweaks', 'reviewer', 'errors', 'all', 'scanner', 'zabbix'));
