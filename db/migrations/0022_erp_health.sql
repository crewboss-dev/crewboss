-- Task #196 (change #15): the ERP health feed. The ERP keeps its sensors; the dashboard pulls GET /api/health
-- (read-only, bearer token) every ERP_HEALTH_POLL_SECONDS and keeps the latest snapshot here, so the Health page and
-- the global home render without a live call. Source 'erp' rides the existing health tables:
--   health_sources('erp'): the raw v1 JSON (snapshot), its generated_at, when fetching started failing, and when the
--     feed-down escalation was sent (cleared when it recovers). bookmark = the highest ERP event id seen (NULL until
--     the first good fetch, which records what is already open as baseline, exactly like Zabbix).
--   health_problems(source 'erp'): one row per ERP system_events id seen open (eventid = the ERP id, host = the
--     event's source, name = its title, severity mapped critical 5 / error 4 / warning 2 / info 1), for dedupe and
--     recovery: escalated once (dedupe key erp-event:<id>) and its recovery pushed when it leaves the open set.
ALTER TABLE health_sources ADD COLUMN IF NOT EXISTS snapshot JSON;  -- JSON, not JSONB: keeps the ERP's key order for display
ALTER TABLE health_sources ADD COLUMN IF NOT EXISTS generated_at TIMESTAMPTZ;
ALTER TABLE health_sources ADD COLUMN IF NOT EXISTS failing_since TIMESTAMPTZ;
ALTER TABLE health_sources ADD COLUMN IF NOT EXISTS down_escalated_at TIMESTAMPTZ;
