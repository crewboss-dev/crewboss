-- #855 (change #82): server reads. A project DECLARES, per deploy target (= server + environment), the few things a job may look at on
-- that server: a container's logs, a systemd unit's journal, one log file, or one fixed probe command. A job asks for one by name
-- through the job MCP; Crewboss runs it on the server with a one-command pass and returns capped, redacted output. Nothing here lets a
-- job run a command of its own: the kind and the spec are written by a person on the Where it lives page and re-checked on the server
-- by /usr/local/bin/crewboss-read against that server's own /etc/crewboss/reads.conf.
--   server_reads        the declared reads. spec = container name | unit name | absolute file path | probe argv as a JSON array.
--   projects.server_reads_prod   reads on a PRODUCTION target are refused unless this is on (dev and staging need no switch).
--   server_read_log     one row per ATTEMPT, the audit trail. Inserted as 'pending' BEFORE a pass is signed (so the rate limit counts reads in
--                       flight), then 'done' or 'refused'. Never holds output, only its size. Survives its project (project_id SET NULL).
CREATE TABLE IF NOT EXISTS server_reads (
    id         SERIAL PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    target_id  INTEGER NOT NULL REFERENCES deploy_targets(id) ON DELETE CASCADE,
    name       TEXT NOT NULL CHECK (name ~ '^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$'),
    kind       TEXT NOT NULL CHECK (kind IN ('log_container', 'log_unit', 'log_file', 'probe')),
    spec       TEXT NOT NULL CHECK (length(spec) BETWEEN 1 AND 600 AND spec !~ '[\r\n\t\x00]'),
    created_by TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT server_reads_spec_shape CHECK (
        CASE kind
            WHEN 'log_container' THEN spec ~ '^[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}$'
            WHEN 'log_unit'      THEN spec ~ '^[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}$'
            WHEN 'log_file'      THEN spec ~ '^/[A-Za-z0-9_./@+-]{1,255}$' AND spec NOT LIKE '%..%' AND spec !~ '/$'
                                      AND (spec LIKE '/var/log/%' OR spec LIKE '/opt/%' OR spec LIKE '/srv/%')
            ELSE spec LIKE '[%]'
        END)
);
CREATE UNIQUE INDEX IF NOT EXISTS server_reads_one ON server_reads (project_id, target_id, lower(name));

ALTER TABLE projects ADD COLUMN IF NOT EXISTS server_reads_prod BOOLEAN NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS server_read_log (
    id         SERIAL PRIMARY KEY,
    job_id     INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    task_id    INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    read_id    INTEGER REFERENCES server_reads(id) ON DELETE SET NULL,
    read_name  TEXT NOT NULL DEFAULT '',
    env        TEXT NOT NULL DEFAULT '',
    server_id  INTEGER REFERENCES commissioned_servers(id) ON DELETE SET NULL,
    params     JSONB NOT NULL DEFAULT '{}',
    bytes      INTEGER NOT NULL DEFAULT 0,
    ok         BOOLEAN NOT NULL DEFAULT false,
    state      TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'done', 'refused')),
    error      TEXT NOT NULL DEFAULT '',
    at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS server_read_log_job ON server_read_log (job_id, at);
CREATE INDEX IF NOT EXISTS server_read_log_project ON server_read_log (project_id, at DESC);
