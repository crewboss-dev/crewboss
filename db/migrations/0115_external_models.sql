-- #861 (phase 1): another provider's model as a reviewer or an illustrator for a job's agent. Gemini is signed in on a RUNNER (never on
-- Crewboss); a job gets its two local tools only when its runner has the capability AND its project has switched this on.
--   projects.gemini_allowed   the data boundary: off by default everywhere; only the project's Owner turns it on (as for server reads).
--                             When on, text a job chooses to send (scrubbed of secrets first) leaves the estate for Google.
--   external_model_use        one row per call. A job's tool BEGINS it (a 'pending' row, written after the switch and the fence
--                             were checked, and BEFORE anything is sent to the provider) and FINISHES it ('done', ok or not).
--                             Never holds the text or the reply, only sizes. A begin refused because the switch is off is kept as 'refused'.
--                             Survives its project and task (SET NULL), like server_read_log.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS gemini_allowed BOOLEAN NOT NULL DEFAULT false;

CREATE TABLE IF NOT EXISTS external_model_use (
    id         SERIAL PRIMARY KEY,
    job_id     INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    task_id    INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    provider   TEXT NOT NULL CHECK (provider IN ('gemini')),
    tool       TEXT NOT NULL CHECK (tool IN ('second_opinion', 'make_image')),
    model      TEXT NOT NULL DEFAULT '',
    bytes_in   INTEGER NOT NULL DEFAULT 0,
    bytes_out  INTEGER NOT NULL DEFAULT 0,
    ok         BOOLEAN NOT NULL DEFAULT false,
    state      TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'done', 'refused')),
    error      TEXT NOT NULL DEFAULT '',
    at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS external_model_use_job ON external_model_use (job_id, at);
CREATE INDEX IF NOT EXISTS external_model_use_project ON external_model_use (project_id, at DESC);
