-- #809: identifier-first sign-in and 30-day sessions.
-- users.email is a SIGN-IN NAME only (the identifier step matches username or email); Crewboss never sends anything to it.
-- Optional, set from /account/security after a second-factor re-confirm; one address per user, compared case-insensitively.
ALTER TABLE users ADD COLUMN IF NOT EXISTS email TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS users_email_lower_key ON users (lower(email)) WHERE email IS NOT NULL;

-- The identifier step keeps its state server-side like the other sign-in steps: kind 'login_id' = "identifier given, now a
-- passkey or the password". user_id is NULL for an unknown or disabled identifier, so the flow looks and counts the same.
ALTER TABLE auth_flows ADD COLUMN IF NOT EXISTS identifier TEXT NOT NULL DEFAULT '';
ALTER TABLE auth_flows DROP CONSTRAINT IF EXISTS auth_flows_kind_check;
ALTER TABLE auth_flows ADD CONSTRAINT auth_flows_kind_check CHECK (kind IN ('login2', 'passkey_auth', 'passkey_reg', 'login_id'));
