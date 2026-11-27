# Server access

Crewboss reaches your servers with short-lived SSH certificates ("passes") that it signs itself. There are no long-lived keys to
hand out. A runner PC never holds a pass or any other SSH material.

Details: `app/server_access.py`, `app/ssh_ca.py`, [deploy-executor.md](deploy-executor.md), [server-reads.md](server-reads.md).

## How it works

* Each account has one SSH certificate authority (ed25519), made the first time it is needed. Its private key is stored encrypted
  (the key for that is the 0600 file `secrets/ssh-ca-enc-key`) and is decrypted only for one signing call.
* A server trusts that authority for exactly one name, `srv-<id>`. You add one line to the login user's `authorized_keys` on the
  server (`cert-authority,principals="srv-<id>" ...`). Crewboss shows you the exact line.
* A pass signed for one server cannot open another account's servers, because the authorities differ.

## Add a server

1. Open the **Servers** page (`/servers` for the owner; `/<account>/servers` for an account) and add the server: a name, its address,
   the SSH port and the login user. Set up that user and `sudo` on the server yourself.
2. The page shows the one line to paste, and setup commands for your operating system. Paste the line into the login user's
   `authorized_keys`.
3. Press **Check access**. Crewboss connects from the app with a pass it just signed and a throw-away key, and runs only read-only
   commands (hostname, `/etc/os-release`, `uname -r`, `sudo -n true`).
4. The first good check pins the server's host key. After that every connection uses `StrictHostKeyChecking=yes` against it, and a
   changed key fails loudly. If you rebuilt the server, remove it and add it again.

Only a commissioned server (a good check and a pinned key) can be signed a pass.

An optional "Connect via" setting sends the connection through another commissioned server of the same account (OpenSSH
`ProxyJump`). A pass for such a server opens both servers for its lifetime. Direct connection is the normal case.

## The passes

| Pass | For | Valid | Can run |
|---|---|---|---|
| Session pass | an agent session or a person, through `POST /api/servers/<id>/ssh-cert` | 60 minutes (`CREWBOSS_SSH_CERT_MINUTES`) | a shell; port forwarding and a terminal are allowed, agent forwarding and X11 are not |
| Deploy pass | the deploy executor, for one approved deploy | about the deploy's time limit plus a minute | one forced command (a `git fetch`, a fast-forward merge to the approved commit and the target's post command) |
| Read pass | one server read for a job | 1 minute | one forced command: `crewboss-read <name>` |

Each pass is backdated by 5 minutes for clock skew. Every pass is recorded (who, which server, serial number, when). Session
tokens get passes only for their own account's servers; an external agent and the overseer project get none.

### Session passes need a per-server switch

A session pass opens a normal shell and no person is asked, so each server has a switch, **AI sessions may open a shell**, on its
card under Machines > Servers > Access and setup. It is **off** for every server, including ones that existed before the switch.
While it is off, `POST /api/servers/<id>/ssh-cert` answers `403`. Only the instance owner can turn it on, and turning it on needs a
fresh authenticator code (or passkey tap) within the last 5 minutes (turning it off never does). The text beside it reads "Lets AI sessions open a 60-minute shell here without
asking you". A server reached through another server needs the switch on for both, because its pass opens both. Deploy passes, read
passes and Check access are approved by a person and do not use the switch.

## Deploy targets

A deploy target is a project's place to deploy: a commissioned server, a path, a branch, an optional one-line post command and an
environment (`dev`, `staging` or `prod`). Only the project's Owner defines targets, on a server the instance owner has pinned to that
project. A job can ask for a deploy of a commit that is already on the target's branch; a person approves it; the app's executor
runs it. Production is promote-only, needs an Admin's approval with a fresh second factor, and has a one-tap rollback. See
[multi-user.md](multi-user.md) and [deploy-executor.md](deploy-executor.md).

## Server reads

A job running on a PC cannot see a live server, so it cannot diagnose a production fault from logs. The project's Owner declares
reads on the project's **Where it lives** page: a container's logs, a systemd unit's journal, a log file under `/var/log/`, `/opt/` or `/srv/`, or
a fixed probe command from a read-only allowlist. A job can run only a declared read, by name, with a time window, a text filter and a line
limit. The server runs only names listed in its own `/etc/crewboss/reads.conf` (or `reads.d/*.conf`), through `crewboss-read`, which
re-checks everything. Output is capped, redacted on a best-effort basis and returned marked as untrusted. Production reads are off
until the project's Owner switches them on. Nothing a read does can change the server.

Setting up a server for reads takes five steps as root (install `scripts/crewboss-read`, make `/etc/crewboss/reads.d`, allow the helper
with `sudo`, save the generated conf file, test). They are written out in [server-reads.md](server-reads.md).
