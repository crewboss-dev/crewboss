-- The GLOBAL PLAN (owner, 2026-10-01, task #129): dash, the overseer, keeps ONE order across every active project in
-- its own table. It never edits another project's tasks; priority changes there are triage suggestions.
-- Buckets: now (at most 3, the owner's WIP limit) / next / later. Each item carries a one-line reason.
-- pinned = the owner placed it (pin or drag): the planner may not move or drop it. today = in the short "today" plan.
CREATE TABLE IF NOT EXISTS global_plan (
    task_id     INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
    bucket      TEXT NOT NULL CHECK (bucket IN ('now', 'next', 'later')),
    position    DOUBLE PRECISION NOT NULL,
    reason      TEXT NOT NULL DEFAULT '',
    pinned      BOOLEAN NOT NULL DEFAULT false,
    today       BOOLEAN NOT NULL DEFAULT false,
    planned_by  TEXT NOT NULL,
    planned_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS global_plan_order ON global_plan (bucket, position);

-- One row per planner run (set_global_plan): when, by whom, how many items, and the short "today" note.
CREATE TABLE IF NOT EXISTS global_plan_runs (
    id          SERIAL PRIMARY KEY,
    planned_by  TEXT NOT NULL,
    items       INTEGER NOT NULL,
    today_note  TEXT NOT NULL DEFAULT '',
    planned_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The once-a-day morning re-plan nudge (Australia/Sydney): the last local date it fired.
CREATE TABLE IF NOT EXISTS global_plan_state (
    id                 BOOLEAN PRIMARY KEY DEFAULT true CHECK (id),
    last_morning_date  DATE
);
INSERT INTO global_plan_state (id) VALUES (true) ON CONFLICT DO NOTHING;
