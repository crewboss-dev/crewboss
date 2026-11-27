-- Materials (task #295, owner 2026-10-13): "anything that might need to be shared should go to dashboard under accounts
-- and/or projects as materials we can upload. Simple folder of resources with info in the db of what each is for."
-- Dev-time source of truth so AI sessions building any app use the real logo/templates, never generic ones; apps copy
-- what they need at build time and never call the dashboard at runtime.
-- One row per stored file VERSION. `slot` names what it is (logo.primary, favicon.32, email.signature…); a project's
-- effective set = its own current rows plus every ancestor's, the NEAREST node winning per slot (a sub-app overrides
-- logo.primary and inherits the rest). Replacing a slot keeps the old row (replaced_at/replaced_by) and its file.
CREATE TABLE IF NOT EXISTS materials (
    id           SERIAL PRIMARY KEY,
    project_id   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    slot         TEXT NOT NULL CHECK (slot ~ '^[a-z0-9][a-z0-9._-]{0,63}$'),
    kind         TEXT NOT NULL CHECK (kind IN ('logo', 'icon', 'image', 'email-template', 'document-template',
                                               'font', 'style', 'other')),
    title        TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 120),
    purpose      TEXT NOT NULL CHECK (length(purpose) BETWEEN 1 AND 500),
    usage_notes  TEXT NOT NULL DEFAULT '',
    filename     TEXT NOT NULL,
    rel_path     TEXT NOT NULL UNIQUE,
    content_type TEXT NOT NULL,
    bytes        INTEGER NOT NULL,
    sha256       TEXT NOT NULL,
    version      INTEGER NOT NULL DEFAULT 1,
    created_by   TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    replaced_at  TIMESTAMPTZ,
    replaced_by  INTEGER REFERENCES materials(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_materials_current_slot ON materials(project_id, slot) WHERE replaced_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_materials_project ON materials(project_id);
