# Deploy requests, approvals and the executor (task #627, change #50, P5)

Rule: **a PC never holds SSH material.** A job's work reaches a server only when a person approves a card and the main
Dashboard server (this app) does the deploy itself. Code: `app/deploys.py`, `app/routers/approvals_pages.py`,
`app/ssh_ca.py` (signing), `db/migrations/0039_deploys.sql`. Tests: `tests/test_deploys.py`.

## Flow

```
job (job token)            person (Entra session)              dashboard executor
request_deploy ──requested──> Approve ──approved──> take lock ──running──> sign cert, ssh ──> done | failed
                              Refuse ──refused                       restart while running ──> orphaned
```

1. **Targets.** A person adds a `deploy_targets` row on `/<account>/servers` (the one editor; the approvals page only links to it), under the
   server it deploys to: project slug, name, a commissioned server on that page (its address, login user and pinned host key are used, so there is one list of reachable boxes), the repo path on the
   box, the branch it must be on, an optional one-line `post_cmd`, `env` (`dev` or `prod`, default prod; migration 0041), enabled. A server carries `reach` (`internal` or
   `external`, default internal), also set on the Servers page. Both are tags the page shows as chips and filters on; they change nothing
   about how a deploy runs. A job cannot create or change a target.
2. **Request.** `request_deploy(fence, target, sha, subject="")` on `/runner/mcp`. The job token and fence are checked like the other job
   tools (stale = `409 Conflict`). `target` must be an enabled target of the job's project; `sha` must be 40 lowercase hex. The row
   records ids and the sha as fact: project, target, job, task, device. `subject` is the job's own one-line description of the
   commit (max 200); the card shows it labelled "as the job described it". The same open target + sha returns the existing
   request; a job may have `CREWBOSS_DEPLOY_MAX_OPEN_PER_JOB` open requests. A Web Push goes to the subscribed phones (ids and the target
   name only; it links to `/<account>/approvals`).
3. **Approval.** `/<account>/approvals` needs a real Entra web session for the account (the page uses the same `_user` gate as
   `/runners`; device and job tokens are not sessions and are refused). Approve and Refuse are page forms behind the same-origin guard.
   Approving stores `decided_by` (a `users` row) and `decided_at`. Another account's request is a no-op/NotFound.
4. **Executor.** Approved requests are run by `deploys.Executor.run_once()`, which `app.main` calls every `CREWBOSS_DEPLOY_POLL_SECONDS`
   (5; 0 = off) from an asyncio task via `asyncio.to_thread`, like the job sweeper. It runs in the app process, one deploy at a time.
   The database lock is the real guard:
   - `claim` moves `approved` to `running` and inserts the lock `(account, 'deploy', 'target:<id>')` (`job_id` NULL, `server_job_id` set)
     in ONE transaction. If the lock is taken the request stays `approved` and is tried again next pass: a second approved deploy for
     the same target **waits**. A job token cannot take that lock: `jobs.acquire_lock` refuses `deploy` and `migrate` (a job may
     take only `merge`), which closes the P2 gap.
   - It signs a certificate with the account CA (`ssh_ca.sign`) and runs ssh with the pinned host key.
   - The output tail (last 4000 characters, redacted best effort: key blocks, `name=secret`, bearer and known token shapes, long
     opaque strings; a commit id is kept) is stored, the lock released, and the result commented on the task.

## The time limit (#829)

`limit` above is the target's own `deploy_targets.timeout_seconds` (60 to 7200; set on the Releases tab when the place to deploy is added),
or, when blank/NULL, `CREWBOSS_DEPLOY_TIMEOUT_SECONDS` (default 600). It sets the `timeout` wrapper (limit - 10), how long the dashboard waits
on ssh, the certificate's validity and the orphan sweep. A big image build wants about 1800.
The one instance-wide executor runs one deploy at a time, so only the INSTANCE OWNER may set a limit above the instance default; a project Owner's
target (`add_target_assigned`) is capped at the default (a shorter limit, 60 and up, is kept).

## The certificate

`ssh_ca.sign(..., minutes=ceil(limit/60)+1, force_command=<the script>, pty=False, port_forwarding=False)`:

