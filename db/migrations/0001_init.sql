-- mallen-dashboard schema. Own database, never the ERP's.

CREATE TABLE IF NOT EXISTS projects (
    id           SERIAL PRIMARY KEY,
    slug         TEXT NOT NULL UNIQUE,
    name         TEXT NOT NULL,
    type         TEXT NOT NULL CHECK (type IN ('internal', 'client')),
    archived_at  TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS subprojects (
    id           SERIAL PRIMARY KEY,
    project_id   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project_id, name)
);

CREATE TABLE IF NOT EXISTS tasks (
    id           SERIAL PRIMARY KEY,
    project_id   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title        TEXT NOT NULL,
    body         TEXT NOT NULL DEFAULT '',
    priority     TEXT NOT NULL CHECK (priority IN ('must', 'should', 'could', 'wont')) DEFAULT 'should',
    status       TEXT NOT NULL CHECK (status IN ('todo', 'doing', 'testing', 'done')) DEFAULT 'todo',
    stream       TEXT NOT NULL CHECK (stream IN ('feature', 'bug', 'tweak', 'review', 'error')),
    owner_stream TEXT NOT NULL,
    needs_owner  BOOLEAN NOT NULL DEFAULT false,
    subprojects  TEXT[] NOT NULL DEFAULT '{}',
    links        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by   TEXT NOT NULL,
    updated_by   TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    done_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_stream ON tasks(stream);

CREATE TABLE IF NOT EXISTS task_comments (
    id           SERIAL PRIMARY KEY,
    task_id      INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    author       TEXT NOT NULL,
    body         TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_task_comments_task ON task_comments(task_id);

CREATE TABLE IF NOT EXISTS knowledge (
    id           SERIAL PRIMARY KEY,
    project_id   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL CHECK (kind IN ('decision', 'fact', 'note', 'link')),
    title        TEXT NOT NULL,
    body         TEXT NOT NULL DEFAULT '',
    subprojects  TEXT[] NOT NULL DEFAULT '{}',
    source       TEXT NOT NULL DEFAULT '',
    needs_review BOOLEAN NOT NULL DEFAULT false,
    created_by   TEXT NOT NULL,
    updated_by   TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_knowledge_project ON knowledge(project_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_kind ON knowledge(kind);

CREATE TABLE IF NOT EXISTS activity (
    id           SERIAL PRIMARY KEY,
    project_id   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    actor        TEXT NOT NULL,
    verb         TEXT NOT NULL,
    entity       TEXT NOT NULL,
    entity_id    INTEGER,
    summary      TEXT NOT NULL,
    at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_activity_project ON activity(project_id);
CREATE INDEX IF NOT EXISTS idx_activity_at ON activity(at DESC);

CREATE TABLE IF NOT EXISTS outbox (
    id            SERIAL PRIMARY KEY,
    stream        TEXT NOT NULL CHECK (stream IN ('features', 'bugs', 'tweaks', 'reviewer', 'errors')),
    project_id    INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    task_id       INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL DEFAULT 'task_update',
    message       TEXT NOT NULL,
    dedupe_key    TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at  TIMESTAMPTZ,
    delivered_via TEXT CHECK (delivered_via IN ('hook', 'tmux', 'mcp', 'channel')),
    attempts      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_outbox_stream_undelivered ON outbox(stream) WHERE delivered_at IS NULL;
-- Same repeating error must not flood a session: one live (undelivered) row per (stream, dedupe_key).
CREATE UNIQUE INDEX IF NOT EXISTS idx_outbox_dedupe_undelivered ON outbox(stream, dedupe_key) WHERE delivered_at IS NULL AND dedupe_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS session_tokens (
    id            SERIAL PRIMARY KEY,
    stream        TEXT NOT NULL CHECK (stream IN ('features', 'bugs', 'tweaks', 'reviewer', 'errors')),
    name          TEXT NOT NULL,
    token_hash    TEXT NOT NULL UNIQUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS catalog_scans (
    id             SERIAL PRIMARY KEY,
    project_id     INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    commit         TEXT NOT NULL,
    generated_at   TIMESTAMPTZ NOT NULL,
    catalog        JSONB NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_catalog_scans_project ON catalog_scans(project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS catalog_diffs (
    id             SERIAL PRIMARY KEY,
    project_id     INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    from_scan_id   INTEGER REFERENCES catalog_scans(id) ON DELETE CASCADE,
    to_scan_id     INTEGER NOT NULL REFERENCES catalog_scans(id) ON DELETE CASCADE,
    workflow_id    TEXT NOT NULL,
    workflow_name  TEXT NOT NULL,
    change_type    TEXT NOT NULL CHECK (change_type IN ('added', 'removed', 'modified')),
    detail         JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_catalog_diffs_project ON catalog_diffs(project_id, created_at DESC);
