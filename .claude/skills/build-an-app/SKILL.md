---
name: build-an-app
description: Plan, build and deploy a new app in the dashboard by conversation, after identify-a-need has agreed the problem and decided software is needed. Covers the server (pick or design one), the app shape, sign-in, the admin panel, monitoring, backups and keys, with a confirm card for every live step. Use when a brief says "build an app/website/service".
---

# How to build an app

Start from the agreed brief (identify-a-need). Keep talking in plain words. The person approves each live
step with a tap; you do the work.

**Everything below is a set of DEFAULTS learnt from building a business ERP: house rules for the first version,
not mandates.** Use them unless the person specifically asks for something different. Then do what they ask,
note the override in the brief, and mention any trade-off in one line. Safety is the exception: confirm
cards for live changes (Rule 0), MFA on local sign-in, and no secrets in code are not overridable.

## 1. Choose the shape

Pick the simplest shape that meets the brief, and say why:
- **Static site:** information pages, no sign-in.
- **Web app:** people sign in and do things.
- **Background service:** runs on a schedule or reacts to events, no screens.
- **Add to an existing app:** often the best answer. Adding a section to an app they already have beats a new one.

## 1b. Start simple: the core records only

Version one covers the few records the problem is actually about, nothing more:
- **a CRM:** accounts and contacts (then notes/activity);
- **a job system:** customers, jobs, and the job's notes and time;
- **an ERP-style app:** accounts, contacts, invoices (then quotes, then payments).

Get those working end to end, put them in front of the person, and grow from what they actually use.
Every extra screen in version one is a screen nobody asked for yet.

## 1c. Web interfaces start from the house design, never a blank page

The hardest part of a web interface is making it look and feel right, and that has already been solved.
Every new web app starts from the **Crewboss default look**: the same layout and interaction rules as this
app, with warm, calm light and dark modes that are easy to read. The first version should look
amazing from day one. Follow the proven standards:
- **Page shape:** Overview / Work / Related, one scope pill, mini cards.
- **Navigation:**
  - left menu drawer (pinned on wide screens), slim header with search;
  - phone: Teams-style header, 5 tabs + More, records open in a bottom sheet.
- **Moves:** list → record opens in a pane; record → related opens in a drawer; on the phone, always a
  bottom sheet.
- **Controls:**
  - ADD pill, DO button, PEEK chip;
  - no per-page search boxes (use filters + the command bar);
  - child records are read-only with a pen, and Delete sits only inside edit with a named confirm.
- **Lists:** server-side paging, 50 at a time; list tables with a master-detail drawer; lists offer a
  refresh rather than jumping.
- **Actions** remove or move an item in place: no jump to the top, focus goes to the next item.
- **Theme:** light, dark and auto, in the default palette.

The default look is the starting point for everyone who uses this software; a person's own logo and name go in
instead.

## 2. The server (only if the shape needs one)

Servers belong to the **account**, not the project.
1. **Look at the account's servers first.** If one has room and suits the job, propose it.
2. **Otherwise design a new one** and show it as a short card:
   - what it's for;
   - size (CPU/memory/disk);
   - operating system;
   - where it runs;
   - monthly cost;
   - backups;
   - monitoring;
   - who can reach it (public or private network only).

   Ask for approval. Once it exists, add it on the account's Servers page and run the shown setup once.
3. Never put a new app on a server that holds something unrelated and sensitive without saying so.

## 3. Design the app (including its admin) BEFORE building

Decide all of these up front and show them as one plan for approval:
- **Sign-in:** who signs in and how. Use the account's existing sign-in (e.g. Microsoft) where it fits. Local
  accounts always come with MFA (passkey or authenticator code), never password-only.
- **Admin panel:** designed now, not later (see the admin-page skill, step 0).
  - If the app has a web interface, it gets an /admin area from day one, behind the same sign-in, with a
    section for each part of the app.
  - If it has no web interface, its settings live in files on the server, documented.
- **Data:** what it stores, where, and how it's backed up. Personal data stays minimal.
  Ask, and **record the answers** on the project's *Where it lives* page (the deploy recipe), so the server's
  manifest and skill can tell the ops tool what to back up (Crewboss only publishes this; it never runs backups):
  - databases (engine, name, the dump command);
  - volumes and data folders, and where users' uploaded files go;
  - config files and the **names** of secrets and where they are kept (never a value; a value is refused);
  - ports, domains, and the health and log endpoints;
  - how much data it can lose (RPO) and how long it can be down (RTO).
  If it keeps nothing worth backing up, tick "keeps no state" so the release check stays quiet.
- **How it is tested:** Browser (web app), Installed (Android/iOS/desktop installer: the runner is the test area and the
  task is pinned to one runner) or No UI. Set it on the same page, with the staging test channel for an installed app.
- **Address:** its web address (domain), on the private network unless it must be public.
- **Secrets and keys:** held in the app's key store or server files, never in code. Platform keys come in one
  way through the key-store door.
- **Monitoring:** how the dashboard will know it's healthy (it sends a health report, or is watched by monitoring).
- **Licences:** only permissive open-source pieces (Apache/MIT/BSD).

## 4. Build

- Create or extend the repo. Build with the builder agents (Sonnet), in a separate working copy.
- An independent code review (Opus; Fable for security-heavy parts) before anything merges.
- Tests pass before merge.
- Record what was built on the project's tasks: the workflow annotations and the knowledge entry go in the
  same change (no drift).
- **The logic map is part of the build, from day one.** Create `.crewboss/logic/` in the repo and describe every
  workflow the app has: what starts it (a button, timer, webhook or queue), its steps and decisions, every AI call
  (model, skill, what it may do), the data it touches, and each side effect (records created or changed, emails,
  pushes, syncs). Update it in the same change as the code, then run `crewboss logic check` and record the report.
  A production release is refused when the map is stale or something new is uncovered. Format: `docs/logic-map.md`.
  Whether the app shows the map in its own admin page (`crewboss logic export`) is optional; keeping it is not.

## 5. Deploy, one confirmed step at a time

Each of these is a **confirm card**. Nothing live happens without a tap (Rule 0):
1. Prepare the server (packages, user, firewall).
2. Install the app (container or service) and its web front (Caddy) with a certificate.
3. Set its keys through the key store.
4. Turn on monitoring and backups. Check the server's *Manifest & backups* view lists the app's stateful parts.
5. Check it works end to end, and show the person.

After each step, say in one line what happened and what's next. If something fails, stop, explain plainly
and propose the fix; never improvise on a live server.

## 6. Hand over

Give the person a short "how to use it" in plain words, plus the check task with the steps to try it. Close
the loop on the brief: is the problem actually fixed? If not, go back to identify-a-need.
