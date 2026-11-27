-- #828: the ERP tags each health event kind "code_fault" | "ops". The kind is kept on the tracked problem so the Health page can
-- label it and the read-only digest summary (GET /api/summary) can list open code faults. NULL = tracked before this (read as ops).
ALTER TABLE health_problems ADD COLUMN IF NOT EXISTS kind TEXT CHECK (kind IN ('code_fault', 'ops'));
-- The 'digest' role token (#828): read-only, may only call GET /api/summary.
ALTER TABLE session_tokens DROP CONSTRAINT IF EXISTS session_tokens_stream_check;
ALTER TABLE session_tokens ADD CONSTRAINT session_tokens_stream_check
    CHECK (stream IN ('features', 'bugs', 'tweaks', 'reviewer', 'errors', 'all', 'scanner', 'zabbix', 'satellite',
                      'reporter', 'digest'));
