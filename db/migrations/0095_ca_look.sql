-- #830: the CA's read-only `look`. A look is a row in the CA's own queue (ca_turns, kind 'look'), not a task job: it has no task, branch,
-- push or approval, and it must be claimable while the chat turn that asked for it is still running (that turn ends right after
-- asking). The same claim, fence, lease and token rules apply. The runner reads the project's repo with Read / Grep / Glob only; the
-- answer is stored as a note in the same conversation and wakes one follow-up chat turn.
ALTER TABLE ca_turns ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'chat' CHECK (kind IN ('chat', 'look'));
ALTER TABLE ca_turns ADD COLUMN IF NOT EXISTS look_project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE;
ALTER TABLE ca_turns ADD COLUMN IF NOT EXISTS look_question TEXT NOT NULL DEFAULT '';
-- one live chat turn per conversation, and one live look per conversation (they no longer exclude each other)
DROP INDEX IF EXISTS uq_ca_turns_live;
CREATE UNIQUE INDEX IF NOT EXISTS uq_ca_turns_live_chat ON ca_turns(conversation_id) WHERE state IN ('queued', 'running') AND kind = 'chat';
CREATE UNIQUE INDEX IF NOT EXISTS uq_ca_turns_live_look ON ca_turns(conversation_id) WHERE state IN ('queued', 'running') AND kind = 'look';
