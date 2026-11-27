-- AI triage decides WHAT a request is (owner 2026-09-30): new tasks are blank requests; the owning
-- session's suggest_triage proposes a category (+ whether it's needed now), and the category the owner
-- accepts is stored here. NULL = not triaged yet. The owner's taxonomy: a Workflow is orchestration (the
-- catalog); an AI Skill is a reusable AI capability built in code (Classify email, Extract invoice);
-- a Rule is a deterministic WHEN/IF -> THEN policy at a step; an AI Instruction is a plain-language
-- policy tuning a Skill's judgement.
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS category TEXT
    CHECK (category IN ('workflow', 'ai_skill', 'rule', 'ai_instruction', 'feature', 'fix', 'ops', 'other'));
CREATE INDEX IF NOT EXISTS idx_tasks_category ON tasks(project_id, category);