- principal `srv-<server id>` only (one server);
- valid from 5 minutes ago (clock skew) for about 11 minutes, not an hour: a deploy's duration, not a session's;
- `-O clear`, then **only** `force-command=<the script>`: no pty, no port forwarding, no agent forwarding, no X11, no user rc;
- the certificate itself can run that one command whatever the client asks for. The client also sets `ClearAllForwardings`,
  `ForwardAgent=no`, `IdentityAgent=none`, `BatchMode`, `StrictHostKeyChecking=yes` against the pinned host key;
- key id `<account>:deploy:<request id>:srv-<id>:<serial>`; recorded in `ssh_cert_issues`; the keypair is generated per deploy in a
  0700 tmpfs dir and removed straight after.

Targets whose server connects through a jump host are refused when they are added and again at run time (a forced-command pass cannot open the jump hop).

## The command

```
timeout <limit - 10> sh -c '
cd <repo> || exit 97
[ "$(git rev-parse --abbrev-ref HEAD)" = <branch> ] || exit 96
echo "@@before $(git rev-parse HEAD)"
git fetch -q origin <branch> || exit 98
git merge-base --is-ancestor <sha> origin/<branch> || exit 95     # the commit is not on the branch yet
git merge -q --ff-only <sha> || exit 98
echo "@@after $(git rev-parse HEAD)"
[ "$(git rev-parse HEAD)" = <sha> ] || exit 99      # post_cmd does NOT run on a mismatch
<post_cmd>'
```

(the inner script is joined with `; `). It never runs `git pull`, which would take whatever is newest: HEAD can only become the
approved sha, even if someone pushes another commit after the approval. `timeout` makes the box stop the script when the
dashboard would give up. The dashboard reads the FIRST `@@before` / `@@after` line (printed before `post_cmd`, so its output can't
spoof them) and accepts only when the exit status is 0 **and** `@@after` equals the approved sha. Otherwise the request is `failed`:

- exit 95 (sha not an ancestor of `origin/<branch>`): failed with "<sha8> is not on <branch> yet (merge it first)", **not** flagged;
- a HEAD-after that is present and different sets `flagged`, shown as "HEAD mismatch" and in the task comment (`flagged` is reserved for this).

## Which commit to ask for (jobs and people)

A job commits on `dash/j<id>-e<fence>`, and the runner pushes that branch only when the job ends; a target deploys its own branch
(for example `main`). So a sha from a running job is normally **not** on the target branch, and `request_deploy` for it would fail
with "not on main yet". The rule, also in the runner's prompt: a job calls `request_deploy` only for a sha already on the target's
branch; otherwise it says in `job_comment` that the work needs merging, and **a person requests the deploy after the merge**:

- **Request a deploy** form on `/<account>/approvals`: target + 40-hex sha.
- **Ask again** on a failed, refused or orphaned card: a new request for the same target and sha.

Both need the same Entra session and the same-origin guard, record who asked (`requested_by`, no job), and still need an Approve.

## Restarts, orphans and locks

- **One executor.** A pg advisory lock (session level, `EXEC_LOCK_KEY`) lets only one app process run the executor pass; a second
  process skips its passes (harmless but idle; set `CREWBOSS_DEPLOY_POLL_SECONDS=0` on a replica to be explicit).
- On startup (`deploys.recover(startup=True)`, in the lifespan), if no other process holds that lock, every `running` request
  belongs to a dead process: it becomes `orphaned`, its lock is marked `orphaned`, and **nothing retries it**. If another process
  is executing, its live deploy is left alone. Each pass orphans only a running request that has run longer than
  its target's time limit + 60, whichever process owns it.
- **Timeout:** if ssh times out on the dashboard's side the request is `failed`, the partial output is kept, and the target's lock is
  marked **orphaned** (the box may still be running it; the `timeout` wrapper stops it, but a person checks and Clears the lock).
- An orphaned lock blocks that target (new approved requests wait) until a person clears it with **Clear** on the Machines page (Runners tab)
  (`jobs.clear_orphan`, the P2 rule); then the human re-requests. The box may be half-deployed: check it first.
- The P2 rule for a *job* that dies holding a deploy/migrate lock still exists (`jobs.end_lease`) for old rows; a job can no longer
  create one.

## Left for later

Migrate/merge on the executor; jump-host targets; a real commit subject (the dashboard has no clone, so the subject is the job's
claim); detached execution that survives a restart; per-target approvers.
