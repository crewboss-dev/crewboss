-- Runners, phase P1 (task #627, change #50): the identity foundation. A runner is a native process on a user's PC that
-- connects OUT to the dashboard and runs Claude Code jobs. This migration adds real users, device tokens bound to ONE
-- user and ONE account, and one-time enrolment codes. Nothing here reaches the session/MCP surfaces: a device token
-- (mdr_...) is a different kind of principal (app/config.py RUNNER_KINDS).
--   users            one row per person who has signed in (Entra object id), upserted at the sign-in callback.
--   device_tokens    one row per enrolled runner. Only the token's SHA-256 is stored, and only the runner's PUBLIC key
--                    (ed25519, base64 of the raw 32 bytes); the private key never leaves the PC.
--   enrolment_codes  a code is shown once, lives at most 5 minutes (the CHECK is the backstop), works once.
--                    never matches a wrong guess (it is found BY the hash of its secret, the only thing stored).
CREATE TABLE IF NOT EXISTS users (
    id           SERIAL PRIMARY KEY,
    oid          TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL DEFAULT '',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    disabled_at  TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS device_tokens (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    account_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name        TEXT NOT NULL DEFAULT '',
    mode        TEXT NOT NULL CHECK (mode IN ('subscription', 'api')),
    token_hash  TEXT NOT NULL UNIQUE,
    pubkey      TEXT NOT NULL,
    pubkey_fp   TEXT NOT NULL,
    platform    TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL,
    revoked_at  TIMESTAMPTZ,
    revoked_by  TEXT,
    last_seen   TIMESTAMPTZ,
    last_ip     TEXT
);
CREATE INDEX IF NOT EXISTS idx_device_tokens_account ON device_tokens(account_id);
CREATE INDEX IF NOT EXISTS idx_device_tokens_user ON device_tokens(user_id);

CREATE TABLE IF NOT EXISTS enrolment_codes (
    id            SERIAL PRIMARY KEY,
    secret_hash   TEXT NOT NULL UNIQUE,
    user_id       INTEGER NOT NULL REFERENCES users(id),
    account_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    mode          TEXT NOT NULL CHECK (mode IN ('subscription', 'api')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ NOT NULL,
    used_at       TIMESTAMPTZ,
    CONSTRAINT enrolment_codes_max_5_min CHECK (expires_at <= created_at + interval '5 minutes')
);
