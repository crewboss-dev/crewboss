-- Session presence, heartbeated by a small stdio MCP "channel" server per tmux session that polls
-- the outbox and pushes messages in. Display status is DERIVED (see app/repo.py session_status),
-- not stored — only the raw last_seen/state are.
CREATE TABLE IF NOT EXISTS sessions (
    stream      TEXT PRIMARY KEY CHECK (stream IN ('features', 'bugs', 'tweaks', 'reviewer', 'errors')),
    state       TEXT NOT NULL DEFAULT 'offline',
    last_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
