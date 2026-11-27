-- Owner-reviewed pins for the agent, command and skill files a project's repository carries (#829, replaces the frontmatter-only check).
-- Before a job starts, its runner hashes every file under any `.claude/{agents,commands,skills}/**` (root and nested) and `.crewboss/skills/**` of
-- the worktree. The job may start only if every (repo path, sha256) is PINNED here; an unknown or changed file blocks it (config.unreviewed).
-- The runner sends the unpinned files' bytes (agent_file frames) so the project Owner can READ them on the Machines page and pin them, with a
-- fresh factor. Nothing is approved by parsing: the parser only DISPLAYS what a file declares.
--   state 'pending'  seen by a runner, not reviewed (shown on the review card; pruned after 30 days)
--   state 'pinned'   the Owner read these exact bytes and pinned them (a job on this project may load the file)
-- The CHECK ties the hash to the stored bytes, so a pin can only ever mean the bytes the Owner was shown.
CREATE TABLE IF NOT EXISTS project_agent_files (
    id                SERIAL PRIMARY KEY,
    project_id        INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    path              TEXT NOT NULL CHECK (length(path) BETWEEN 1 AND 300),
    sha256            TEXT NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    content           BYTEA NOT NULL CHECK (octet_length(content) <= 98304),
    state             TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'pinned')),
    source            TEXT NOT NULL DEFAULT 'preflight' CHECK (source IN ('preflight', 'job')),
    seen_job_id       INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    seen_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    pinned_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    pinned_at         TIMESTAMPTZ,
    CONSTRAINT project_agent_files_hash CHECK (sha256 = encode(sha256(content), 'hex')),
    CONSTRAINT project_agent_files_pinned CHECK (state <> 'pinned' OR pinned_at IS NOT NULL),
    UNIQUE (project_id, path, sha256)
);
CREATE INDEX IF NOT EXISTS project_agent_files_state ON project_agent_files (project_id, state);

-- A job that created or changed agent / command / skill files is flagged (they go to the Owner for review before they can run).
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS review_paths TEXT[] NOT NULL DEFAULT '{}';
