-- Task #404 (owner, 2026-10-15): each server hosts its own health checks and PUSHES them to the dashboard, spooling
-- while the link or the dashboard is down; the dashboard stops pulling. Contract: docs/health-report-contract.md.
-- A 'reporter' token is a write-only role bound to ONE project (optionally to named sources inside it): it may only
-- POST /api/health/report for that project.
ALTER TABLE session_tokens DROP CONSTRAINT IF EXISTS session_tokens_stream_check;
ALTER TABLE session_tokens ADD CONSTRAINT session_tokens_stream_check
    CHECK (stream IN ('features', 'bugs', 'tweaks', 'reviewer', 'errors', 'all', 'scanner', 'zabbix', 'satellite',
                      'reporter'));
-- NULL = any source name; else the only sources this reporter token may post for.
ALTER TABLE session_tokens ADD COLUMN IF NOT EXISTS sources TEXT[];

-- Every report received, for idempotency (report_id) and the Health timeline: kept for a bounded window (7 days of
-- observed_at), pruned on write. A replayed spool lands here in its real order.
CREATE TABLE IF NOT EXISTS health_reports (
    id            BIGSERIAL PRIMARY KEY,
    project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    source        TEXT NOT NULL,
    report_id     UUID NOT NULL,
    observed_at   TIMESTAMPTZ NOT NULL,
    received_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    health        JSON NOT NULL,          -- JSON, not JSONB: keeps the app's key order for display
    token_id      INTEGER,
    UNIQUE (project_id, report_id)
);
CREATE INDEX IF NOT EXISTS health_reports_timeline ON health_reports (project_id, source, observed_at DESC);
CREATE INDEX IF NOT EXISTS health_reports_observed ON health_reports (observed_at);

-- The latest state per (project, source) = the report with the NEWEST observed_at (not the newest arrival).
CREATE TABLE IF NOT EXISTS health_report_latest (
    project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    source        TEXT NOT NULL,
    report_id     UUID NOT NULL,
    observed_at   TIMESTAMPTZ NOT NULL,
    received_at   TIMESTAMPTZ NOT NULL,
    health        JSON NOT NULL,
    PRIMARY KEY (project_id, source)
);
