-- Every question a session puts to the owner lives on a task (owner, 2026-10-01): the question, the context the
-- owner needs to answer it cold, the choices (if it's a simple multiple choice) and the answer, whichever way it
-- came back (the dashboard, or the owner replying in the Claude session, which the session then records).
CREATE TABLE IF NOT EXISTS task_questions (
    id            SERIAL PRIMARY KEY,
    task_id       INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    asked_by      TEXT NOT NULL,
    question      TEXT NOT NULL,
    context       TEXT NOT NULL DEFAULT '',
    options       JSONB,
    answer        TEXT,
    answered_via  TEXT CHECK (answered_via IN ('dashboard', 'session')),
    asked_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    answered_at   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS task_questions_task ON task_questions (task_id);
CREATE INDEX IF NOT EXISTS task_questions_open ON task_questions (asked_at) WHERE answered_at IS NULL;
