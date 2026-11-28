# Reference notes

These are the detailed implementation notes that used to be the README. The README and the guides in this folder are the
starting point; this file holds the specifics. Crewboss is a self-hosted board for projects, tasks, workflows, knowledge and AI sessions. Its assistant is **CA**
(Crewboss Assistant). Settings are `CREWBOSS_*` environment variables (see `docs/configuration.md`).

- `app/`      FastAPI web app + REST API + MCP (tasks, knowledge, activity, workflows, later System Health)
- `scanner/`  workflow + AI discovery over your repos → `catalog` JSON (schema: `docs/catalog-schema.md`)
- `db/`       dashboard's own Postgres migrations (never the ERP's database)
- Client mini dashboard (tag `mini-dashboard`, task #126): DORMANT dashboard-side pieces only (`tasks.client_visible`,
  the client-safe snapshot, client request intake). The standalone client-server app was retired 2026-10-01; the
  mini dashboard will be an app in the ERP Customer Portal, fed by PUSH from here (decision k21), once that portal is designed.

## Install and run (task #707, change #62)

One install command and one start command; the app and Postgres run in Docker, published on 127.0.0.1 only. You need Docker
Engine with Compose v2, Python 3.10+ and a clone of this repo.

    ./crewboss install      # asks: localhost or network? (network: domain, proxy), then builds, starts, makes the owner and prints a one-time temporary password
    ./crewboss up | down | restart | status | logs [-f] [app|db|caddy]
    ./crewboss owner [--reset]
    ./crewboss reset-owner

Every question has a flag, so it runs unattended: `./crewboss install --mode network --domain crew.example.com --proxy bundled --yes`. `--home DIR` (default `~/.crewboss`) is the instance folder; it holds `instance.yaml`,
`compose.env` (the env file the CLI passes to compose: project name, port, database volume, folders, a random database password;
0600; the repo's own `.env` is never read), `secrets/` (0700: `ssh-ca-enc-key`, `vapid-private.pem`, `runner-identity-key`, made by the
installer (same recipes as the sections below), never overwritten) and `materials/`. `--project NAME` (default `crewboss`) names the compose project;
the installer refuses a name that already has containers or volumes, and a `--home` that is not missing or empty (unless it is a re-run of the same project), is a git checkout or holds a foreign `instance.yaml`, or
is a symlink to another install. If the default port 8931 is taken it offers the next free one. `down` never removes data;
`down --delete-data` asks you to type the project name. Every compose call is pinned with `-p`, `--env-file` and `-f`, with all
`COMPOSE_*`, `CREWBOSS_*` and `POSTGRES_*` variables removed from the environment, so it cannot act on another project.

