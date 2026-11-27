-- #809 (owner's final spec, 11-16): passkey-only sign-in. A password exists only as a ONE-TIME TEMPORARY password an admin hands over
-- (server-generated, 24 h, single use); signing in with it allows nothing but adding a passkey, after which no password remains.
ALTER TABLE users ADD COLUMN IF NOT EXISTS password_temp       BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE users ADD COLUMN IF NOT EXISTS password_expires_at TIMESTAMPTZ;

-- A restricted (enrolment-only) session knows WHY it is restricted ('password' = signed in with a temporary or old password,
-- 'recovery' = signed in with a recovery code) and how far along it is: 'passkey' = must add a passkey, 'codes' = passkey added,
-- recovery codes next. It becomes a normal session only when the person ticks "I've saved them".
ALTER TABLE web_sessions ADD COLUMN IF NOT EXISTS enrol_kind  TEXT;
ALTER TABLE web_sessions ADD COLUMN IF NOT EXISTS enrol_stage TEXT NOT NULL DEFAULT 'passkey';
ALTER TABLE web_sessions DROP CONSTRAINT IF EXISTS web_sessions_enrol_kind_check;
ALTER TABLE web_sessions ADD CONSTRAINT web_sessions_enrol_kind_check CHECK (enrol_kind IS NULL OR enrol_kind IN ('password', 'recovery'));
ALTER TABLE web_sessions DROP CONSTRAINT IF EXISTS web_sessions_enrol_stage_check;
ALTER TABLE web_sessions ADD CONSTRAINT web_sessions_enrol_stage_check CHECK (enrol_stage IN ('passkey', 'codes'));

-- Anyone who already holds a passkey no longer has a password. (TOTP columns stay as they are, unread by sign-in except for the one
-- last password + authenticator-code sign-in of a person who has a password and an authenticator but no passkey yet.)
UPDATE users SET password_hash = NULL, password_temp = false, password_expires_at = NULL
 WHERE password_hash IS NOT NULL AND EXISTS (SELECT 1 FROM webauthn_credentials w WHERE w.user_id = users.id);

-- Instance-wide settings the owner edits in Administration (#809: "Sign-in lasts"). A missing row = the built-in default.
CREATE TABLE IF NOT EXISTS instance_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by INTEGER REFERENCES users(id) ON DELETE SET NULL
);
