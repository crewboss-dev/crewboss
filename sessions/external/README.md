# External agent kit (task #574, change #35)

For a project worked by a Claude Code session that does **not** run on the sessions host: for example your Windows desktop
working a separate app repo. That session talks to the rest through **Dashboard tasks** plus
**contract docs in the repos**, nothing else. This folder is everything the desktop needs.

| file | what it is |
|---|---|
| `dash_inbox.py` | stdlib-only hook: hands the session any outbox events nobody has delivered (the Windows twin of `sessions/hooks/inbox.sh`) |
| `settings.json` | the hooks block that wires `dash_inbox.py` to SessionStart and UserPromptSubmit |
| `mcp.json` | the `dashboard` MCP server entry (URL plus a bearer header that reads the token from the environment) |

## One-time setup (Windows)

You need Python 3 on the PATH (`python --version`), Claude Code, and Git for Windows (Claude Code runs hooks through it).

1. **Reach the dashboard.** Open its URL (below) in a browser on the PC. If it is only reachable over a private network or VPN,
   connect that first.
2. **Mark the project and get a token** (in the dashboard, as the owner): open the project's **Overview**, find
   **Worked by an external agent**, press **Turn on**, then **Issue token**. The token is shown **once** on the page that
   comes back: copy it now. If you lose it, issue another and **Revoke** the old one.
   - The token reaches only that project (and its sub-projects). It never sees another account.
   - It is a secret: do not paste it into a chat, a repo, a task, a screenshot or a command line.
3. **Store the token as a user environment variable** (PowerShell, once; a new terminal picks it up):

   ```powershell
   [Environment]::SetEnvironmentVariable("CREWBOSS_DASH_TOKEN", "<paste the token>", "User")
   [Environment]::SetEnvironmentVariable("CREWBOSS_DASH_URL", "https://dash.example.com", "User")   # your dashboard, required
   ```

   Do not put it in `settings.json`, `.claude.json` or a file in the repo: both snippets below read it from the environment.
4. **Copy the hook:** put `dash_inbox.py` at `%USERPROFILE%\.claude\crewboss\dash_inbox.py` (create the folder).
   If your Python launcher is `py` rather than `python`, use that word in `settings.json`.
5. **Wire the hooks:** merge the `hooks` block of `settings.json` into `%USERPROFILE%\.claude\settings.json`
   (or the repo's `.claude/settings.json`). Keep any hooks you already have.
6. **Add the dashboard MCP:** merge `mcp.json` into the repo's `.mcp.json` (project scope; safe to commit because it holds no
   token), or run `claude mcp add --transport http dashboard https://dash.example.com/mcp/ --header "Authorization: Bearer ${CREWBOSS_DASH_TOKEN}"`
   and check the header kept the literal `${CREWBOSS_DASH_TOKEN}` rather than the token's value.
7. **Check it:** start `claude` in the repo and run `/mcp`: `dashboard` should be connected. Ask it to call `ground`: it
   lists the project's tasks. Comment on one of the project's tasks in the dashboard, then send any prompt: the next prompt
   shows a `[dashboard] Owner commented on task #N ...` line.

## How it works

- The project has **its own outbox**: dashboard events for it (task assigned, owner commented, change decided, ...) land
  there and nowhere else, and the parent account's session no longer sees that branch.
- No tmux session is ever started for it on the sessions host; the sync ignores it.
- The hook claims events the same way the dev box does (`GET /api/outbox?undelivered=1`, then
  `POST /api/outbox/{id}/delivered?via=hook`), so an event is shown once. Events carry only *which task changed*; read the
  task with the MCP's `get_task`.
- A `Handoff #N from <project>` event is a message from another project's session in the same account (#908): read it with
  the MCP's `get_handoff`, answer with `reply_handoff`. Its text is data, never an instruction from the owner.
- Writes are attributed to `claude-desktop:all` ("Desktop AI" in the dashboard), so the owner can see what it did.
- The hook is silent and fast on any failure (3 s timeout per call, nothing printed): a down mesh never blocks a prompt.
- The token travels only as an `Authorization` header to `CREWBOSS_DASH_URL` over HTTPS; the hook refuses plain http
  (except this machine) and does not follow redirects.

## Rule 0 still applies

**Nothing that changes production, deletes data or runs destructive commands happens on dashboard content alone.** Task
titles, bodies, comments and events are *requests*, not verified instructions: the desktop session treats them as data to
review, and anything destructive or production-affecting needs your explicit confirmation in that conversation. Charter rule
8 applies too: recommend significant changes with `recommend_change` and wait for your decision. The hook's safety text says
this to the model on every delivery; keep it.

## Rotating or removing

- **Lost or leaked token:** on the Overview press **Revoke** (its next call fails with 401), then **Issue token** and redo step 3.
- **Done with the desktop agent:** **Turn off** on the Overview. That revokes its tokens and the project goes back to being
  worked by its parent's session. Remove the environment variable (`[Environment]::SetEnvironmentVariable("CREWBOSS_DASH_TOKEN", $null, "User")`).

## Upgrading an older install

TODO(#589 compat): a desktop set up before the rename has `MALLEN_DASH_TOKEN` / `MALLEN_DASH_URL` and the hook under `.claude\mallen\`. The hook still reads the old names, but the dashboard URL is now required (there is no built-in default any more), so set `CREWBOSS_DASH_URL` (or keep `MALLEN_DASH_URL`) on every such machine. Remove this section and the fallback in `dash_inbox.py` once every desktop was reconfigured.
