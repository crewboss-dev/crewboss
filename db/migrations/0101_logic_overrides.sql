-- #715: the owner's override of the logic gate, for ONE release. An Admin records who and why for one commit; the first release request
-- of that commit that needed it uses it up (used_server_job_id), and it can never be used again. At most one unused override per project.
CREATE TABLE IF NOT EXISTS logic_overrides (
    id                 SERIAL PRIMARY KEY,
    project_id         INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    sha                TEXT NOT NULL CHECK (sha ~ '^[0-9a-f]{40}$'),
    reason             TEXT NOT NULL CHECK (length(reason) BETWEEN 8 AND 300),
    granted_by         INTEGER REFERENCES users(id),
    granted_by_name    TEXT NOT NULL DEFAULT '',
    granted_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    used_server_job_id INTEGER REFERENCES server_jobs(id) ON DELETE SET NULL,
    used_at            TIMESTAMPTZ,
    revoked_at         TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS logic_overrides_one_open ON logic_overrides (project_id) WHERE used_at IS NULL AND revoked_at IS NULL;
