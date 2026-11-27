# Getting started

This takes you from a clone to your first agent job. It assumes one machine for the server and a PC (can be the same one) for the runner.

## Requirements

* **Server**: Docker Engine with Compose v2, Python 3.10 or newer (only for the `./crewboss` launcher), and a `git clone` of this repository (not a ZIP download: the installer needs the clone's `.git` and refuses without it).
  The app and its Postgres database run in containers and are published on `127.0.0.1` only.
* **Runner PC** (the computer that runs the agents, not the server): Python 3.12 or newer, `git`, and [Claude Code](https://docs.claude.com/en/docs/claude-code) (`claude`) installed and signed in.
* **GitHub**: a project's repository must be on GitHub (`https://github.com/<owner>/<repo>.git` or the `git@github.com:` form) for agent jobs.

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

> LAN mode is for a local network. Don't expose it to the internet without upgrading to a signed certificate and passkey sign-in.

The installer then builds the image, starts the containers, creates the owner and prints a one-time temporary password. Copy it now.

Everything has a flag, so it can run unattended:

```
./crewboss install --mode localhost --yes
./crewboss install --mode lan --yes
./crewboss install --mode network --domain crew.example.com --proxy bundled --yes
```

The default port is 8931 (the next free one if that is taken; with `--yes` the installer takes the next free port without asking). Your settings, secrets and database password live in the instance
folder, `~/.crewboss` by default (`--home DIR` to change it). See [configuration.md](configuration.md).

Day to day:

```
./crewboss status          # containers, health, URL, certificate days left
./crewboss up | down | restart
./crewboss logs -f app
./crewboss lan-address     # LAN mode: this computer's network address changed; reissues the certificate and restarts
```

The proxy runs as the `frontdoor` service (a small sidecar next to Caddy that the Front door card talks to). `./crewboss up` also
upgrades an install from before this: its bundled proxy settings move under the sidecar (`ensure_frontdoor`), nothing else changes.

`down` never removes data. `down --delete-data` asks you to type the project name first. Use `./crewboss` for every Compose action: it pins
the Compose project, the env file and the compose file, so it cannot touch another project on the same machine.

## Opening Crewboss from another computer

The app listens on `127.0.0.1` only, and a browser may sign in only from the app's own address: it refuses writes from any other
address (a 403 "Origin not a dashboard host"), and passkeys need `https://` or `localhost`. So `http://<server-ip>:<port>` shows the
sign-in page but cannot sign you in. Three ways that work:

1. **Same computer**: open `http://localhost:<port>` (the installer prints it).
2. **Another computer, quick**: an SSH tunnel. On the other computer run
   `ssh -L <port>:localhost:<port> <server>`, leave it open, and browse to `http://localhost:<port>`. Nothing is exposed.
3. **Another computer, permanent**: the network install with a real host name and a Let's Encrypt certificate:
   ```
   ./crewboss install --mode network --domain crew.example.com --proxy bundled --yes
   ```
   The name must already point at the server and ports 80 and 443 must reach it. The installer writes `origin_hosts` (the host names
   a browser may sign in from) and `public_url` into `instance.yaml` for you. If you already run a reverse proxy with https, use
   `--proxy existing`: it prints ready-made Caddy and nginx snippets (also saved in `<instance folder>/proxy/`) and you point them at
   `127.0.0.1:<port>`. `--https-port 8443` only changes the port the container binds on this machine; browsers still use `https://crew.example.com` (a router forwards 443 to it), which is what the installer assumes (`--public-port 443`). If browsers really use `https://crew.example.com:8443`, say so with `--public-port 8443`: it goes into `public_url` and `origin_hosts` as `crew.example.com:8443`. The same flag works with your own proxy on a non-standard port: `./crewboss install --mode network --domain crew.example.com --proxy existing --public-port 8443`.
   Passkeys are tied to the name: keep it for good. See [configuration.md](configuration.md).

## 2. Sign in for the first time

Open the address the installer printed (it ends in `/auth/login`).

1. Enter the username (`owner` unless you chose another) and the one-time password. It works once and expires after 24 hours.
2. Choose your own password: 12 to 128 characters. Crewboss refuses a common password; there are no other rules.
3. Scan the QR code with an authenticator app on your phone (Google Authenticator, Microsoft Authenticator, Authy or a password
   manager), then type the 6 digits it shows. Ten wrong codes in a row (after the right password) slow sign-in down, and a hundred lock it until a recovery code or an administrator's Reset sign-in; the count never expires, and only a right code or recovery code clears it. A new password or authenticator signs you out everywhere else. If you can't scan, type the key shown under "Can't scan it?" into the app.
4. Save the recovery codes it shows you somewhere safe (a password manager or a printout). Each works once.

From then on you sign in with your password and a 6-digit code from the app. This is the same in every mode. If you lose your
phone, use a recovery code (it also asks for your current password, then you set up a new authenticator app and get new recovery codes). If you lose the phone and the password, or all your recovery codes, run `./crewboss reset-owner` on the server for a new one-time password; it wipes every
way the owner signed in (password, authenticator, passkeys and recovery codes), so you set them all up again. An admin's **Reset sign-in**
on the Users page does the same for another person.

Sensitive actions (keys, roles, servers, the Front door) ask you to confirm again with a **fresh** 6-digit code. Crewboss never accepts
the code you just used, so wait for the app to show the next one. If you added a passkey but no password, Crewboss asks you once to add a
password and authenticator app as a backup.

To add other people, open **Users** (avatar menu): give a username and a role, and hand over the one-time temporary password.

## 3. Make a project

A new install already has an "Example project" to look around in. To make your own: on a wide screen first open the menu (the burger
icon at the top left; it starts folded away), then press the **+** next to the **Accounts** heading ("New project or account"). Give it a
name, a slug (lowercase letters, digits and hyphens: it is the address, and nothing fills it in for you) and a short name (2-12 characters). **Sits under** starts on the first project in the list; leave it, or pick another parent. (The **+ Create** button in the header makes a *task*, not a project.)
Then open the project's **Settings** tab, use "Edit repo and environments" to set its **git remote** (a GitHub URL). A project needs one before a job
can run. The deploy places (dev, staging, production) are set up under **Servers**; see [server-access.md](server-access.md). You can skip them for now.

## 4. Enrol a runner

A runner is what runs the agents. On the PC:

```
pipx install ./runner          # from a checkout of this repository (needs Python 3.12 or newer)
```

Then in Crewboss open **Machines** from the Administration section (the section switcher at the top left; or go to `/runners`), stay on the **Runners** tab, choose **Subscription** (your own Claude sign-in) or **API key**,
and press **New enrolment code**. On the PC:

```
crewboss-runner enrol '<code>'
crewboss-runner run
```

A localhost install is plain http, so add `--insecure-http`: `crewboss-runner enrol --insecure-http '<code>'`. Without it the runner tries https and
fails with "SSL: RECORD_LAYER_FAILURE". The code lasts 5 minutes. A runner on another computer cannot reach `localhost`: use an SSH tunnel to the same
port (see above) or a network install. `--insecure-http` is refused for a host that is not localhost unless you also pass `--allow-insecure-remote`.

On a LAN install the code is longer and carries a pin of the server's certificate (the runner checks it before sending anything, so the
browser's certificate warning does not matter). If the install also has a public address, **New enrolment code** asks **Where is this
computer?** Do not add `--insecure-http`. See "Enrolling on a local network" in [runners.md](runners.md).

A PC that is already enrolled to another Crewboss can hold both: `crewboss-runner enrol '<code>' --name <profile>`, then
`crewboss-runner use <profile>` to switch (one is active at a time; `crewboss-runner profiles` lists them; see [runners.md](runners.md)).

`crewboss-runner status` should show the PC linked, and the Runners tab shows it online. To start it at login, run
`crewboss-runner service install`. Per-operating-system notes are in [runners.md](runners.md).

## 5. Add a task

Press **Create** in the header and pick the project. Say what you want done in a sentence or two. Tasks have a stream, a priority, sub-tasks and
comments. Plans, changes and questions (an agent asks you something and you answer it on the task) all hang off tasks.

## 6. Run your first agent job

1. Open a task and find the **Run on a PC** block.
2. Pick a profile (`builder` writes code; `designer` plans and reviews, with read-only tools; `chores` runs mechanical jobs).
3. Start it. Your click is the approval for your own subscription runner.
4. Your runner makes a worktree on a branch called `dash/j<id>-e<fence>`, runs Claude Code, pushes the branch and reports back. The task
   shows the job's state and the branch. You can cancel it from the same block.

The agent reads the task, comments on it and ticks sub-tasks through the job's own MCP tools. It cannot deploy: if it needs a deploy,
it asks, and a person approves on the **Approvals** page.

The first time a repository carries `.claude` agent, command or skill files, the job is held until the project's Owner reads and pins
them on the **Machines** page (Runners tab, "Agent files to review"). Later jobs run once the files are pinned. See [../SECURITY.md](../SECURITY.md).

## 7. Review, merge or deploy

When a job finishes, read its branch and the task's thread. Merging the branch and deploying it are separate approvals, made by a person on the
project's **Releases** page (deploy requests also appear on **Approvals**). Nothing ships until someone presses the button.

## 8. Ask CA

CA (Crewboss Assistant) is the chat drawer on every page (the CA icon in the header). It runs on your own runner, so the runner
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
a password and a code). It is optional: password plus authenticator app stays the default, and each person can add a passkey in their
account security page. If you want it for an install facing the internet, we recommend it.

Another way to get a padlock, not built in yet: Caddy can also prove you own the address through your DNS provider, which needs no open
ports. See [configuration.md](configuration.md).

## The how-to tour

The first time a new person signs in, a short tour walks through the numbered sections above that come after signing in, outlining the real buttons as it goes. People who already had an account when the tour was added do not get it by themselves. It remembers where you stopped,
and **Skip tour** closes it. To see it again, open the profile menu and choose **How-to**. Its text lives in `app/tour_steps.yaml`, and a test keeps
the headings of this page and the tour's steps the same.

## Where next

* [configuration.md](configuration.md): `instance.yaml` and the instance folder
* [runners.md](runners.md): runners on Linux, macOS, WSL2 and Windows
* [server-access.md](server-access.md): servers, SSH passes, deploy targets and server reads
* [multi-user.md](multi-user.md): roles, release approvals and project sharing
* [logic-map.md](logic-map.md): the map of an app's workflows kept in its own repository
* [reference.md](reference.md): detailed notes on each part
