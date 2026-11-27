-- CA parity with the ERP PA (task #727): file uploads on a conversation.
-- A person attaches screenshots (png/jpeg/webp/gif), .md/.txt and PDF to a message; the runner feeds them to Claude as real
-- image / document / text content blocks (never as files for Claude to read). STORAGE is the server's own disk (owner: "files are
-- stored on the server", no external storage): the bytes live in a plain folder (config.CA_UPLOADS_DIR, a bind mount like the
-- materials folder) as <user>/<conversation>/<id>.bin; this table is the metadata. Per person AND per conversation: a row is found by
-- (id, user_id), another person's id is a 404.
-- LIFECYCLE (owner 2026-11-09): a file on a chat linked to a TASK is kept while the task is open and stripped once the task has been
-- closed (done) for more than 30 days (reopening keeps it); a file on any other chat is stripped 90 days after the chat was last active
-- (the ERP PA's #616 fallback); one attached but never sent goes after a day. Stripping deletes the file and keeps THIS row as a stub
-- (path NULL, removed_at/removed_reason set) so the transcript still reads "shot.png - removed after task closed". Swept by
-- ca_files.sweep() in the CA scheduler pass.
CREATE TABLE IF NOT EXISTS ca_attachments (
    id              SERIAL PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES ca_conversations(id) ON DELETE CASCADE,
    user_id         INTEGER NOT NULL REFERENCES users(id),
    message_id      INTEGER REFERENCES ca_messages(id) ON DELETE SET NULL,
    filename        TEXT NOT NULL,
    mime            TEXT NOT NULL,                       -- from the CONTENT (sniffed), never the browser's claim
    kind            TEXT NOT NULL CHECK (kind IN ('image', 'pdf', 'text')),
    size            INTEGER NOT NULL,
    path            TEXT,                                -- relative to CA_UPLOADS_DIR; NULL once stripped
    removed_at      TIMESTAMPTZ,
    removed_reason  TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ca_attachments_msg ON ca_attachments(message_id);
CREATE INDEX IF NOT EXISTS idx_ca_attachments_conv ON ca_attachments(conversation_id, user_id);
CREATE INDEX IF NOT EXISTS idx_ca_attachments_created ON ca_attachments(created_at);

-- MANY CHATS PER ITEM (task #727, owner: "multiple chats attached to tasks ... its all context for that task"). A conversation may be
-- LINKED to one item (a task, change, knowledge entry, workflow or project) with an x on the chip to unlink. The link is kept beside
-- the context JSON as two plain columns so "the discussions of task 12" is an indexed lookup. Deliberately NOT unique: any number of
-- conversations, of any number of people, can link to one item; starting a chat on a linked item always makes a NEW one.
ALTER TABLE ca_conversations ADD COLUMN IF NOT EXISTS entity_kind TEXT;
ALTER TABLE ca_conversations ADD COLUMN IF NOT EXISTS entity_id   TEXT;
CREATE INDEX IF NOT EXISTS idx_ca_conversations_entity ON ca_conversations(entity_kind, entity_id) WHERE entity_kind IS NOT NULL;
UPDATE ca_conversations SET entity_kind = 'task', entity_id = context->>'task_id'
 WHERE entity_kind IS NULL AND context ? 'task_id' AND context->>'task_id' ~ '^[0-9]+$';

-- Which discussion started which job (a CA confirm card's start_job records its conversation). SET NULL: deleting a chat never
-- touches the job. Shown on both sides (the task's Discussions section, the job brief).
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS ca_conversation_id INTEGER REFERENCES ca_conversations(id) ON DELETE SET NULL;
