-- #987/#90 review fixes. hold_at: when a job was put on hold (a hold older than 7 days fails, the sweeper). base_branch: an earlier job's pushed branch a new
-- job starts FROM (Run again carries its commits forward; a review or the tests check out the branch under review). owner_says: the owner's "it is not
-- fixed" words, copied to the job at queue time so the brief can read them (the task's note is cleared then).
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS hold_at     TIMESTAMPTZ;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS base_branch TEXT NOT NULL DEFAULT '';
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS owner_says  TEXT;
