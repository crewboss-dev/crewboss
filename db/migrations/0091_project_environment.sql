-- #810 step 6: a project lists what a machine needs to build it (its ENVIRONMENT), and Crewboss checks a runner against that list whenever the
-- project's "Runs on" switches to it (or on "Check again"), then marks it compliant or not. Non-compliant is a warning, never a block.
--
-- project_env_requirements: one row per need. `kind` is a FIXED catalogue the RUNNER implements as built-in probes (runner/crewboss_runner/
--   envcheck.py): tool | env | android_sdk_pkg | file | keystore. The dashboard sends the runner only (kind, name, args) and the runner maps the
--   kind to its own code: it never executes a command string from here. `os` says which machines the row applies to; `install_hint` is what the
--   person is told when it is missing. `expected_fingerprint` is for a keystore: the SHA-256 certificate fingerprint (public, not a secret; the
--   key itself never reaches Crewboss).
-- project_env_checks: a check is a lightweight runner JOB (no Claude agent): queued, claimed by the runner over its verified socket, finished
--   with one short status per row. `items` keeps what the page shows (the row's kind/name/hint and the runner's status), `result` is
--   compliant | missing. The latest finished check per (project, runner) is what the badges read.
CREATE TABLE IF NOT EXISTS project_env_requirements (
    id                   SERIAL PRIMARY KEY,
    project_id           INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind                 TEXT NOT NULL CHECK (kind IN ('tool', 'env', 'android_sdk_pkg', 'file', 'keystore')),
    name                 TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 240),
    min_version          TEXT NOT NULL DEFAULT '' CHECK (min_version = '' OR min_version ~ '^[0-9]+(\.[0-9]+){0,3}$'),
    os                   TEXT NOT NULL DEFAULT 'any' CHECK (os IN ('any', 'linux', 'windows', 'macos')),
    install_hint         TEXT NOT NULL DEFAULT '' CHECK (length(install_hint) <= 300),
    expected_fingerprint TEXT NOT NULL DEFAULT '' CHECK (expected_fingerprint = '' OR expected_fingerprint ~ '^([0-9A-F]{2}:){31}[0-9A-F]{2}$'),
    source               TEXT NOT NULL DEFAULT 'manual' CHECK (source IN ('manual', 'suggested')),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS project_env_requirements_one ON project_env_requirements (project_id, kind, lower(name), os);

CREATE TABLE IF NOT EXISTS project_env_checks (
    id           SERIAL PRIMARY KEY,
    project_id   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    device_id    INTEGER NOT NULL REFERENCES device_tokens(id) ON DELETE CASCADE,
    state        TEXT NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'running', 'done')),
    result       TEXT CHECK (result IN ('compliant', 'missing')),
    items        JSONB NOT NULL DEFAULT '[]'::jsonb,
    runner_os    TEXT NOT NULL DEFAULT '',
    requested_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at   TIMESTAMPTZ,
    checked_at   TIMESTAMPTZ
);
-- at most one LIVE check per (project, runner): a second request while one waits changes nothing
CREATE UNIQUE INDEX IF NOT EXISTS project_env_checks_live ON project_env_checks (project_id, device_id) WHERE state IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS project_env_checks_latest ON project_env_checks (project_id, device_id, id DESC) WHERE state = 'done';
CREATE INDEX IF NOT EXISTS project_env_checks_device ON project_env_checks (device_id, state);
