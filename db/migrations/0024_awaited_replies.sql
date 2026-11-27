-- dash → any session, reply only when asked (owner, 2026-10-13: "Dash can communicate with any app. But no
-- communication the other way unless dash is waiting on a response"). The overseer may comment on any project's task;
-- the owning session gets an ids-only event. A comment made with await_reply opens ONE row here; the owning session's
-- next comment on that task closes it and sends dash ONE ids-only 'task_reply' event. Without an open row, a session's
-- comments never reach dash.
CREATE TABLE IF NOT EXISTS awaited_replies (
    id               SERIAL PRIMARY KEY,
    task_id          INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    comment_id       INTEGER NOT NULL REFERENCES task_comments(id) ON DELETE CASCADE,
    asked_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    replied_at       TIMESTAMPTZ,
    reply_comment_id INTEGER REFERENCES task_comments(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_awaited_replies_open ON awaited_replies(task_id) WHERE replied_at IS NULL;
