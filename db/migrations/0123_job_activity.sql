-- #89: the live runner conversation. What a job's agent says and does (assistant text, tool calls, results, permission prompts, status) and
-- what Claude and Gemini say to each other, as small redacted frames the runner sends over its verified socket and the task window shows.
--   seq     the job's own running number (server-assigned, the history API's cursor)
--   fence   the claim that sent it; rseq the runner's own number inside that fence (a re-sent batch is dropped by rseq, never doubled)
-- Kept 30 days (the job sweeper), capped per job (rows and bytes) in app/job_activity.py. 0121 (job hold) and 0122 (job outcome) are #987's; this is the next after them.
CREATE TABLE IF NOT EXISTS job_activity (
    job_id  BIGINT      NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    seq     BIGINT      NOT NULL,
    fence   BIGINT      NOT NULL,
    rseq    BIGINT      NOT NULL,
    at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    speaker TEXT        NOT NULL CHECK (speaker IN ('claude', 'gemini', 'runner')),
    kind    TEXT        NOT NULL CHECK (kind IN ('assistant_text', 'tool_call', 'tool_result', 'permission_request', 'status', 'note')),
    text    TEXT        NOT NULL DEFAULT '',
    meta    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (job_id, seq)
);
CREATE INDEX IF NOT EXISTS job_activity_at_idx ON job_activity (at);
