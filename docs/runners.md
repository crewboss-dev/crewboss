# Runners

A runner is a small program (`crewboss-runner`, in `runner/`) that you install on a PC you own. It connects out to Crewboss (nothing
connects in), claims agent jobs, runs each one with Claude Code in a temporary copy of your project (a git worktree), pushes the branch and
reports the result. Your PC is the machine that runs the model. The Crewboss server runs no model. "Enrol" means linking a PC to Crewboss
with a one-time code.

The wire protocol is in [runner-protocol.md](runner-protocol.md). The security rules are in [../SECURITY.md](../SECURITY.md).

## Requirements

* Python 3.12 or newer, `git`, `pipx`, and Claude Code ([install](https://docs.claude.com/en/docs/claude-code)): type `claude` once and sign in.
  For a subscription runner, `claude` must stay signed in (`claude auth status`). The in-app how-to has the install one-liners
  (`curl -fsSL https://claude.ai/install.sh | bash`; Windows `irm https://claude.ai/install.ps1 | iex`).
* Check Python with `python3 --version` (`py --version` on Windows). If it says 3.11 or lower, get a newer one first. Ubuntu 22.04:
  `sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt install python3.12 python3.12-venv` (Ubuntu 24.04 and newer have it). Debian 12:
  install [uv](https://docs.astral.sh/uv/) and run `uv python install 3.12`. macOS: `brew install python@3.12`. Windows:
  `winget install --id Python.Python.3.12 -e`. Then add `--python python3.12` to the `pipx install` command below.
* A route from the PC to Crewboss: the same home or office network, or a VPN you already have. Do not put Crewboss on the open internet
  unless you followed the "own web address" steps. Runners need only outbound access.
* The PC must be able to push to the project's repository: `git push` works there without asking for a password (for HTTPS sign in once
  with `gh auth login`; or use an SSH key).
* The project needs a git remote set (the project's Settings tab, "Edit repo and environments"). Only
  `https://github.com/<owner>/<repo>.git` and `git@github.com:<owner>/<repo>.git` forms are accepted by default.

## Two modes

| Mode | Uses | Who answers permission prompts |
|---|---|---|
| Subscription | the PC's own Claude Code sign-in (`claude --remote-control`) | you, from claude.ai/code or your phone. If nobody answers within a grace period the job goes back to the queue |
| API key | the PC's own `ANTHROPIC_API_KEY` (`claude -p`) | nobody: only tools on the profile's list are allowed |

Neither your Claude login nor your API key is ever sent to Crewboss.

## Install and enrol

1. On the PC, install the runner. No clone is needed:
   ```
   # Linux (Debian/Ubuntu): sudo apt install pipx git      macOS: brew install pipx git
   # Windows (PowerShell):  py -m pip install --user pipx  (after installing Git and Python 3.12)
   pipx ensurepath          # then CLOSE this terminal and open a new one (Windows: py -m pipx ensurepath)
   pipx install "git+https://github.com/crewboss-dev/crewboss.git#subdirectory=runner"   # Windows: py -m pipx install "git+...#subdirectory=runner"
   crewboss-runner --version
   ```
   If `crewboss-runner` is "not found", you skipped the close-and-reopen step (or run it as `python3 -m crewboss_runner`). If you
   already have a clone, `pipx install ./runner` from it does the same.
2. In Crewboss click the app name at the top left, choose **Administration**, then **Machines** (owner account only; or go to
   `<your address>/machines`), stay on the **Runners** tab, choose the mode and press **New enrolment code**. The code is shown once and
   lasts 5 minutes.
3. On the PC, enrol (already enrolled to another Crewboss? add `--name <profile>`, then `crewboss-runner use <profile>`; see Several Crewboss servers below).

   **Which enrol command?**

   | Your install | Command |
   |---|---|
   | Localhost (plain http, a try-out on one computer) | `crewboss-runner enrol --insecure-http '<code>'` |
   | Home or office network (LAN install, recommended) | `crewboss-runner enrol '<code>'`: the code is long and contains `#`; copy all of it. Do NOT add `--insecure-http` |
   | Your own web address | `crewboss-runner enrol '<code>'` |

   `--insecure-http` is for the localhost try-out only: without it a localhost install fails with an SSL error, and with it a LAN code is refused.
   A runner on another computer cannot reach `localhost`: use an SSH tunnel to the same port or a LAN or network install. `--insecure-http` is
   refused for any host that is not `localhost`, `127.0.0.0/8` or `::1` (an SSH tunnel makes the host `localhost`); `--allow-insecure-remote`
   overrides that, and then anyone on the path can read and alter the traffic. The runner also acts on nothing the server sends until the
   server's proof has verified against the pinned key, so over TLS a middlebox cannot hand it jobs. Over plain http an on-path attacker can relay
   the challenge to the real server and so can read and alter everything, which is why a remote http install needs `--allow-insecure-remote`.
4. Start it: `crewboss-runner run`. This keeps the terminal busy (it runs in the foreground): leave it open, and open a second window for
   `crewboss-runner status`. Or press Ctrl+C and run `crewboss-runner service install` to start it at login instead (below).
5. `crewboss-runner status` shows the state; the **Machines** page, **Runners** tab, shows the PC online.
6. Optional: give the PC labels (lowercase `a-z0-9-`, up to 32 characters) on the Machines page, Runners tab. A job that requires a label
   (for example `android-usb`) runs only on a PC that has it.

Other commands: `crewboss-runner doctor` checks Python, git, Claude Code, the key store, enrolment, autostart and disk;
`crewboss-runner repair` fixes the safe problems it finds (`--dry-run` lists them); `crewboss-runner forget` deletes this PC's key and
token; `crewboss-runner uninstall` also removes the autostart and state.

## Enrolling on a local network (LAN install)

A LAN install has an `https://` address with a self-signed certificate. On **Machines > New enrolment code**, an install that also has a public address asks **Where is this computer?**: **On this network** makes a code for the local address, **Anywhere** makes one for the public address.

A local-network code looks like `192.0.2.20:443#abcd2345.<26 characters>-<secret>`. The part after the dot is a pin of the server's certificate. Enrol as usual:

```
crewboss-runner enrol '<code>'
```

Do **not** add `--insecure-http`: it is for the localhost try-out only. A LAN install is https, and a pinned code refuses plain http. The runner checks the certificate against the pin **before it sends anything**, then trusts that server's key for this address (`crewboss-runner status` shows `tls: pinned <id>`). Your browser's certificate warning does not affect the runner, and the runner never skips verification. A routine renewal keeps the same key, so every runner, including one that was off the whole time, accepts the renewed certificate on its next connection. Only a deliberate key change needs the signed announcement (the server signs it with its identity key); a runner that was off during a key change must be enrolled again. Claude Code on the PC trusts the same certificate (`NODE_EXTRA_CA_CERTS`); the Anthropic API is unaffected. A runner with an older version refuses a pinned code: update it first.

## Several Crewboss servers on one PC (profiles)

One PC can be enrolled to several Crewboss servers (a dev one and a live one, or two clients). Each enrolment is a **profile** with its own
key, token, settings, state, log and status page link; exactly **one profile is active**, and that is the one the runner connects to.
Everything is a command, the same on Linux, macOS and Windows (the Windows tray only calls these commands).

```
crewboss-runner enrol '<code>'                   # the first one on a PC is the profile "default" (and active)
crewboss-runner enrol '<code>' --name client-b   # another server: a new profile (does not change the active one)
crewboss-runner profiles                         # name, server, runner name, state (enrolled / revoked / missing key); * = active
crewboss-runner use client-b                     # switch: stops the runner, makes client-b active, starts it again
crewboss-runner use client-b --force             # even if a job is running (the job goes back to the queue)
crewboss-runner profile rename client-b clientb
crewboss-runner profile remove clientb --yes     # deletes that profile's key, token and state; refuses on the active one
crewboss-runner status --profile default         # one command against a profile that is not active (also doctor, uninstall)
crewboss-runner run --profile client-b           # run that profile in the foreground (for testing)
crewboss-runner uninstall                        # the active profile (only if it is the last); --all removes every profile and the autostart
```

- Names are 1-32 of `a-z 0-9 -`. `enrol` with a name that exists is refused unless you add `--replace`; the device's own name on the server is
  `--runner-name` (default: the host name). A PC that already has profiles needs `--name` for the next one.
- There is **one autostart** (logon task, Windows service, systemd unit or LaunchAgent). It runs the active profile: the runner reads which
  one is active when it starts, so `use` restarts it and it reconnects to the new server. `use` refuses while a job is running unless `--force`;
  it asks the runner to stop gracefully first, and if the runner will not stop it says so and changes nothing. With no autostart (a
  runner you started by hand) it stops it and tells you to start it again.
- A runner enrolled before profiles becomes the profile `default` the first time any command runs, with no re-enrol: the key and token are
  copied under the new name, checked, and only then are the old ones removed. If anything fails the old layout keeps working and is retried.
- Where things live: `<config>/profiles/<name>/` (settings; the 0600 secrets file when there is no keyring), `<config>/active` (the active
  name), `<state>/profiles/<name>/` (status, `run.pid`, `ui-url.txt`, jobs, `runner.log`), and the OS keyring service `crewboss-runner/<name>`.
  Windows keeps each log in `%LOCALAPPDATA%\crewboss-runner\logs\<name>\`. The keep-awake setting and the Gemini key file stay PC-wide.
- **Profiles are not a security boundary.** Every server enrolled on one PC runs as the same OS user, so each can read the others' keys, tokens,
  repositories and job folders, and its jobs run with the same rights. Servers that must not trust each other need separate OS users or
  separate PCs. Do not use profiles for two unrelated clients.
- Upgrading from a single enrolment while a job is running is safe: the move to `default` waits until that runner has exited (until then every
  command looks at the old folders, so `service stop`, `use` and `uninstall` still see it) and happens on the first command afterwards, or when
  the runner next starts. Only a normal user-level process migrates; the Windows service's supervisor never does, and never reads the `active` file.
- With the Windows service installed, `use` from a normal (non-administrator) shell stops the runner gracefully with a "switch" request;
  the service restarts it within seconds on the new profile and reconnects (a switch is not counted as a crash). Update the service copy
  (`crewboss-runner service install --windows-service`, administrator) after upgrading the runner: `doctor` warns when it is stale.
- `CREWBOSS_RUNNER_PROFILE=<name>` does the same as `--profile` for one process. A profile is removed on the PC only: also revoke its device on
  that server's Machines page, Runners tab.

## By operating system

Most people: just run `crewboss-runner service install`. It detects the platform and sets up autostart. Add `--dry-run` to see what it would write.

| Platform | Autostart | Notes |
|---|---|---|
| Linux | a systemd **user** unit, `crewboss-runner.service` | `journalctl --user -u crewboss-runner -f` for logs. |
| macOS | a LaunchAgent, `dev.crewboss.runner` | log at `~/Library/Logs/crewboss-runner.log` |
| WSL2 | by default a Windows Scheduled Task that starts the runner inside WSL at Windows logon; `service install --systemd` uses a systemd user unit instead | the runner stops when WSL shuts down. A USB device such as a phone for `adb` needs `usbipd-win` to attach it to WSL |
| Windows (native; new and less tested than Linux) | by default a **hidden** per-user Scheduled Task "Crewboss runner" at logon (runs `crewboss-runnerw.exe`, so no console window; no admin). `service install --windows-service`, from an administrator PowerShell, installs a real Windows service that runs as you instead | the runner depends on `pywinpty` and `pywin32` there. Log: `%LOCALAPPDATA%\crewboss-runner\runner.log` (rotating). Agents run in a process group that is stopped with `taskkill` |

`crewboss-runner service stop` and `service start` stop and start whichever autostart is installed (Windows task or service,
systemd user unit, LaunchAgent). `crewboss-runner status` prints the status page link, which is per start: a runner with no console
cannot print it, so it also keeps the link in a file only you can read, and a fresh link is issued each time one is opened.

Windows has two ways: the default (a hidden task, no admin needed) and an optional real Windows service (`service install --windows-service`, administrator). Most people want the default. The details of the service are at the end of this page.

### Sleep

While at least one job is running (claimed, running, or pushing and finishing) the runner holds a **system-sleep inhibitor**, and
releases it as soon as no job is active (concurrent jobs are counted). An idle, connected runner never keeps the PC awake, and
the display is never kept on. Without it, an idle-sleep mid-job freezes the `claude` process and drops the link.

| OS | How |
|---|---|
| Windows | `SetThreadExecutionState(ES_CONTINUOUS \| ES_SYSTEM_REQUIRED)` from a dedicated thread |
| macOS | `caffeinate -i -w <runner pid>` |
| Linux | a `systemd-inhibit --what=sleep ... sleep infinity` child (if `systemd-inhibit` is missing, one log line and the job carries on) |
| WSL2 | not possible from inside WSL: it does not stop Windows sleeping. Set Windows to never sleep while plugged in |

It stops **idle** sleep only. Closing a laptop lid or pressing the power button still sleeps the PC, so plug in and leave the lid
open for long jobs. Turn it off with `CREWBOSS_RUNNER_KEEP_AWAKE_DURING_JOBS=0` (setting `keep_awake_during_jobs`, default on).
It is released on job end, failure, cancel, runner shutdown (Ctrl+C, service stop) and at process exit; `crewboss-runner doctor`
shows whether it is available here and whether it is on. If the PC sleeps anyway, the server requeues the job when the lease lapses
(see docs/runner-protocol.md, "Liveness and leases").

Secrets (the device key and token) go in the operating system's keyring. If there is none (a headless Linux box), they go in a
file readable only by you, and `crewboss-runner status` says so.

## Runner app (tray + control panel)

**Windows only: the tray.** `crewboss-runner tray` (or the no-console `crewboss-runner-tray`) shows an icon in the notification area
(`pystray` + `Pillow`, installed with the runner on Windows only). Colour = state, polled from the runner's local files every ~3 s:
grey stopped, green idle and connected, blue a job is running, amber needs attention (not enrolled, revoked, disconnected, a health
check failing). Menu: a status line, Start, Stop, Open control panel, Quit tray. Start and Stop work whether autostart is the hidden
task, the Windows service or nothing (then `crewboss-runnerw run` is started detached); Stop is the graceful stop of `service stop`
(with a running job the tray sends you to the panel to confirm). The tray can start at logon through its own per-user task
"Crewboss runner tray" (a checkbox in the panel; no admin).

**Every OS: the control panel.** It is the runner's local status page (127.0.0.1 only), extended: state card (server, runner name,
state, current job as "Task #id" with an Open in Crewboss link and elapsed time (the runner never receives task text), last jobs, keep-awake), Start / Stop (a confirm while a job runs), Automatic start (off / at
logon / Windows service on Windows; LaunchAgent on macOS; systemd user unit on Linux), keep-awake toggle (saved in
`prefs.json` in the config folder; an environment variable wins), health (doctor rows, the `elevated` state and its Repair
buttons), the last 100 log lines, enrolment with a pasted code (only when not enrolled), "Open Crewboss", and a **Danger zone**:
"Remove runner from this PC" (and any autostart change) cannot be done from the runner's own page, which would end itself mid-request: it shows the command to run, or use the tray. Elsewhere it runs the `crewboss-runner uninstall` logic after you type REMOVE (it refuses while a job runs, leaves
your repositories alone, and the device stays listed on the Machines page (Runners tab) until revoked there), then shows
`pipx uninstall crewboss-runner` to copy.

On **Linux and macOS there is no tray**: the runner is command-line first (`run`, `status`, `doctor`, `service ...`), and the page is
optional (`crewboss-runner status` prints its link).

Choosing "Windows service" asks Windows for administrator rights (UAC); the CLI then asks for your Windows password in its own
console. The password is never typed into the page. The page is protected by the one-use link (token in the URL fragment), an
session secret sent as an `Authorization: Bearer` header (no cookies: they are shared by every port of 127.0.0.1 and would let another
local user's server ride the session), Host and Origin checks, a light rate limit, and actions that take only fixed choices. The tray opens it through a short-lived file in the user-only state folder that redirects to the one-use link, so
the key is never on a command line.

## What a job does

1. The runner claims a job over its socket. The claim carries ids only, never task text; the agent reads the task through the job's own MCP tools.
2. It makes a git worktree on branch `dash/j<id>-e<fence>` from a bare repository cache, and checks the worktree for agent files that
   the project's Owner has not pinned (see [../SECURITY.md](../SECURITY.md)).
3. It starts Claude Code with the profile's model and its tool list. The job's token and MCP config sit in a file outside the worktree.
4. The agent works, using the job tools: read the brief, comment, tick sub-tasks, take a merge lock, ask for a deploy.
5. The runner pushes the branch, reports the result and removes the worktree. Unpushed work is discarded with it.

A job is stopped if it runs past `max_seconds` (default 4 hours), if the runner shuts down, or if you cancel it or revoke the device.

## A job with Bash is not sandboxed

A job runs in a fresh git worktree, but the worktree is not a sandbox. If its profile names `Bash`, the agent can run any command
as the runner's user: it can read files outside the worktree, use that user's SSH keys, git credentials and Claude sign-in, and
reach the network. The tool list limits which tools exist, not what a shell does. Text the agent reads (a task, a client request, a
log, a web page) can steer it.

* Run the runner as a **dedicated low-privilege user** on a machine or VM with no production credentials, not as your own login and
  never as root. Give that user only the git access for the repositories it works on.
* Use a profile without `Bash` (for example `designer`) for jobs that only read or review.
* A runner holds no SSH keys of Crewboss and cannot deploy. A server pass is only ever signed by the server: a deploy or read pass
  after a person's approval, or a 60-minute shell pass that an AI **session** (not a runner) may request on a server whose
  "AI sessions may open a shell" switch the owner turned on. That switch is off by default. See [server-access.md](server-access.md)
  and the [threat model](threat-model.md).

## Gemini second opinions and pictures (optional)

A runner with a Gemini API key file can give a job two extra tools, `second_opinion` and `make_image`, in projects whose Owner has switched
"Gemini for jobs" on. Every call is recorded on the server before data leaves; there are no caps in Crewboss (set limits in Google's console). See [gemini.md](gemini.md).

## Profiles

An agent profile is a model, a list of skills and a list of allowed tools. The seeded ones are `builder` (Sonnet; Read, Edit, Write,
Glob, Grep, Bash), `chores` (Haiku; Read, Glob, Grep, Bash) and `designer` (Opus; Read, Glob, Grep), plus an `android` profile that
needs the `android-usb` label. Profiles are rows in the database; the seeded model names are starting values you can change.

## Advanced: Windows service details

It logs on **as your user**, not as SYSTEM, because the keyring (Credential Manager) that holds the device
key and your Claude Code login are per user. **It runs as you, with standard-user rights, even though it is a service.** A service
logon would otherwise get your full, unfiltered administrator token (no UAC filtering) and every agent would inherit it, so the
service process only *supervises*: it starts the real runner as a child with a filtered token (administrators deny-only, privileges
removed, medium integrity), restarts it with backoff if it dies, and does not restart it after exit 1 (not enrolled) or 2 (revoked):
it logs that to the Event Log and stops. The runner also refuses to claim jobs when it finds itself elevated, unless
`CREWBOSS_RUNNER_ALLOW_ELEVATED=1`; `crewboss-runner status` and `doctor` show whether it is.

**The service runs from an admin-only copy.** The supervisor holds your full administrator token, so it must never load code from a
folder you (or an agent running as you) can write. `service install --windows-service` therefore builds `%ProgramFiles%\Crewboss
Runner\` (a venv made with `--copies`, the same runner version installed into it, Administrators and SYSTEM full control, Users read
and execute, inheritance off) and runs `pythonservice.exe` from there with `PYTHONNOUSERSITE=1`, a `PYTHONPATH` that lists only that
folder, a PATH of Windows folders only, and its own log (`service.log`) inside it. It reads from you only `stop.request`, `run.pid` and
`status.json`, as plain numbers and JSON. It refuses to install if the base Python can be modified by non-administrators (a per-user
python.org install, or the Store Python; the folder's ACL is checked, not guessed from the path): install Python "for all users" into
Program Files, or use the hidden task. The copy does **not** update when you upgrade the runner (`pipx upgrade`): `doctor` warns,
and you re-run `service install --windows-service` from an administrator PowerShell. `service uninstall` removes the copy.

**Remaining risk, stated plainly.** The supervisor shares your logon session with your own processes. The runner child (and so every
agent) is medium integrity, which is mandatory: if that cannot be set, the child is not started and the service stops with an Event
Log message. That reduces same-session attacks (per-session device links, a junction in the state folder where the supervisor writes
`stop.request`) but is not a security boundary. If that matters, use the hidden logon task: it has no administrator process at all.

Install order (`service install --windows-service`, administrator PowerShell, run from the account that enrolled the runner): refuse if
that account has no enrolment; ask for the Windows password (not stored or logged by us); grant "Log on as a service"; check the
password with `LogonUser`; install; start and wait until the service and the runner are up; **only then** remove the hidden task. On any
failure the half-made service is deleted and the task is kept. Entra ID (AzureAD) accounts cannot be service logon accounts: use the
hidden task. Start type is Automatic (Delayed), restart on failure. Installing one kind removes the other, so there is never a second
runner; `service uninstall` removes whichever is installed. `service stop`, `start` and `uninstall` of the service need an
administrator PowerShell. A service runs in session 0 with no desktop: sign in to Claude Code (`claude`) **before** installing; the
service cannot open a browser to log in. `crewboss-runner doctor` shows which kind is installed, whether it is running, whether the runner is elevated (the Machines page, Runners tab,
shows "Runs as administrator: jobs refused" for one that is) and whether the service's copy is current and still admin-only. Assistant
turns and looks are refused by an elevated runner too.

Stopping: `crewboss-runner service stop` (and `uninstall`) first asks the runner to shut down the way Ctrl+C does (a stop-request file
in its state folder, which it watches), waits up to 30 s, and only then falls back to ending the task (which is a hard kill). If a job
is running it refuses unless you add `--force` (the job goes back to the queue). With no console the runner gives itself a hidden one,
so each agent still gets a graceful CTRL_BREAK before any hard kill.
