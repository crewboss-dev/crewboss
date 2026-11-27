-- The page tabs of a project or account record (#773): the Jira-style tab bar under the record's title. One row per project, SHARED by everyone
-- who can see the project; only the project's owner or an admin edits it (app/tabs.py, routes /<slug>/tabs/...). `tabs` = the ordered keys on the bar
-- (the starting layout is code: Summary, List, Board for a project; List, Board, Approvals for an account, used while a project has no row),
-- `labels` = per-project renames {key: label}, `default_tab` = the tab a bare /<slug> opens on ('' = the page's own landing).
CREATE TABLE IF NOT EXISTS project_tabs (
    project_id  INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    tabs        TEXT[]  NOT NULL DEFAULT '{}',
    labels      JSONB   NOT NULL DEFAULT '{}'::jsonb,
    default_tab TEXT    NOT NULL DEFAULT '',
    updated_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
