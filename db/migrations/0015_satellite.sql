-- Client mini dashboard (task #106, owner chose a SATELLITE on each client server, 2026-10-01).
-- The project records that it WANTS a satellite and which server it lives on; deploying it is a separate,
-- owner-confirmed step (estate-ops). The satellite holds ONE project and talks to this dashboard over the mesh
-- with a project-bound 'satellite' token: it may only read that project's client snapshot and post requests.

ALTER TABLE projects ADD COLUMN IF NOT EXISTS satellite_enabled   BOOLEAN NOT NULL DEFAULT false;
-- The server's name as Zabbix knows it (health_problems.host) — what the satellite's health panel filters on.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS satellite_host      TEXT NOT NULL DEFAULT '';
-- How the client manager reaches it: a LAN address on the client's network and/or its NetBird name.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS satellite_lan_ip    TEXT NOT NULL DEFAULT '';
ALTER TABLE projects ADD COLUMN IF NOT EXISTS satellite_mesh_name TEXT NOT NULL DEFAULT '';
-- Last time the satellite fetched its snapshot (shows "connected / last seen" on the settings panel).
ALTER TABLE projects ADD COLUMN IF NOT EXISTS satellite_seen_at   TIMESTAMPTZ;

-- What the client sees: only tasks marked client-visible (a client's own request is visible by default).
-- Bodies, comments, triage reasoning and pricing never leave this dashboard.
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS client_visible   BOOLEAN NOT NULL DEFAULT false;
-- A request from the satellite: who asked (the manager's display name) and the satellite's own request id,
-- so a retried push never makes a second task.
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS requested_by      TEXT;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS client_request_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS tasks_client_request ON tasks (project_id, client_request_id)
    WHERE client_request_id IS NOT NULL;

-- A 'satellite' token: the one role kind that IS bound to a project.
ALTER TABLE session_tokens DROP CONSTRAINT IF EXISTS session_tokens_stream_check;
ALTER TABLE session_tokens ADD CONSTRAINT session_tokens_stream_check
    CHECK (stream IN ('features', 'bugs', 'tweaks', 'reviewer', 'errors', 'all', 'scanner', 'zabbix', 'satellite'));
