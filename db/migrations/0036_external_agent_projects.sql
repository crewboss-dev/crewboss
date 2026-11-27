-- External-agent projects (change #35, task #574, owner 2026-10-27): a project can be worked by a Claude Code session that
-- does NOT run on the sessions server (e.g. a Windows desktop working a mobile app repo). It talks to the rest
-- through Dashboard tasks plus contract docs in the repos.
--   projects.external_agent  the flag. It counts as having a session for routing (own outbox, its branch is left out of the
--                            parent session's reach) but is NOT in the session plan, so the launcher never starts a tmux
--                            session for it. Exclusive with session_roles (one or the other); the app refuses the
--                            combination with a message, this CHECK is the backstop.
--   session_tokens.agent     gains 'claude-desktop' (attribution only, like 'gemini': same reach rules, actor
--                            'claude-desktop:all').
-- (0034/0035 belong to an unmerged branch; the runner takes files in name order, so the gap is harmless.)
ALTER TABLE projects ADD COLUMN IF NOT EXISTS external_agent BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE projects DROP CONSTRAINT IF EXISTS projects_external_agent_no_roles;
ALTER TABLE projects ADD CONSTRAINT projects_external_agent_no_roles
    CHECK (NOT external_agent OR cardinality(COALESCE(session_roles, '{}')) = 0);

ALTER TABLE session_tokens DROP CONSTRAINT IF EXISTS session_tokens_agent_check;
ALTER TABLE session_tokens ADD CONSTRAINT session_tokens_agent_check
    CHECK (agent IN ('claude', 'gemini', 'claude-desktop'));
