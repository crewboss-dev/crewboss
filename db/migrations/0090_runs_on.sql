-- #810 step 5: "Runs on". A person picks which of THEIR runners a project's jobs go to; saved per person per project (owner q606), because a
-- runner belongs to one person and a job only ever goes to its starter's runners.
--   project_runner_prefs   (project, person) -> runner. Revoking a runner drops the row (ON DELETE CASCADE only fires on a real delete; a
--                          revoked runner reads as "any" in app/runs_on.py get_pref).
--   jobs.pin_device_id     the runner the job was started for (NULL = any of the starter's). The claim rule gains
--                          `pin_device_id IS NULL OR pin_device_id = d.id` (app/jobs.py _ELIGIBLE_WHERE: one rule for claim and nudges).
-- A job is NEVER moved by the system: the PC may hold a phone, a USB key or local state. A person's click moves it (runs_on.repin).
CREATE TABLE IF NOT EXISTS project_runner_prefs (
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_id  INTEGER NOT NULL REFERENCES device_tokens(id) ON DELETE CASCADE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_project_runner_prefs_device ON project_runner_prefs(device_id);
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS pin_device_id INTEGER REFERENCES device_tokens(id) ON DELETE SET NULL;
