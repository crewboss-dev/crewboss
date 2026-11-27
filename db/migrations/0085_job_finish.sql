-- #775 / #769: a job's agent says it is finished (job_finish). The job stays 'running' (the runner still has to stop the
-- session, push and finish); these columns are the "finishing" mark the runner reads from its heartbeat reply.
-- keep_open: a person steering the session over Remote Control may want it left open after job_finish.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS finish_outcome TEXT CHECK (finish_outcome IN ('done', 'blocked'));
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS finish_summary TEXT NOT NULL DEFAULT '';
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS finish_requested_at TIMESTAMPTZ;
-- The mark belongs to ONE attempt: it counts only while finish_fence = the job's current fence (and is cleared on requeue/claim).
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS finish_fence BIGINT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS keep_open BOOLEAN NOT NULL DEFAULT false;
