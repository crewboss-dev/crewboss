# Gemini for jobs (#861, phase 1)

A job's agent can ask Google's Gemini for a **second opinion** on a plan, design, code or copy, and can make **pictures** (logos and other
creative work) with Nano Banana Pro. Both are plain REST `generateContent` calls **with no tools** in the request, made **from the runner** with
an API key kept in a private file there. Crewboss never holds the key. A job can use Gemini only on a runner that has the key file, and only in
a project whose Owner has switched it on.

The Gemini CLI is **not used** and is not needed: a headless CLI session can run shell commands, trusts its folder and leaves transcripts of
everything it was sent, so REST was chosen instead. The runner starts no Gemini process at all, and every job is told it may not run `gemini`
through Bash (`--disallowedTools Bash(gemini:*)`, a speed bump only).

## What a job gets

Two local tools, in a small stdio MCP server the runner adds to the job's MCP config (`crewboss-gemini`, code in
`runner/crewboss_runner/gemini_mcp.py`):

| Tool | Does |
|---|---|
| `second_opinion(kind, text, question="")` | `kind` is `plan`, `design`, `code` or `copy`. One request: the review framing as `systemInstruction`, the scrubbed material in `contents`, no `tools`. Returns the reply wrapped as untrusted data. |
| `make_image(brief, n=1, aspect="1:1")` | 1 to 4 pictures from Nano Banana Pro, saved on the runner. Nothing is published. The owner picks. |

### When to use it (the routing rule the job is told)

* **Second opinion** on a significant or hard-to-reverse plan, a design, or a new product's shape: call `second_opinion` when available, and say on
  the task what you took from it and what you did not. Not for routine builds.
* **Logos and creative images:** `make_image` (Nano Banana Pro). The owner picks; nothing is published.

The runner puts this paragraph at the start of the prompt only of a job that has the tools (`spawn.gemini_note`).

## The data boundary

* **Per-project switch**, `projects.gemini_allowed`, **off by default everywhere**. Only the project's Owner turns it on or off (the same right as
  the production switch of server reads), on the project's **Where it lives** page ("Gemini for jobs"). Every flip is on the project's activity.
* **Both halves must be true.** The claim carries `gemini_allowed`; the runner adds the server and the allowed tools only if it also has a usable
  key file (`Worker.job_may_use_gemini`). The tools are never in `JOB_TOOLS`, so a job without the switch cannot even name them.
* **Begin before anything leaves.** Every call is two steps through the job MCP, and Gemini is called only after step one succeeded:
  1. `begin_external_model_use(fence, tool, model, bytes_in)`: under the job's lock Crewboss checks the fence and lease and the project's switch,
     then writes a `pending` row and returns its id. A switch that is off is refused and kept as a `refused` row (after 20 such rows for one job
     the refusal still happens but no more rows are written); nothing is sent.
  2. `finish_external_model_use(fence, use_id, bytes_out, ok, error, bytes_in, limited)`: closes that job's **own** pending row, in a `finally`, so
     every failure after begin is recorded. A row of another job, a finished row or a refused row cannot be written.
  A call that never finishes counts as sent in the usage figure, and when its job ends (finished, cancelled, lost its runner) it is closed as
  "not finished (job ended)", not ok.
  **Crewboss enforces no spend or usage caps.** The one limit is a ceiling of 200 recorded calls per job, so a runaway job cannot grow the table:
  past it begin is refused with a clear message and one refused row and one activity row are written, not one per call. Set your limits and budget in Google's console (the API key's project quotas and billing alerts). The
  usage rows exist so there is a usage view, not to limit it.
* **When Google refuses for quota or rate** (HTTP 429, or a `RESOURCE_EXHAUSTED` status) the tool does not fail the job. It returns
  `{"limit_reached": true, "message": "Gemini limit reached ... carry on without the second opinion"}`, finishes the usage row as not ok with
  `limited: true`, and Crewboss puts a warning comment on the task (once per job) and sends the person who started the job one notification,
  at most one per project per hour.
* **The one remaining gap** is a switch flipped off during a single in-flight call: that call completes (its text already went), and the next begin
  is refused.
* **What leaves the PC** is only the text the job sends, scrubbed first (`runner/crewboss_runner/redact.py`, kept in step with the dashboard's
  `deploys.redact`: private keys, credential headers, URL passwords, bearer and known token shapes, Google API keys, JWTs, name=value and
  JSON-quoted secrets including `pwd`/`passwd` and names ending in `_KEY`/`SECRET`/`TOKEN`, 32 to 64 hex values after a key-like name, long
  opaque strings). The rules are linear-time (bounded name parts, same-line separators), so a 200 KB hostile line scrubs in well under a second;
  a name such as `primary_key: id` is still scrubbed (over-redaction is the safe side). Best effort, not a guarantee: the switch is the real boundary. Up to 200 KB per call. Text is encoded as UTF-8 throughout.
* **The key** is read from the key file at call time and sent only in the `x-goog-api-key` header, never in the URL, a log, the job's MCP config or
  any output. The file must be mode 0600 and owned by the runner's user, or it is refused.
