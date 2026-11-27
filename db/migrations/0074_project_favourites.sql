-- Favourite projects (#778): a person stars a project and it appears under "Favourites" at the top of the left menu, on every device. Per PERSON: every read and
-- write is by user_id (NULL = the sign-in-off / Microsoft owner, who has no users row, matched with COALESCE like saved_filters), so one person's stars are
-- never another's. `position` is reserved for a drag-to-reorder later (the menu is alphabetical for now). A project that becomes invisible to the person
-- drops out of their list at read time (app/favourites.py), and the row goes with the project (ON DELETE CASCADE).
CREATE TABLE IF NOT EXISTS project_favourites (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER REFERENCES users(id) ON DELETE CASCADE,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS project_favourites_unique ON project_favourites (COALESCE(user_id, 0), project_id);
CREATE INDEX IF NOT EXISTS project_favourites_user ON project_favourites (user_id);
