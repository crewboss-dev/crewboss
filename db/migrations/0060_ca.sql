-- CA, the Crewboss Assistant (task #702, v1). A PA-style assistant in a drawer. Everything is per user (user_id on every row), so
-- multi-user (#713) only has to scope it. A chat turn is NOT a task job (no task, no branch, no worktree), so it has its own slim
-- queue, ca_turns, with the same fence/lease/token rules as jobs (app/ca.py). Numbered 0060 so it can't clash with a parallel branch.
--   ca_conversations  one chat, bound to the page + card it was started from (context). claude_session_id lets the runner --resume.
--   ca_messages       the transcript. An assistant message carries tool_summary (what the tools did), model and tokens.
--   ca_turns          the queue: one row per user message awaiting an answer. States queued -> running -> done|failed|cancelled.
--                     token_hash is the turn token (cbt_...): the MCP credential for that one turn, dead when it ends.
--   ca_cards          ask_owner questions and confirm cards shown inline; a tap resolves them (never the model).
--   ca_reminders      "tell me at 4pm": fired by the server's scheduler thread, no AI. repeat is NULL or daily|weekdays|weekly.
--   ca_notices        in-app notices (toast + drawer badge): a job finished, a reminder fired. ref de-dupes (one per job).
CREATE TABLE IF NOT EXISTS ca_conversations (
    id                SERIAL PRIMARY KEY,
    user_id           INTEGER NOT NULL REFERENCES users(id),
    account_id        INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    project_id        INTEGER REFERENCES projects(id) ON DELETE SET NULL,
    context           JSONB NOT NULL DEFAULT '{}',
    title             TEXT NOT NULL DEFAULT '',
    claude_session_id TEXT,
    session_device_id INTEGER,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    archived_at       TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_ca_conversations_user ON ca_conversations(user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS ca_turns (
    id               SERIAL PRIMARY KEY,
    conversation_id  INTEGER NOT NULL REFERENCES ca_conversations(id) ON DELETE CASCADE,
    user_id          INTEGER NOT NULL REFERENCES users(id),
    account_id       INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_message_id  INTEGER,
    state            TEXT NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'running', 'done', 'failed', 'cancelled')),
    device_id        INTEGER REFERENCES device_tokens(id),
    fence            BIGINT,
    token_hash       TEXT,
    lease_expires_at TIMESTAMPTZ,
    output           TEXT NOT NULL DEFAULT '',
    last_seq         INTEGER NOT NULL DEFAULT 0,     -- the runner numbers its answer frames; a jump = text lost, shown as "interrupted"
    gap              BOOLEAN NOT NULL DEFAULT false,
    model            TEXT NOT NULL DEFAULT '',
    error            TEXT NOT NULL DEFAULT '',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_at       TIMESTAMPTZ,
    finished_at      TIMESTAMPTZ
);
-- one live turn per conversation: a second message while the CA is answering is refused, not queued behind it
CREATE UNIQUE INDEX IF NOT EXISTS uq_ca_turns_live ON ca_turns(conversation_id) WHERE state IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS idx_ca_turns_queue ON ca_turns(user_id, account_id, state);

CREATE TABLE IF NOT EXISTS ca_messages (
    id              SERIAL PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES ca_conversations(id) ON DELETE CASCADE,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    turn_id         INTEGER REFERENCES ca_turns(id) ON DELETE SET NULL,
    role            TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content         TEXT NOT NULL DEFAULT '',
    tool_summary    JSONB NOT NULL DEFAULT '[]',
    model           TEXT NOT NULL DEFAULT '',
    tokens_in       INTEGER,
    tokens_out      INTEGER,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ca_messages_conv ON ca_messages(conversation_id, id);

CREATE TABLE IF NOT EXISTS ca_cards (
    id              SERIAL PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES ca_conversations(id) ON DELETE CASCADE,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    turn_id         INTEGER REFERENCES ca_turns(id) ON DELETE SET NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('question', 'confirm')),
    payload         JSONB NOT NULL DEFAULT '{}',
    state           TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'done', 'dismissed', 'failed')),
    result          TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_ca_cards_conv ON ca_cards(conversation_id, id);

CREATE TABLE IF NOT EXISTS ca_reminders (
    id           SERIAL PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id),
    text         TEXT NOT NULL,
    due_at       TIMESTAMPTZ NOT NULL,
    repeat       TEXT CHECK (repeat IN ('daily', 'weekdays', 'weekly')),
    sent_at      TIMESTAMPTZ,
    cancelled_at TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ca_reminders_due ON ca_reminders(due_at) WHERE cancelled_at IS NULL;

CREATE TABLE IF NOT EXISTS ca_notices (
    id         SERIAL PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id),
    kind       TEXT NOT NULL,
    ref        TEXT NOT NULL DEFAULT '',
    title      TEXT NOT NULL,
    body       TEXT NOT NULL DEFAULT '',
    url        TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    read_at    TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ca_notices_ref ON ca_notices(user_id, kind, ref) WHERE ref <> '';
CREATE INDEX IF NOT EXISTS idx_ca_notices_user ON ca_notices(user_id, id DESC);
