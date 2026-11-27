# Threat model

This page says what Crewboss protects, what it does not, and who you are trusting when you run it. Read it before you put an
instance on a network other people can reach. For how to report a problem, see [SECURITY.md](../SECURITY.md).

Crewboss is a control plane. It holds your plans and task text, it tells AI agents on your PCs what to work on, and it can sign
passes that open your servers. Treat the Crewboss server like a build server with deploy rights: whoever controls it controls
what it can reach.

## What is worth protecting

| Asset | Where it lives | If it is lost |
|---|---|---|
| Tasks, comments, knowledge, plans, chat history, uploads | the Postgres database; the materials and CA-uploads folders | private project information is exposed or altered |
| The SSH certificate authority of each account | the database (encrypted) plus `secrets/ssh-ca-enc-key` | anyone with both can sign a pass for every commissioned server of that account |
| Sign-in material: password hashes, authenticator secrets, passkey public keys, recovery-code hashes, sessions | the database plus `local-auth.key` | with both, recovery codes can be guessed offline; sessions can be forged only by writing to the database |
| Session, runner, job and reporter tokens | hashes only in the database; the plain token is with its holder | a stolen token acts as its holder until revoked |
| The runner identity key | `secrets/runner-identity-key` | someone can pose as your server to a runner that is enrolling |
| The merge bot's GitHub token, the key-store writer credential, the push (VAPID) key | 0600 files mounted into the app | merges to enrolled repositories; writes to your key store; fake notifications |
| Source code and git credentials | each runner PC | code theft or unwanted pushes |
| Your Claude sign-in or API key | each runner PC only. It is never sent to Crewboss | someone else spends your Claude account |

## The parts and the lines between them

```
browser ──https──> reverse proxy ──> Crewboss app ──> Postgres
                                        │   ▲
                              ssh pass  │   │ outbound WebSocket + job MCP (job token)
                                        ▼   │
                                target servers   runner PC ──> Claude Code ──> Anthropic
                                        ▲              │
                                        │              └──> GitHub (the PC's own git credentials)
                              GitHub <──┘ (merge bot token, only if you switch the merge executor on)
```

**Browser to server.** Sign-in is a password plus an authenticator-app code (a passkey where the install has a signed certificate and the person added one); a recovery code or a one-time temporary password only opens a restricted session that
can finish setting up the account. The session is a random token in an HttpOnly, SameSite=Lax cookie; only its hash is stored. Every browser write
must come from one of the instance's own origins (`origin_hosts`). Sensitive actions need a second-factor check within 12 hours, and the
most sensitive within 5 minutes. Pages are escaped by the template engine, and Markdown is sanitised before it is shown.

**Server to runner.** A runner connects out; nothing connects in to a PC. The runner pins the server's identity key when it enrols
and each side proves its key on every connection. The runner acts on nothing the server sends (a job nudge, a Gemini key, a claim result) until the server's proof has verified against the pinned key on that connection; before that it closes the socket. `--insecure-http` enrolment is refused for any host but this computer unless `--allow-insecure-remote` is also given. The server sends a runner only the jobs started by the person who enrolled it.
The fingerprint in an enrolment code is short (40 bits). It is a second check behind TLS, not a replacement for it: enrol runners
over https only.

**Runner to Claude.** The runner starts Claude Code in a fresh git worktree, with hooks switched off, messages from other Claude
sessions refused, and a tool list from the job's profile. Tools the profile does not name are denied.

**Server to target servers.** The app connects with a pass it signs itself, to a pinned host key. A deploy pass and a read pass
can each run one fixed command and expire in minutes. A session pass is a normal shell for 60 minutes, and a session can ask
for one only on a server where the owner has turned that on (off by default; see below).

**Server to GitHub.** Only if you enrol a repository for the merge executor. The token belongs to a dedicated bot account.

## Who is trusted with what

