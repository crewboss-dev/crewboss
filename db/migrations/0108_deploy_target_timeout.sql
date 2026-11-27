-- Per-target deploy time limit (#829 item 5): building a big image takes longer than the 600 s default.
-- NULL = the instance default (CREWBOSS_DEPLOY_TIMEOUT_SECONDS, 600 s): every existing target keeps today's behaviour.
ALTER TABLE deploy_targets ADD COLUMN IF NOT EXISTS timeout_seconds INTEGER;
DO $$ BEGIN
    ALTER TABLE deploy_targets ADD CONSTRAINT deploy_targets_timeout_range CHECK (timeout_seconds IS NULL OR timeout_seconds BETWEEN 60 AND 7200);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
