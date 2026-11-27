# The logic map (task #715, change #67)

Every app Crewboss builds keeps an always-current map of its workflows and AI processes **in its own repo**, so "how does this button
work, and what does it create?" is answered by a file that travels with the code, is reviewed in pull requests, and is checked at release.

- Format: `.crewboss/logic/*.yaml` (this page).
- Rules and discovery: `app/logic_map.py` (pure, no database). Dashboard side: `app/logic.py` (reports, gate, override, release diff).
- Commands: `crewboss logic ...` (the launcher; it runs `scripts/crewboss_logic.py` on the folder you are in, so an app with its own `app` package is fine; `python -m app.cli logic ...` also works inside Crewboss itself).
- Pages: the project's **Logic** tab (`/<slug>/logic`) and `/<slug>/logic/diff?sha=` (also a chip on each release).

## Files

Each `*.yaml` / `*.yml` file under `.crewboss/logic/` holds one workflow (a mapping with an `id`) or a `workflows:` list. A file may also carry
an `ignore:` list for entry points that are deliberately not mapped.

```yaml
workflows:
  - id: quote.won                       # lower-case letters, digits, . _ -  (stable: it is the key in diffs)
    name: Quote marked as won
    summary: One plain sentence of what this does for the business.
    trigger:
      type: button                      # button | route | timer | webhook | queue | event | cli | manual
      control: "Quote page: Won"        # the UI control that starts it (the button / menu item)
      detail: A person closes the quote as won
      route: "POST /quotes/{id}/won"    # optional: covers the discovered route of that name
      code_ref: {file: app/quotes.py, function: mark_won}
    data: ["table:quotes", "table:work_orders"]     # what the workflow touches
    steps:
      - id: save
        kind: action                    # action | decision | ai | data | call | loop | trigger
        label: Mark the quote won
        detail: Optional longer text.
        code_ref: {file: app/quotes.py, function: mark_won}   # function optional; "*" = the whole file
        data: ["table:quotes"]
        effects: ["change:quotes (status=won)", "create:work_orders", "email:customer", "sync:xero"]   # every side effect, named
      - id: needs-wo
        kind: decision
        question: Does the quote include labour?          # required for a decision
        label: Labour on the quote?
      - id: draft
        kind: ai
        label: Draft the hand-over note
        ai: {model: auto-newest Sonnet, skill: handover-note, may: ["write a draft", "read the quote"]}   # needs skill or prompt
    edges:                              # optional: without it the steps run straight on, in order
      - {from: trigger, to: save}
      - {from: save, to: needs-wo}
      - {from: needs-wo, to: draft, label: yes}
    covers:                             # optional: other code this workflow is responsible for
      - {file: app/quotes_events.py, function: "*"}

ignore:
  - {file: "app/admin/*", reason: "internal tooling, not a business workflow", kind: route}   # kind optional
```

Required: workflow `id`, `name`, `summary`, `trigger.type`, a non-empty `steps` list; each step `id` (unique in the workflow), `label`, a known `kind`;
a decision's `question`; an ai step's `ai.skill` or `ai.prompt`. A bad file produces a clear error that names the file and the place; it never
crashes the scan.

## What is checked

`crewboss logic check` (and the report the dashboard stores) answers:

| Check | Meaning |
|---|---|
| **errors** | a file is not valid YAML, or breaks the rules above |
| **stale** | a `code_ref` file is gone, or no longer contains the named function |
| **uncovered** | an entry point the scanner found in the code that no workflow names (through a `code_ref`, a `trigger.route` or `covers`) and that is not under `ignore` |

Discovery is generic: **Python** (decorated state-changing routes, `route(methods=[...])`, timer/queue decorators, `while True` + `sleep` loops, AI SDK calls
such as `messages.create`), **JS/TS** (express-style `post/put/patch/delete`, Nest decorators, cron, queue workers, AI SDK calls) and **Go** (net/http and
gin/echo/chi routes, cron, subscriptions, AI SDK calls). It skips tests, vendored code, migrations and build output. It will not see dynamic
dispatch; that is why hand-written entries and the uncovered list are both needed.

## The release gate

The project's **Logic** tab has a switch: **off** (the report is shown, nothing refuses) or **enforce**. When enforced, a *production* release request
(not a rollback) is refused when the commit has no report, its map has errors or stale entries, or it adds uncovered entry points compared with the last
release (the report of the last commit that went to production; the first report of a project is its starting point).

A project Admin can override the gate for **one release**: a recorded grant (who, why, which commit), used up by the first release request of that commit that
needed it. Only one unused override exists per project at a time. Each use is recorded on the release and in the activity log.

## Getting a report to the dashboard

1. `crewboss logic check` in the checkout (a job runs this before it finishes).
2. A job posts the report with the job MCP's `record_logic(fence, sha, report)`; CI or a person uses
   `crewboss logic report --post <dashboard url> --project <slug> --token-file <file>` (a `scanner` or session token, `POST /api/logic/report?sha=`).

## For an app's own admin page (optional)

`crewboss logic export [--format json|html] [-o file]` writes the slim map (JSON, for your own page) or one self-contained HTML page. Whether an app
shows it is the designer's choice; Crewboss never requires it.

## Faults

When the app's health feed names the failing workflow and step on an event (`workflow` and `step` fields, next to the release stamp), the Health page links that
event to the step on the Logic tab (best effort: the link works when the step is in the newest report).

## Migrating an existing catalog

`python -m app.cli logic import-catalog catalog.json [--dir .] [--name file.yaml]` converts a scanner catalog (docs/catalog-schema.md) into a first
map. What the catalog cannot say (button names, per-step side effects) is left for a person or agent to fill in.
