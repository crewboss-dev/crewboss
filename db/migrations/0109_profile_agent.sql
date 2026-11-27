-- Agent profiles as real Claude Code agents (#829 item 3): a profile can name a Claude Code agent (.claude/agents/<name>.md in the
-- project's repository, or the runner PC's own ~/.claude/agents), and the runner starts the job with `claude --agent <name>`.
-- '' = no agent (today's behaviour). The agent's own prompt and tool list come from its file; the profile's allowed_tools still
-- decide what is pre-approved (a tool outside it raises a permission prompt, which the runner now detects and gives the job back on).
ALTER TABLE agent_profiles ADD COLUMN IF NOT EXISTS agent TEXT NOT NULL DEFAULT '';
DO $$ BEGIN
    ALTER TABLE agent_profiles ADD CONSTRAINT agent_profiles_agent_shape CHECK (agent ~ '^([A-Za-z0-9][A-Za-z0-9_.:-]{0,63})?$');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Two builder agents of the model routing table, as profiles beside builder / chores / designer / android.
-- The agent files must exist in the repository the job runs on (the ERP repo carries them); a profile whose agent is missing there fails
-- at start, which the runner reports as a failed session start.
INSERT INTO agent_profiles (name, model, description, skills, allowed_tools, task_types, agent, unattended_ok) VALUES
 ('db-migrator', 'sonnet', 'Schema migrations with the prod-data dry run (the db-migrator Claude Code agent)', '{}',
  '{Read,Edit,Write,Glob,Grep,Bash,Skill}', '{migration}', 'db-migrator', false),
 ('code-auditor', 'opus', 'Independent review of a diff or branch for correctness and security; reports, does not fix (the code-auditor agent)', '{}',
  '{Read,Glob,Grep,Bash,Skill}', '{audit,security-review}', 'code-auditor', false)
ON CONFLICT (name) DO NOTHING;

-- Skill (a build skill, such as a design system) must be allowed or its permission prompt hangs a subscription job: the profiles that edit
-- or review UI and code get it. chores (haiku, mechanical) and android do not.
UPDATE agent_profiles SET allowed_tools = array_append(allowed_tools, 'Skill')
 WHERE name IN ('builder', 'designer') AND NOT ('Skill' = ANY(allowed_tools));
