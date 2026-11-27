-- Multi-AI attribution (task #167). A session token now records WHICH AI holds it, so Claude Code and the
-- Gemini CLI can both drive the dashboard and every write is honestly attributed (actor = '<agent>:<stream>').
-- The dashboard is already AI-agnostic (MCP + HTTP API); `agent` is attribution only and never affects a
-- token's reach or isolation (that is project_id + stream, unchanged). Existing tokens are Claude's.
ALTER TABLE session_tokens ADD COLUMN IF NOT EXISTS agent TEXT NOT NULL DEFAULT 'claude';
ALTER TABLE session_tokens DROP CONSTRAINT IF EXISTS session_tokens_agent_check;
ALTER TABLE session_tokens ADD CONSTRAINT session_tokens_agent_check
    CHECK (agent IN ('claude', 'gemini'));
