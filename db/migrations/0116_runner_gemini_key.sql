-- #861: a Google API key can be sent to a runner from its Machines card. Crewboss NEVER stores the key: it passes it once over the runner's
-- authenticated socket and keeps only what is needed to say "key ending ...abcd" and to see which key a runner holds.
--   gemini_key_fp       first 8 hex characters of the key's sha256 (the runner returns the same value, which the server checks)
--   gemini_key_last4    the last four characters of the key
--   gemini_key_set_at   when it was last set; all three are NULL when the key was removed or never sent from here
-- A key placed by hand in the runner's key file is not recorded here (the runner reports it can use Gemini, nothing more).
ALTER TABLE device_tokens ADD COLUMN IF NOT EXISTS gemini_key_fp TEXT;
ALTER TABLE device_tokens ADD COLUMN IF NOT EXISTS gemini_key_last4 TEXT;
ALTER TABLE device_tokens ADD COLUMN IF NOT EXISTS gemini_key_set_at TIMESTAMPTZ;
