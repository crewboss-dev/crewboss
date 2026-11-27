# Workflow catalog schema (contract between scanner/ and app/)

The scanner writes ONE JSON document per scan: `{"project": "mallen-erp", "commit": "<sha>", "generated_at": "<iso>",
"workflows": [Workflow, ...], "coverage": Coverage}`. The app stores each scan (history) and diffs consecutive scans per
workflow id. `coverage` is OPTIONAL (see "Coverage" below).

Workflow:
```json
{
  "id": "billing.xero-token.refresh",            // lower-case, dot-separated: group.thing.action (stable across scans)
  "name": "Billing › Xero token › Refresh",       // `Group › Thing › Action`
  "group": "Billing",                             // Sales|Service|Monitoring|Billing|Email and AiOS|Remote access|Documents and Sign|Platform|Infrastructure
  "subproject": "ERP",                            // ERP|AiOS|Sign|LinkedIn|Portal|Connector|Infrastructure
                                                  // (Infrastructure = host automations; file paths are relative to mallen-knowledge)
  "summary": "one plain sentence",
  "trigger": {"type": "timer|webhook|email|button|event|api", "detail": "every 10 min / POST /webhook/syncro/asset / ..."},
  "has_ai": false,
  "steps": [Step, ...],                           // in order; the viewer draws them as cards
  "edges": [{"from": "step-id", "to": "step-id", "label": "optional (yes/no/on error)"}],  // omit for a straight run;
                                                  // REQUIRED for the whole flow once any step is a decision/loop
  "side_effects": ["db:invoices", "xero", "graph-mail", "push", ...],
  "source_refs": [{"file": "app/workers/token_refresh.py", "lines": "40-120", "hash": "sha256:abc..."}],
  "confidence": "auto|annotated",                 // annotated = names/steps from $CREWBOSS_HOME/scanner/annotations.yaml
  "box_jobs": [{"unit": "netbird-watchdog.timer", "hosts": ["web-1", "web-2"]}],  // OPTIONAL, annotations only: the
                                                  // systemd units this workflow runs as on servers (servers.yml names;
                                                  // no `hosts` = every server). See "No drift" below.
  "problems": [                                   // OPTIONAL, annotated only; present => the workflow is BROKEN
    {"code": "missing_function|missing_file", "where": "steps[call-xero].code_ref", "step": "call-xero",
     "file": "app/workers/token_refresh.py", "function": "refresh", "message": "... no longer exists"}
  ]
}
```
`source_refs[].hash` is OPTIONAL (a content hash of the referenced file/lines, any stable algorithm —
sha256 recommended). When present on an `annotated` workflow, the app compares it against the previous
scan's hash for the same file: if the hash changed but nothing else about the workflow did, the code
moved on without its annotation being revisited, and the app flags the workflow "layout may be stale"
and raises one dedup'd Tweak task to fix it. Omit it (or leave $CREWBOSS_HOME/scanner/annotations.yaml unhashed) and
staleness detection simply never fires for that workflow — it is additive, not required.

What the scanner hashes (scanner/lib/refhash.py): the AST of the FUNCTIONS the workflow names in that
file — the ref's own optional `function` key (a name or list of names; `Class.method` allowed), else
every step `code_ref.function` pointing at the same file — plus the module-local helpers they call
(transitively). Comments, formatting and line shifts don't move it; an edit to an unrelated function
in the same file doesn't either. Non-Python files (or refs that name no function) fall back to the
`lines` range, then the whole file. Every `source_ref` whose file exists gets a hash, auto and
annotated alike; the value is `sha256:<64 hex>`.

