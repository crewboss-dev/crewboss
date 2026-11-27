-- Runners, phase P5 (task #627, change #50): how a job's work reaches a server. A PC never holds SSH material; a job only
-- ASKS (request_deploy), a person approves a card on /<account>/approvals, and the dashboard's own executor (app/deploys.py)
-- signs a short-lived, single-command certificate and runs the deploy. See docs/deploy-executor.md.
--   deploy_targets   per project: which commissioned server (host, login user and pinned host key come from
--                    commissioned_servers, so there is one list of reachable boxes), the repo path on it, the post-pull
--                    command, the branch it must be on, and enabled. Set by a person on the approvals page; never by a job.
--   server_jobs      one deploy request: requested -> approved -> running -> done | failed | orphaned, or refused. A restart
--                    while running makes it orphaned (never retried). head_before / head_after / output_tail record the run.
--   locks.server_job_id   the executor's deploy lock (job_id NULL, resource 'target:<id>') names the server_job holding it.
CREATE TABLE IF NOT EXISTS deploy_targets (
    id         SERIAL PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    server_id  INTEGER NOT NULL REFERENCES commissioned_servers(id) ON DELETE CASCADE,
    name       TEXT NOT NULL CHECK (name ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$'),
    repo_path  TEXT NOT NULL CHECK (repo_path ~ '^/[A-Za-z0-9._/-]{1,200}$' AND repo_path NOT LIKE '%..%'),
    post_cmd   TEXT NOT NULL DEFAULT '' CHECK (length(post_cmd) <= 500 AND post_cmd !~ '[\r\n\x01-\x08]'),
    branch     TEXT NOT NULL DEFAULT 'main' CHECK (branch ~ '^[A-Za-z0-9][A-Za-z0-9._/-]{0,100}$'),
    enabled    BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_deploy_targets_name ON deploy_targets(project_id, lower(name));

CREATE TABLE IF NOT EXISTS server_jobs (
    id            SERIAL PRIMARY KEY,
    account_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    target_id     INTEGER REFERENCES deploy_targets(id) ON DELETE SET NULL,
    target_name   TEXT NOT NULL,
    job_id        INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    task_id       INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    device_id     INTEGER REFERENCES device_tokens(id) ON DELETE SET NULL,
    sha           TEXT NOT NULL CHECK (sha ~ '^[0-9a-f]{40}$'),
    subject       TEXT NOT NULL DEFAULT '',      -- claimed by the job (outside text, shown labelled as such)
    state         TEXT NOT NULL DEFAULT 'requested'
                  CHECK (state IN ('requested', 'approved', 'running', 'done', 'failed', 'orphaned', 'refused')),
    requested_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    requested_by INTEGER REFERENCES users(id),  -- set when a PERSON asked (job_id is then NULL)
    decided_by    INTEGER REFERENCES users(id),  -- who approved or refused
    decided_at    TIMESTAMPTZ,
    started_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ,
    head_before   TEXT NOT NULL DEFAULT '',
    head_after    TEXT NOT NULL DEFAULT '',
    output_tail   TEXT NOT NULL DEFAULT '',
    error         TEXT NOT NULL DEFAULT '',
    owner         TEXT NOT NULL DEFAULT '',      -- the app process instance running it (a different one = a dead one)
    flagged       BOOLEAN NOT NULL DEFAULT false -- HEAD after was not the approved sha
);
-- The same sha for the same target is asked for once at a time.
CREATE UNIQUE INDEX IF NOT EXISTS server_jobs_one_open ON server_jobs(target_id, sha)
    WHERE state IN ('requested', 'approved', 'running');
CREATE INDEX IF NOT EXISTS idx_server_jobs_account ON server_jobs(account_id, state);

ALTER TABLE locks ADD COLUMN IF NOT EXISTS server_job_id INTEGER REFERENCES server_jobs(id) ON DELETE SET NULL;
