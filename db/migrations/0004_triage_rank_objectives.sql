-- Ranking within a bucket, Claude's triage challenge, and objectives/pulse support.

ALTER TABLE tasks ADD COLUMN IF NOT EXISTS rank DOUBLE PRECISION NOT NULL DEFAULT 0;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS triage_state TEXT NOT NULL DEFAULT 'new'
    CHECK (triage_state IN ('new', 'suggested', 'accepted', 'overridden', 'discussing'));
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS suggestion JSONB;

-- Backfill: id order approximates creation order; gives every existing row a distinct, ordered rank
-- with room either side for future inserts.
UPDATE tasks SET rank = id * 1024.0 WHERE rank = 0;

CREATE INDEX IF NOT EXISTS idx_tasks_rank ON tasks(project_id, priority, rank);

-- Objectives are knowledge(kind='objective') with an optional numeric target.
ALTER TABLE knowledge DROP CONSTRAINT IF EXISTS knowledge_kind_check;
ALTER TABLE knowledge ADD CONSTRAINT knowledge_kind_check
    CHECK (kind IN ('decision', 'fact', 'note', 'link', 'objective'));
ALTER TABLE knowledge ADD COLUMN IF NOT EXISTS target_value NUMERIC;
ALTER TABLE knowledge ADD COLUMN IF NOT EXISTS target_period TEXT;

-- Workflow staleness diffs need a 'stale' change_type alongside added/removed/modified.
ALTER TABLE catalog_diffs DROP CONSTRAINT IF EXISTS catalog_diffs_change_type_check;
ALTER TABLE catalog_diffs ADD CONSTRAINT catalog_diffs_change_type_check
    CHECK (change_type IN ('added', 'removed', 'modified', 'stale'));
