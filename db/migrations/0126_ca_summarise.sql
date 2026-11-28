-- #91: the task window's AI summary is written by a runner turn, kind 'summarise', in the CA's own queue (ca_turns): the same claim, fence, lease and
-- expiry as a chat turn or a look, but it belongs to a TASK, not a conversation. summary_input is the scrubbed facts the model is given (untrusted data).
-- pin_device_id keeps the turn on a runner the project allocated (#987) when it has any. 0125 is #987's (follow-up hold).
ALTER TABLE ca_turns ALTER COLUMN conversation_id DROP NOT NULL;
ALTER TABLE ca_turns DROP CONSTRAINT IF EXISTS ca_turns_kind_check;
ALTER TABLE ca_turns ADD CONSTRAINT ca_turns_kind_check CHECK (kind IN ('chat', 'look', 'summarise'));
ALTER TABLE ca_turns ADD COLUMN IF NOT EXISTS summary_task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE;
ALTER TABLE ca_turns ADD COLUMN IF NOT EXISTS summary_input TEXT NOT NULL DEFAULT '';
ALTER TABLE ca_turns ADD COLUMN IF NOT EXISTS pin_device_id INTEGER REFERENCES device_tokens(id) ON DELETE SET NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_ca_turns_live_summarise ON ca_turns(summary_task_id) WHERE state IN ('queued', 'running') AND kind = 'summarise';
CREATE INDEX IF NOT EXISTS idx_ca_turns_summarise_recent ON ca_turns(summary_task_id, created_at) WHERE kind = 'summarise';
