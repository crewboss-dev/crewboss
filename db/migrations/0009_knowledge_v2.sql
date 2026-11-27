-- Knowledge v2 (owner 2026-09-30): every entry is a collapsed card — a one-line title plus a two-line
-- SUMMARY — that expands into its Markdown body. Entries are archived (with a reason), never deleted.
-- Additive only: existing rows keep working; 0010 backfills their summaries.

ALTER TABLE knowledge ADD COLUMN IF NOT EXISTS summary TEXT NOT NULL DEFAULT '';
ALTER TABLE knowledge ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;
ALTER TABLE knowledge ADD COLUMN IF NOT EXISTS archived_by TEXT;
ALTER TABLE knowledge ADD COLUMN IF NOT EXISTS archive_reason TEXT NOT NULL DEFAULT '';
-- Objectives: what the target number counts, so the card can say "$40k / month" rather than "40000".
ALTER TABLE knowledge ADD COLUMN IF NOT EXISTS target_unit TEXT
    CHECK (target_unit IN ('money', 'count', 'percent', 'hours'));
CREATE INDEX IF NOT EXISTS idx_knowledge_live ON knowledge(project_id, updated_at DESC) WHERE archived_at IS NULL;

-- Images in knowledge bodies live IN the dashboard database (so the nightly pg_dump backs them up with
-- everything else) and are served only from the dashboard's own origin at /media/knowledge/<id>.<ext>.
-- Every upload is decoded and RE-ENCODED server-side (png/jpeg/webp only, metadata dropped, long edge
-- capped), so what is stored is always a plain raster image — never SVG, never the uploaded bytes.
CREATE TABLE IF NOT EXISTS knowledge_images (
    id           SERIAL PRIMARY KEY,
    project_id   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    sha256       TEXT NOT NULL,
    content_type TEXT NOT NULL CHECK (content_type IN ('image/png', 'image/jpeg', 'image/webp')),
    data         BYTEA NOT NULL,
    width        INTEGER NOT NULL CHECK (width > 0),
    height       INTEGER NOT NULL CHECK (height > 0),
    bytes        INTEGER NOT NULL,
    created_by   TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (project_id, sha256)
);
