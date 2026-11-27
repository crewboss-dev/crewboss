-- #810 step 3: a cheap, login-free probe of every direct server, so the Machines page can say "Ready", "Not answering" or "Needs setup"
-- without signing a pass. The sweeper runs `ssh-keyscan` against the pinned key type every few minutes and compares the key it gets with
-- the pinned one (app/server_access.py). Nothing here is a secret.
--   last_probe_at     when the last probe finished (NULL: never probed, e.g. just commissioned: the check itself proved it)
--   probe_ok          did the last probe answer with the pinned key (NULL: never probed)
--   probe_misses      consecutive probes that did not answer (2 = "Not answering")
--   unreachable_since when the run of misses began (60 minutes of it = Needs setup)
--   probe_error       the last probe's reason, short
ALTER TABLE commissioned_servers ADD COLUMN IF NOT EXISTS last_probe_at TIMESTAMPTZ;
ALTER TABLE commissioned_servers ADD COLUMN IF NOT EXISTS probe_ok BOOLEAN;
ALTER TABLE commissioned_servers ADD COLUMN IF NOT EXISTS probe_misses INTEGER NOT NULL DEFAULT 0 CHECK (probe_misses >= 0);
ALTER TABLE commissioned_servers ADD COLUMN IF NOT EXISTS unreachable_since TIMESTAMPTZ;
ALTER TABLE commissioned_servers ADD COLUMN IF NOT EXISTS probe_error TEXT NOT NULL DEFAULT '' CHECK (length(probe_error) <= 300);
