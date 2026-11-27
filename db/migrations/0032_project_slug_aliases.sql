-- Rename project (#577): a project's OLD slugs, so its old URLs and callers keep working after the owner changes its
-- slug (app/tree.py rename_project). repo.get_project_by_slug falls back to this table: a page GET at an old slug is
-- redirected to the current one, a page write there is a 404, and an API/MCP caller passing project='<old slug>'
-- resolves to the project. An alias is never reused by another project; renaming back to it drops it.
CREATE TABLE IF NOT EXISTS project_slug_aliases (
    slug        TEXT PRIMARY KEY,               -- an old slug (never a current one: projects.slug is checked first)
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS project_slug_aliases_project_idx ON project_slug_aliases (project_id);
