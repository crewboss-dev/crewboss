-- #581 review: decisions a session records in chat are VERIFIED against the owner's own prompts (UserPromptSubmit hook),
-- the owner acknowledges each one, and an override keeps the session's quote and name as evidence.
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS words_verified BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS acknowledged_at TIMESTAMPTZ;
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS superseded_owner_words TEXT NOT NULL DEFAULT '';
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS superseded_by TEXT NOT NULL DEFAULT '';

-- TRANSIENT: the owner's recent prompts per session token. Kept ~30 minutes (at most 200 per token, each under 2,000
-- characters), deleted on every insert and read and by the periodic sweep. Normalised text, never shown anywhere.
-- Not worth backing up: exclude this table from any DB dump (the dashboard repo has no backup script of its own).
CREATE TABLE IF NOT EXISTS owner_prompts (
    id          BIGSERIAL PRIMARY KEY,
    token_id    INTEGER NOT NULL REFERENCES session_tokens(id) ON DELETE CASCADE,
    prompt      TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS owner_prompts_token ON owner_prompts (token_id, created_at);