`problems` is OPTIONAL and only ever set on `annotated` workflows (auto ones are derived from the code
and can't drift). The scanner (scanner/lib/drift.py) lists every hand-written reference that no longer
resolves: a `source_refs[].file` / step `code_ref.file` that's gone (`missing_file`), or a
`code_ref.function` / `source_refs[].function` no longer defined in its file (`missing_function`).
The key is absent on a clean workflow. The app treats a non-empty `problems` as BROKEN: it shows the
workflow as broken, records a `broken` catalog diff, and raises one dedup'd Tweak task to fix the
annotation (a broken workflow is not also flagged stale — broken is the stronger signal).
Step:
```json
{
  "id": "call-xero",                              // stable slug within the workflow; health records reference it
  "kind": "trigger|precode|ai|logic|action|decision|loop",
  "question": "Is the token within 15 min of expiry?",  // decision only (required): the condition, in plain words
  "parent": "check-expiry",                       // OPTIONAL: the decision/loop scope this step sits inside
  "branch": "Yes",                                // required when `parent` is a decision: which branch
  "label": "Call Xero: refresh token",
  "detail": "short plain description",
  "system": "xero|graph|db|claude|syncro|zabbix|stripe|push|r2|sharepoint|crayon|pingco|internal",
  "code_ref": {"file": "...", "lines": "a-b", "function": "..."},
  "ai": {                                         // only when kind == "ai"
    "provider": "anthropic",
    "model_rule": "auto-newest Haiku, escalates to Sonnet below 0.7 confidence",
    "prompt_name": "_TRIAGE_SYSTEM",
    "prompt_location": {"file": "app/clients/claude.py", "lines": "a-b"},
    "prompt_text": "the system prompt constant as written in code (code only — never runtime data)",
    "context_injected": ["description of each variable/shape fed in — NEVER real email/record content"],
    "max_tokens": 512, "temperature": null,
    "output": "json-parsed-text|forced-tool-schema|text",
    "tools": []
  }
}
```
## Coverage (additive — business tables no workflow covers)

```json
"coverage": {
  "scope": "app/",                 // the code searched for writes
  "business_tables": 109,          // business tables some function under scope writes
  "covered": 98,
  "uncovered": [{"table": "stripe_payouts",
                 "writers": [{"file": "app/services_invoicing.py", "function": "record_payout"}]}]
}
```
A **business table** is one the ERP's `db/migrations/*.sql` create (replayed in order: CREATE, DROP, RENAME)
that isn't platform bookkeeping (logs, audit trails, usage counters, read/ack state — `sqlwrites.NON_BUSINESS_TABLES`).
A **writer** is a function (top-level, `Class.method`, or `<module>` for module-level SQL) with an `INSERT INTO` /
`UPDATE` / `DELETE FROM` string literal naming it (docstrings ignored). A table is **covered** when any workflow,
annotated or auto, lists `db:<table>` in `side_effects` or has a step whose `code_ref` (file + function) is one of its
writers. `uncovered` is the gap list the dashboard can show; like `problems`, it is a report, never a scan failure.
Validation (`scanner/lib/schema.py` `validate_coverage`): integer counts, and each `uncovered` entry has a `table` and
a non-empty `writers` list of `{file, function}`.

## Integrations (additive — the ERP's graded links, #393)

The ERP commits its integration registry's export (mallen-erp `app/integrations_registry.py` `export()`) as
`docs/generated/integrations.json`. When that file is on the ERP's scanned ref, the scanner copies it verbatim into
the catalog as the top-level `integrations` (scanner/lib/integrations.py) — no file, no key:

```json
"integrations": {
  "version": 1,
  "vocab": {"kinds": {...}, "flows": {...}, "starts": {...}, "zones": {...}, "resolves": {...}, "paths": {...},
            "auths": {...}, "transports": {...}},            // code → plain label
  "integrations": [{"id": "xero", "name": "Xero accounting", "module": "core", "purpose": "...", "us": "",
                    "peer": "Xero", "zone": "cloud", "flow": "in|out|both", "starts": "us|them|both", "kind": "api",
                    "label": "invoices, contacts", "endpoint": "api.xero.com", "resolves": "public_dns",
                    "path": "public", "auth": "oauth2", "transport": "tls", "keys": ["XERO_CLIENT_ID", ...],
                    "notes": "", "status": "live|planned|leftover", "accepted": "", "grade": "A|B|C|D",
                    "why": ["Encrypted.", "Signed in with: OAuth 2."]}],
  "systems": {"xero": ["xero"], "graph": ["ms-mail-send", ...], "internal": []},   // step `system` → ids
  "summary": {"A": 31, "B": 28, "C": 9, "D": 1}
}
```
Key NAMES only — never key state or values. Validated twice: by the scanner (`validate_catalog`, a shape problem
refuses the import) and by the app on import (`app/integrations.py validate`, a 400).

**Which integrations a step uses.** A step MAY carry `integrations: ["id", ...]` (annotations only) to narrow what
its `system` maps to; otherwise it uses every id `systems[step.system]` lists. One rule, three places:
`app/integrations.py resolve_step`, the canvas bundle (`frontend/src/canvas/integrations-model.ts`), and the export
below. An id the export doesn't know is annotation drift: a `problems` entry with code `unknown_integration`
(`{"code", "where": "steps[<id>].integrations", "step", "integration", "message"}`) — the workflow shows broken and a
Tweak task is raised — and `scanner/run.sh --check` fails on it. Checked only when the export exists.

**exports/catalog.json** (app/catalog_export.py): after every import of the ERP project's catalog, the app writes
`{version: 1, project, commit, generated_at, built_at, integrations_version, workflows, skills}` into the checkout's
`exports/` — the workflows as scanned with each step's resolved `integrations: [ids]` (known ids, weakest grade first,
`[]` when none), and the AI Skills as the /skills page derives them. `commit` = the ERP commit scanned, `generated_at` =
when it was scanned, `built_at` = when the file was written.

