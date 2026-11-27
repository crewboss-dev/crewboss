# Multi-user access (task #713)

One layer decides who may see and change a project: `app/access.py`. Nothing else holds a second rule.

## Principals
- **owner**: all projects and the owner-only surfaces. Also every Microsoft (OIDC) super admin, and the pages with sign-in off.
- **designer**: projects with `projects.owner_user_id = them`, plus `project_members` rows (`editor` writes, `viewer` reads).
- **viewer**: read only; a share is capped at read.
- **none**: a request that needed a signed-in person and has none (fail closed).

`projects.owner_user_id` NULL means the instance owner's own project. A session token acts for the owner of its project
(`access.of_session`); on an owner-less project it acts as the owner, so tokens issued before multi-user keep working.

## Project roles and release governance (change #77)
Per project: **Owner** (the creator: adds people, sets roles, transfers ownership, sets the release policy), **Admin** (approves merges
to main and production deploys; deploys dev and production), **Member** (works, runs agents, deploys dev, *requests* production),
**Viewer** (read only). The instance owner is above all projects. This replaces the "designers deploy to their assigned servers with no
approval" rule: Members deploy dev freely and production goes through an Admin.

- Targets are `dev | staging | production` (stored `prod`; `production` is accepted). A runner is never a production target: targets are
  commissioned servers; a runner only ever previews.
- Production is promote-only (`deploys._ran_before`): the exact commit needs a successful dev or staging deploy for the project. Every way
  of asking is checked (a job, a person on the approvals page, `request_release`). A project with `release_policy = production_only`
  (no dev environment, e.g. a website) accepts a successful runner **preview run** of the commit instead (`deploys.record_preview`, the job
  MCP tool `record_preview`; stored as a `server_jobs` row with `env = 'preview'`).
