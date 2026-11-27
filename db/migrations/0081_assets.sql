-- Assets (task #778 part 2, P1): one model for the building blocks agents and apps share: skills, rules, materials.
-- Design: the Assets data model and flows document. Nothing reads these tables yet (P2 adds the resolver, P3 the import).
-- Asset TYPES live in code (app/asset_types.py), not in a CHECK list, so new types and add-ons need no migration.

CREATE TABLE assets (
    id                 SERIAL PRIMARY KEY,
    uid                TEXT NOT NULL UNIQUE,                 -- random public id for URLs; ids are never enumerable
    type               TEXT NOT NULL CHECK (type ~ '^[a-z][a-z0-9-]{1,31}$'),
    key                TEXT NOT NULL CHECK (length(key) BETWEEN 1 AND 128),
    name               TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 120),
    summary            TEXT NOT NULL DEFAULT '' CHECK (length(summary) <= 500),
    owner_scope        TEXT NOT NULL CHECK (owner_scope IN ('instance', 'account', 'project')),
    owner_project_id   INTEGER REFERENCES projects(id) ON DELETE CASCADE,   -- NULL for instance; the account itself for account scope
    account_id         INTEGER REFERENCES projects(id) ON DELETE CASCADE,   -- the partition; NULL only for instance scope
    origin             TEXT NOT NULL DEFAULT 'authored' CHECK (origin IN ('authored', 'builtin', 'repo', 'import')),
    share_all          BOOLEAN NOT NULL DEFAULT false,       -- instance: every account; account: every project in it; project: every project in its account
    current_version_id INTEGER,                               -- the live version (composite FK below, deferred)
    locked             BOOLEAN NOT NULL DEFAULT false,       -- copy of the current version's fields.locked, for fast filtering
    state              TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'archived')),
    archived_at        TIMESTAMPTZ,
    archived_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    archive_reason     TEXT NOT NULL DEFAULT '',
    created_by         TEXT NOT NULL,
    created_user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT assets_scope_shape CHECK (
           (owner_scope = 'instance' AND owner_project_id IS NULL AND account_id IS NULL)
        OR (owner_scope = 'account'  AND owner_project_id IS NOT NULL AND owner_project_id = account_id)
        OR (owner_scope = 'project'  AND owner_project_id IS NOT NULL AND account_id IS NOT NULL)),
    -- the owner project really sits in the partition the row claims
    CONSTRAINT assets_owner_in_partition FOREIGN KEY (owner_project_id, account_id) REFERENCES projects (id, partition_id),
    CONSTRAINT assets_repo_is_project CHECK (origin <> 'repo' OR owner_scope = 'project'),
    CONSTRAINT assets_builtin_is_instance CHECK (origin <> 'builtin' OR owner_scope = 'instance'),
    UNIQUE (id, account_id)                                  -- target of the shares' and pins' partition FKs
);
-- One asset per (type, owner, key), archived ones included. A built-in and a repo mirror are their own namespaces, so an
-- instance asset can override a built-in of the same key (and a repo file a project's own).
CREATE UNIQUE INDEX assets_owner_key ON assets (type, COALESCE(owner_project_id, 0), (origin = 'builtin'), (origin = 'repo'), key);
CREATE INDEX assets_partition ON assets (account_id, type, state);
CREATE INDEX assets_instance ON assets (type, state) WHERE owner_scope = 'instance';
CREATE INDEX assets_owner ON assets (owner_project_id) WHERE owner_project_id IS NOT NULL;

CREATE TABLE asset_versions (
    id               SERIAL PRIMARY KEY,
    asset_id         INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    n                INTEGER NOT NULL CHECK (n >= 1),
    schema_version   INTEGER NOT NULL,
    fields           JSONB NOT NULL DEFAULT '{}',
    body             TEXT NOT NULL DEFAULT '',
    content_hash     TEXT NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    note             TEXT NOT NULL DEFAULT '' CHECK (length(note) <= 500),
    created_via      TEXT NOT NULL CHECK (created_via IN ('web', 'ca', 'job', 'import', 'builtin', 'repo')),
    source_job_id    INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    source_commit    TEXT CHECK (source_commit IS NULL OR source_commit ~ '^[0-9a-f]{40}$'),
    approved_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_by       TEXT NOT NULL,
    created_user_id  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (asset_id, n),
    UNIQUE (id, asset_id)
);
CREATE INDEX asset_versions_hash ON asset_versions (content_hash);

ALTER TABLE assets ADD CONSTRAINT assets_current_version FOREIGN KEY (current_version_id, id)
    REFERENCES asset_versions (id, asset_id) DEFERRABLE INITIALLY DEFERRED;

-- IMMUTABLE: a pin and a job record point at a version id, and that id must always mean the same bytes. No UPDATE, ever.
-- A DELETE is allowed only as the cascade of deleting the asset itself (the trigger then runs below the foreign-key
-- trigger, pg_trigger_depth() > 1); a direct DELETE FROM asset_versions is refused.
CREATE OR REPLACE FUNCTION asset_versions_immutable() RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'asset versions are immutable (asset_versions.id=%)', OLD.id USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF pg_trigger_depth() > 1 THEN
        RETURN OLD;
    END IF;
    RAISE EXCEPTION 'asset versions cannot be deleted on their own (asset_versions.id=%)', OLD.id USING ERRCODE = 'integrity_constraint_violation';
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER asset_versions_immutable BEFORE UPDATE OR DELETE ON asset_versions
    FOR EACH ROW EXECUTE FUNCTION asset_versions_immutable();

CREATE TABLE asset_blobs (                                   -- one stored file, content-addressed; the path layout is the writer's choice
    sha256       TEXT PRIMARY KEY CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    rel_path     TEXT NOT NULL UNIQUE,
    bytes        INTEGER NOT NULL CHECK (bytes >= 0),
    content_type TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE asset_files (
    version_id INTEGER NOT NULL REFERENCES asset_versions(id) ON DELETE CASCADE,
    path       TEXT NOT NULL CHECK (path ~ '^[A-Za-z0-9._-][A-Za-z0-9._/-]{0,199}$' AND path !~ '\.\.'),
    sha256     TEXT NOT NULL REFERENCES asset_blobs(sha256),
    PRIMARY KEY (version_id, path)
);
CREATE INDEX asset_files_blob ON asset_files (sha256);

-- Both link tables carry the asset's partition and the trigger below overwrites it from the asset, so a caller cannot
-- claim another one; the composite FK then refuses a target outside it. MATCH SIMPLE skips the FK for instance assets only
-- (account_id NULL), which may reach any account.
CREATE OR REPLACE FUNCTION asset_link_partition() RETURNS TRIGGER AS $$
BEGIN
    SELECT a.account_id INTO NEW.account_id FROM assets a WHERE a.id = NEW.asset_id;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TABLE asset_shares (                                  -- the owner side's act: "this asset reaches that project / account"
    asset_id          INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    target_project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    account_id        INTEGER,
    shared_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    shared_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (asset_id, target_project_id),
    FOREIGN KEY (asset_id, account_id) REFERENCES assets (id, account_id) ON UPDATE CASCADE,
    CONSTRAINT asset_shares_same_partition FOREIGN KEY (target_project_id, account_id) REFERENCES projects (id, partition_id)
);
CREATE INDEX asset_shares_target ON asset_shares (target_project_id);
CREATE TRIGGER asset_shares_partition BEFORE INSERT OR UPDATE OF asset_id ON asset_shares
    FOR EACH ROW EXECUTE FUNCTION asset_link_partition();

CREATE TABLE asset_pins (                                    -- the consuming project's act: "hold this asset at version N here"
    asset_id          INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    project_id        INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    account_id        INTEGER,
    version_id        INTEGER NOT NULL,
    reason            TEXT NOT NULL DEFAULT '' CHECK (length(reason) <= 300),
    pinned_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    pinned_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (asset_id, project_id),
    FOREIGN KEY (version_id, asset_id) REFERENCES asset_versions (id, asset_id),     -- the pinned version belongs to THIS asset
    CONSTRAINT asset_pins_same_partition FOREIGN KEY (project_id, account_id) REFERENCES projects (id, partition_id)
);
CREATE INDEX asset_pins_project ON asset_pins (project_id);
CREATE TRIGGER asset_pins_partition BEFORE INSERT OR UPDATE OF asset_id ON asset_pins
    FOR EACH ROW EXECUTE FUNCTION asset_link_partition();

CREATE TABLE asset_tags (
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    tag      TEXT NOT NULL CHECK (tag ~ '^[a-z0-9][a-z0-9-]{0,31}$'),
    PRIMARY KEY (asset_id, tag)
);
CREATE INDEX asset_tags_tag ON asset_tags (tag);

CREATE TABLE asset_drafts (                                  -- proposals from the CA or an agent job; MUTABLE; publishing makes an immutable version
    id              SERIAL PRIMARY KEY,
    asset_id        INTEGER REFERENCES assets(id) ON DELETE CASCADE,         -- NULL = proposes a NEW asset
    type            TEXT NOT NULL,
    owner_scope     TEXT NOT NULL CHECK (owner_scope IN ('instance', 'account', 'project')),
    owner_project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    account_id      INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    key             TEXT NOT NULL,
    name            TEXT NOT NULL,
    fields          JSONB NOT NULL DEFAULT '{}',
    body            TEXT NOT NULL DEFAULT '',
    files           JSONB NOT NULL DEFAULT '[]',
    base_version_id INTEGER REFERENCES asset_versions(id),
    proposed_via    TEXT NOT NULL CHECK (proposed_via IN ('ca', 'job')),
    job_id          INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
    card_id         INTEGER,
    task_id         INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    state           TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'published', 'rejected', 'superseded')),
    decided_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    decided_at      TIMESTAMPTZ,
    created_by      TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX asset_drafts_open ON asset_drafts (account_id, state) WHERE state = 'open';

CREATE TABLE job_asset_uses (                                -- #718: what each job attempt was given and what it actually had
    job_id     INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    fence      BIGINT NOT NULL,
    asset_id   INTEGER REFERENCES assets(id) ON DELETE SET NULL,
    version_id INTEGER REFERENCES asset_versions(id) ON DELETE RESTRICT,   -- a used version can never be deleted
    layer      SMALLINT NOT NULL CHECK (layer BETWEEN 0 AND 5),
    type       TEXT NOT NULL,
    key        TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    repo_path  TEXT,
    pinned     BOOLEAN NOT NULL DEFAULT false,
    locked     BOOLEAN NOT NULL DEFAULT false,
    state      TEXT NOT NULL DEFAULT 'resolved' CHECK (state IN ('resolved', 'delivered', 'missing')),
    PRIMARY KEY (job_id, fence, type, key, layer)
);
CREATE INDEX job_asset_uses_version ON job_asset_uses (version_id);
CREATE INDEX job_asset_uses_asset ON job_asset_uses (asset_id, job_id DESC);
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS asset_set_hash TEXT;
