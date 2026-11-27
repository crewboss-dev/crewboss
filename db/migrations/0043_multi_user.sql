-- Multi-user from day 1 (task #713, owner 2026-11-08/09). Local sign-in is the multi-user path; Microsoft (OIDC) mode keeps
-- its single-owner behaviour (every OIDC super admin is an owner).
--
--   users.role           owner | designer | viewer. owner: everything (users, servers, every project). designer: the projects
--                        they own or were shared into, may write there, may deploy to the servers the owner assigned them.
--                        viewer: read only. users.disabled_at (0037) is how a user is switched off.
--   projects.owner_user_id   who created the project (backfilled to the instance owner). NULL = the instance owner's own
--                        (nobody else sees it unless it is shared): fail closed.
--   project_members      a person's role in a project (change #77): 'admin' (approves merges and production deploys), 'member'
--                        (works, runs agents, deploys dev, requests production) or 'viewer'. The creator (projects.owner_user_id) is
--                        the project's Owner. Per PROJECT: never flows down to child projects (docs/multi-user.md).
--   server_assignments   the commissioned servers a user may use as a deploy target for their own projects (owner assigns).
--   invites              a one-time sign-up link, 24 h. Only the SHA-256 of the token is stored.
--   *_user_id columns    the real person behind an audit row; the old text actor ('owner') stays readable beside it.
ALTER TABLE users ADD COLUMN IF NOT EXISTS role TEXT NOT NULL DEFAULT 'designer';
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'users_role_check') THEN
        ALTER TABLE users ADD CONSTRAINT users_role_check CHECK (role IN ('owner', 'designer', 'viewer'));
    END IF;
END $$;
-- The local owner, and every Microsoft (OIDC) user row, is an owner: only a super admin ever signs in that way.
UPDATE users SET role = 'owner' WHERE is_owner OR oid NOT LIKE 'local:%';

ALTER TABLE projects ADD COLUMN IF NOT EXISTS owner_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL;
-- The instance owner is ONE principal: backfill to their LOCAL account when there is one (the person who actually signs in),
-- else to the Microsoft row. (A legacy Microsoft row with no username may be user 1 while the local owner is a later row.)
UPDATE projects SET owner_user_id = (SELECT id FROM users WHERE role = 'owner'
                                      ORDER BY (username IS NOT NULL) DESC, is_owner DESC, id LIMIT 1)
 WHERE owner_user_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_projects_owner_user ON projects(owner_user_id);

CREATE TABLE IF NOT EXISTS project_members (
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role       TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('admin', 'member', 'viewer')),
    added_by   INTEGER REFERENCES users(id) ON DELETE SET NULL,
    added_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (project_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_project_members_user ON project_members(user_id);

-- An assignment pins a server to ONE project (and names the person it was assigned to, the project's Owner): only that project's
-- Owner defines deploy targets on it, and only that project can deploy to it.
CREATE TABLE IF NOT EXISTS server_assignments (
    server_id   INTEGER NOT NULL REFERENCES commissioned_servers(id) ON DELETE CASCADE,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    assigned_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    assigned_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (server_id, project_id)
);

-- Instance-level audit (invites, roles, resets, assignments): readable only by the instance owner, not filed under any project.
CREATE TABLE IF NOT EXISTS instance_audit (
    id            BIGSERIAL PRIMARY KEY,
    at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    verb          TEXT NOT NULL,
    summary       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_server_assignments_user ON server_assignments(user_id);

CREATE TABLE IF NOT EXISTS invites (
    id         SERIAL PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    role       TEXT NOT NULL CHECK (role IN ('designer', 'viewer')),
    label      TEXT NOT NULL DEFAULT '',
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    used_at    TIMESTAMPTZ,
    used_by    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    revoked_at TIMESTAMPTZ,
    CONSTRAINT invites_max_24h CHECK (expires_at <= created_at + interval '24 hours')
);

-- Audit: who really did it. activity.actor keeps its text ('owner', 'claude:all', 'user:7'); user_id names the person when a
-- signed-in person (or a session token acting for its project's owner) made the write.
ALTER TABLE activity ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE change_log ADD COLUMN IF NOT EXISTS decided_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE task_questions ADD COLUMN IF NOT EXISTS answered_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE ssh_cert_issues ADD COLUMN IF NOT EXISTS user_id INTEGER REFERENCES users(id) ON DELETE SET NULL;
-- A designer's deploy to a server the owner assigned them needs no approval: it is recorded as approved by themselves, flagged.
ALTER TABLE server_jobs ADD COLUMN IF NOT EXISTS auto_approved BOOLEAN NOT NULL DEFAULT false;
