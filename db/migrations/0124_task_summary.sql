-- #91: the task window's 3-line summary (What / Done / Left). summary_ai holds {"ai": {what,done,left}, "owner": {..}|null, "restore": {..}|null}:
-- the AI's text, the owner's edit (shown until a NEW event arrives) and the owner's last version kept for "Restore your version".
-- Numbered 0124: #987 has 0121 and 0122, #89 has 0123 (on their branches).
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS summary_ai JSONB;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS summary_owner_edited_at TIMESTAMPTZ;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS summary_updated_at TIMESTAMPTZ;
