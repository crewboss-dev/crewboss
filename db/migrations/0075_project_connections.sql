-- Project connections (#784, P1): how one project relates to another, as rows rather than tree position.
--   part_of          the subject lives in the other project's code (one host per project; a host is never itself part_of)
--   depends_on       the subject needs the other (directed, informational)
--   integrates_with  the two talk to each other (symmetric: stored ONCE, lower id first, read both ways)
-- Both ends must sit in the same account partition (accounts never see each other's data, #368); an account is never an end.
-- Additive: nothing reads this table yet except the Connections displays and editor (app/connections.py).
CREATE TABLE IF NOT EXISTS project_connections (
    id          SERIAL PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,   -- the subject
    other_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,   -- the object
    kind        TEXT NOT NULL CHECK (kind IN ('part_of', 'depends_on', 'integrates_with')),
    note        TEXT NOT NULL DEFAULT '',                                      -- one line, <= 300 chars (checked in the app too)
    created_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT project_connections_not_self CHECK (project_id <> other_id),
    CONSTRAINT project_connections_symmetric CHECK (kind <> 'integrates_with' OR project_id < other_id),
    CONSTRAINT project_connections_unique UNIQUE (project_id, other_id, kind)
);
CREATE UNIQUE INDEX IF NOT EXISTS project_connections_one_host ON project_connections (project_id) WHERE kind = 'part_of';
CREATE INDEX IF NOT EXISTS project_connections_other ON project_connections (other_id);

-- Pairs a person removed: instance.yaml's `connections:` seed (app/connections.py seed_from_config) never re-adds these.
CREATE TABLE IF NOT EXISTS connection_seed_tombstones (
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    other_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    removed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, other_id, kind)
);

-- The partition root of a project, as app/tree.py account_root_id has it TODAY (the tree is still nested until P2 flattens it):
-- the nearest account at or above it, else the topmost node. NULL for an unknown id.
CREATE OR REPLACE FUNCTION project_account_root(pid INTEGER) RETURNS INTEGER AS $$
DECLARE
    cur_id INTEGER := pid;
    top_id INTEGER := pid;
    k TEXT;
    up INTEGER;
    hops INTEGER := 0;
BEGIN
    LOOP
        SELECT kind, parent_id INTO k, up FROM projects WHERE id = cur_id;
        IF NOT FOUND THEN
            RETURN CASE WHEN cur_id = pid THEN NULL ELSE top_id END;   -- a dangling parent ends the chain at the last real node
        END IF;
        IF k = 'account' THEN
            RETURN cur_id;
        END IF;
        top_id := cur_id;
        IF up IS NULL OR hops > 100 THEN
            RETURN top_id;
        END IF;
        cur_id := up;
        hops := hops + 1;
    END LOOP;
END;
$$ LANGUAGE plpgsql STABLE;

CREATE OR REPLACE FUNCTION project_connections_same_account() RETURNS trigger AS $$
DECLARE
    a TEXT;
    b TEXT;
BEGIN
    SELECT kind INTO a FROM projects WHERE id = NEW.project_id;
    SELECT kind INTO b FROM projects WHERE id = NEW.other_id;
    IF a = 'account' OR b = 'account' THEN
        RAISE EXCEPTION 'a connection joins two projects, never an account' USING ERRCODE = 'check_violation';
    END IF;
    IF project_account_root(NEW.project_id) IS DISTINCT FROM project_account_root(NEW.other_id) THEN
        RAISE EXCEPTION 'both ends of a connection must be in the same account' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.kind = 'part_of' THEN
        IF EXISTS (SELECT 1 FROM project_connections WHERE kind = 'part_of' AND project_id = NEW.other_id AND id IS DISTINCT FROM NEW.id) THEN
            RAISE EXCEPTION 'the host is itself part of another project: no host chains' USING ERRCODE = 'check_violation';
        END IF;
        IF EXISTS (SELECT 1 FROM project_connections WHERE kind = 'part_of' AND other_id = NEW.project_id AND id IS DISTINCT FROM NEW.id) THEN
            RAISE EXCEPTION 'this project hosts others, so it cannot itself be part of one: no host chains' USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS project_connections_same_account ON project_connections;
CREATE TRIGGER project_connections_same_account BEFORE INSERT OR UPDATE ON project_connections
    FOR EACH ROW EXECUTE FUNCTION project_connections_same_account();