**The same file offline, for one ERP commit (#402, no drift).** The ERP's `bin/release-prep.sh` builds its admin
site's catalog for the exact commit it ships, before every merge to main, from the host with the dashboard's venv:

```
cd <dashboard checkout> && .venv/bin/python -m app.catalog_export --project mallen-erp --ref <sha> --out <file>
#   --erp-ref <sha>                    same as --ref
#   --repo <dir>                       default: the scanner's repo for the project (scanner/scan.py PROJECT_REPOS)
#   --erp-tree <dir> --commit <sha>    an already-exported tree instead of a ref
```

It scans that ref exactly as the auto-rescan does (`git archive`, this install's `$CREWBOSS_HOME/scanner/annotations.yaml`, the
LinkedIn and mallen-knowledge repos at their local `origin/main`), runs the import's own checks
(`catalog_import.check_document`) and writes through the one renderer the live write uses (`catalog_export.render`),
so the two cannot diverge (tests/test_catalog_export_cli.py proves byte-equality). Pure offline: no `git fetch`, no
dashboard, no database. Exit codes: **0** written; **1** refused — a schema problem, a privacy-sweep finding (counted,
never quoted), an annotation naming an integration id the ref's `docs/generated/integrations.json` doesn't list, or
no `integrations.json` at that ref; **2** bad arguments, an unknown project, or git couldn't resolve/export the ref.
Broken annotations (missing file/function) are printed as `DRIFT:` and exported flagged, as the import stores them.

## No drift (#402) — what the dashboard checks against the catalog

`app/drift.py`, config `app/drift.yaml` (reviewed), every 5 min; findings show in the Health page's "No drift" section.

- **A linked admin site's catalog vs what it runs.** Each linked project's health feed (`GET <base>/api/health`, v1;
  the ERP's is the one the dashboard already pulls) adds two fields, both optional (absent = "not reported", no alert):

  ```json
  "app_sha": "<the deployed commit, 40 hex>",
  "catalog": {"commit": "<the commit the admin site's catalog was built from>", "built_at": "<iso 8601>"}
  ```

  The dashboard runs `git diff --name-only <catalog.commit> <app_sha> -- <catalog_paths> ':(exclude)<exclude_paths>'`
  in a read-only mount of the project's `.git` (ERP: `app connector aios scanner`, minus `app/generated/` and
  `app/static/canvas/`, the ERP deploy-check's own rule). Non-empty = an **error** ("Admin site shows workflows from
  an older version than what's running"), sent once to the errors session. `catalog.commit != app_sha` alone is normal.
  A commit the copy doesn't have yet = a **warning** ("can't verify"). Main having workflow changes the running app
  lacks, first seen by the dashboard more than `undeployed_hours` (24) ago = a **warning**. Linking another project's
  admin site is a `drift.yaml` entry (`feed_url`, `read_token_file`, `repo`, paths) plus its `.git` mount.
- **Server automation with no diagram.** Zabbix item `systemd.unit.discovery[timer]` (template "Mallen Server Health",
  agent 2 systemd plugin) lists each server's timers. A timer is covered when an annotation's `box_jobs` names it for
  that server (or for every server) or it is on `drift.yaml`'s `os_timers` list; anything else is a **warning**
  "Automation on <hosts> with no diagram: <unit>" (one per unit). Cron jobs aren't checked yet.

## Decisions, loops and scopes (additive — Power Automate-style condition cards)

A **decision** step (`kind: "decision"`) is a condition card. It carries a `question` in plain words
("Is the token within 15 min of expiry?") and **at least two outgoing edges, every one labelled** with
a short branch name — `Yes`/`No`, or outcome names (`lead`, `case`, `reply needed`, `fyi / ignore`,
`route / queue`). Labels are unique per decision. `code_ref` points at the `if`/`match` that makes the
choice. A branch may be EMPTY: its labelled edge goes straight to the next step after the condition
(the guard-clause shape — `No` → carry on, `Yes` → a terminating step).

A **loop** step (`kind: "loop"`) is an "Apply to each" card: `label` reads `For each <thing>` and
`detail` names the collection (`accounts_ready(freq) — resolved cases with unbilled work`). Its
outgoing edges are one into its body (a step whose `parent` is the loop) and, optionally, one to the
step after the loop (the exit, taken once every item is done). A body's last step has no edge back to
the loop — the next iteration is implied.

**Scopes.** Any step may carry `parent: <id of a decision or loop>`; inside a decision it also carries
`branch: <one of that decision's edge labels>`. A branch's steps are exactly the steps with that
`parent` + `branch`; a loop's body is the steps with that `parent`. Scopes nest (a decision inside a
loop body carries `parent: <loop>`, its branch steps carry `parent: <decision>`). Steps after a scope
(outside it — the enclosing scope's `parent`, or none at top level) are the **rejoin**: the canvas draws
them below the container. The containers are extra structure ON TOP of `edges`; `edges` stays the
complete flow either way.

**The explicit-edges rule.** A straight run still omits `edges` (the canvas chains steps in array
order). But when a workflow has ANY decision or loop, it must give explicit `edges` for the WHOLE flow —
every branch, every rejoin, the loop entry/exit — because array order can no longer say what follows
what. (Older annotations that give only the branching part of their edges and have no decision/loop are
unaffected.)

Validation (scanner/lib/schema.py `validate_flow`) — enforced on every scan:
- a decision has a non-empty `question` and ≥ 2 outgoing edges, all labelled, labels unique;
- a loop has a non-empty `detail`, at least one body step, and an edge into its body;
- every edge endpoint is a real step id (all workflows);
- `parent` names an existing decision/loop (never itself; no parent cycles); `branch` is required inside
  a decision and must match one of its edge labels, and is not allowed inside a loop; each scoped step is
  reachable from its branch's edge (decision) or from its loop;
- once a workflow has a decision/loop: `edges` is non-empty and every non-trigger step is reachable from
  the trigger — the first step (the canvas synthesises a trigger in front of it when it isn't one) or
  any `trigger`-kind step (a mid-flow human approval such as "Owner clicks '+ Qualify'").

**Auto-derived decisions** (on by default; `python -m scanner.scan --no-auto-decisions` turns them off).
For an AUTO workflow the scanner looks, in the one function it already analyses (plus the same-module
helpers it calls, one level deep), for an `if`/`elif`/`else` or `match` whose branches reach DIFFERENT
side-effect steps it already detects (a `write-<table>`, a `call-<system>`, the AI call). It emits a
decision whose branch labels are the condition source text, shortened (`if billing_mode == 'suppress'`,
`elif …`, `else`, `case 'redirect'`), labels the card `Condition (line N)`, and says in its `detail` that
it is auto-derived. A step reached from more than one branch is hoisted to where the branches rejoin; a
condition left choosing between nothing different is dropped. At most two levels of nesting are drawn.
Annotated workflows are never touched, and a derived workflow that fails validation is left as a
straight run.

PRIVACY RULE: the catalog is built from CODE only. It may quote system-prompt constants (they are code); it must never
contain runtime data (emails, records, customer content, secrets, env values).

## How the dashboard reads it (canvas + inventories)

- **Links.** Explicit `edges` are drawn as given (labels kept). A trigger card is synthesised from
  `workflow.trigger` only when no step has `kind: "trigger"`. Every step nothing leads into (and that
  isn't a trigger or the first step) is linked from the step before it in array order — so an omitted
  `edges` is a straight run, and a partially annotated workflow gets its lead-in/tail joined on.
  (`frontend/src/workflow-graph.ts`, node-tested.) The canvas itself is the shared bundle `app/static/canvas/`
  (docs/canvas-contract.md), the same one the ERP's admin site draws with.
- **Structure.** Steps with `parent` / `branch` (and `kind: "decision" | "loop"`, `question`) nest into
  condition scopes and loops; without them, a step with several labelled out-edges opens a scope whose
  branches run until they rejoin. Graphs that don't nest (several roots, cross-branch merges, cycles)
  fall back to a plain layered layout. (`frontend/src/workflow-structure.ts`.)
- **AI Skills** (`/skills`) are derived, not declared: every `kind: "ai"` step grouped by
  `code_ref.file` + `code_ref.function` (else `ai.prompt_name`).
- **Rules & AI Instructions** (`/rules`) read an optional top-level `rules` array — until the scanner
  emits it the page is empty (`fixtures/rules-fixture.json` is loaded only when `CREWBOSS_RULES_FIXTURE` is set):
  ```json
  {"id": "email.skip-internal-senders", "name": "Skip mail from our own staff",
   "kind": "rule|instruction|builtin", "enabled": true,
   "workflow_step": {"workflow": "email.triage.classify", "step": "heuristic-gate"},  // optional
   "skill": "classify-email",                    // instructions only: the AI Skill id it tunes
   "conditions": "WHEN/IF summary", "action": "THEN summary (rules)", "guidance": "(instructions)",
   "source": {"file": "aios/.claude/skills/email-triage/classify.py"}}
  ```
  Same PRIVACY RULE: summaries of code/config, never runtime data. Hit counts arrive with phase 3.
