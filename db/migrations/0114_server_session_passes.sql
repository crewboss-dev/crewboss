-- Per-server switch (OSS security review F3): may an AI SESSION token ask for its own one-hour SSH pass on this server
-- (POST /api/servers/{id}/ssh-cert)? OFF by default, and every existing row starts OFF too: a pass a person approves (a deploy,
-- a server read, Check access) never depended on it. Turning it ON is an owner action with a fresh passkey tap.
ALTER TABLE commissioned_servers
    ADD COLUMN IF NOT EXISTS sessions_may_request_passes BOOLEAN NOT NULL DEFAULT false;
