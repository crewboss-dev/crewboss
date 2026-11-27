-- Release governance (change #77, task #713): dev first, production by promotion only, an Admin approves.
--   deploy_targets.env     dev | staging | prod ('production' is accepted as an alias of prod: app/deploys.clean_env). Existing
--                          targets keep their value.
--   server_jobs            env (copied from the target at request time), hotfix + reason (an explicit flag, audited), and the
--                          follow-up task that merges a hotfix back. Who asked and who approved are requested_by / decided_by
--                          (a real users.id); auto_approved marks a dev/staging deploy nobody had to approve.
--   merge_requests         a branch that wants to reach main: tests green + an independent code-auditor pass (recorded here, on
--                          the job that did the work) + an Admin's approval. The dashboard records and gates; the merge itself
--                          is done by the job / runner afterwards (state 'merged').
ALTER TABLE deploy_targets DROP CONSTRAINT IF EXISTS deploy_targets_env_check;
ALTER TABLE deploy_targets ADD CONSTRAINT deploy_targets_env_check CHECK (env IN ('dev', 'staging', 'prod'));

ALTER TABLE server_jobs ADD COLUMN IF NOT EXISTS env TEXT NOT NULL DEFAULT 'prod';
-- admins_at_request: how many Admins the project had when this was asked, so removing an Admin can't turn a two-person rule into a
-- solo self-approval.
ALTER TABLE server_jobs ADD COLUMN IF NOT EXISTS admins_at_request INTEGER NOT NULL DEFAULT 0;
ALTER TABLE server_jobs ADD COLUMN IF NOT EXISTS hotfix BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE server_jobs ADD COLUMN IF NOT EXISTS hotfix_reason TEXT NOT NULL DEFAULT '';
-- release_policy: 'standard' (dev or staging first, then production) or 'production_only' (an app with no dev environment, such as a
-- website: a successful RUNNER PREVIEW run of the exact commit stands in for dev). A preview run is a server_jobs row with env =
-- 'preview', no target and state 'done', so promotion checks one table. rollback marks a request to put back the previous release.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS release_policy TEXT NOT NULL DEFAULT 'standard';
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'projects_release_policy_check') THEN
        ALTER TABLE projects ADD CONSTRAINT projects_release_policy_check CHECK (release_policy IN ('standard', 'production_only'));
    END IF;
END $$;
ALTER TABLE server_jobs ADD COLUMN IF NOT EXISTS rollback BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE server_jobs ADD COLUMN IF NOT EXISTS tag TEXT NOT NULL DEFAULT '';
ALTER TABLE server_jobs DROP CONSTRAINT IF EXISTS server_jobs_env_check;
ALTER TABLE server_jobs ADD CONSTRAINT server_jobs_env_check CHECK (env IN ('dev', 'staging', 'prod', 'preview'));
ALTER TABLE server_jobs ADD COLUMN IF NOT EXISTS followup_task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL;
UPDATE server_jobs s SET env = t.env FROM deploy_targets t WHERE t.id = s.target_id;

CREATE TABLE IF NOT EXISTS merge_requests (
    id               SERIAL PRIMARY KEY,
    project_id       INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    job_id           INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    branch           TEXT NOT NULL CHECK (branch ~ '^[A-Za-z0-9][A-Za-z0-9._/-]{0,100}$'),
    sha              TEXT NOT NULL CHECK (sha ~ '^[0-9a-f]{40}$'),
    hotfix           BOOLEAN NOT NULL DEFAULT false,
    admins_at_request INTEGER NOT NULL DEFAULT 0,
    state            TEXT NOT NULL DEFAULT 'requested' CHECK (state IN ('requested', 'approved', 'refused', 'merged')),
    tests_green_at   TIMESTAMPTZ,
    audit_passed_at  TIMESTAMPTZ,
    audit_by         TEXT NOT NULL DEFAULT '',
    audit_note       TEXT NOT NULL DEFAULT '',
    requested_by     INTEGER REFERENCES users(id) ON DELETE SET NULL,
    requested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_by       INTEGER REFERENCES users(id) ON DELETE SET NULL,
    decided_at       TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_merge_requests_project ON merge_requests(project_id, state);
