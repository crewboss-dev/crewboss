-- #830: a task's Discussion. A conversation bound to a task (ca_conversations.entity_kind = 'task') is read by everyone who can view the
-- task and written by everyone who can edit it, so its access is the task's access (checked at every call, never stored): moving the task
-- to another project moves the access with it. A GLOBAL (unbound) conversation stays its starter's own; when one of its CA tools changes a
-- task (a comment, a tick, a new task, an answer, a job) it is LINKED here so the task's history lists it. A link row shows the others only
-- who and when, never the chat's title or text.
-- `shared` marks a task conversation that belongs to the task (new ones). A conversation bound to a task BEFORE #830 stays false: its
-- starter's own, shown to the others only as a soft link, until the starter chooses "Share with the task".
ALTER TABLE ca_conversations ADD COLUMN IF NOT EXISTS shared BOOLEAN NOT NULL DEFAULT false;
CREATE TABLE IF NOT EXISTS ca_conversation_links (
    conversation_id INTEGER NOT NULL REFERENCES ca_conversations(id) ON DELETE CASCADE,
    task_id         INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (conversation_id, task_id)
);
CREATE INDEX IF NOT EXISTS idx_ca_links_task ON ca_conversation_links(task_id);