**Localhost** (try it out): plain http on `http://localhost:<port>`, no proxy. **Network**: always a publicly trusted certificate
(Let's Encrypt), never plain http. After "localhost or network?" it asks for the domain, which sets `public_url`, the passkey RP ID
and the certificate name (keep it for good: passkeys are tied to it), and checks that it resolves and points at this server (a
warning, `--skip-dns-check` to go on, `--public-ip` for NAT, `--skip-checks` to skip; no domain: it prints a DuckDNS walkthrough). Then:

- *I already run a reverse proxy with HTTPS*: the app stays on 127.0.0.1; the installer prints (and saves in `proxy/`) a ready-made Caddy
  and nginx snippet that routes to the app port, strips the incoming `X-Crewboss-Front-Door` and `X-MDD-Front-Door` request headers and
  allows the websocket upgrade for `/runner`.
- *No*: the bundled Caddy (compose profile `caddy`, the official image, a Caddyfile in the instance folder) gets a certificate by
  HTTP-01. Port 80 answers only the challenge and a redirect, 443 serves the app. The installer cannot test reachability from outside;
  it prints what must be true. TODO(#707): DNS-01 needs a Caddy build with a DNS provider module (a later custom image).

**Certificate check** (`app/tls_check.py`): when `public_url` is https and not localhost, the app fetches it with normal verification at
start-up and every 12 h; a failure (untrusted, expired, wrong name, under 7 days left) is logged loudly and shown as a banner on every
page. Instance setting `tls_check`: `warn` (default when the key is absent: never blocks start), `enforce` (also refuses to START, after
a 150 s grace for a first certificate; the installer writes it for new network installs) or `off`. `tls_check_connect: host:port` makes
the check dial that address (the bundled proxy) while still verifying the public name. If the server cannot reach its own public name,
use `warn`.

**Add-on mounts.** The base compose is generic. Mounts for other systems (Zabbix token, ERP health tokens, Entra client secret, a
platform-key writer credential, an estate docs folder, a linked repo's `.git`) live in `docker-compose.addons.example.yml`: copy it and
set `COMPOSE_FILE=docker-compose.yml:<your-file>` in the env file compose reads (an absolute path to your own file works too).

## Instance folder

Each install keeps its own data and config OUTSIDE this repo, in `CREWBOSS_HOME` (default `~/.crewboss`; mounted read-only
at `/crewboss-home` in the container). Copy the examples in and edit:

| File | From | Holds |
|---|---|---|
| `instance.yaml` | `instance.example.yaml` | name, URLs and hosts, servers, project roles, platform key stores, add-ons |
| `drift.yaml`, `health_feeds.yaml` | `drift.example.yaml`, `health_feeds.example.yaml` | linked admin sites; how project health arrives |
| `seed.yaml` (optional) | `seed.example.yaml` | the project and tasks created on first start (default: one "example" project) |
| `denylist.txt` (optional) | | values that must never be committed here, one per line |
| `scanner/annotations.yaml`, `scanner/scanner.yaml`, `scanner/health_map.yaml` (optional) | `scanner/*.example.yaml` | the hand-curated workflow annotations, which repos the scanner reads and where its rescan job imports (`scanner/config.py`) |
| `sessions.env`, `session-briefing.json` (optional) | `sessions/sessions.example.env`, `sessions/session-briefing.example.json` | the standing Claude sessions' dashboard URL, token folder, state folder, overseer project and briefing wording (`sessions/README.md`) |
| `tests/` (optional) | | the checks on this install's own annotations: `.venv/bin/python -m pytest $CREWBOSS_HOME/tests -q` |

No instance.yaml means a fresh install with neutral defaults: the app still starts (set the project roles to get an
overseer). Environment variables still override instance.yaml. Run `scripts/install-hooks.sh` once per clone: the pre-commit
hook (`scripts/commit_guard.py`) blocks a commit while a tracked file contains a denylist value or a secret-shaped string;
`scripts/denylist-allow.txt` lists the paths still waiting for later scrub slices. The hook also runs gitleaks
(`.gitleaks.toml`) on the staged changes when it is installed; CI (`.github/workflows/guard.yml`) requires it and scans the whole
tree. Tests run against `tests/home`, never your real `CREWBOSS_HOME`.

Wire names: new API tokens start `cb_` (runner device `cbr_`, job `cbj_`) and the front-door header is `X-Crewboss-Front-Door`;
the pre-rename `mdd_`/`mdr_`/`mdj_` tokens and `X-MDD-Front-Door` are still accepted until every live holder was re-issued
(`TODO(#589 compat)` marks each place; `tests/test_wire_compat.py` goes with them).

## Signing in (task #706; password + authenticator by default since #976)

`auth:` in instance.yaml (or `CREWBOSS_AUTH`) is `local` (default) or `oidc` (Microsoft sign-in, optional). An install that already has
OIDC configured (client id and secret file) and no `auth:` key stays on `oidc`. Local sign-in is a password plus a 6-digit code from an
authenticator app, in every mode. A password is 12 to 128 characters, with no composition rules, and is checked against a bundled list of
common passwords. Passkeys (passwordless sign-in) are an optional upgrade, offered only where the install has a signed certificate (or on
`localhost`); a person who has a passkey and no password is asked once to add a password and authenticator app as a backup. The old
`/auth/login/legacy` page is gone. First run:

    ./crewboss owner                        # or: python -m app.cli owner create. Prints a ONE-TIME TEMPORARY PASSWORD (24 h, single use)

then at `/auth/login` enter the username and the temporary password (leave the code empty), choose your own password, scan the QR code (or
use the key shown under "Can't scan it?"), type a code from the app and save the recovery codes. A recovery code opens the same restricted session
(your CURRENT password, then a new authenticator, then new codes; there is no new-password branch). Wrong codes after the right password are counted
with no time window (10 in a row slows sign-in down, 100 locks it until a recovery code or Reset sign-in), and a new password or authenticator ends the person's other sign-ins. Lost the phone and the codes too? An admin's "Reset sign-in" (Users page) gives a
person a new temporary password and wipes ALL their sign-in factors (password, authenticator, passkeys, recovery codes); the owner's own way
back is `./crewboss reset-owner` on the server (it wipes all of the owner's factors the same way and prints a
new temporary password; it exists nowhere in the web app). Passkeys need `public_url` to be https (or localhost). Sensitive actions (keys,
tokens, roles, servers, deploy and merge approvals) need a fresh authenticator code (or passkey tap) in the last 12 hours (`CREWBOSS_WEB_STEPUP_HOURS`), the most sensitive
5 minutes; the owner sets how long a sign-in lasts (1 hour to 90 days, default 30 days) on the Users page. The key file
`$CREWBOSS_HOME/secrets/local-auth.key` (`CREWBOSS_LOCAL_AUTH_KEY_FILE`, created 0600) keys the recovery-code hashing: back it up SEPARATELY from the database backup. In the
shipped compose it lives on its own writable volume at `/crewboss-secrets`; `owner create` makes it and fails loudly if it can't. Sign-in rate limits read the client address from `X-Forwarded-For` only when the request comes from a
`trusted_proxies` address (instance.yaml or `CREWBOSS_TRUSTED_PROXIES`; default loopback only; the installer writes the proxy's address). If you expose the
app straight on a LAN with no proxy in front, set `trusted_proxies: []`. The account page is `/account/security`. Code: `app/local_auth.py`, `app/routers/local_auth_pages.py`, `tests/test_local_auth.py`, `tests/test_passwordless.py`.

## More than one person (task #713)

Local sign-in is the multi-user mode (Microsoft mode keeps its list of super admins, who are all owners). The owner adds
people from **Users** (avatar menu, `/admin/users`): a username, a role and optionally an email; Crewboss shows a one-time temporary
password once (24 hours, one use, there is no email) to hand over. The person uses it only to choose a password, set up an authenticator app and save recovery codes;
the owner can Reset sign-in, disable the person, clear their sign-in email or sign them out everywhere.

| Role | Sees | Can change |
|---|---|---|
| owner | everything, plus users, servers, platform keys, health, the global plan | everything |
| designer | the projects they made, and projects shared with them | those, by their role in each project: Owner (creator), Admin, Member, Viewer (below) |
| viewer | projects shared with them | nothing |

Per project: **Owner** (creator: adds people, sets roles, transfers ownership), **Admin** (approves merges to main and production
deploys), **Member** (works, runs agents, deploys dev, requests production), **Viewer** (read only). Production is promote-only: a commit
must have run on dev or staging first (or, for a "production only" project such as a website, on a green runner preview), an Admin approves it
with a fresh second factor (another Admin if there are two or more), and a one-tap rollback restores the previous release. See
`docs/multi-user.md` (Releases and Team pages: `/<project>/releases`, `/<project>/team`).

What the dashboard enforces and what it only records: a deploy to production is refused unless the commit ran on dev/staging (or has
a runner preview, for a production-only project) and an Admin approved it with a fresh second factor; the executor deploys only
approved rows. For merges to main the dashboard gates the *record*: a job reports its tests and a separate code-auditor pass through the job MCP
(`record_check`), an Admin presses **Approve merge** on the Releases page (fresh second factor; another Admin when there are two or
more), and the job then reports the merge (`report_merge`), which is accepted only for an approved request. The git merge itself is done by the job,
so the second lock is GitHub branch protection: the project's Owner presses **Protect main on GitHub** (a confirm card showing the exact
API call; it uses the machine's `gh` login). A deploy target's commands (path, post command) are defined only by the project's Owner on a server
the instance owner pinned to that project; Members and Admins only deploy what is defined.

A project someone may not see is a 404, the same as a missing one, on every page, list, search, SSE stream, MCP tool and runner
call. Sharing is per project: sharing a parent does not share its children, and sharing a child does not share its parent.
A designer's new projects are plain projects (never accounts) with no standing Claude session on this machine; their agents run
on their own runner. Rules, the access layer and the route-coverage test: `app/access.py`, `tests/test_isolation.py`,
`tests/test_multi_user.py`, `docs/multi-user.md`.

## Runner identity key (task #627)

Runners pin Crewboss by the fingerprint (`fp8`) of one ed25519 key. The app only READS it; make it ONCE on the host,
before the first deploy, or enrolment answers 503:

    .venv/bin/python -m app.runner_identity keygen      # writes $CREWBOSS_SECRETS_DIR/runner-identity-key (default ~/.config/crewboss) (0600), never overwrites

docker-compose.yml mounts it read-only at `/run/secrets/runner-identity-key`. A new key invalidates every issued code and
every runner's pinned fingerprint, so back it up with the other secrets and never regenerate it casually.

## Runner jobs (task #627, P2)

A runner (device token `mdr_`) claims **jobs**; each job is one run of one task by one agent made from a profile
(`agent_profiles`, seeded from the model routing table: `builder` sonnet, `chores` haiku, `designer` opus; no fable profile).
Migration `0038_jobs_locks.sql`: `projects.git_remote`, `agent_profiles`, `jobs`, `locks`, `fence_seq`. Rules are in `app/jobs.py`.

| Route | Auth | What |
|---|---|---|
| `claim` message on `WS /runner/ws` (REST `POST /runner/jobs/claim` answers 403 to everyone) | verified socket | next job this device may run, ONE SQL statement: same account, own user's jobs only; a subscription runner only approved jobs, an API runner only unattended-eligible jobs and never a client-authored task. 200 = ids-only payload + a job token (`mdj_`), 204 = none |
| `heartbeat` / `finish` / `release` messages `{job_id, fence}` on the socket (`release` requeues a job the runner could not start; the claim payload carries `max_seconds`, `CREWBOSS_JOB_MAX_SECONDS`, default 4 h) (REST heartbeat answers 403) | verified socket | renews the lease; 409 on a stale fence |
| `/runner/mcp` (MCP) | job token only | `job_brief`, `job_comment`, `job_tick_subtask`, `acquire_lock` (merge locks only), `release_lock`, `request_deploy` (P5) (no `job_done`: a device job ends only through the runner's `finish` message); every call checks the fence (stale = "409 Conflict") |
| `/<account>/runners` | web sign-in | lists runners (status plus a live online/offline chip) and jobs (state, device, profile, fence) and locks; Clear on an orphaned lock |

- One live (queued/running) job per task (partial unique index). The payload never carries task text: the agent reads it through `job_brief`.
- Job tokens are minted per job at claim, scoped to that job, and resolve only while the job is running, its device is not revoked or expired and its user is not disabled. They are cleared when the job finishes, is requeued or its device is revoked (revoking ends the device's running jobs like an expired lease). `require_session`, `/ssh-cert` and the main `/mcp` refuse them. A valid token with a stale fence gets "409 Conflict".
- A new fence (`fence_seq`) is drawn at every claim and every requeue; the branch is `dash/j<id>-e<fence>`.
- Leases (`CREWBOSS_JOB_LEASE_SECONDS`, default 90) are judged on database time by a sweeper (`CREWBOSS_JOB_SWEEP_SECONDS`, 0 = off). A clock jump or sleeping host (`CREWBOSS_JOB_CLOCK_JUMP_SECONDS`) gives one lease of grace. An expired job (or one whose device is revoked) requeues with a new fence (`CREWBOSS_JOB_MAX_ATTEMPTS`, then it fails for a human); one holding a deploy/migrate lock is failed and the lock marked **orphaned**, never retried, until a person clears it. A merge lock just goes.
- Locks are keyed `(account, kind, resource)`; the account is the job's, the resource a project slug in that account (another account's is NotFound).
- Since P5 a job token may take only a **merge** lock: `deploy` and `migrate` are refused (only the server-side executor takes the deploy lock). Deploys: see "Deploy requests and the executor" below.
- The runner package (P4) is `runner/` (see "Runner program" below).

## Runner WebSocket (task #627, P3)

`WS /runner/ws` (`app/runner_ws.py`; the wire protocol, precise enough to write a client from, is `docs/runner-protocol.md`).
A runner holds one outbound socket: it is the runner's liveness, the "a job is available" channel and the place a revoke lands.

- **Refused before the upgrade:** a Host not on the allowlist (the MCP transport allowlist, Crewboss's own host from `CREWBOSS_RUNNER_BASE_URL`/`CREWBOSS_PUBLIC_URL`, plus `CREWBOSS_RUNNER_ALLOWED_HOSTS`, comma-separated); ANY `Origin` header; anything but a live device token (`mdr_`, in the Authorization header; job and session tokens are refused).
- **Mutual signed challenge:** the server signs with the runner identity key (the runner checks the key it pinned), the device signs with its own key. The transcripts bind both nonces, the device id and the Host the runner dialled (replay, relay and reflection fail). Repeated every `CREWBOSS_RUNNER_WS_CHALLENGE_SECONDS` (20); a wrong answer closes with 4401, no answer within `CREWBOSS_RUNNER_WS_PROOF_TIMEOUT_SECONDS` (10) with 4408.
- **Liveness:** a verified socket updates the device's `last_seen` only. It does **not** renew job leases: the REST heartbeat stays the only renewal, because it names the job and the fence and the broker checks both (a socket is per device and cannot say which job a runner still holds). So fencing is untouched, and a runner with a live socket but a hung agent still loses its job.
- **Nudges:** when a job becomes claimable (queued with approval, approved, or requeued after a lost runner) every connected socket of a device that could claim it (the claim's own eligibility SQL, shared) gets `{"type":"job_available","job_id","task_id"}`, ids only. On connect, jobs already queued are offered the same way. The runner then claims over the socket.
- **Revoke:** `devices.revoke` closes the device's live socket with **4401** right after its commit (within 1 s), on top of `end_for_device` releasing its leases. Backstop: every `CREWBOSS_RUNNER_WS_RECHECK_SECONDS` (5) each socket re-checks its device, so an expired token or a disabled user closes it too (disabling a user has no app code path yet; `runner_ws.close_user` is there for when it does).
- **One app process:** the registry of live sockets is per process, in memory. That is right for the single app process; two processes would need a shared bus (Postgres LISTEN/NOTIFY) so a nudge or revoke reaches the other one.
- One verified socket per device: a newer connection replaces the older (4409) only once it has passed its first signed challenge, so a stolen token alone cannot knock the real runner offline; at most one unproven socket per device is parked. Frames are limited to 8 KiB (`--ws-max-size` in the Dockerfile; also checked in the app): an agent file for review travels in chunks of ~5.5 KB, reassembled and hash-checked on the server.

## Runner program (task #627, P4)

`runner/` is the `crewboss-runner` package (Python 3.12+, `pipx install ./runner`): `enrol <code>`, `run`, `status`, `forget`. It
holds the P3 socket, claims and heartbeats over it (REST claim/heartbeat now answer 403 to everyone, so a stolen device token
can no longer take or hold a job without the device key), runs each job in a git worktree on `dash/j<id>-e<fence>` with Claude Code
(`claude -p` for an API runner, `claude --remote-control` for a subscription runner), pushes the branch and finishes the job.
Keys and the token are in the OS keyring (a 0600 file if there is none, reported by `status`). The job token is in a 0600 file
outside the worktree. It checks `claude` and `git` before claiming (and, in subscription mode, that Claude Code is signed in: `claude auth status`, exit code only; a PC that is not signed in shows "needs attention" on the Runners page and takes no jobs until you run `claude` there and log in), watches a new subscription session for 120 s and gives the job back with a push to you if it sits at a login prompt or never registers, releases a job it cannot start (and backs off), kills the agent on shutdown or after `max_seconds`. The runner never sends the PC's Claude login or API key to Crewboss. Details, the spawn spike and
the new `claim`/`heartbeat`/`finish` messages: `docs/runner-protocol.md`. Tests: `tests/test_runner_program.py` (fake dashboard
+ fake `claude`), `tests/test_runner_socket_jobs.py`.

## Start a job from a task, git remotes, labels (task #627, P4b)

- **Run.** The task window has one **Run** button (under the Now line; profile, labels and runner are in its Run settings popover) and a job is queued for
  the signed-in person (`POST /<project>/tasks/<id>/run`). The starter's click is their approval, so their own subscription runner
  can take it. The drawer shows the job's state (queued, running on <device>, done, failed, cancelled) with the branch, and a
  Cancel (`POST /<project>/tasks/<id>/run/cancel`, the starter only) that ends the lease cleanly: the job is `cancelled`, its token
  dies, merge locks go, deploy/migrate locks are left orphaned for a person, and the runner's next heartbeat or finish is a 409 so it
  stops. Only a real Entra web session behind the same-origin guard can start or cancel; a device or job token is refused (403),
  even next to a cookie. The task must belong to the project in the URL (another account's is a 404). Client-authored tasks never run
  unattended. There is no MCP tool to queue jobs.
- **Git remote.** Owner-only project details (the project's Settings tab, "Edit repo and environments") now set `projects.git_remote`: only
  `https://github.com/<owner>/<repo>.git` or `git@github.com:<owner>/<repo>.git` (hosts in `config.GIT_REMOTE_HOSTS`; no leading `-`,
  no other forms). When `repo` is `owner/name` the form suggests the matching remote. A project needs one before a job can be run.
- **Labels.** Migration `0040_runner_labels.sql`: `device_tokens.labels`, `agent_profiles.requires_labels`, `jobs.requires_labels`
  (copied from the profile at creation plus any added on the Run form) and the seeded `android` profile (sonnet; Read, Edit, Write,
  Glob, Grep, Bash; task type `android`; needs `android-usb`; not unattended). Labels are lowercase `[a-z0-9-]{1,32}`, at most 16 per
  device, set by the owner per device on `/<account>/runners`; a runner cannot set its own. A job is claimable, claimed and nudged only
  by a device whose labels contain all the job's `requires_labels`.

### Enrol a Linux PC

1. **Network first.** A runner reaches Crewboss over a **private network**: we recommend a
   VPN, a mesh network or the same LAN, and never the open internet. The deployment tool only opens the port; reachability is yours.
2. On the PC (Python 3.12+, `git` and `claude` installed and logged in): `pipx install "git+https://github.com/crewboss-dev/crewboss.git#subdirectory=runner"` (or `pipx install ./runner` from a clone).
3. In Crewboss open **Machines** (Runners tab), choose the mode (Subscription = the person's own Claude login; API key) and press
   "New enrolment code". The code is shown once and lives 5 minutes.
4. On the PC: `crewboss-runner enrol '<code>'`, then keep `crewboss-runner run` running as a systemd **user** service
   (`~/.config/systemd/user/crewboss-runner.service` with `ExecStart=%h/.local/bin/crewboss-runner run`, `Restart=on-failure`,
   `WantedBy=default.target`; then `systemctl --user enable --now crewboss-runner`). The Machines page's how-to (Runners tab) has the full unit.
   `crewboss-runner status` prints state; `journalctl --user -u crewboss-runner` shows the local status page address.
5. Back on the Machines page (Runners tab), give the PC its labels. For Android work with a phone on USB (adb), add `android-usb`.
6. Set the project's git remote, open a task, press Run.

#### Windows: use WSL

Most Windows users already SSH into a Linux VM. Enrol that VM as a normal Linux PC (above). Native Windows is supported (the PowerShell tab of the in-app how-to, see [runners.md](runners.md); it is new and not yet tested on real hardware at 0.1.1). WSL2 is the alternative, for people who prefer a Linux toolchain on the Windows machine:

1. In an admin PowerShell: `wsl --install -d Ubuntu`, then reboot.
2. In Ubuntu: `sudo apt install python3 pipx git`, install Claude Code, and sign in (`claude`).
3. The same steps as above: `pipx install "git+https://github.com/crewboss-dev/crewboss.git#subdirectory=runner"`, then `crewboss-runner enrol <code>`, then `crewboss-runner run`.
4. Keep the WSL session running: the runner stops when WSL shuts down.
5. Reach Crewboss over the private network (a VPN or mesh network) from inside WSL.
6. A USB device such as a phone for adb needs `usbipd-win` to attach it to WSL (https://github.com/dorssel/usbipd-win), then label the runner `android-usb`.

## CA, the Crewboss Assistant (task #702, v1)

A PA-style assistant in a **right-hand drawer on every page** (the header's "CA" button; on a phone the round button in the middle of
the bottom bar). It answers when you type to it, runs the reminders you ask for, and tells you when an agent finishes a task. It is
about software design, build, deploy and test and the designers' tasks; it does no server support. Typing only (voice is v2).

**How it runs.** The Crewboss server is always on; your PCs are runners. Claude runs on YOUR runner, on your own Claude Code
sign-in (or your API key in an api-mode runner): the server never sees that credential and runs no model. A message becomes a *turn*
(`ca_turns`, a slim queue beside the task jobs: a chat turn has no task, branch or worktree). Your runner is nudged over its
verified socket, claims the turn, runs `claude -p --output-format stream-json --model <ca_model>` in an empty folder with **no
built-in tools**, and streams the answer back (`ca_event` frames -> the server -> `GET /ca/stream` SSE -> the drawer). The only
tools Claude has are the CA's MCP tools at `/runner/ca-mcp`, authenticated by a per-turn token (`cbt_...`, dead when the turn
ends) that is scoped to your user and account. The model is `ca_model` in `instance.yaml` (default `sonnet`). The next turn resumes the
Claude session on the same PC, else replays the last messages. If no runner of yours is online the drawer says so ("Your runner is
offline: start it ...") and keeps your words in the box. Each conversation is bound to the page and selected card it was started
from (a New chat takes the page you are on).

**What it can do.** Read your open work across the projects of the account your runner belongs to (waiting on you, open
questions, stuck, stale, running jobs), read a task, list jobs and waiting changes, comment, tick a step, and make, list and cancel
reminders. **Rule 0:** it never records an answer, records a decision, or starts, stops or retries an agent job itself: it puts a
**confirm card** in the chat and the effect happens only when you tap it (a tap is also the approval for a job). ask_owner
questions show as tappable cards (option buttons or a reply box). Task text, comments and questions reach the model marked as
untrusted data, and its instructions say never to follow them.

**Notifications.** When a job you started ends done or failed you get a toast, a badge on the CA button and a Web Push ("Task #N done:
<title>"). Reminders ("remind me at 4pm to ...") are fired by a scheduler thread in the server, no AI: in-app plus push; if the
server was down they are sent on start with "(missed while offline)", once each however many repeats were missed. The scheduler
(`app/ca_notify.py`, `CREWBOSS_CA_TICK_SECONDS`, default 20; 0 turns it off) also fails chat turns whose runner vanished. It is part
of the app process, not a systemd timer. Web Push subscriptions are per install, not yet per person.

**Runbook: bring CA up on the same box as the server (e.g. a trial).**
1. Sign-in must be on (CA works as a signed-in person; with sign-in off the drawer says so). In `instance.yaml` optionally set
   `ca_model:` and `timezone:` (see `instance.example.yaml`). Deploy this build (it applies migration `0060_ca.sql`); the runner
   identity key must exist (`python -m app.runner_identity keygen`, see "Runner identity key").
2. On that box, as the user who will use CA: `claude` installed and signed in (`claude auth status`), `git` installed,
   `pipx install "git+https://github.com/crewboss-dev/crewboss.git#subdirectory=runner"` (or `pipx install ./runner` from a clone).
3. In Crewboss open **Machines** (Runners tab) for the ACCOUNT whose projects you want CA to see, choose **Subscription**, press
   "New enrolment code". The runner's server address must be one the box reaches (same box: `CREWBOSS_RUNNER_BASE_URL` or `public_url`;
   plain-http local development needs `crewboss-runner enrol --insecure-http`).
4. `crewboss-runner enrol '<code>'`, then `crewboss-runner run` (keep it as the systemd user service in "Enrol a Linux PC").
   `crewboss-runner status` should say the link is ready, and the Machines page (Runners tab) shows the PC online.
5. Open any page, press CA, ask "what's waiting on me?". Check: a reply streams in; "remind me in 2 minutes to test" rings in-app
   and by push; start an agent on a task from the card and see "Task #N done" when it ends.

Files: `app/ca.py` (conversations, turns, bus), `app/ca_tools.py` + `app/ca_mcp.py` (tools, cards), `app/ca_notify.py` (notices,
reminders, scheduler), `app/routers/ca_pages.py` (/ca/...), `runner/crewboss_runner/ca.py`, `app/templates/partials/ca_drawer.html`,
`app/static/js/ca.js`, `app/static/css/ca.css`. Tests: `tests/test_ca.py`, `tests/test_ca_runner.py`. Not in v1: voice, watchers
that wake CA on Health problems, the code re-plan, log-pull fault detection, retiring the dash session, the API-key gateway (#725).

## Deploy requests and the executor (task #627, P5)

PCs never hold SSH material. A running job calls the job MCP tool `request_deploy(fence, target, sha)` (a target name of its
project, a full 40-hex commit); that makes a `requested` row in `server_jobs` and a phone push. A person approves it on
`/<account>/approvals` (Entra web session only, same-origin-guarded form posts; Approve / Refuse record who and when), and then the
dashboard's own executor takes the target's deploy lock, signs a one-command SSH certificate from the account CA and runs
`git fetch` then `git merge --ff-only <sha>` (so HEAD can only become the approved commit, which must already be on the target's branch) plus the target's `post_cmd`, checking that HEAD equals the approved sha. A job's own branch is not on the target branch yet, so a job asks only for a sha already there; otherwise a person requests the deploy after merging (the approvals page has a Request a deploy form and Ask again on failed cards). A restart mid-deploy marks it
`orphaned` and nothing retries it. Targets (`deploy_targets`, migration 0039; `env` dev/prod and the server's `reach` internal/external, migration 0041) are defined by a person on
the Servers page (task #648, change #52): the page lists each commissioned server with its deploy targets, filter chips (All, Dev, Prod,
Internal, External), and Add a server. The old servers.yml/Zabbix estate view sits in one collapsed block at its bottom until the ERP
has it (#647). The approvals page links there. Settings: `CREWBOSS_DEPLOY_POLL_SECONDS` (5, 0 = off), `CREWBOSS_DEPLOY_TIMEOUT_SECONDS` (600),
`CREWBOSS_DEPLOY_MAX_OPEN_PER_JOB` (5). Only one app process executes deploys (a pg advisory lock). Everything else: `docs/deploy-executor.md`. Tests: `tests/test_deploys.py`.
