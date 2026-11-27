-- #726: what each project keeps that must survive a rebuild, and the per-server manifest generated from it.
--   state_recipes      the project's declared stateful parts (databases, volumes, upload folders, config and secret NAMES, ports,
--                      domains, health/log endpoints, RPO/RTO). target_id NULL = the project's default; a row with a target_id
--                      overrides it for that deploy target. Names and locations only: Crewboss refuses anything that looks like a value.
--   server_manifests   the latest generated manifest + skill per server and a hash of its content (generated, never hand-edited).
--                      Drift = a fresh build no longer hashes to content_hash. pushed_at / push_error record the optional webhook.
--   manifest_tokens    read-only bearer tokens, one server each, hashed at rest (shown once): GET /api/servers/{id}/manifest.
CREATE TABLE IF NOT EXISTS state_recipes (
    id         SERIAL PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    target_id  INTEGER REFERENCES deploy_targets(id) ON DELETE CASCADE,
    recipe     JSONB NOT NULL,
    updated_by TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS state_recipes_one ON state_recipes (project_id, COALESCE(target_id, 0));

CREATE TABLE IF NOT EXISTS server_manifests (
    server_id    INTEGER PRIMARY KEY REFERENCES commissioned_servers(id) ON DELETE CASCADE,
    manifest     JSONB NOT NULL,
    skill        TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    reason       TEXT NOT NULL DEFAULT '',
    generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    pushed_at    TIMESTAMPTZ,
    push_error   TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS manifest_tokens (
    id         SERIAL PRIMARY KEY,
    server_id  INTEGER NOT NULL REFERENCES commissioned_servers(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    label      TEXT NOT NULL DEFAULT '' CHECK (length(label) <= 80),
    created_by TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS manifest_tokens_server ON manifest_tokens (server_id);
