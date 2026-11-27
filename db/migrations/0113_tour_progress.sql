-- #938: the first-run how-to tour. One row per PERSON (user_id NULL = the sign-in-off owner, matched with COALESCE like project_favourites):
--   status     'active' (started or restarted, not done), 'finished' (reached the end) or 'skipped' (closed early)
--   last_step  the id of the last step reached (ids live in app/tour_steps.yaml); '' before the first step is saved
-- No row = the person has never seen the tour, so it starts by itself on their first page. ONLY NEW PEOPLE get that (owner, q744): the backfill at the foot
-- of this file marks every user that already exists as finished. They can still open it from How-to in the profile menu. Progress is server-side so it follows them across devices.
CREATE TABLE IF NOT EXISTS tour_progress (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER REFERENCES users(id) ON DELETE CASCADE,
    status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'finished', 'skipped')),
    last_step   TEXT NOT NULL DEFAULT '' CHECK (length(last_step) <= 40),
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at    TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS tour_progress_one ON tour_progress (COALESCE(user_id, 0));

-- Backfill: everyone who exists now has already found their way round. A fresh install has no users here, so this inserts nothing there. The sign-in-off owner
-- (no users row, user_id NULL) is "existing" when the instance already holds tasks. Re-running changes nothing (DO NOTHING).
INSERT INTO tour_progress (user_id, status, last_step, ended_at)
    SELECT id, 'finished', 'done', now() FROM users
    ON CONFLICT ((COALESCE(user_id, 0))) DO NOTHING;
INSERT INTO tour_progress (user_id, status, last_step, ended_at)
    SELECT NULL, 'finished', 'done', now() WHERE EXISTS (SELECT 1 FROM tasks)
    ON CONFLICT ((COALESCE(user_id, 0))) DO NOTHING;
