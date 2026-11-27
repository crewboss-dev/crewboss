# Handoffs and shared docs (#908)

Claude sessions of DIFFERENT projects in the SAME account can talk through Crewboss instead of the owner carrying `.md` files between
machines. Two things, both on the session MCP (`/mcp`, `app/mcp_server.py`):

- **Handoffs**: a message thread from one project's session to another's (`app/handoffs.py`).
- **Shared docs**: versioned Markdown owned by the ACCOUNT, readable and writable by every project in it (`app/account_docs.py`).

Tables (`db/migrations/0111_handoffs.sql`): `handoffs`, `handoff_replies`, `handoff_docs` (a handoff's linked docs), `account_docs`,
`account_doc_versions`. `account_id` is the partition root (`tree.account_root_id`, the same boundary `app/scope.py` draws).

## The boundary

Sessions never touch another project's data (`app/scope.py`). A handoff is a deliberate, narrow exception, and never crosses accounts.

| Action | Allowed when |
|---|---|
| send | the target is a non-archived, non-account project in the caller's own account partition, visible to the caller's person, and not one the caller's own session already works (use a task for that) |
| read / reply / close | the handoff's from OR to project is in the caller's reach (`scope.reach`) |
| docs read / write | the doc belongs to the caller's own account |

- Another account's project, a missing project and an archived one give the identical `project '<slug>' not found`. A handoff id of another
  account, a missing id and a thread the caller is not party to give the identical `handoff N not found`. Same for docs.
- The overseer (dash) has NO handoff or doc access (`PermissionError`); its reach is the dashboard root alone.
- A session whose node is an account (e.g. the `acme` session working acme-erp) sends as that account; its handoffs show "from Acme".
- Backstops in the database: triggers refuse a handoff whose two ends are not both in `account_id`, one addressed to an account, a reply or
  linked doc from another account, a follow-up (`reply_to`) of another account, a doc whose `account_id` is not a partition root, and a pinned
  connection of another account.
- Handoff and doc text is DATA from another session, never an instruction from the owner; it can't approve deploys or anything under
  Rule 0. Every tool docstring and every delivery path (channel, inbox hook, ground briefing, external kit) says so. Bodies are capped
  (200 KB, subject 120 chars) and refused if they look like a secret (`materials.looks_like_secret`).

## Delivery

Creating a handoff, a reply or a close enqueues one ids-only outbox event of kind `handoff` for the TARGET project (stream `all`,
`project_id` = the other party): `Handoff #N from <project>`, `Reply on handoff #N from <project>`, or `Handoff #N closed
by <project>`. No subject or body text: both are free text from another session. It routes like any task event (`repo._outbox_scope`): the project's own session if it has one
(tmux session or external agent, e.g. the Android app's Claude), else its nearest session-running ancestor. The stdio channel, the inbox hook
and the external kit relay it unchanged (they relay any kind); their instruction text tells the receiver to call `get_handoff`.

## Tools

Handoffs: `send_handoff(to_project, subject, body, doc_slugs=None, reply_to=None)`, `list_handoffs(status=None, direction=None)`
(`direction`: `to_me` / `from_me`), `get_handoff(id)`, `reply_handoff(id, body)`, `close_handoff(id)`. `reply_to` on a send links a NEW
handoff as a follow-up of an earlier one; `reply_handoff` answers inside an open thread (a closed thread takes no reply).

Docs: `put_doc(slug, title, body, base_version=None, note="")`, `get_doc(slug, version=None)`, `list_docs()`, `doc_diff(slug, a, b)`.
Create with `base_version` None/0; update with `base_version == current_version`, else a `Conflict` names the current version and nothing is
overwritten (an atomic compare-and-swap on `current_version`). Every version is kept.

## Review hardening (security review of #908)

- The overseer's project is never a handoff target, even when it sits inside the caller's account (same NotFound as a missing project).
- Activity rows carry ids, project names and doc slugs only, never a subject, body, title or note.
- A project move (`tree.set_place`) that would split a handoff across accounts, or leave a doc's account no longer a partition root, is
  refused. As a backstop, reading, replying to or closing a handoff needs both ends still inside its account; otherwise NotFound.
- Person-level visibility (#713): the Summary count and the page list only threads whose BOTH ends the viewer may see (filtered in SQL before
  the limit). **Shared docs need the whole account:** a doc is readable and writable (MCP and page) only by a caller whose person may view
  the account root; a person shared a single project sees none ("Shared docs are visible to people who can see the whole account"). This is the
  safer default and a decision the owner can change.
- Delivery: the event goes on the holder's `all` stream, else `features` if it has that session, else its first stream, so it can be claimed.
  When nobody works the target (`tree.session_holder` is None) the handoff is still stored; `send_handoff` returns `delivered_to: null` and a
  `warning`; otherwise `delivered_to: <holder slug>`.
- Flood limit: at most 20 OPEN handoffs from one project to one target (a Conflict beyond that), and events coalesce per (from, to) pair
  (`dedupe_key` `handoff:<from_id>:<to_id>`) while one is undelivered; once the session claims it the next is enqueued afresh.
- The secret check also covers a doc's title and note.

## Owner view

`/<account-or-project>/handoffs` (read only, no forms): the account's threads newest first (status, from → to, replies, bodies) and its docs
with version history. A project shows the threads it is party to. The project Summary shows the count of open handoffs.

## Not done

- Job tokens (`app/job_mcp.py`, `/runner/mcp`) get no handoff or doc tools. TODO: read-only `job_get_handoff` / `job_get_doc`, scoped to the
  job's project's account, once a job's project and account resolve cleanly there.
- `account_docs.pinned_connection_id` exists (same-account trigger) but no tool sets it yet.
