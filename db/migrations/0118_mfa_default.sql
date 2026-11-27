-- #976 slice A (owner q826, 2026-11-27): password + authenticator app (TOTP) + recovery codes is the default sign-in in every mode;
-- passkeys are an optional "passwordless sign-in" upgrade. This reverses #809's "no password".

-- The restricted (enrolment-only) session now walks password -> totp -> codes. 'passkey' stays allowed for sessions already in flight
-- (the code treats it as 'password').
ALTER TABLE web_sessions ALTER COLUMN enrol_stage SET DEFAULT 'password';
ALTER TABLE web_sessions DROP CONSTRAINT IF EXISTS web_sessions_enrol_stage_check;
ALTER TABLE web_sessions ADD CONSTRAINT web_sessions_enrol_stage_check CHECK (enrol_stage IN ('password', 'totp', 'codes', 'passkey'));

-- Each passkey is bound to the RP ID (web address) it was made under. NULL = made before this change: local_auth.backfill_rp_id()
-- stamps it with the current public host at start-up (every existing credential was made under it).
ALTER TABLE webauthn_credentials ADD COLUMN IF NOT EXISTS rp_id TEXT;

-- The one-time "add a password and authenticator app as a backup" nudge for someone who signs in with a passkey only.
ALTER TABLE users ADD COLUMN IF NOT EXISTS mfa_nudged_at TIMESTAMPTZ;

-- Consecutive wrong authenticator codes for a person whose password was right: NO time window, reset only by a successful second factor
-- (a code, or a recovery code) or an admin's Reset sign-in. Soft lock with a growing delay from 10, hard lock at 100.
ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_fail_streak INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_fail_at TIMESTAMPTZ;