- Production approval (`deploys.approve_checked`): an Admin with a second factor shown in the last 5 minutes; with two or more Admins the
  approver is not the requester; a solo Admin may approve their own. Merges to the default branch (`release_gates`) need green tests, a recorded
  code-auditor pass, and the same Admin approval (second factor within 5 minutes). A merge is marked merged only if approved (by the job's report, or on a project that opted in to the merge executor, only by Crewboss itself).
- Hotfix: an explicit flag with a reason, production only. Dev/staging may be skipped, the skip is written to the audit log, and approving
  it opens a follow-up task to merge the fix back into dev and main.
- Rollback: one tap puts the target back on its previous release (`deploys.previous_release`), behind the same Admin approval.
- GitHub branch protection: `release_gates.protect_plan` builds a CARD for a human repository admin: the exact `PUT .../branches/<default>/protection`
  call, a `gh api` command, and a sentence on what it REPLACES (the call replaces the whole protection; the card reads the existing one when
  its credential may). It targets the repository's real default branch (read from GitHub), never a hard-coded `main`. Crewboss never
  applies it: its bot credential only merges and is not an administrator. (`protect_apply`, kept in `release_gates`, is for a caller that
  holds an admin client; no page uses it.)
- **Merge executor (#829, `app/merge_executor.py`, migration 0110).** Runners are told not to push main and hold no server keys, so
  Crewboss performs the merge itself. **Authority is the GitHub repository's, not the project's:**
  - the INSTANCE owner (second factor within 5 minutes) keeps a list of `owner/name` repositories the executor may touch
    (`merge_repos`, Releases page), each enrolled FOR ONE project; nobody else edits it;
  - a project Owner (second factor within 5 minutes) may switch the executor on only for the repository its remote points at, if that
    repository is enrolled for that project, no other project already merges into it (unique index), web sign-in is on
    (`CREWBOSS_WEB_AUTH` not `off`), a bot login is configured (`CREWBOSS_GITHUB_BOT_LOGIN` or instance.yaml `merge_executor.bot_login`:
    **turning it on is refused without one**, the identity check fails closed), a token file is configured, `GET /user` answers, and the
    login is exactly that bot and not on the deny-list of human accounts (`CREWBOSS_GITHUB_DENY_LOGINS`, comma separated, or instance.yaml
    `merge_executor.deny_logins`, a YAML list or a comma separated string; entries are lower-cased). Turning it on also sends back every
    request approved BEFORE it (`approved before Crewboss merging was on; ask again`: a plain `sent_back`, a new request, not a re-queue;
    every pass sweeps any left over);
  - changing the project's `git_remote` turns the opt-in off; removing the enrolment turns it off too;
  - each merge request stores the repository it was made for (`merge_repo`); at execute time the executor refuses (sent back) unless that
    repository, the project's remote, the opt-in and the enrolment all still agree.

  Once a request has tests green and the independent code-auditor pass for the exact commit and an Admin approved it (second factor within
  5 minutes), the executor: claims it (`approved` -> `merging`, `merging_at`), only if the approval was made AFTER the opt-in
  (`decided_at > merge_via_crewboss_at`) and is younger than 24 hours (older ones are sent back as expired); re-checks the gates and that
  `decided_by` is still an enabled Admin; requires the pushed branch head to still be the approved commit; compares it with the default
  branch and updates that branch with ONE ref update to the approved commit, `force: false`. No merge commit, no force-push, no branch
  deletion. The credential's identity is asked again (`GET /user`) right before each ref update, once per pass: it must still equal the
  bot login and not be on the deny-list, else the request is sent back (`github`, re-queueable once the right token is mounted), nothing
  is written and a warning is logged. Every outcome write is fenced on the claim (`merging_at` + `merge_attempts`): a stalled first
  attempt whose claim `recover()` returned and a second attempt took cannot overwrite the row, and reports `skipped`, never `merged`.
  A `diverged` branch, a moved head or a refused fast-forward is **sent back** (`sent_back`, reason in `merge_note`, the task is
  commented and returned to *doing*); a GitHub 4xx other than non-fast-forward is sent back at once; network errors, 5xx, 408/429 and rate
  limits are retried with exponential backoff (30 s doubling to 10 min, 7 tries, about 25 minutes; `next_attempt_at`), then sent back. An
  Admin can **re-queue** a request sent back for a GitHub failure (`sent_back_kind = github`) or an expired approval (`expired`) with a fresh
  factor and no new commit. `recover()` (start-up and every pass) returns a `merging` claim older than 3 minutes to `approved`; a rerun is
  idempotent (a push that landed compares `identical` and is recorded as merged). `run_once` holds its own advisory lock (one process).
  A commit already on the branch is recorded as merged with no change. With the executor on, a job's `report_merge` is NOT an attestation,
  and `mark_merged` itself refuses (in its UPDATE) a request of an opted-in project: only the executor marks one merged.

  Credential: plain HTTPS to `api.github.com` (`app/github_client.py`; the image has no `gh`). A fine-grained token of a NEW dedicated bot
  account (never a person's), **Contents: write** on the enrolled repositories only, in a 0600 file mounted at
  `CREWBOSS_GITHUB_TOKEN_FILE` (default `/run/secrets/github-token`; see `docker-compose.addons.example.yml`), never in an environment
  variable, the database or a log. Redirects are not followed; the path is whitelisted. Audit: `decided_by`, `decided_at`, `sha`, `merge_repo`
  plus `merged_at`, `merged_sha`, `merged_by`, `merge_target`, on the Releases page and in the activity log. Rollback is the existing deploy
  rollback (the merge is a fast-forward; nothing is rewritten).
- **Making the default branch reachable only through this path.** The Releases page offers a second card, "only Crewboss may change it"
  (`protect_plan(..., push_login=)`): push restricted to the identity of Crewboss's GitHub credential (asked from GitHub, never typed; a
  normal bot user goes in `restrictions.users`, a GitHub App `slug[bot]` in `restrictions.apps`), linear history, no force-push, no
  deletion, admins included. GitHub allows push restrictions on organisation repositories only. A human repository admin runs the shown
  `gh` command once with their own login; Crewboss does not hold an admin credential and applies nothing. The card reads GitHub with the
  bot credential, so it needs the project Owner, a second factor within 5 minutes, and a repository the instance owner enrolled for THIS project. It was not applied to any real
  repository when this was built.
- Pages: `/{slug}/releases` and `/{slug}/team`.
- Wired: job MCP `record_check` (tests | auditor, for the job's own commit) and `report_merge`; Releases page Approve merge; the Owner's
  "Show the protection card" (plan only: reads the default branch and existing protection through `app/github_client.py`; tests use a fake).
- A server assignment pins a server to ONE project of the assignee (`server_assignments`). Only that project's Owner (or the instance
  owner, on the Servers page) defines targets on it; Members and Admins deploy what exists. The two-person rule counts Admins when the
  request is made (`admins_at_request`) and an Admin with a waiting request can't be demoted or removed.
- The instance owner is one principal: every owner-role users row counts as the same person (`access.owner_user_ids`), and projects the
  owner creates are filed under the local owner account (`access.canonical_owner_id`); migration 0043 backfills the same way.
- Invite links carry the token in the URL fragment (`/auth/invite#...`), which browsers never send, so it is not in any access log.
- Instance audit (invites, roles, MFA resets, server pins): `instance_audit`, shown only on the owner's Users page.

## The tree has no project root (change #76)
Accounts and personal projects are top level. The overseer's project is an ordinary project (migration 0044 puts it under the estate
account, the one holding the `health` project) that keeps its own session and the overseer role. Its writes are its own routed projects;
its cross-account reads, requests and comments are unchanged. The account's session never reaches the overseer's branch (it has a session
of its own), and the overseer is still given no SSH passes. The overseer's project now inherits its account's shared rules and skills
like any project under it.

## Decisions
- **Parents and children.** Sharing is per project and never inherited. Sharing a parent shows the parent only (its page rolls up
  just the children the person can see); a child made by someone else is not shared by sharing its parent; sharing a child does not
  expose its parent (a visible child of a hidden parent is drawn as a top-level project).
- **Routing.** Work never routes across people (`tree._same_owner`): a designer's project under the owner's tree is not read by, or
  announced to, the owner's tmux session.
- **Designers and sessions.** A designer cannot create accounts or standing Claude sessions (those start processes on the owner's
  dev box); their agents run on their own runner (`jobs`, per user). Runners are enrolled for an account or the top of a personal
  partition (`tree.is_runner_home`); a person sees and revokes only their own.
- **Deploys.** A person defines and uses deploy targets only on servers the owner assigned them (`server_assignments`); dev and staging
  deploys need no approval (recorded `auto_approved`, `decided_by` = the requester); production follows the release rules above.
- **MFA.** Every invited person is held in the enrolment-only session (the #706 flow) until they have chosen a password, confirmed an authenticator app and saved their recovery codes (or added a passkey).
  Granting reach (invite, role, reset, share, assign) needs a second factor shown in the last 5 minutes.

## Enforcement points
- `/{slug}/...` pages: `pages.project_from_path` (view for reads, change for writes; `pages.slug_policy` adds owner-only and
  creator-only sections).
- Global pages, SSE, finder, media, plan: `access.*` in the handler, and the data layer's ambient filter
  (`access.ambient_ids()` in `repo.global_overview`, `focus`, `open_questions`, `recent_activity`, `locate_task`, `changes.list_for`,
  `plan.plan_for_home`, the knowledge graph) for anything that forgets.
- REST and MCP: `scope.reach` is intersected with the token's person's visible projects.
- Audit: `activity.user_id`, `change_log.decided_user_id`, `task_questions.answered_user_id`, `ssh_cert_issues.user_id`,
  `server_jobs.decided_by`. The text actor (`owner`) is kept beside them.

## Test
`tests/test_isolation.py::test_every_route_is_scoped_owner_only_token_or_explicitly_allowlisted` enumerates every route and requires a
scoping dependency or a reasoned allowlist entry. `tests/test_multi_user.py` proves the behaviour.

## Not built yet
Per-user Claude connection / API key gateway (#725, change #74); email invites and notifications (in-app only by decision).