| Who | Can | Is trusted to |
|---|---|---|
| **Person with a shell on the Crewboss host** | everything: read the database and the key files, create or reset the owner | be the owner. There is no defence against this person |
| **Instance owner** | everything in the app: users, servers, keys, every project | the same as above |
| **Designer** (account role) | create projects; work in projects they own or are shared into; enrol runners; deploy to servers the owner pinned to their project | run code on their own PCs and on the servers pinned to their projects |
| **Project Admin / Member / Viewer** | Admin approves production deploys and merges, answers an agent's questions to the owner and decides recommended changes; Member works, runs agents, deploys to dev and staging, comments and ticks steps; Viewer reads and can write nothing, including through the assistant | stay inside the projects shared with them |
| **Runner PC and its user** | receive that person's jobs and assistant chats, with the task text in them | everything that PC can already do |
| **AI agent in a job** | use the tools its profile names, in a worktree, as the runner's user | nothing. Its input is treated as untrusted, but see the limits below |
| **Standing session** (a Claude session holding a session token) | read and write its project through the API; request an SSH pass only for a commissioned server whose owner switch "AI sessions may open a shell" is on | act for the project's owner |
| **Client** (satellite token) | read one project's client snapshot and file a request | nothing. Client text is marked untrusted everywhere |
| **Reporter, scanner, digest, manifest tokens** | their one endpoint | nothing else |

Two points in this table surprise people:

* **Pinning a server to a designer's project gives that designer a shell on it.** A deploy target has a free-text post command,
  and a dev or staging deploy runs with no second approval. Pin only servers you would give that person an SSH login to.
* **A session token can be a server credential, but only where you allow it.** Each commissioned server has a switch, "AI sessions
  may open a shell", that is **off** until the owner turns it on (owner only, with a fresh second-factor check in the last 5 minutes). While it is off, a
  session token is refused when it asks for a pass. While it is on, a session can get a 60-minute shell on that server and no
  person is asked. A server reached through a jump server needs the switch on for both, because its pass opens both. Passes a
  person approves (a deploy, a server read, Check access) do not use the switch. Turn it on only for servers you would give an
  unattended AI session a shell on, issue session tokens only on hosts you control, and do not switch it on for a server in an
  account whose sessions read text from people you do not trust.

## What Crewboss enforces in code

* No page, API route or MCP tool works without a signed-in person or a valid token. There is no default password and no web
  sign-up: the owner is created from the server's shell.
* A project you cannot see answers 404, the same as one that does not exist.
* A job cannot deploy, merge or approve. It can only ask. A person approves a deploy or a merge on a page, with a recent second-factor check
  tap for production and for merges. With two or more Admins, the requester cannot approve their own.
* Production takes only a commit that already ran on dev or staging, unless a person marks a hotfix and gives a reason.
* A deploy can move a server only to the exact approved commit.
* The assistant (CA) changes things only through cards that a person taps. The card shows the full text that will be recorded.
  A comment, a ticked step, an answered question or a decided change needs the person's right to change that project, checked
  when the card is made and again when it is tapped; answering a question or deciding a change needs the project's Admin or
  Owner. The record names the person, not the owner. Demoting someone to the Viewer role revokes their runners.
* A task can be moved into another project only by someone who can change that project. A project you cannot see answers the
  same 404 whether it exists, is archived or belongs to another account.
* Key values travel only over https to the key store; an `http://` address is refused (except localhost).
* A job starts only if every agent, command, skill and settings file in the repository matches a hash the project's Owner
  approved. Settings that add hooks or widen permissions block the job.
* Key values are written one way to your key store and never stored, logged or shown. The SSH authority's private key is
  decrypted only for one signing call.
* Uploaded files are served with a sandboxing policy and are never run.

## What Crewboss does not protect you from

* **A compromised Crewboss host or database.** Back up, patch and restrict it like any server that can deploy.
* **A runner PC's owner.** They are fully trusted for their own jobs. Their Claude sign-in, API key, git credentials, SSH keys
  and files are on that PC, outside Crewboss's control.
