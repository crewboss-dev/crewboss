-- Server access (task #367, owner 2026-10-14): "In the servers page… I add the IP address and it gives me the public key.
-- All I do is log into the server, put the public key in, and now Dashboard has access to that server." The owner chose a
-- PER-ACCOUNT SSH certificate authority: the line pasted on a server trusts that account's CA for ONE principal
-- (srv-<id>), so a pass signed for one server opens no other, and account A's sessions never reach account B's servers.
-- See app/ssh_ca.py and app/server_access.py.

-- One ed25519 CA per account, created on first need. The private key is stored ENCRYPTED (Fernet, key in a 0600 file
-- mounted into the container: config.SSH_CA_KEY_FILE), decrypted only into a 0700 temp dir for one ssh-keygen -s call.
CREATE TABLE IF NOT EXISTS ssh_cas (
    account_id      INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    public_key      TEXT NOT NULL,
    private_key_enc TEXT NOT NULL,
    fingerprint     TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- A server the owner added on the account's Servers page. name/address/login_user end up in ssh's argv, so they are
-- checked here as well as in app/server_access.py (never a leading '-', no spaces or shell characters).
CREATE TABLE IF NOT EXISTS commissioned_servers (
    id                   SERIAL PRIMARY KEY,
    account_id           INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name                 TEXT NOT NULL CHECK (name ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$'),
    address              TEXT NOT NULL CHECK (address ~ '^[A-Za-z0-9][A-Za-z0-9.:-]{0,252}$'),
    port                 INTEGER NOT NULL DEFAULT 22 CHECK (port BETWEEN 1 AND 65535),
    login_user           TEXT NOT NULL CHECK (login_user ~ '^[a-z_][a-z0-9_-]{0,31}$'),
    status               TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'commissioned', 'failed')),
    host_key             TEXT,          -- pinned on the first good check, known_hosts form (HostKeyAlias mallen-dash-srv-<id>)
    host_key_fingerprint TEXT,
    os_name              TEXT,
    os_version           TEXT,
    hostname             TEXT,
    kernel               TEXT,
    sudo_nopasswd        BOOLEAN,
    commissioned_at      TIMESTAMPTZ,
    last_check_at        TIMESTAMPTZ,
    last_check_error     TEXT,
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by           TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_commissioned_servers_name ON commissioned_servers(account_id, lower(name));

-- Every certificate issued, kept after the server record is removed (server_id is not a foreign key on purpose).
CREATE SEQUENCE IF NOT EXISTS ssh_cert_serial_seq;
CREATE TABLE IF NOT EXISTS ssh_cert_issues (
    id           SERIAL PRIMARY KEY,
    server_id    INTEGER NOT NULL,
    account_id   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    actor        TEXT NOT NULL,
    key_id       TEXT NOT NULL,
    serial       BIGINT NOT NULL UNIQUE,
    principal    TEXT NOT NULL,
    key_fingerprint TEXT NOT NULL,
    valid_before TIMESTAMPTZ NOT NULL,
    issued_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ssh_cert_issues_server ON ssh_cert_issues(server_id);
