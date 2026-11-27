-- #861: what Crewboss knows about a runner's Google key file when the last change was not confirmed.
--   NULL          the record (fingerprint, last four, set_at) is what the runner confirmed, or there is no key sent from here
--   'unconfirmed' the runner did not answer the last set or remove in time: the file may or may not have changed
--   'unknown'     the runner answered with a different fingerprint than the one sent: the key was cleared again; paste it again
-- The key itself is never stored (see 0116).
ALTER TABLE device_tokens ADD COLUMN IF NOT EXISTS gemini_key_state TEXT CHECK (gemini_key_state IN ('unconfirmed', 'unknown'));
