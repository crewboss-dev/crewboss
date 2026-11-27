-- Project tree (owner, 2026-10-01; dashboard k30, task #132) and the Change log (k33, task #134).

-- THE TREE. dash (config.OVERSEER_PROJECT) is the root and has no parent. Level 1 under dash = ERP ACCOUNTS
-- (kind 'account'; Mallen first, client accounts once they have a project) and top-level personal projects;
-- below an account, its projects (kind 'project'), which may have children of their own (the ERP → portals…).
-- A project = a repo, or a folder inside one (repo_path). Cycles and illegal moves are refused in app code.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS parent_id INTEGER REFERENCES projects(id) ON DELETE RESTRICT;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'project';
ALTER TABLE projects DROP CONSTRAINT IF EXISTS projects_kind_check;
ALTER TABLE projects ADD CONSTRAINT projects_kind_check CHECK (kind IN ('account', 'project'));
ALTER TABLE projects ADD COLUMN IF NOT EXISTS erp_account_id TEXT;        -- accounts: the ERP account they mirror
ALTER TABLE projects ADD COLUMN IF NOT EXISTS note TEXT NOT NULL DEFAULT '';
ALTER TABLE projects ADD COLUMN IF NOT EXISTS repo TEXT NOT NULL DEFAULT '';
ALTER TABLE projects ADD COLUMN IF NOT EXISTS repo_path TEXT NOT NULL DEFAULT '';
-- Dev and prod kept in step (the later dev → prod promotion feature reads this; no logic yet).
ALTER TABLE projects ADD COLUMN IF NOT EXISTS dev_prod_sync BOOLEAN NOT NULL DEFAULT false;
CREATE INDEX IF NOT EXISTS projects_parent ON projects (parent_id);

-- Environments are OPTIONAL per project: most are worked on live (prod only); the ERP and the AiOS have dev + prod.
CREATE TABLE IF NOT EXISTS project_environments (
    project_id     INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    env            TEXT NOT NULL CHECK (env IN ('dev', 'prod')),
    server         TEXT NOT NULL DEFAULT '',
    path           TEXT NOT NULL DEFAULT '',
    url            TEXT NOT NULL DEFAULT '',
    deploy_method  TEXT NOT NULL DEFAULT '',
    zabbix_host    TEXT NOT NULL DEFAULT '',
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, env)
);

-- THE CHANGE LOG: every significant change is Recommended → Decided → Done, recorded BEFORE it is made.
CREATE TABLE IF NOT EXISTS change_log (
    id              SERIAL PRIMARY KEY,
    project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title           TEXT NOT NULL,
    recommendation  TEXT NOT NULL DEFAULT '',
    options         JSONB,
    recommended_by  TEXT NOT NULL,
    stage           TEXT NOT NULL DEFAULT 'recommended' CHECK (stage IN ('recommended', 'decided', 'done')),
    decision        TEXT CHECK (decision IN ('approved', 'rejected', 'changed')),
    decision_note   TEXT NOT NULL DEFAULT '',
    decided_by      TEXT,
    decided_at      TIMESTAMPTZ,
    links           JSONB NOT NULL DEFAULT '{}'::jsonb,
    done_at         TIMESTAMPTZ,
    verified        BOOLEAN NOT NULL DEFAULT false,
    backfill_key    TEXT UNIQUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS change_log_project_stage ON change_log (project_id, stage);
