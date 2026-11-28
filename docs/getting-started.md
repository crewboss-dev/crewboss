# Getting started

This takes you from a clone to your first agent job. It assumes one machine for the server and a PC (can be the same one) for the runner.

Crewboss is a web page you run on your own computer. You write what you want done; an AI on one of your PCs does it and asks you
before anything risky.

## Words you'll see

* **Server**: the computer that runs Crewboss itself (the web page and its database).
* **Runner**: a small program on a PC of yours that does the AI's work. It connects out to Crewboss; nothing connects in.
* **Enrol**: link a PC to Crewboss using a one-time code from the Machines page. The code works once and lasts 5 minutes.
* **Job**: one run of the AI on one task, on a runner.
* **Worktree**: a temporary copy of your project that a job works in, so your own files are never touched. The result is pushed as a branch.
* **Authenticator app**: a free phone app that shows a new 6-digit code every 30 seconds (Google Authenticator and Microsoft Authenticator
  are common). Install one before you open the sign-in page.

## Requirements

* **Server**: a computer running Linux, macOS, or Windows with WSL2 (a Linux or macOS machine is simplest; Windows with WSL2 is fine for
  trying it out). The `./crewboss` launcher is a Python script and does not run in plain PowerShell. It needs:
  * `git`, and a `git clone` of this repository (not a ZIP download: the installer needs the clone's `.git` and refuses without it).
  * Python 3.10 or newer (only for the launcher).
  * Docker Engine with Compose v2. Install it from [docs.docker.com/engine/install](https://docs.docker.com/engine/install/) (Docker Desktop on
    macOS and Windows). On Linux, then run `sudo usermod -aG docker $USER` and **log out and back in**, so you can use Docker without `sudo`.
    Check: `docker compose version` prints 2 or higher, and `docker run --rm hello-world` works.
  The app and its Postgres database run in containers and are published on `127.0.0.1` only.
* **Runner PC** (the computer that runs the agents, not the server; it can be the same machine): Python 3.12 or newer, `git`, `pipx`, and
  [Claude Code](https://docs.claude.com/en/docs/claude-code) (`claude`) installed and signed in (type `claude` once and sign in).
  Check Python with `python3 --version` (`py --version` on Windows). If it is older than 3.12, get a newer one first:
  * Ubuntu 24.04 and newer: nothing to do. Ubuntu 22.04: `sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt install python3.12 python3.12-venv`.
  * Debian 12: install [uv](https://docs.astral.sh/uv/) and run `uv python install 3.12`.
  * macOS: `brew install python@3.12`. Windows: `winget install --id Python.Python.3.12 -e`.
  * Then add `--python python3.12` to the `pipx install` command in step 4.
* **GitHub**: a project's repository must be on GitHub (`https://github.com/<owner>/<repo>.git` or the `git@github.com:` form) for agent jobs.
  The runner PC must be able to push to it: `git push` there works without asking for a password (for HTTPS, sign in once with
  `gh auth login`; or use an SSH key).

## 1. Install

```
git clone https://github.com/crewboss-dev/crewboss.git
cd crewboss
./crewboss install
```

The installer asks one question: **How will people open Crewboss?**

1. **Just on this computer.** The quickest, for trying it out. Only this computer can open it (`http://localhost:<port>`).
2. **On my home or office network** (recommended). Other computers and phones on the same Wi-Fi or office network can open it. The
   installer shows the address (for example `https://192.0.2.20`) and asks you to confirm it. Your browser warns you once: click
   **Advanced**, then **Continue**. Nothing to install. Step by step: [lan-quickstart.md](lan-quickstart.md).
3. **On the internet with my own web address.** You need a web address that points at this computer and port 80 opened on your
   router. You can also start with option 2 and upgrade later from the admin panel (see "Give it your own web address").

> LAN mode is for a local network. Don't expose it to the internet without first upgrading to your own web address with a signed certificate (Administration, then Front door). Then each person can also add a passkey, which is recommended for anything on the internet.

The installer then builds the image, starts the containers, creates the owner and prints a one-time temporary password. Copy it now.

Everything has a flag, so it can run unattended:

```
./crewboss install --mode localhost --yes
./crewboss install --mode lan --yes
./crewboss install --mode network --domain crew.example.com --proxy bundled --yes
```

The default port is 8931, or the next free one if that is taken. With `--yes` the installer takes the next free port without asking. Your settings, secrets and database password live in the instance
folder, `~/.crewboss` by default (`--home DIR` to change it). See [configuration.md](configuration.md).

Day to day:

```
./crewboss status          # containers, health, URL, certificate days left
./crewboss up | down | restart
./crewboss logs -f app
./crewboss lan-address     # LAN mode: this computer's network address changed; reissues the certificate and restarts
```

`down` never removes data. `down --delete-data` asks you to type the project name first. Use `./crewboss` for every Compose action: it pins
the Compose project, the env file and the compose file, so it cannot touch another project on the same machine.

## Opening Crewboss from another computer

On a home or office network (option 2)? Skip this section: the address the installer printed already works from other computers and phones.

The app listens on `127.0.0.1` only, so with option 1 (just this computer) `http://<server-ip>:<port>` shows the sign-in page but cannot
sign you in. To reach it from another computer, either install with option 2 or 3, or open a tunnel: on the other computer run
`ssh -L <port>:localhost:<port> <server>`, leave it open, and browse to `http://localhost:<port>`. The permanent
https options (`--mode network --domain ...`, your own proxy, non-standard ports) are in [configuration.md](configuration.md#opening-the-app-from-another-computer-details).

## 2. Sign in for the first time

Open the address the installer printed (it ends in `/auth/login`).

1. Enter the username (`owner` unless you chose another) and the one-time password. It works once and expires after 24 hours.
2. Choose your own password: 12 to 128 characters. Crewboss refuses a common password; there are no other rules.
3. Scan the QR code with your authenticator app (a free phone app that shows a new 6-digit code every 30 seconds: Google Authenticator,
   Microsoft Authenticator, Authy or a password manager; install it before you open this page), then type the 6 digits it shows. If you can't
   scan, type the key shown under "Can't scan it?" into the app. A new password or authenticator signs you out everywhere else. If you get
   locked out by wrong codes, see the recovery paragraph below.
4. Save the recovery codes it shows you somewhere safe (a password manager or a printout). Each works once.

From then on you sign in with your password and a 6-digit code from the app. This is the same in every mode. If you lose your
phone, use a recovery code (it also asks for your current password, then you set up a new authenticator app and get new recovery codes). If you lose the phone and the password, or all your recovery codes, run `./crewboss reset-owner` on the server for a new one-time password; it wipes every
way the owner signed in (password, authenticator, passkeys and recovery codes), so you set them all up again. An admin's **Reset sign-in**
on the Users page does the same for another person. (The exact lockout limits are in [threat-model.md](threat-model.md).)

Sensitive actions (keys, roles, servers, the Front door) ask you to confirm again with a **fresh** 6-digit code. Crewboss never accepts
the code you just used, so wait for the app to show the next one. If you added a passkey but no password, Crewboss asks you once to add a
password and authenticator app as a backup.

To add other people, open **Users** (avatar menu): give a username and a role, and hand over the one-time temporary password.

## 3. Make a project

A new install already has an "Example project" to look around in. To make your own: on a wide screen first open the menu (the **Menu** button at the top left, a small panel icon; it starts folded away), then press the **+** next to the **Accounts** heading ("New project or account"). Give it a
name, a slug (a short web name with only lowercase letters, numbers and hyphens, for example `my-app`; it is the address, and nothing fills it in for you) and a short name (2-12 characters). **Sits under** starts on the first project in the list; leave it, or pick another parent. (The **+ Create** button in the header makes a *task*, not a project.)
Then open the project's **Settings** tab, use "Edit repo and environments" to set its **git remote** (a GitHub URL). A project needs one before a job
can run. The deploy places (dev, staging, production) are set up under **Servers**; see [server-access.md](server-access.md). You can skip them for now.

## 4. Enrol a runner

A runner is what runs the agents. On the PC, install pipx first if you do not have it, then the runner. It needs no clone of this repository:

```
# Linux (Debian/Ubuntu):  sudo apt install pipx git        macOS:  brew install pipx git
# Windows (PowerShell):   winget install --id Git.Git -e ; winget install --id Python.Python.3.12 -e ; py -m pip install --user pipx
pipx ensurepath          # then CLOSE this terminal and open a new one (Windows: py -m pipx ensurepath)
pipx install "git+https://github.com/crewboss-dev/crewboss.git#subdirectory=runner"      # Windows: py -m pipx install "git+...#subdirectory=runner"
crewboss-runner --version
```

(Already have a clone, for example as a contributor? `pipx install ./runner` from it does the same.) If `crewboss-runner` is "not found"
you skipped the close-and-reopen step. The in-app how-to shows the same steps per system, plus Claude Code.

Then in Crewboss click the app name at the top left, choose **Administration**, then **Machines** (owner account only; or go to
`<your address>/machines`). Stay on the **Runners** tab, choose **Subscription** (your own Claude sign-in) or **API key**, and press
**New enrolment code**. Which enrol command to use:

| Your install | Command on the PC |
|---|---|
| Just on this computer (localhost, plain http, a try-out) | `crewboss-runner enrol --insecure-http '<code>'` |
| Home or office network (recommended) | `crewboss-runner enrol '<code>'` (the code is long and contains `#`: copy all of it; do NOT add `--insecure-http`) |
| Your own web address | `crewboss-runner enrol '<code>'` |

Then start it:

```
crewboss-runner run
```

`run` keeps this window busy: leave it open (it keeps running). Open a second window for `crewboss-runner status`, or press Ctrl+C and run
`crewboss-runner service install` so it starts by itself at login.

Details: the code lasts 5 minutes. A localhost install is plain http, and without `--insecure-http` the runner tries https and fails with
"SSL: RECORD_LAYER_FAILURE". A runner on another computer cannot reach `localhost`: use an SSH tunnel to the same port or a network
install. `--insecure-http` is refused for a host that is not localhost unless you also pass `--allow-insecure-remote`. On a LAN install the
code carries a pin of the server's certificate (the runner checks it before sending anything, so the browser's certificate warning does not
matter). If the install also has a public address, **New enrolment code** asks **Where is this computer?** (**On this network** or **Anywhere**). See "Enrolling on a local network" in [runners.md](runners.md).

A PC that is already enrolled to another Crewboss can hold both: `crewboss-runner enrol '<code>' --name <profile>`, then
`crewboss-runner use <profile>` to switch (one is active at a time; `crewboss-runner profiles` lists them; see [runners.md](runners.md)).

`crewboss-runner status` should show the PC linked, and the Machines page (Runners tab) shows it online. Per-operating-system notes are in [runners.md](runners.md).

## 5. Add a task

Press **Create** in the header and pick the project. Say what you want done in a sentence or two. Tasks have a stream, a priority, sub-tasks and
comments. Plans, changes and questions (an agent asks you something and you answer it on the task) all hang off tasks.

## 6. Run your first agent job

1. Open a task. Under the task's **Now** line (one sentence about the latest run) there is one **Run** button. A small line next to it says
   which profile and which runner it will use ("Will run on ..."); the **Run settings** popover lets you change the profile, add labels or pick
   another runner.
2. Press **Run**. Your click is the approval for your own subscription runner. Crewboss picks a runner by itself: online, with the labels the job
   needs, the least busy first. To limit which runners take a project's jobs, tick them under **Runners for this project** on the project's
   **Settings** tab (none ticked = any runner of the person who presses Run).
3. Your runner makes a worktree on a branch called `dash/j<id>-e<fence>`, runs Claude Code, pushes the branch and reports back. While it works,
   the **Activity** slot shows the live runner view: what the agent says and does, as it happens (watch only; you cannot type into it). **Stop**
   ends the job.
4. When the job ends, an **outcome card** says what happened (done, needs more work, blocked or failed) in plain words, with what the agent
   did, the checks it ran, **next steps** and **remaining work**. Its button is **Review the change** when the work is done, or **Run again**
   (which starts from the branch the last run pushed) when more is needed.

The task window reads top to bottom: the **Now** line, the **Summary** (three short lines: What, Done, Left, kept current by the AI after each
run, comment and answer, labelled "Written by the AI" or "Auto summary"; you can edit it, and "Restore your version" brings yours back), the
original request, and the **Timeline** (comments, questions, runs and branches in one list).

The agent reads the task, comments on it and ticks sub-tasks through the job's own MCP tools. It cannot deploy: if it needs a deploy,
it asks, and a person approves on the **Approvals** page.

The first time a repository carries `.claude` agent, command or skill files, the job is **waiting on you** (not failed) until the project's Owner
reads and pins them on the **Machines** page (**Runners** tab, "Agent files to review"). Then the job runs by itself. A job held for more than
7 days fails with a plain message. See [../SECURITY.md](../SECURITY.md).

## 7. Review, merge or deploy

When a job finishes, read its branch and the task's thread. Merging the branch and deploying it are separate approvals, made by a person on the
project's **Releases** page (deploy requests also appear on **Approvals**). Nothing ships until someone presses the button.

## 8. Ask the Crewboss Assistant (CA)

CA, the Crewboss Assistant, is the chat drawer on every page (the CA icon in the header). It runs on your own runner, so the runner
must be online. Ask "what's waiting on me?". It can read your open work, comment, tick steps and set reminders. It never records an
answer, makes a decision or starts a job by itself: it shows a confirm card and acts only when you tap it.

## 9. Where settings live

Your own settings (theme, account security, sign out) are in the profile menu: the round avatar at the top right of the header, or at the top left
on a phone. The section switcher at the top left opens **Administration** (users, machines and servers) for the owner.

## 10. Give it your own web address (optional)

This is for when you want a padlock instead of the one-time browser warning, or want to reach Crewboss from outside. Open
**Administration**, then **Front door** (owner only). The card shows how people open Crewboss now and walks you through three steps:

1. **Your web address.** Type the address you want (for example `crew.example.com`). Don't have one? A free one takes two minutes; the
   card tells you how.
2. **Point it here and open port 80.** At your domain provider, point the address at your internet connection. On your router, forward
   port 80 (and 443 if you can) to this computer. Port 80 is the minimum. The card lists the usual router menu names.
3. **Check and switch on.** Press **Check**; it tells you in plain words what works. Then **Switch on**. If anything fails, nothing is
   changed and the card says why. The old network address keeps working.

What you get: no browser warning, and the option of **passwordless sign-in with a passkey** (your fingerprint, face or phone instead of
a password and a code). It is optional: password plus authenticator app is the default, and passkeys become available once you have your own web address with a
signed certificate. Each person can then add a passkey in their account security page. For an install facing the internet, we recommend it.

Another way to get a padlock, not built in yet: Caddy can also prove you own the address through your DNS provider, which needs no open
ports. See [configuration.md](configuration.md).

## The how-to tour

The first time a new person signs in, a short tour walks through the numbered sections above that come after signing in, outlining the real
buttons as it goes. People who already had an account when the tour was added do not get it by themselves. It remembers where you stopped,
and **Skip tour** closes it. To see it again, open the profile menu and choose **How-to**.

## Where next

* [configuration.md](configuration.md): `instance.yaml` and the instance folder
* [runners.md](runners.md): runners on Linux, macOS, WSL2 and Windows
* [server-access.md](server-access.md): servers, SSH passes, deploy targets and server reads
* [multi-user.md](multi-user.md): roles, release approvals and project sharing
* [logic-map.md](logic-map.md): the map of an app's workflows kept in its own repository
* [reference.md](reference.md): detailed notes on each part
