-- #810 step 4: ONE Machines page. A machine can do two jobs: run jobs (a runner = a device_tokens row) and receive deploys (a
-- commissioned_servers row). `server_id` says "this runner and that server are the same machine". It is NEVER set automatically: the card
-- suggests a join when the runner's host name or address matches a server, and a person says yes (app/devices.py link_server).
-- ON DELETE SET NULL: forgetting the server leaves the runner exactly as it was.
ALTER TABLE device_tokens ADD COLUMN IF NOT EXISTS server_id INTEGER REFERENCES commissioned_servers(id) ON DELETE SET NULL;
CREATE INDEX IF NOT EXISTS idx_device_tokens_server ON device_tokens(server_id) WHERE server_id IS NOT NULL;

-- The Machines page's role filter (All / Runs jobs / Receives deploys / Needs attention) is saveable per person like Recent's (#772).
ALTER TABLE saved_filters DROP CONSTRAINT IF EXISTS saved_filters_page_check;
ALTER TABLE saved_filters ADD CONSTRAINT saved_filters_page_check CHECK (page IN ('list', 'board', 'recent', 'machines'));
