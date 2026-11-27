-- Local sign-in (task #706): Crewboss's own username + password + second factor sign-in (app/local_auth.py), beside the
-- optional OIDC mode. v1 has ONE owner account, but the users table stays the multi-user home.
-- A local user's `oid` is the synthetic 'local:<username>' so oid stays NOT NULL / UNIQUE and the OIDC upsert, runner
-- devices and enrolment codes (all keyed on users.id) keep working unchanged.

ALTER TABLE users ADD COLUMN IF NOT EXISTS username          TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash     TEXT;       -- argon2id; NULL = no local password
ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_secret_enc   TEXT;       -- Fernet token of the confirmed TOTP secret
ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_pending_enc  TEXT;       -- a secret shown at enrolment, not yet confirmed
ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_confirmed_at TIMESTAMPTZ;
ALTER TABLE users ADD COLUMN IF NOT EXISTS totp_last_step    BIGINT;     -- replay guard: the last accepted 30 s step
ALTER TABLE users ADD COLUMN IF NOT EXISTS session_epoch     INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_owner          BOOLEAN NOT NULL DEFAULT false;
CREATE UNIQUE INDEX IF NOT EXISTS users_username_key ON users (lower(username)) WHERE username IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS users_one_owner ON users (is_owner) WHERE is_owner;

-- Web sessions of local users: which user, the epoch it was issued under (users.session_epoch moves on "sign out
-- everywhere" / reset, ending every older session), whether it may only enrol a factor, and when a second factor was
-- last shown (sensitive account actions need one within 5 minutes). user_id is NULL for OIDC sessions.
ALTER TABLE web_sessions ADD COLUMN IF NOT EXISTS user_id          INTEGER REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE web_sessions ADD COLUMN IF NOT EXISTS epoch            INTEGER NOT NULL DEFAULT 0;
ALTER TABLE web_sessions ADD COLUMN IF NOT EXISTS enrolment_only   BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE web_sessions ADD COLUMN IF NOT EXISTS second_factor_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS web_sessions_user_idx ON web_sessions (user_id);

CREATE TABLE IF NOT EXISTS webauthn_credentials (
    id            SERIAL PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    credential_id TEXT NOT NULL UNIQUE,          -- base64url
    public_key    BYTEA NOT NULL,                -- COSE key
    sign_count    BIGINT NOT NULL DEFAULT 0,
    transports    TEXT[] NOT NULL DEFAULT '{}',
    name          TEXT NOT NULL DEFAULT '',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS webauthn_credentials_user_idx ON webauthn_credentials (user_id);

-- Only the SHA-256 of a recovery code is stored; a code works once.
CREATE TABLE IF NOT EXISTS recovery_codes (
    id         SERIAL PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code_hash  TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    used_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS recovery_codes_user_idx ON recovery_codes (user_id);

-- Failed sign-in attempts, one row per key ('ip:<ip>', 'user:<sha256 of the lower-cased username>'): the backoff reads them.
CREATE TABLE IF NOT EXISTS login_attempts (
    id  BIGSERIAL PRIMARY KEY,
    key TEXT NOT NULL,
    at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS login_attempts_key_idx ON login_attempts (key, at);

-- The first-run setup code (printed by the console command, 24 h, hashed).
CREATE TABLE IF NOT EXISTS setup_codes (
    id         SERIAL PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code_hash  TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    used_at    TIMESTAMPTZ
);

-- A local sign-in step in progress, bound to a cookie holding the token (only its hash is stored): 'login2' = password
-- accepted, second factor awaited; 'passkey_auth' / 'passkey_reg' = a WebAuthn challenge. Short-lived, used once.
CREATE TABLE IF NOT EXISTS auth_flows (
    token_hash TEXT PRIMARY KEY,
    kind       TEXT NOT NULL CHECK (kind IN ('login2', 'passkey_auth', 'passkey_reg')),
    user_id    INTEGER REFERENCES users(id) ON DELETE CASCADE,
    session_id BIGINT,
    challenge  BYTEA,
    next_path  TEXT NOT NULL DEFAULT '/',
    attempts   INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
);