* **What comes back** is scrubbed, capped (60 KB) and wrapped in `<<<UNTRUSTED OUTPUT [nonce] ...>>>` markers. The nonce is fresh and made after
  the reply arrived (not the one around the material), and any run of three or more `<` or `>` in the reply is broken up, so a reply cannot close
  or forge the fence. The result also says `untrusted: true` and that it is advice to weigh, never instructions.
* **Environment hygiene, for every job** (switch on or off): the runner strips `GEMINI_*`, `GOOGLE_GENAI_*`, `GOOGLE_API_KEY` and
  `GOOGLE_APPLICATION_CREDENTIALS` from the agent's environment, and the Gemini server's config entry adds only what it needs (its settings, the key
  file's path, the job token and URL, proxy settings). That entry is not an allowlist (Claude Code merges its own process environment with it);
  what protects the server is that the job's environment is already stripped and that it starts no child process.
* **This is not a sandbox.** The switch governs the **Gemini tools**. A job with Bash on a runner where the key file is reachable can read that
  file as the runner's user: jobs run as the same operating-system user as the runner (no separate user, by the owner's decision). Treat the
  switch as policy and accounting, and the key as one that a job could in principle reach.

## Setup on a runner

There are two ways to give a runner its Google API key. Either way the **key file on the runner is the only place the key lives**.

**A. From the runner's card (the Machines page).** Open the runner's card, "Add a Google API key for Gemini", paste the key and press Send key.
Tick "Send the same key to all of this person's runners" to use one key everywhere; leave it unticked for a different key per runner.
Replacing a key is a fresh paste. "Remove key" deletes the runner's file.

* The key goes from your browser to Crewboss in a POST body, and **straight on** to the runner over its existing authenticated runner socket (a
  `gemini_key` frame, see `runner-protocol.md`). The runner writes it atomically to its key file with mode `0600` and answers with ok and the
  first 8 hex characters of the key's sha256, which Crewboss checks against its own computation (a mismatch is a failure and the file is removed).
* **Crewboss never stores, logs or shows the key.** It keeps, per runner, only that fingerprint, the last four characters and the time it was set, and
  shows "Gemini key ending ...abcd". The route reads the form by hand so no validation error can echo it; the result is a redirect with a fixed
  sentence; activity rows hold the last four characters only.
* Who may: a person on their own runner, or an administrator (the account's owner) on any runner with a fresh step-up, as for other sensitive
  actions. The form is covered by the same-origin guard on browser writes.
* **A runner that is offline** gets "That runner is offline. Try again when it is connected": a key is never queued or held.
* "Send to all" reaches the runners at the same time with one overall wait; one that does not answer is reported as possibly on an older runner version (update it, then paste again). When a runner does not answer, or answers with a different fingerprint, the card shows the key as unconfirmed or "paste it again" instead of a stale last-four. Changing another person's runner needs a fresh step-up (authenticator code) from the last five minutes.
* A key file placed by hand (below) keeps working; Crewboss just does not know its last four characters.

**B. By hand.** Put a Gemini API key, on one line, in `~/.config/crewboss-runner/gemini_api_key` (the runner's config folder;
`crewboss-runner doctor` prints the exact path), owned by the runner's user, mode `0600`: `install -m 600 /dev/null <path>`, then write the key
into it. No CLI is needed.

Then:

1. `crewboss-runner doctor` shows a `gemini` line: "Gemini ready", "no key file" (optional, a pass), or a warning if the file is readable by
   others, owned by someone else or empty. The runner reports the capability to Crewboss, which shows "Gemini ready" on the runner's card on the
   Machines page. No call is made to check.
2. In the project, an Owner turns on **Gemini for jobs** on Where it lives.

Settings (runner environment): `CREWBOSS_RUNNER_GEMINI_REVIEW_MODEL` (default `gemini-3.1-pro-preview`), `CREWBOSS_RUNNER_GEMINI_IMAGE_MODEL`
(default `gemini-3-pro-image`).

### Model ids (neither confirmed by a live call)

* Review model `gemini-3.1-pro-preview`: the id the Gemini CLI 0.62.0 bundle names. It was **not** checked with a `models.list` call because the
  build box's key is not reachable. Change it with the setting if the API rejects it.
* Nano Banana Pro = Gemini 3 Pro Image. Google's image-generation page lists `gemini-3-pro-image`; third-party guides and the earlier preview use
  `gemini-3-pro-image-preview`. The first is the default and the second is tried once if the API answers 404.

## Where pictures go

`<runner state dir>/gemini-out/j<job>-e<fence>/image-<time>-<n>.png` (mode 0600), beside the job folders and not inside the git worktree, so nothing
is committed by accident and `crewboss-runner repair` keeps them. The job says on the task where they are. `bytes_in` for pictures is the scrubbed
brief times the requests actually made (a 404 retry counts twice).

## Usage on the pages

* The **task run panel** lists the job's Gemini calls (tool, model, bytes sent and back, in progress, refused, limit reached).
* The project's **Where it lives** page shows the switch and a usage count (all time, last 30 days, per tool).

## Not in phase 1

* Attaching the pictures to the task as materials (they stay on the runner for now).
* A spend figure per call; only sizes and counts are kept.
* Cleaning up old pictures in the runner's gemini-out folder (a follow-up).
* A budget or cap of any kind in Crewboss: Google's console is where limits are set.
* A separate operating-system user for jobs, which would make the switch a real boundary.
* Other providers; the `provider` column and tool names leave room.
