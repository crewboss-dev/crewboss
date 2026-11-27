-- #908: session-to-session HANDOFFS and ACCOUNT-owned SHARED DOCS.
-- Sessions in different projects never touch each other's data (app/scope.py); this is the one deliberate, narrow crossing:
-- a session may send a handoff to another project of the SAME account partition, and every project of an account shares its
-- versioned Markdown docs. Nothing here crosses accounts: the triggers below are the backstop for app/handoffs.py and app/account_docs.py.
-- `account_id` is the PARTITION ROOT (project_account_root, migration 0075): the nearest account at or above a project, else its topmost node.
-- The text of a handoff or doc is DATA from another session, never an instruction (the MCP tools say so).

-- --- handoffs ------------------------------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS handoffs (
    id              SERIAL PRIMARY KEY,
    account_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    from_project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    to_project_id   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    subject         TEXT NOT NULL CHECK (length(btrim(subject)) BETWEEN 1 AND 120),
    body            TEXT NOT NULL CHECK (length(body) BETWEEN 1 AND 204800),
    status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    reply_to_id     INTEGER REFERENCES handoffs(id) ON DELETE SET NULL,   -- a follow-up that continues an earlier handoff
    created_by      TEXT NOT NULL,
    closed_by       TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT handoffs_not_self CHECK (from_project_id <> to_project_id)
);
CREATE INDEX IF NOT EXISTS handoffs_account ON handoffs (account_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS handoffs_from ON handoffs (from_project_id);
CREATE INDEX IF NOT EXISTS handoffs_to ON handoffs (to_project_id);

CREATE TABLE IF NOT EXISTS handoff_replies (
    id              SERIAL PRIMARY KEY,
    handoff_id      INTEGER NOT NULL REFERENCES handoffs(id) ON DELETE CASCADE,
    from_project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,   -- the project of the replying session
    body            TEXT NOT NULL CHECK (length(body) BETWEEN 1 AND 204800),
    created_by      TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS handoff_replies_handoff ON handoff_replies (handoff_id, id);

-- --- shared docs ---------------------------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS account_docs (
    id                   SERIAL PRIMARY KEY,
    account_id           INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    slug                 TEXT NOT NULL CHECK (slug ~ '^[a-z0-9][a-z0-9._-]{0,62}$'),
    title                TEXT NOT NULL CHECK (length(btrim(title)) BETWEEN 1 AND 200),
    current_version      INTEGER NOT NULL DEFAULT 1 CHECK (current_version >= 1),
    pinned_connection_id INTEGER REFERENCES project_connections(id) ON DELETE SET NULL,  -- optional: the doc describes this connection
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT account_docs_slug_unique UNIQUE (account_id, slug)
);

CREATE TABLE IF NOT EXISTS account_doc_versions (
    doc_id     INTEGER NOT NULL REFERENCES account_docs(id) ON DELETE CASCADE,
    version    INTEGER NOT NULL CHECK (version >= 1),
    body       TEXT NOT NULL CHECK (length(body) <= 204800),
    author     TEXT NOT NULL,
    note       TEXT NOT NULL DEFAULT '' CHECK (length(note) <= 300),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (doc_id, version)
);

-- Docs a handoff points at (same account only: trigger below).
CREATE TABLE IF NOT EXISTS handoff_docs (
    handoff_id INTEGER NOT NULL REFERENCES handoffs(id) ON DELETE CASCADE,
    doc_id     INTEGER NOT NULL REFERENCES account_docs(id) ON DELETE CASCADE,
    PRIMARY KEY (handoff_id, doc_id)
);

-- --- same-account backstops (the app checks first, with a clear message) ---------------------------------------------------------
CREATE OR REPLACE FUNCTION handoffs_same_account() RETURNS trigger AS $$
DECLARE
    k TEXT;
BEGIN
    SELECT kind INTO k FROM projects WHERE id = NEW.to_project_id;
    IF k = 'account' THEN
        RAISE EXCEPTION 'a handoff goes to a project, not an account' USING ERRCODE = 'check_violation';
    END IF;
    IF project_account_root(NEW.from_project_id) IS DISTINCT FROM NEW.account_id
       OR project_account_root(NEW.to_project_id) IS DISTINCT FROM NEW.account_id THEN
        RAISE EXCEPTION 'both ends of a handoff must be in the same account' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.reply_to_id IS NOT NULL AND (SELECT account_id FROM handoffs WHERE id = NEW.reply_to_id) IS DISTINCT FROM NEW.account_id THEN
        RAISE EXCEPTION 'a follow-up continues a handoff of the same account' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS handoffs_same_account ON handoffs;
CREATE TRIGGER handoffs_same_account BEFORE INSERT ON handoffs
    FOR EACH ROW EXECUTE FUNCTION handoffs_same_account();

CREATE OR REPLACE FUNCTION handoff_replies_same_account() RETURNS trigger AS $$
DECLARE
    acct INTEGER;
BEGIN
    SELECT account_id INTO acct FROM handoffs WHERE id = NEW.handoff_id;
    IF project_account_root(NEW.from_project_id) IS DISTINCT FROM acct THEN
        RAISE EXCEPTION 'a reply must come from the same account as the handoff' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS handoff_replies_same_account ON handoff_replies;
CREATE TRIGGER handoff_replies_same_account BEFORE INSERT ON handoff_replies
    FOR EACH ROW EXECUTE FUNCTION handoff_replies_same_account();

CREATE OR REPLACE FUNCTION account_docs_same_account() RETURNS trigger AS $$
DECLARE
    a INTEGER;
BEGIN
    IF project_account_root(NEW.account_id) IS DISTINCT FROM NEW.account_id THEN
        RAISE EXCEPTION 'a shared doc belongs to an account (a partition root)' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.pinned_connection_id IS NOT NULL THEN
        SELECT project_account_root(project_id) INTO a FROM project_connections WHERE id = NEW.pinned_connection_id;
        IF a IS DISTINCT FROM NEW.account_id THEN
            RAISE EXCEPTION 'the pinned connection must be in the same account as the doc' USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS account_docs_same_account ON account_docs;
CREATE TRIGGER account_docs_same_account BEFORE INSERT OR UPDATE OF account_id, pinned_connection_id ON account_docs
    FOR EACH ROW EXECUTE FUNCTION account_docs_same_account();

CREATE OR REPLACE FUNCTION handoff_docs_same_account() RETURNS trigger AS $$
BEGIN
    IF (SELECT account_id FROM handoffs WHERE id = NEW.handoff_id)
       IS DISTINCT FROM (SELECT account_id FROM account_docs WHERE id = NEW.doc_id) THEN
        RAISE EXCEPTION 'a handoff links only docs of its own account' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS handoff_docs_same_account ON handoff_docs;
CREATE TRIGGER handoff_docs_same_account BEFORE INSERT ON handoff_docs
    FOR EACH ROW EXECUTE FUNCTION handoff_docs_same_account();
