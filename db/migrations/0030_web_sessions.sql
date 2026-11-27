-- Dashboard sign-in (task #394): Microsoft (Entra) OIDC for the owner's pages; the rules are app/web_auth.py.

-- One row per signed-in browser. Only the hash of the cookie's token is stored. A session ends at expires_at (sign-in
-- + 120 min, owner q296) whatever its use; the gate also re-checks the oid against the server's super admin allowlist
-- on every request. auth_time (when Microsoft last authenticated the person) is kept for the 15-minute re-prompt
-- before a server Connect (#484/#485).
CREATE TABLE IF NOT EXISTS web_sessions (
    id            BIGSERIAL PRIMARY KEY,
    token_hash    TEXT NOT NULL UNIQUE,
    oid           TEXT NOT NULL,
    name          TEXT NOT NULL DEFAULT '',
    username      TEXT NOT NULL DEFAULT '',
    amr           TEXT[] NOT NULL DEFAULT '{}',
    auth_time     TIMESTAMPTZ NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ NOT NULL,
    last_seen_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    ip            TEXT NOT NULL DEFAULT '',
    user_agent    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS web_sessions_expires_idx ON web_sessions (expires_at);

-- A sign-in in progress: the hash of its state (the browser holds the state itself in a short-lived cookie), the
-- nonce the ID token must carry, the PKCE verifier, and where to go afterwards. Used once, then deleted.
CREATE TABLE IF NOT EXISTS oidc_flows (
    state_hash     TEXT PRIMARY KEY,
    nonce          TEXT NOT NULL,
    code_verifier  TEXT NOT NULL,
    next_path      TEXT NOT NULL DEFAULT '/',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
