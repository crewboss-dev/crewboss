-- Runners, phase P2 (task #627, change #50): agent profiles, jobs, leases, fencing and locks. An agent is made per job
-- from a profile; a job is one run of one task on one user's runner. The broker (app/jobs.py) hands jobs out with ONE
-- SQL statement; every write by a job carries its FENCE, and a stale fence is a 409.
--   projects.git_remote   where a runner clones the project's code from (an ids-only job payload carries no text).
--   fence_seq             monotonic fencing tokens: a new value at every claim and every requeue.
--   agent_profiles        model + skills + allowed tools + task types, seeded from the model routing table. No 'fable'
--                         profile is seeded: Fable is escalation only, chosen by a human (the CHECK allows one to be added).
--   jobs                  ONE live (queued/running) job per task (partial unique index). account_id is the task's account,
--                         derived server-side. unattended + approved_at decide which runner may claim it.
--   locks                 keyed (account, kind, resource): tenant-namespaced, so two accounts can never contend. 'orphaned'
--                         = the holder's lease expired on a deploy/migrate: never retried, a human clears it.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS git_remote TEXT NOT NULL DEFAULT '';

CREATE SEQUENCE IF NOT EXISTS fence_seq;

CREATE TABLE IF NOT EXISTS agent_profiles (
    id            SERIAL PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE CHECK (name ~ '^[a-z0-9][a-z0-9_-]{0,38}$'),
    model         TEXT NOT NULL CHECK (model IN ('opus', 'sonnet', 'haiku', 'fable')),
    description   TEXT NOT NULL DEFAULT '',
    skills        TEXT[] NOT NULL DEFAULT '{}',
    allowed_tools TEXT[] NOT NULL DEFAULT '{}',
    task_types    TEXT[] NOT NULL DEFAULT '{}',
    unattended_ok BOOLEAN NOT NULL DEFAULT false,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Model routing table (owner, change #38): code/builders/migrations/UI/scripts -> sonnet; mechanical chores -> haiku;
-- design/planning/review/coordination -> opus. unattended_ok is false by default (a human turns it on per profile).
INSERT INTO agent_profiles (name, model, description, skills, allowed_tools, task_types, unattended_ok) VALUES
 ('builder', 'sonnet', 'Writes code: builders, migrations, UI, scripts', '{}',
  '{Read,Edit,Write,Glob,Grep,Bash}', '{code,migration,ui,script}', false),
 ('chores', 'haiku', 'Mechanical chores: scans, test runs, annotation fixes, summaries', '{}',
  '{Read,Glob,Grep,Bash}', '{scan,test-run,annotation-fix,summary}', true),
 ('designer', 'opus', 'Design, planning, review, coordination', '{}',
  '{Read,Glob,Grep}', '{design,planning,review,coordination}', false)
ON CONFLICT (name) DO NOTHING;

CREATE TABLE IF NOT EXISTS jobs (
    id               SERIAL PRIMARY KEY,
    task_id          INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    account_id       INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id          INTEGER NOT NULL REFERENCES users(id),
    device_id        INTEGER REFERENCES device_tokens(id),
    profile_id       INTEGER NOT NULL REFERENCES agent_profiles(id),
    state            TEXT NOT NULL DEFAULT 'queued'
                     CHECK (state IN ('queued', 'running', 'done', 'failed', 'cancelled')),
    unattended       BOOLEAN NOT NULL DEFAULT false,
    approved_at      TIMESTAMPTZ,
    approved_by      INTEGER REFERENCES users(id),
    fence            BIGINT NOT NULL DEFAULT 0,
    branch           TEXT NOT NULL DEFAULT '',
    token_hash       TEXT UNIQUE,
    lease_expires_at TIMESTAMPTZ,
    attempts         INTEGER NOT NULL DEFAULT 0,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_at       TIMESTAMPTZ,
    finished_at      TIMESTAMPTZ,
    summary          TEXT NOT NULL DEFAULT ''
);
-- One live job per task: a second queued/running job for the same task is refused by the table itself.
CREATE UNIQUE INDEX IF NOT EXISTS jobs_one_live_per_task ON jobs(task_id) WHERE state IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS idx_jobs_account ON jobs(account_id, state);
CREATE INDEX IF NOT EXISTS idx_jobs_lease ON jobs(lease_expires_at) WHERE state = 'running';

CREATE TABLE IF NOT EXISTS locks (
    account_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('deploy', 'merge', 'migrate')),
    resource    TEXT NOT NULL,
    job_id      INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    fence       BIGINT NOT NULL,
    state       TEXT NOT NULL DEFAULT 'held' CHECK (state IN ('held', 'orphaned')),
    acquired_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, kind, resource)
);
CREATE INDEX IF NOT EXISTS idx_locks_job ON locks(job_id);
