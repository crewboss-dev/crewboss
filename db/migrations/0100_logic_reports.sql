-- #715 (change #67): the logic map. An app keeps its workflows in .crewboss/logic/*.yaml; a job (or CI) runs `crewboss logic report` on a
-- commit and posts the result here. One row per (project, commit): the normalised map, the schema errors, the STALE code refs and the
-- UNCOVERED entry points found in the code. The release gate reads the row of the commit being released.
CREATE TABLE IF NOT EXISTS logic_reports (
    id          SERIAL PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    sha         TEXT NOT NULL CHECK (sha ~ '^[0-9a-f]{40}$'),
    workflows   JSONB NOT NULL DEFAULT '[]'::jsonb,
    errors      JSONB NOT NULL DEFAULT '[]'::jsonb,
    stale       JSONB NOT NULL DEFAULT '[]'::jsonb,
    uncovered   JSONB NOT NULL DEFAULT '[]'::jsonb,
    discovered  INTEGER NOT NULL DEFAULT 0,
    reported_by TEXT NOT NULL DEFAULT '',          -- 'job:<id>', or the token / person the API named
    locked_at   TIMESTAMPTZ,                       -- set when the gate judges the commit: the report is frozen from then on
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project_id, sha)
);
CREATE INDEX IF NOT EXISTS logic_reports_project ON logic_reports (project_id, created_at DESC);

-- The gate is per project: 'off' (the report is shown, nothing refuses) or 'enforce' (a production release is refused when the map
-- is invalid or stale, or when it adds uncovered entry points compared with the last release). Existing projects start 'off'.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS logic_gate TEXT NOT NULL DEFAULT 'off' CHECK (logic_gate IN ('off', 'enforce'));
