-- projects.partition_id (task #778 part 2, P1; the prerequisite the Assets design names from #784).
-- The partition a project belongs to = tree.account_root_id: its nearest account at or above it, else the topmost node
-- (a top-level personal project is its own partition). Kept by a trigger on every tree write, so other tables can pin a row
-- to ONE account with a composite foreign key (id, partition_id) and the schema itself refuses a cross-account link.
-- A side effect, intended: moving a project that owns rows pinned this way into another account is refused by the database.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS partition_id INTEGER;

CREATE OR REPLACE FUNCTION projects_refresh_partition(root_id INTEGER) RETURNS VOID AS $$
BEGIN
    WITH RECURSIVE t(id, part) AS (
        SELECT p.id,
               CASE WHEN p.kind = 'account' OR p.parent_id IS NULL THEN p.id
                    ELSE (SELECT q.partition_id FROM projects q WHERE q.id = p.parent_id) END
          FROM projects p WHERE p.id = root_id
        UNION
        SELECT c.id, CASE WHEN c.kind = 'account' THEN c.id ELSE t.part END
          FROM projects c JOIN t ON c.parent_id = t.id
    )
    UPDATE projects p SET partition_id = t.part FROM t WHERE p.id = t.id AND p.partition_id IS DISTINCT FROM t.part;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION projects_partition_trigger() RETURNS TRIGGER AS $$
BEGIN
    PERFORM projects_refresh_partition(NEW.id);
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS projects_partition ON projects;
CREATE TRIGGER projects_partition AFTER INSERT OR UPDATE OF parent_id, kind ON projects
    FOR EACH ROW EXECUTE FUNCTION projects_partition_trigger();

-- Backfill: every top-level node, then down its subtree.
DO $$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT id FROM projects WHERE parent_id IS NULL LOOP
        PERFORM projects_refresh_partition(r.id);
    END LOOP;
END $$;

ALTER TABLE projects ADD CONSTRAINT projects_id_partition_key UNIQUE (id, partition_id);
