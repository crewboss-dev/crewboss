-- #915: planning dates. A project has named MILESTONES (a date to make); a task may belong to one milestone of its own
-- project and carries its own planning dates and a WAITING state. Additive only: nullable columns, no data rewrites.
--   milestones              id, project_id, name, due_on, note, created_by, created_at, archived_at (archived, never deleted)
--   tasks.due_on            the task's own deadline. With none, the task inherits its milestone's due_on AT READ TIME
--                           (app/planning.py effective_due), so moving a milestone moves the tasks that follow it.
--   tasks.lead_days         effort + expected waiting, in days (0..365)
--   tasks.start_by          the day work must start: stored. Computed as the effective due date - lead_days unless
--                           start_by_manual (set explicitly); app/planning.py keeps it current.
--   tasks.waiting_on/for/until   who/what the task waits on, why, and when it is expected back. Clearing waiting_on clears the
--                           other two (CHECK below).
--   tasks.start_reminded_for / chase_reminded_for   the start_by / waiting_until date a push was last sent for: one push per
--                           date, and a CHANGED date re-arms it (the check is "date <> reminded_for").
CREATE TABLE IF NOT EXISTS milestones (
    id          SERIAL PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name        TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 120 AND name !~ '[\r\n]'),
    due_on      DATE NOT NULL,
    note        TEXT NOT NULL DEFAULT '' CHECK (length(note) <= 500),
    created_by  TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    archived_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS milestones_name_one ON milestones (project_id, lower(name)) WHERE archived_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_milestones_project ON milestones(project_id);

ALTER TABLE tasks ADD COLUMN IF NOT EXISTS milestone_id INTEGER REFERENCES milestones(id) ON DELETE SET NULL;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS due_on DATE;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS lead_days INTEGER CHECK (lead_days IS NULL OR lead_days BETWEEN 0 AND 365);
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS start_by DATE;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS start_by_manual BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS waiting_on TEXT CHECK (waiting_on IS NULL OR length(waiting_on) BETWEEN 1 AND 120);
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS waiting_for TEXT CHECK (waiting_for IS NULL OR length(waiting_for) <= 200);
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS waiting_until DATE;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS start_reminded_for DATE;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS chase_reminded_for DATE;

DO $$ BEGIN
    ALTER TABLE tasks ADD CONSTRAINT tasks_waiting_together
        CHECK (waiting_on IS NOT NULL OR (waiting_for IS NULL AND waiting_until IS NULL));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE INDEX IF NOT EXISTS idx_tasks_milestone ON tasks(milestone_id) WHERE milestone_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tasks_planning ON tasks(start_by, due_on, waiting_until)
    WHERE start_by IS NOT NULL OR due_on IS NOT NULL OR waiting_until IS NOT NULL;

-- A task's milestone is one of ITS OWN project's (the same rule the schema applies to other cross-project links).
CREATE OR REPLACE FUNCTION tasks_milestone_same_project() RETURNS TRIGGER AS $$
BEGIN
    IF NEW.milestone_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM milestones m WHERE m.id = NEW.milestone_id AND m.project_id = NEW.project_id) THEN
        RAISE EXCEPTION 'a task''s milestone must belong to the task''s own project' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS tasks_milestone_same_project ON tasks;
CREATE TRIGGER tasks_milestone_same_project BEFORE INSERT OR UPDATE OF milestone_id, project_id ON tasks
    FOR EACH ROW EXECUTE FUNCTION tasks_milestone_same_project();
