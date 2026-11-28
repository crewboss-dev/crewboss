-- #990 (change #90): the agent's finish call carries, additively, what it did next to the summary: next steps, what is left, the checks it ran.
-- Old runners and old agents send none of them: NULL / empty = nothing said. A requeue clears them (the next attempt says its own).
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS next_steps    JSONB;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS remaining_work JSONB;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS tests_run     TEXT NOT NULL DEFAULT '';
-- "Needs more work" on a task: the owner's words (CA, or the box on the outcome card). Cleared when the next job is queued.
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS needs_work_note TEXT;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS needs_work_at   TIMESTAMPTZ;
-- Runners belong to the PROJECT (#79 settled by #90): the runners allocated to it. No row = every runner of the starter's account may take its jobs (as today).
CREATE TABLE IF NOT EXISTS project_runners (
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    device_id  INTEGER NOT NULL REFERENCES device_tokens(id) ON DELETE CASCADE,
    added_by   TEXT NOT NULL DEFAULT '',
    added_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, device_id)
);
CREATE INDEX IF NOT EXISTS idx_project_runners_device ON project_runners(device_id);
