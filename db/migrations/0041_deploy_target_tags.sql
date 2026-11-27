-- Servers page = deploy targets (task #648, change #52): two tags the page shows as chips and filters on.
--   deploy_targets.env           which environment a target deploys to: dev | prod (a person sets it; default prod)
--   commissioned_servers.reach   how the box is reached from outside: internal | external (a person sets it; default internal)
ALTER TABLE deploy_targets ADD COLUMN IF NOT EXISTS env TEXT NOT NULL DEFAULT 'prod';
ALTER TABLE commissioned_servers ADD COLUMN IF NOT EXISTS reach TEXT NOT NULL DEFAULT 'internal';
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'deploy_targets_env_check') THEN
        ALTER TABLE deploy_targets ADD CONSTRAINT deploy_targets_env_check CHECK (env IN ('dev', 'prod'));
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'commissioned_servers_reach_check') THEN
        ALTER TABLE commissioned_servers ADD CONSTRAINT commissioned_servers_reach_check CHECK (reach IN ('internal', 'external'));
    END IF;
END $$;
