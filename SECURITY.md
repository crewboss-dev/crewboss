# Security policy

## Reporting a vulnerability

Please report it privately. Do not open a public issue or pull request for a security problem.

Use GitHub's private vulnerability reporting: open this repository's **Security** tab and choose **Report a vulnerability**.
Say what you found, which part of Crewboss it affects, and how to reproduce it. If you can, say which commit you tested.

We will acknowledge the report, tell you whether we accept it as a vulnerability, and keep you informed until it is fixed.
Please give us time to ship a fix before you publish details.

## Supported versions

Crewboss has no numbered releases yet. Only the `main` branch is supported: fixes go to `main`, and you should run a recent `main`.

## Security model in brief

The full threat model is in [docs/threat-model.md](docs/threat-model.md).

The detail is in `docs/multi-user.md`, `docs/runner-protocol.md`, `docs/server-reads.md` and `docs/deploy-executor.md`. This
is the short version, so you know what is in scope.

**Runners.** A runner is a PC you own that runs Claude Code jobs. It holds one outbound WebSocket to Crewboss, proved in both
directions with a signed challenge (the runner pins the server's identity key at enrolment; the server checks the device key).
A runner holds no SSH keys and no deploy credentials, and it never sends your Claude login or API key to Crewboss. It cannot
deploy and it cannot approve anything: a job can only ask for a deploy, and a person approves it. Each job gets a token that
names that one job and stops working when the job ends, is requeued or the device is revoked. A job that lost its lease is
refused with `409 Conflict`.

**A job with Bash is not sandboxed.** The tool ceiling below limits which Claude Code tools exist; it does not confine what a
shell does. A job whose profile names `Bash` runs commands as the runner's user, with that user's files, credentials, SSH keys,
git credentials and network. A job that reads text you did not write (a client request, an issue, a log, a web page) can be
steered by it. Run runners as a dedicated low-privilege user on a machine that holds nothing you would mind losing, and keep
`Bash` out of profiles for jobs that only read or review. See `docs/threat-model.md` and `docs/runners.md`.

**Tool ceilings.** Each agent profile names the Claude Code tools a job may use (a ceiling). The runner passes that list as
`--allowedTools` and passes every dangerous tool the ceiling does not name (Bash, Write, Edit, WebFetch, SendMessage and
others) as `--disallowedTools`. A ceiling names a whole tool, never a pattern; the server refuses a pattern. A headless job runs
in `dontAsk` mode, so anything not listed is refused. A subscription job whose Claude Code stops at a permission prompt is
given back to the queue, never approved by the runner.

**Agent-file pinning.** A repository can carry `.claude` files (agents, commands, skills, settings) that change what an agent
may do. Before a job starts, the runner walks the whole worktree for them. Every file must match a sha256 pin that the
project's Owner approved after reading it. An unknown or changed file blocks the job (it fails closed). Symlinks are never
pinnable. Settings files that carry hooks or other command-running keys, a wider permission list or environment values block
the job, and no pin can lift that.

**Step-up authentication.** Sign-in is a password plus an authenticator-app code, with recovery codes as the fallback; passkeys are an optional upgrade where the install has a signed certificate, and are the phishing-resistant choice for an internet-facing install. Actions that grant reach or approve
change (keys, tokens, roles, invites, servers, deploy and merge approvals) need a second factor shown recently: within 12 hours
by default, and within 5 minutes for the most sensitive ones (approvals, merges, granting access). Production is promote-only:
a commit must have run on dev or staging first, and a project Admin approves it. With two or more Admins, the approver is not
the requester.

**Secrets are write-only.** Platform keys are set from Crewboss and written one way to each project's own key store through a
signed request. Crewboss does not print, log, store or return the key value or the writer credential, and the store's status
reports carry no values. The SSH certificate authority's private key is stored encrypted, decrypted only for one signing call,
and never shown.

**Server access.** Crewboss signs one-hour SSH passes from a per-account certificate authority, only for servers you have
commissioned and pinned a host key for. There are two kinds. Deploys and server reads use a pass, valid for a short time, that
can run one fixed command and nothing else; a person approves them. A **session pass** is different: it is a normal shell, valid
for 60 minutes, that a standing AI session asks for itself with its session token, and no person is asked. A session can get
one only on a server whose switch "AI sessions may open a shell" the owner turned on. It is off for every server by default, only
the owner can turn it on, and turning it on needs a fresh second-factor step-up within the last 5 minutes. Leave it off unless you want that. See
`docs/server-access.md` and `docs/threat-model.md`.

**Untrusted text.** Task text, comments, logs and anything else written by people outside your team reach agents marked as
untrusted data, and the agents are told not to follow it.

## Hardening your install

* Keep the app on `127.0.0.1` and put a reverse proxy with a trusted certificate in front (`./crewboss install` can set up either).
* Set `trusted_proxies` to your proxy's address only (the installer does this; the default is loopback only).
* Read the [threat model](docs/threat-model.md): what Crewboss protects, what it does not, and who you are trusting.
* Keep runners on a private network (VPN, mesh or LAN), not the open internet.
* Back up `$CREWBOSS_HOME/secrets/` separately from the database. Do not regenerate the runner identity key casually: it
  invalidates every enrolment.

## Keeping private data out of this repository

Nothing instance-specific or secret may be committed. Three layers enforce it. A `pre-push` hook (`scripts/push_guard.py`) blocks a
push that carries a secret pattern, a value from the pusher's private denylist, an internal-network address (RFC1918 or CGNAT), or a
file such as `.env`, `*.pem`, `*.sqlite`, `*.dump`, `*.bak` or `id_*`, and runs gitleaks over the pushed commits. The `guard` CI
workflow repeats the checks that need no private input on every push and pull request, over the full history of the range. Finally,
maintainers can enable an automated read of each pull request's diff by setting the `ANTHROPIC_API_KEY` repository secret; only the
diff is sent. If you find a leaked secret in this repository's history, report it privately as described above rather than in a
public issue, and treat the secret as compromised.
