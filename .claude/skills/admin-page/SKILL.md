---
name: admin-page
description: Recommend where a top-level project's admin settings should live (an /admin page or config files) before anyone builds one. Use when a project needs settings or keys that people change, when a project is added or moved in the project tree, or when someone asks for an admin page. Recommends only; the owner decides and the page is built by hand.
---

# Where should this project's admin live?

An admin surface holds the settings and keys of ONE top-level project and of every project under it in the tree.
Sub-projects never get their own admin page: they get a section in their top-level project's admin.
Never assume a project needs a page. Work through the questions below, give a recommendation, and let the
owner decide. Nothing is built automatically.

## 0. New software being designed, or an existing project?

**New software designed in the dashboard conversation** (intent → server → build): the admin panel is
designed AT INITIATION, as part of the software.
- **If what you're building has a web interface,** it gets an admin panel from the start: an /admin area
  inside the same app, behind the same sign-in, with one section per sub-project.
- **If it has no web interface,** its settings live in files (step 4).

Decide this while designing the server and the software, not afterwards. Then skip to the Output.

**An existing project** that already runs: work through steps 1-5.

## 1. Is it a top-level project?

A top-level project is a direct child of an account in the project tree. If the project isn't top-level, its
settings go in a section of its top-level ancestor's admin (page or files). Stop there.

If it moves to a different top-level parent, its section moves with it in the same release. Raise a task on
both top-level projects.

## 2. Does it have anything to administer?

List the settings and keys people actually change: secrets, API keys, rotation dates, feature switches,
schedules. If there's nothing, recommend **no admin**. Don't build an empty page.

## 3. Does the project ALREADY have web credentials?

Check that it already has BOTH:
- **a web server as part of the project:** Apache, Caddy or Nginx, or an app server behind one; and
- **a sign-in for people:** an existing login (OIDC/Entra, local accounts with MFA, or similar).

| Web server | Sign-in for people | Recommendation |
|---|---|---|
| yes | yes | Add an **/admin page** to the existing app, behind the existing sign-in, restricted to admins. |
| yes | no  | **Admin lives in files.** Don't build a sign-in just to host settings. |
| no  | —   | **Admin lives in files.** |

**Never create credentials, a login system or a new web app only to hold an admin page.**

## 4. "Admin lives in files": how to do it properly

- Settings go in config or env files on the project's server, mode 600 for secrets, never in git.
- Changes go through the repo + a deploy, or for keys, through the platform-keys door if the project
  implements one.
- Document every setting in the project's docs:
  - the file;
  - the owner;
  - how to rotate it;
  - which other systems hold a copy.
- Record "admin: files" on the project in the dashboard, so the drift check knows not to expect a page.

## 5. If an /admin page is right

- Its address is the project's existing web address + `/admin`. If the project has no public web address
  but meets step 3, the owner sets its address by hand in the dashboard.
- Reachable only from the private network unless the project itself is public.
- One section per sub-project.
- Keys write one way through the platform-keys door, and values are never shown again.
- Record the admin URL on the project in the dashboard.

## Output

Give a short recommendation: top-level or not; the items found; web server yes/no; sign-in yes/no;
**page / files / none**; and the next step. Ask the owner to decide before anything is built.
