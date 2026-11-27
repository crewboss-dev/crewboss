-- Merge executor (task #829): Crewboss itself fast-forwards an approved branch into a GitHub repository's default branch.
-- (Numbered 0110 so it sorts after feat/829-runner-jobs' 0108_deploy_target_timeout and 0109_profile_agent. Idempotent.)
--   merge_repos                   the INSTANCE-level allow-list of GitHub repos ('owner/name', lower case) the executor may touch, each
--                                 enrolled FOR ONE project. Only the instance owner edits it (fresh factor): authority is the repo's.
--   projects.merge_via_crewboss   opt-in per project (default off); needs the repo to be on the allow-list.
--   projects.merge_repo           the repo the opt-in was made for; UNIQUE among opted-in projects (one project per repo).
--   projects.merge_via_crewboss_at  when it was turned on: only approvals made AFTER it are ever claimed.
--   merge_requests.state          + merging (claimed by the executor) and sent_back (refused by the executor, with a reason).
--   merge_requests.merge_repo     the repo the request was made against; the executor refuses if the project's remote has moved.
--   merge_requests.merging_at     when the executor claimed it (a crashed claim is returned to approved by recover()).
--   merge_requests.next_attempt_at  backoff: a GitHub error is retried at this time (about 25 minutes over 7 tries).
--   merge_requests.sent_back_kind 'github' (retries used up / refused by GitHub) or 'expired' can be re-queued by an Admin; '' cannot.
--   merge_requests.merge_*        the audit trail beside decided_by / decided_at / sha.
CREATE TABLE IF NOT EXISTS merge_repos (
    full_name TEXT PRIMARY KEY CHECK (full_name = lower(full_name) AND full_name ~ '^[a-z0-9][a-z0-9._-]{0,99}/[a-z0-9._-]{1,100}$'),
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    added_by  TEXT NOT NULL DEFAULT '',
    added_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE projects ADD COLUMN IF NOT EXISTS merge_via_crewboss BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS merge_repo TEXT NOT NULL DEFAULT '';
ALTER TABLE projects ADD COLUMN IF NOT EXISTS merge_via_crewboss_at TIMESTAMPTZ;
CREATE UNIQUE INDEX IF NOT EXISTS projects_merge_repo_uniq ON projects (merge_repo) WHERE merge_via_crewboss AND merge_repo <> '';
ALTER TABLE merge_requests ADD COLUMN IF NOT EXISTS merged_at TIMESTAMPTZ;
ALTER TABLE merge_requests ADD COLUMN IF NOT EXISTS merged_sha TEXT NOT NULL DEFAULT '';
ALTER TABLE merge_requests ADD COLUMN IF NOT EXISTS merged_by TEXT NOT NULL DEFAULT '';
ALTER TABLE merge_requests ADD COLUMN IF NOT EXISTS merge_target TEXT NOT NULL DEFAULT '';
ALTER TABLE merge_requests ADD COLUMN IF NOT EXISTS merge_note TEXT NOT NULL DEFAULT '';
ALTER TABLE merge_requests ADD COLUMN IF NOT EXISTS merge_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE merge_requests ADD COLUMN IF NOT EXISTS merge_repo TEXT NOT NULL DEFAULT '';
ALTER TABLE merge_requests ADD COLUMN IF NOT EXISTS merging_at TIMESTAMPTZ;
ALTER TABLE merge_requests ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ;
ALTER TABLE merge_requests ADD COLUMN IF NOT EXISTS sent_back_kind TEXT NOT NULL DEFAULT '';
ALTER TABLE merge_requests DROP CONSTRAINT IF EXISTS merge_requests_state_check;
ALTER TABLE merge_requests ADD CONSTRAINT merge_requests_state_check
    CHECK (state IN ('requested', 'approved', 'refused', 'merged', 'merging', 'sent_back'));
-- A branch name is a URL path segment for GitHub: no '..', '//' or trailing '/' (git forbids them anyway). Rows from before this check that
-- break it would 500 on ANY later update (approve, refuse, record a check), so every OPEN one is first moved to a terminal state with a
-- note; then the CHECK is added NOT VALID (finished rows are history and are not re-checked, and no open row can trip it). Idempotent.
ALTER TABLE merge_requests DROP CONSTRAINT IF EXISTS merge_requests_branch_check;
UPDATE merge_requests SET state = 'sent_back', sent_back_kind = '', next_attempt_at = NULL,
       merge_note = 'closed by the merge executor migration: this branch name is not a plain git branch name; ask for the merge again'
 WHERE state IN ('requested', 'approved', 'merging')
   AND NOT (branch ~ '^[A-Za-z0-9][A-Za-z0-9._/-]{0,100}$' AND branch !~ '[.][.]' AND branch !~ '//' AND branch !~ '[/.]$' AND branch !~ '[.]lock$');
ALTER TABLE merge_requests ADD CONSTRAINT merge_requests_branch_check
    CHECK (branch ~ '^[A-Za-z0-9][A-Za-z0-9._/-]{0,100}$' AND branch !~ '[.][.]' AND branch !~ '//' AND branch !~ '[/.]$' AND branch !~ '[.]lock$') NOT VALID;
