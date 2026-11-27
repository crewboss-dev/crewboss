-- Saved filters (#772): a person saves the current filter set of a page (a project's List or Board, or Recent) under a name, and
-- applies it again with one click, on any device. Per person: every read and write is by user_id (NULL = the sign-in-off / Microsoft
-- owner, who has no users row, matched with IS NOT DISTINCT FROM), so another person's saved filters are never visible.
-- `page` is the page type; project_id is set for the project-scoped pages (List, Board) and NULL for Recent. `data` is the filter
-- set as the page's own query parameters (app/saved_filters.py keeps only the known keys), plus `cols` for the List's visible columns.
-- Numbered 0072 so it can't clash with a parallel branch.
CREATE TABLE IF NOT EXISTS saved_filters (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER REFERENCES users(id) ON DELETE CASCADE,
    page        TEXT NOT NULL CHECK (page IN ('list', 'board', 'recent')),
    project_id  INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    data        JSONB NOT NULL DEFAULT '{}'::jsonb,
    is_default  BOOLEAN NOT NULL DEFAULT false,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS saved_filters_name
    ON saved_filters (COALESCE(user_id, 0), page, COALESCE(project_id, 0), lower(name));
CREATE INDEX IF NOT EXISTS saved_filters_lookup ON saved_filters (user_id, page, project_id);
