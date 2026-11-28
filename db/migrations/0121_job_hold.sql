-- #987: a job a runner gave back because the project's agent files are not reviewed is WAITING ON A PERSON, not failed. It stays 'queued'
-- (so one live job per task still holds) but carries a hold the claim skips; the attempt is not counted. Pinning the last pending file
-- of the project clears the hold. Additive: a NULL hold_reason is every job that exists today.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS hold_reason TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS hold_count INTEGER;
