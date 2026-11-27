-- Jump host for a commissioned server (task #580, owner 2026-10-27): a server that is only reachable by jumping through
-- another one (a special case, not the norm). via_server_id names another commissioned server of the SAME account that the
-- dashboard and sessions hop through (ssh -J). The same-account / not-itself / no-chain / commissioned rules are
-- enforced in app/server_access.py; the FK stops a via server being deleted while others jump through it (remove()
-- says so first).
ALTER TABLE commissioned_servers
    ADD COLUMN IF NOT EXISTS via_server_id INTEGER REFERENCES commissioned_servers(id) ON DELETE RESTRICT;
ALTER TABLE commissioned_servers DROP CONSTRAINT IF EXISTS commissioned_servers_via_not_self;
ALTER TABLE commissioned_servers ADD CONSTRAINT commissioned_servers_via_not_self CHECK (via_server_id IS NULL OR via_server_id <> id);
CREATE INDEX IF NOT EXISTS idx_commissioned_servers_via ON commissioned_servers(via_server_id) WHERE via_server_id IS NOT NULL;

-- Audit (#580 review): a via pass also opens the via box for the hour, so the issue row records that box's id too and
-- "who could reach server V" can be found by server_id OR via_server_id. NULL for direct passes. No FK on purpose
-- (like server_id, rows outlive the server record).
ALTER TABLE ssh_cert_issues ADD COLUMN IF NOT EXISTS via_server_id INTEGER;
CREATE INDEX IF NOT EXISTS idx_ssh_cert_issues_via ON ssh_cert_issues(via_server_id) WHERE via_server_id IS NOT NULL;

-- Backstop for the no-chain rule (set_via also locks both rows): a via must have no via of its own, and a server that
-- others jump through can't be given one. SHARE locks make two racing writers wait on each other instead of making a cycle.
CREATE OR REPLACE FUNCTION commissioned_servers_via_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.via_server_id IS NOT NULL THEN
        IF EXISTS (SELECT 1 FROM commissioned_servers WHERE id = NEW.via_server_id AND via_server_id IS NOT NULL FOR SHARE) THEN
            RAISE EXCEPTION 'via server % itself has a via; jump hosts cannot chain', NEW.via_server_id;
        END IF;
        IF EXISTS (SELECT 1 FROM commissioned_servers WHERE via_server_id = NEW.id FOR SHARE) THEN
            RAISE EXCEPTION 'server % is used as a via by others; it cannot have one', NEW.id;
        END IF;
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS commissioned_servers_via_guard ON commissioned_servers;
CREATE TRIGGER commissioned_servers_via_guard BEFORE INSERT OR UPDATE OF via_server_id ON commissioned_servers
    FOR EACH ROW EXECUTE FUNCTION commissioned_servers_via_guard();
