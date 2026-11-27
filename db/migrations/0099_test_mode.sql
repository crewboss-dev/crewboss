-- #839: how a project is tested. browser = a web app (dev: runner preview, staging: a URL, production: live); installed = an app that has
-- to be installed to be tested (the runner is the test area, a task is pinned to one runner); none = no UI (tests and logs).
--   test_channel   installed apps: where the staging build goes (internal track, TestFlight, a test installer); free text, no secrets.
-- #726: require_state_declared makes the "state not declared for backup" release check a block instead of a warning (the project opts in).
ALTER TABLE projects ADD COLUMN IF NOT EXISTS test_mode TEXT NOT NULL DEFAULT 'browser';
ALTER TABLE projects ADD COLUMN IF NOT EXISTS test_channel TEXT NOT NULL DEFAULT '';
ALTER TABLE projects ADD COLUMN IF NOT EXISTS require_state_declared BOOLEAN NOT NULL DEFAULT false;
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'projects_test_mode_check') THEN
        ALTER TABLE projects ADD CONSTRAINT projects_test_mode_check CHECK (test_mode IN ('browser', 'installed', 'none'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'projects_test_channel_check') THEN
        ALTER TABLE projects ADD CONSTRAINT projects_test_channel_check CHECK (length(test_channel) <= 200);
    END IF;
END $$;
