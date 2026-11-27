-- Tasks v2 (owner's To Do / Planner model): an ASSIGNEE (the owner, or the AI session that owns the task's
-- stream) and a SUBTASK checklist the AI can propose changes to. Additive only.

ALTER TABLE tasks ADD COLUMN IF NOT EXISTS assignee TEXT NOT NULL DEFAULT 'ai'
    CHECK (assignee IN ('owner', 'ai'));
-- Existing rows: an escalation already flagged for the owner is the owner's; everything else was
-- always the stream session's work.
UPDATE tasks SET assignee = 'owner' WHERE needs_owner AND assignee = 'ai';
CREATE INDEX IF NOT EXISTS idx_tasks_assignee ON tasks(project_id, assignee, status);

CREATE TABLE IF NOT EXISTS subtasks (
    id          SERIAL PRIMARY KEY,
    task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    text        TEXT NOT NULL,
    done        BOOLEAN NOT NULL DEFAULT false,
    position    INTEGER NOT NULL DEFAULT 0,
    created_by  TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    done_at     TIMESTAMPTZ,
    done_by     TEXT,
    -- A PENDING AI proposal on this row, or NULL. {op: add|remove|edit|move, text?, position?, rationale,
    -- batch, by, at}. op=add rows are proposed-only: not part of the checklist (or its progress) until
    -- the owner accepts.
    proposal    JSONB
);
CREATE INDEX IF NOT EXISTS idx_subtasks_task ON subtasks(task_id, position);
