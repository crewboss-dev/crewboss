-- A 'responsibility' is a standing duty the owner gave a project's AI session ("you own the Zabbix integration").
-- Unlike a decision it never ages out of the session briefing: every active one is listed at the top of GROUND.
ALTER TABLE knowledge DROP CONSTRAINT IF EXISTS knowledge_kind_check;
ALTER TABLE knowledge ADD CONSTRAINT knowledge_kind_check
    CHECK (kind IN ('decision', 'fact', 'note', 'link', 'objective', 'responsibility'));
