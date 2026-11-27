-- Account-level SHARED items (owner, 2026-10-02; dashboard task #148). An account (projects.kind = 'account') holds
-- no tasks; it holds the rules, AI skills and knowledge that apply to EVERY project under it (e.g. Mallen's
-- "Humanizer" skill), and every sub-project page and session briefing inherits them.
-- A project's own rules and skills are scanned from its code (catalog); an account has no code, so its shared ones
-- are AUTHORED entries in the knowledge table, with two new kinds: 'rule' (a plain-language rule / AI instruction)
-- and 'skill' (an AI skill: title, purpose as the summary, the instructions as the body). App code allows these two
-- kinds on account projects only.
ALTER TABLE knowledge DROP CONSTRAINT IF EXISTS knowledge_kind_check;
ALTER TABLE knowledge ADD CONSTRAINT knowledge_kind_check
    CHECK (kind IN ('decision', 'fact', 'note', 'link', 'objective', 'responsibility', 'rule', 'skill'));
