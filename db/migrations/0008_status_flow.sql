-- Status flow v2 (owner 2026-09-30, the ADHD-friendly Focus / Board / Plan views):
--   todo|doing|testing|done  ->  backlog|ready|doing|review|done
-- A new request lands in BACKLOG; the owner accepting (or overriding) the AI's triage makes it READY;
-- work starts -> DOING; the AI finishes and needs the owner's test/OK -> REVIEW; DONE.
-- Mapping: todo -> ready when its triage was already decided (accepted/overridden), else backlog;
-- testing -> review; doing/done unchanged.
ALTER TABLE tasks DROP CONSTRAINT IF EXISTS tasks_status_check;
UPDATE tasks SET status = CASE
    WHEN status = 'todo' AND triage_state IN ('accepted', 'overridden') THEN 'ready'
    WHEN status = 'todo' THEN 'backlog'
    WHEN status = 'testing' THEN 'review'
    ELSE status END
WHERE status IN ('todo', 'testing');
ALTER TABLE tasks ADD CONSTRAINT tasks_status_check
    CHECK (status IN ('backlog', 'ready', 'doing', 'review', 'done'));
ALTER TABLE tasks ALTER COLUMN status SET DEFAULT 'backlog';

-- Focus reads the owner's Now / Up next / Review / Done today across projects.
CREATE INDEX IF NOT EXISTS idx_tasks_assignee_status ON tasks(assignee, status);
CREATE INDEX IF NOT EXISTS idx_tasks_done_at ON tasks(done_at) WHERE status = 'done';