* **An agent that is allowed to run commands.** The worktree is not a sandbox. A profile that names `Bash` lets the agent run
  any command as the runner's user: read files outside the worktree, use the PC's credentials, reach the network. The tool list
  limits which tools exist, not what a shell does. If a job will read text you did not write (a client request, an issue, a
  log, a web page, a dependency's README), assume that text can steer the agent. Run such jobs on a PC or VM that holds nothing
  you would mind losing, under an account with no production credentials.
* **Prompt injection in general.** Task text, comments, knowledge, handoffs, repository files and server logs all reach agents.
  Crewboss labels untrusted text and tells agents not to follow it. That lowers the risk; it does not remove it. The hard stops
  are the approvals above, so read what you approve. An agent can write a convincing reason for a bad deploy.
* **Standing sessions.** Their safety rules ("production changes need the owner's word") live in the session's briefing, not in
  code, and they hold a token that can obtain a server shell. They are for a host and an owner you trust completely.
* **Code you approve.** A merged commit runs on your servers with the deploy user's rights, including the target's post command.
* **What a job self-reports.** "Tests green" and "audit passed" on a merge request are recorded by jobs. They inform the Admin's
  decision; they are not proof.
* **Server-read output.** Redaction of log output is best effort. Do not declare a read over a file that holds secrets.
* **Other people on the same instance, beyond project boundaries.** Crewboss separates projects, not tenants. Everyone shares
  one database and one app process, and the owner sees everything. Do not host mutually hostile organisations on one instance.
* **Denial of service.** There are sign-in and enrolment rate limits, but no general request limiting. Put that in your proxy
  if the instance faces the internet.
* **Sign-in switched off.** `CREWBOSS_WEB_AUTH=off` makes every page the owner's with no login. It is for tests. Never use it
  on a machine with a browser you use for anything else, or on any shared network.

## Defaults, and what happens if you expose it as installed

* The app listens on `127.0.0.1` only. The database is not published at all.
* A non-local install is expected to have a publicly trusted certificate; the installer sets `tls_check: enforce`, and the app
  then refuses to start without one.
* Passkeys are bound to the host name in `public_url`. If you put a default install behind a proxy on another name without
  changing `public_url`, nobody can sign in. It fails closed.
* **Password plus authenticator app is not phishing-resistant.** A fake page can relay the password and a code within 30 seconds.
  Passkeys are bound to the real address and are. An internet-facing install should switch passkeys on for everyone, and the owner
  should use one. The Front door card shows how many people still sign in without one. The authenticator secret is encrypted with
  `local-auth.key`, recovery codes are keyed hashes, passwords are argon2id with a common-password list, and sign-in rate limits are per IP, per user and IP, and a per-user ceiling. Authenticator codes have one more limit with no time window: after the right password, 10 wrong codes in a row slow sign-in down (a doubling delay), 100 lock the code step until a recovery code or an admin's Reset sign-in, and the count is cleared only by a right code, a recovery code or a reset; it is raised only by someone who has the password, so a stranger cannot lock the owner out. The step-up code check on the account page (a signed-in session proving itself again) is counted separately, per session and not on that count: after 10 wrong step-up codes that one session is signed out and an audit line is written, so someone holding a stolen cookie can end their own access by guessing but can never lock the owner's sign-in. The owner sees both in the audit log. A new password or authenticator ends the person's other sign-ins.
* **A chosen password is back.** It sits in a database dump as an argon2id hash. Reuse elsewhere is the classic risk; the second factor covers it.
* **LAN mode: a self-signed certificate and a click-through.** The first visit cannot tell the real server from an attacker on the same
  network (ARP or DNS spoofing). The installer prints the certificate's fingerprint and `/trust` explains how to compare it. Firefox and
  Safari remember the exception per certificate; Chrome and Edge forget it after about a week or a restart, so people learn to click
  through. An attacker who gets in gains the password and a live-relayed code, so a session. That is why LAN mode is for a local
  network only: "LAN mode is for a local network. Don't expose it to the internet without upgrading to a signed certificate and passkey
  sign-in." The session cookie stays `Secure`; HSTS is never sent on LAN hosts; passkeys are not offered there.
* **The LAN key** (`frontdoor/lan.key`, 0600) is readable by the proxy and the app. Whoever reads it can impersonate the LAN server to
  browsers that clicked through. It cannot decrypt past traffic (ephemeral key exchange). Runners are still safe: they act only after the
  server proves its identity with its ed25519 key, and a runner pins the certificate it enrolled against. There is no certificate authority
  that could forge other websites; that is why an own-CA design was dropped.
* **The admin panel can change the front door.** Owner only, with a fresh step-up (5 minutes) and the same-origin guard. The sidecar accepts
  only typed actions from the token holder. A compromised app could at most attach a well-formed domain (get a certificate for a name
  that serves the app it already controls), remove it, or rotate the LAN certificate. It cannot write Caddy config, serve files, proxy
  elsewhere, turn the secure connection off or use Docker. Every action is audited with the user. A public-address guard only warns.
* Over plain http on a non-local name, passkeys do not work and the session cookie is not marked Secure. Do not run that way.
* `trusted_proxies` defaults to loopback only, and the installer writes your actual proxy's address. A proxy that reaches the app
  from another address (a container network, another machine) must be listed there, or every sign-in attempt is counted against
  the proxy's address. If you change the port binding to `0.0.0.0` with no proxy, the sign-in still holds, but traffic is
  unencrypted.
* The app container runs as root and has write access to the materials, uploads and exports folders on the host. A flaw that
  gave code execution in the app would have that reach.
* Responses carry `X-Content-Type-Options: nosniff`, `Referrer-Policy: same-origin`, `X-Frame-Options: DENY` and, on pages,
  `Content-Security-Policy: frame-ancestors 'none'`. There is no full Content-Security-Policy yet (the pages use inline
  handlers), so a script injected into a page would not be stopped by the browser. Add `Strict-Transport-Security` at your proxy.

## Safe deployment checklist

1. **Install with `./crewboss install`.** It generates the database password and the key files, sets file modes to 0600 and
   folders to 0700, and keeps the app on `127.0.0.1`.
2. **Serve it over https with a trusted certificate**, through the bundled proxy or your own. Keep `tls_check: enforce`.
3. **Set `public_url`, `origin_hosts` and `mcp_allowed_hosts` to the real host name** and nothing else. A sibling site listed
   in `origin_hosts` can make your browser write to Crewboss.
4. **Set `trusted_proxies` to your proxy's address only.** The installer does this; the default is loopback only.
5. **Add `Strict-Transport-Security` at the proxy**, and strip the `X-Crewboss-Front-Door` request header. The app sends the
   framing, sniffing and referrer headers itself.
6. **Prefer a private network** (VPN, mesh or LAN) for the instance and its runners. If it must face the internet, add request
   limits at the proxy.
7. **Create the owner from the server shell**, finish the password and authenticator-app setup at once, and store the recovery codes offline. On an internet-facing install add a passkey on two devices.
8. **Give people the least role that works.** Viewer for readers. Leave the per-server "AI sessions may open a shell" switch off
   unless you want that. Pin servers only to projects whose Owner you would give SSH
   access to. Use two Admins on any project that deploys to production, so the two-person rule applies.
9. **Keep agent profiles narrow.** Use a profile without `Bash` for jobs that only read or review. Do not allow unattended
   (API-mode) jobs on tasks that carry outside text.
10. **Run runners as a dedicated low-privilege user** (not your own login, never root) on a PC or VM that holds no production credentials, with a git credential limited to
    the repositories it works on. Enrol over https (`--insecure-http` is for localhost only). Revoke a runner the day its PC is lost or handed on.
11. **Use a dedicated login user on each target server**, with only the `sudo` rights the deploy needs. Removing the
    certificate-authority line from its `authorized_keys` revokes Crewboss's access to that server.
12. **Use a dedicated bot account for the merge executor**, with write access to the enrolled repositories only, and apply
    branch protection yourself.
13. **Back up** the database, the materials and uploads folders, and the secrets folder. Keep `local-auth.key` and
    `ssh-ca-enc-key` apart from the database backup: together they undo the encryption at rest. Test a restore.
14. **Update** by pulling a recent `main` and rebuilding the image; this also picks up base-image and dependency fixes.
    Dependencies are version ranges, so rebuild on a schedule and read the release notes.
15. **Watch the audit trails:** the instance audit on the Users page, each project's activity, and the list of signed passes.
    Rotate a token or revoke a runner at the first doubt.

## If something goes wrong

| Event | Do this |
|---|---|
| A person leaves or an account is suspect | Disable the user (ends their sessions, runners and tokens), then review their projects' activity |
| A runner PC is lost | Revoke the runner; rotate the git and Claude credentials that were on it |
| A session token leaks | Delete its row (it stops on the next call); review the passes signed for its account |
| The Crewboss host is compromised | Remove the certificate-authority line from every server, rotate the merge bot token and key-store credential, restore to a clean host, make a new runner identity key and re-enrol every runner |
| The owner loses their password, or their authenticator app and every recovery code (a recovery code alone is not enough: it also asks for the current password, owner decision q828) | `./crewboss reset-owner` on the server shell |

## Reporting a vulnerability

Report privately through the repository's **Security** tab ("Report a vulnerability"). Do not open a public issue. The full
policy is in [SECURITY.md](../SECURITY.md).
