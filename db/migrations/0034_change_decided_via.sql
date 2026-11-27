-- A change can be decided by the owner in a conversation with a session (record_decision), not only on the
-- Changes page. Remember where the decision came from and the owner's own words.
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS decided_via TEXT NOT NULL DEFAULT 'dashboard';
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS owner_words TEXT NOT NULL DEFAULT '';
