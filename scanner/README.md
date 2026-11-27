# Workflow + AI discovery scanner

Builds the catalog described in `docs/catalog-schema.md`: one JSON document per scan, `{project,
commit, generated_at, workflows}`, that the dashboard's `app/` turns into the Power-Automate-style
pipeline map + AI Node Inspector. Runs against **code only** — see PRIVACY below.

## How it works

```
scanner/discover/*.py   →  raw records (one per worker / webhook / decision / AI call site / MCP tool)
scanner/build.py        →  raw record -> auto Workflow skeleton (confidence: auto)
$CREWBOSS_HOME/scanner/annotations.yaml → hand-curated Workflow objects (confidence: annotated) that REPLACE the
                            auto entry for any source file they claim
scanner/scan.py         →  orchestrates: git archive -> discover -> merge -> validate -> write out/
```

**Discovery** (`scanner/discover/`), one module per surface:
- `workers.py` — the `app/workers/*.py` sleep-loops: reads the `INTERVAL`/`ENABLED` env pattern by
  regex (these are always simple `os.environ.get("X", "600")` one-liners), `app.clients` imports for
  systems touched, and any `INSERT/UPDATE/DELETE` table names found as string literals.
- `webhooks.py` — the `@router.post("/webhook/...")` handlers in `app/routes/webhook.py`.
- `decisions.py` — `app/decisions.py`'s `@executor(dtype)` / `@inline_handler(dtype)` registries
  (button-triggered) and its `produce_*` functions (the background jobs that populate the queue).
- `ai_erp.py` — the core piece. Walks every **public** function in `app/clients/claude.py` and finds
  either a direct `.messages.create(` call, or (one level down) a call to a private same-module helper
  that makes it — the `triage_alert -> _triage_once` shape. When a public function calls that helper
  **more than once** with different `model` arguments and the function body references
  `_escalate_model`/`_escalate_threshold`, it's recorded as the two-pass Haiku→Sonnet convention with
  both models named. The prompt is read straight from the referenced `_XXX_SYSTEM` module constant
  (implicit string concatenation is already one `ast.Constant` by the time we see it, so a multi-line
  prompt reads back whole). `context_injected` is built by resolving the `messages=[{"content": X}]`
  argument back through parameter-passing to its real construction, then listing every
  `<name>.get("key")` / `<name>["key"]` access plus any bare parameter referenced — identifiers only,
  never a literal value.
- `ai_aios.py` — same idea for the AiOS email-triage skill (`classify_email`, `classify_sales_reply`,
  `draft_reply`), which is real Anthropic traffic (raw `httpx`, no SDK) but shaped differently: prompts
  are built at RUNTIME by a `_system_prompt(role, ...)`-style function, not a static constant, and the
  AI helper is called through an **injectable parameter** (`caller=_call_claude`) for testability
  rather than by its real name. Rather than build a second generic dataflow resolver for three
  functions, the wiring (`_WIRING` dict) is explicit — see Known gaps.
- `ai_httpx.py` — the same for Sign (`sign/app/services/claude.py`) and the LinkedIn app (raw httpx to
  `api.anthropic.com/v1/messages`). Handles the case where the "system" value is a **stored/caller-
  supplied** value (LinkedIn's `draft_outreach` takes its base prompt from the database) — that is
  NEVER printed as `prompt_text`; only the fixed code riders around it are.
- `mcp_tools.py` / `connector.py` — clusters `@mcp.tool` functions (50 in `connector/server.py`, 13 in
  `aios/aios_mcp/server.py`) into FAMILIES (one workflow per family, not one per tool) using a small
  hand-checked name→family map (the generic verb-strip fallback badly over-split real names like
  `list_accounts` vs `update_account`).
- `routes.py` — resolves the REAL trigger for a `claude.py` AI function that no worker/webhook/decision
  reaches: which `@router.get/post` handler in `app/routes/*.py` calls it, directly or through 1-2
  edges of a small generic call-graph BFS over the WHOLE `app/` tree (not just `app/routes/`), because
  several AI functions are called from a shared helper module (`app/kb.py`, `app/persona.py`,
  `app/scheduling_approval.py`, `app/convodocs.py`, `app/worksummary.py`) that a route calls into — the
  route itself never mentions `claude` by name. A "call" here includes a bare function reference passed
  to a scheduler (`background.add_task(learn_from_finished_chat, ...)`), not just `f(...)` — that's how
  `extract_persona` (reached via `planning.py`'s background task → `persona.learn_from_chat` →
  `claude.extract_persona`) resolves. 23 of the 26 previously-unresolved `claude.py` functions resolve
  this way; `scanner/lib/route_ai_names.py` hand-names/groups each one (the coordinator's mapping:
  quotes/opportunities/leads → Sales; cases/kb/worknote → Service; invoices → Billing;
  devices/alerts → Monitoring; documents → Documents and Sign; admin/command → Platform).

- `state_routes.py` — the plain-button business processes (mark an opportunity lost, invoice a quote
  stage, raise a PO, cancel a quote) that none of the automation surfaces above can see. Every
  `@router.post/put/patch/delete` handler in `app/routes/*.py` that WRITES a business table — directly
  or within 2 call hops into `app/` (`routes.walk_calls`, the same BFS `routes.py` uses) — becomes a
  route record with its tables (`sqlwrites.literal_writes`; a table counts only if the migrations
  create it and it isn't a log/audit/read-state table) and the ENTITY its path addresses: the first
  path segment, plus a sub-entity under it (`/quotes/{id}/line/{lid}/tick` → Quote line,
  `/quotes/{id}/stage/{pid}/invoice` → Quote stage, `/quotes/{id}/po/create` + `/po/…` → Purchase
  order via `ENTITY_ALIASES`). `build.py` groups them into ONE auto workflow per (route file, entity) —
  `Sales › Quote › Actions`, one step per route with its tables in `detail` — never one per route
  (~330 routes become ~73 workflows); a one-route sub-entity folds into its parent entity. Groups come
  from `naming.ROUTE_FILE_GROUP` (quotes/opportunities/leads → Sales, cases/kb → Service, invoices →
  Billing, devices/alerts → Monitoring, documents → Documents and Sign, admin → Platform, …). Left out:
  pure UI-state paths (preferences, filters, autosave, timezone, read marks, login), and any route an
  annotation or another discovered workflow already names (webhook handlers, the routes AI functions
  were resolved to) — the same `_claimed_pairs` rule, applied after everything else is built, so an
  annotation REPLACES exactly the routes it claims and an entity with nothing left disappears.

**Assembly** (`scanner/build.py`): every raw record becomes a minimal AUTO workflow (trigger + one step
per system/table touched, plus an AI step if `ai_erp` found the function reachable). Then
`$CREWBOSS_HOME/scanner/annotations.yaml` (the instance folder, never this repo; `annotations.example.yaml` shows the shape) — a list of full `Workflow` objects, one per entry — is loaded; any AUTO
workflow whose source file is referenced by an annotation's `source_refs` or any step's `code_ref.file`
is **dropped** (workers — one file is one conceptual worker), or, for webhooks/decisions where MANY
unrelated handlers share ONE file, any whose specific `(file, function)` pair a step's `code_ref`
names is dropped (`_claimed_pairs`) — and the annotation is used instead (`confidence: annotated`).
AI call sites in `claude.py` reachable from neither a surviving auto entry nor an annotation are
resolved against a real calling route first (`routes.find_routes_for`); only the genuine remainder
(no route within 2 hops — e.g. a function only ever called from a background worker pass) falls back to
a generic `Platform`/"trigger unresolved" entry, so nothing silently disappears.

**Naming** (`scanner/lib/naming.py`): ids are `group.thing.action`, names are `Group › Thing ›
Action`, from the fixed group list in `docs/catalog-schema.md`. Auto-discovered entries get a
best-effort group from a small lookup table (`WORKER_GROUP`, `WEBHOOK_GROUP`, `DECISION_GROUP`) that is
deliberately coarse — accuracy for anything that matters lives in the annotation, not the fallback.

## Infrastructure workflows (the third root)

Host-level automations — systemd timers and watchdog scripts that run on the servers, not in the application —
live in the **knowledge** repo (`repos.knowledge` in scanner.yaml) under `infra/`. They are annotation-only (nothing is
auto-discovered there): an annotation with `group: Infrastructure` + `subproject: Infrastructure` has its
`code_ref`/`source_refs` paths read relative to that repo, which `scan.py` and `auto_rescan.py`
export from `origin/main` like the other two repos (`--knowledge-repo`, `''` to skip; its SHA lands in
`commits[<that repo's folder name>]`). Drift and hashing work as for any non-Python file: existence check, whole
file (or `lines`) hashed. If the repo isn't available the Infrastructure refs are skipped, never reported
broken. Current entries: `infrastructure.netbird-watchdog.self-heal`, `infrastructure.armbian-ramlog.hourly-sync`.

Every server-level job carries `box_jobs: [{unit: <name>.timer, hosts: [<servers.yml names>]}]` (no `hosts` = every
server). The dashboard compares it with the timers Zabbix finds on each server and warns about any timer no annotation
names and `app/drift.yaml` doesn't list as an OS timer (#402, docs/catalog-schema.md "No drift").

## What's auto vs annotated

Auto discovery proves a thing EXISTS and is wired (a worker loop, a webhook route, a decision executor, an AI call
site resolved to the route that reaches it, a connector tool family). It names and groups it from the file-derived name
and `naming.yaml` (below), and marks it `confidence: auto`. Anything where the step order, the decisions or the
wording matter is annotated by hand in `annotations.yaml`: an annotation REPLACES the auto entry for whatever source
files and functions it claims, and is marked `confidence: annotated`. Workers are claimed per file (one file is one
worker); webhooks and decisions are claimed per (file, function), because many unrelated handlers can share one file.

### Naming tables (`naming.yaml`)

When no annotation claims an item, its catalog group comes from five small tables in
`$CREWBOSS_HOME/scanner/naming.yaml` (`CREWBOSS_NAMING` overrides the path): `worker_group` (worker file stem),
`webhook_group` (webhook path), `decision_group` (decision type), `route_file_group` (route file stem, for the auto
"Entity > Actions" workflows) and `route_entity_group` (an entity-level override). Group names come from the fixed
vocabulary in `scanner/lib/naming.py`. The public defaults are empty, so an unlisted item lands in `Platform`; a missing
file means no overrides, and an unknown table name or a non-string value is an error. `scanner/naming.example.yaml`
shows every table with made-up names.

## Coverage report

`scanner/lib/coverage.py` lists every business table some function under `app/` writes that NO
workflow covers (annotated or auto: no `db:<table>` side effect and no step pointing at one of its
writers), with the writing functions. It lands in the catalog as the top-level `coverage` object
(`docs/catalog-schema.md` "Coverage") — the app stores the whole document, so the dashboard can show it
later; `scan.py` prints one `UNCOVERED:` line per table plus `covered/total` in the summary. Like
drift, it never fails a scan. The tables it reports are usually platform plumbing reached from workers or helpers rather than
from routes; annotate them or accept them as the known remainder.

## Decisions, loops and scopes

`docs/catalog-schema.md` ("Decisions, loops and scopes") is the contract: `kind: decision` (a
`question` + >= 2 labelled edges), `kind: loop` (`For each …`, `detail` names the collection), and
`parent`/`branch` on any step to put it inside a condition branch or a loop body, so the canvas can
draw Power Automate-style condition cards and "Apply to each" containers. A workflow with any
decision/loop must spell out its whole flow as `edges`; `scanner/lib/schema.py::validate_flow` checks
labels, endpoints, scopes and reachability from the trigger on every scan.

**Annotated** workflows spell out their flow with `kind: decision` and `kind: loop` steps and explicit `edges`; keep the
existing step ids when you add them, so anything that refers to a step (a health map, say) keeps resolving. A step that
already WAS the condition can become the decision card in place.

**Auto-derived** (`scanner/lib/autodecide.py`, on by default, `--no-auto-decisions` to turn off): for
an AUTO workflow, an `if`/`elif`/`else` or `match` in the analysed function (same-module helpers
followed one level) whose branches reach DIFFERENT side-effect steps becomes a decision labelled with
the condition source (`if mode == 'suppress'`, `else`), capped at two levels; guard clauses
and branches that share their steps don't qualify, and a rewrite that fails validation is rolled back
so it can never block an auto-import. It is a safety net for new, not-yet-annotated code: real branches often
share their writes or reach them through helpers more than two levels down, so on a mature codebase it is usually silent.

## Known gaps

- **Raw-httpx AI callers are not in `ai_usage`.** Callers that use `httpx` directly instead of the SDK are discovered
  (`discover/ai_httpx.py`) but write no usage row, so their token use is invisible to a usage dashboard.
- **Injected AI callers are wired by a table.** A function that takes the AI call as a parameter (for test fakes) is
  not found by name; `discover/ai_aios.py` wires those with an explicit `_WIRING` table.
- **Tool families are a hand-checked map** (`mcp_tools.FAMILY_OVERRIDES`); a new tool still gets a family from the
  generic fallback, just a coarser one.
- **AI functions with several calling routes** keep the first route found; `route_ai_names.MULTI_CALLER_FUNCS` records
  the rest rather than hiding them.
- **Background passes that no route reaches** are not found by the route search; annotate them.
- **"Entity > Actions" workflows draw as a straight run**: each step is an independent button, not a sequence. Entity
  names come from URL paths, so an oddly shaped path gives an odd name; an annotation that claims those routes
  replaces them. Writes are only seen as SQL string literals within 2 hops of the handler.
- **Auto step line ranges are sometimes omitted**; `code_ref.lines` is optional in the validator.

## Staleness hashes and annotation drift

Every `source_refs[]` entry carries `hash: sha256:...` (`scanner/lib/refhash.py`). It hashes the AST of
the functions the workflow names in that file (the ref's optional `function` key, else the step
`code_ref.function`s pointing at that file) plus the module-local helpers they call, so comments,
formatting, line shifts and edits to unrelated functions don't move it. Non-Python refs fall back to the
`lines` range, then the whole file. When an ANNOTATED workflow's hash moves but nothing else about it
changed, the app flags it "layout may be stale" and raises one Tweak task. The flag stays on later scans
until the annotation is edited or that task is closed. To pin a ref to a specific helper, give it
`function: name` (or a list) in `annotations.yaml`.

`scanner/lib/drift.py` checks every annotated workflow's hand-written references against the scanned
tree. If a step `code_ref` or a `source_ref` names a file or function that no longer exists, the
workflow gets a `problems` list (`missing_file` / `missing_function`, see `docs/catalog-schema.md`).
`scan.py` prints each problem as a `DRIFT:` line. The app shows the workflow as **broken** and raises a
"Fix broken workflow annotation" Tweak task on every import until the annotation is fixed. Drift is not
a scan failure (`--check` still passes), because the catalog has to reach the app for the task to be
raised.

## The ERP's integrations list (#393)

When the scanned ERP tree has `docs/generated/integrations.json` (its integration registry's export: every link to
another system, graded A–D), the scanner copies it into the catalog as the top-level `integrations`
(`lib/integrations.py`, docs/catalog-schema.md "Integrations"). An annotation step may name the integrations it uses —
`integrations: [xero]` — narrowing what its `system` maps to; an id the list doesn't know is drift
(`unknown_integration` on the workflow: broken on the dashboard, a Tweak task) and makes `run.sh --check` exit 1.
No file on the ERP's main = no key and no check. The dashboard draws it on each project's Integrations page and in
the workflow canvas's step panel, and writes `exports/catalog.json` (resolved ids per step) for the ERP's admin site.

### The admin site's catalog, offline (#402)

The ERP's release-prep builds `exports/catalog.json` for the exact ERP commit it ships, without the live dashboard:

```
cd <crewboss checkout> && .venv/bin/python -m app.catalog_export --project <slug> --ref <sha> --out <file>
```

Same scan as the auto-rescan (`export_repo` + `scan_trees` + the instance's `annotations.yaml`), the import's checks, the live
export's renderer. Exit 1 on a schema problem, a privacy finding, an unknown integration id or no
`docs/generated/integrations.json` at that ref; exit 2 on bad arguments or an unknown ref. Details:
docs/catalog-schema.md, "Integrations".

## Auto-rescan (keeps the dashboard current)

`scanner/auto_rescan.py` runs every 5 min from a timer (`scanner/systemd/*.example`, installed by `scanner/systemd/install.sh`),
as the user who owns the checkout. Its settings (repos, project, import URL, token file, state folder) are
`$CREWBOSS_HOME/scanner/scanner.yaml` (see `scanner.example.yaml` and `scanner/config.py`); the annotations are
`$CREWBOSS_HOME/scanner/annotations.yaml`. `CREWBOSS_HOME` defaults to `~/.crewboss`, so the timer, a person's shell and the
application's release script all find the same files. Each tick:

1. It runs `git fetch origin` in the configured repos (primary, secondary, knowledge). Only the
   remote-tracking refs change; working trees are never touched.
2. It compares {the origin/main SHAs, the annotations file, the scanner's own code} with the inputs of
   the last successful import (`<state_dir>/rescan.json`). If they match, it exits 0
   without scanning. An annotation or scanner fix therefore re-imports on the next tick even when
   neither repo moved.
3. Otherwise it `git archive`s each origin/main to a temp dir and scans it. It refuses to import if
   there is any schema problem or privacy finding.
4. It POSTs the catalog to `<import_url>?project=<project>` with
   `Authorization: Bearer <the token file>`. The token is never printed or
   logged, and it's redacted from error bodies. This endpoint goes through the same code path as
   `python -m app.catalog_import` (`app.catalog_import.import_document`). It accepts a `scanner`
   role token, which can do nothing else (no task writes, no MCP), or a `features` session token.
5. Only after a successful import does it record the new inputs, so a failed tick retries next time.

A failure is logged to the journal and exits 1, so `systemctl status crewboss-rescan` shows it
as failed. Every run (imported, unchanged or failed) rewrites
`<state_dir>/rescan-status.json`, for the Health page or a Zabbix item to read:
`ok`, `status`, `last_error`, `consecutive_failures`, `last_run_epoch`, `last_success_epoch`,
`last_import_epoch`, `scan_id`, `commits`, `workflows` and `broken`. Alert on `ok == false`, or on
`last_success_epoch` being more than ~15 min old (the timer stopped). Note: a Zabbix agent running as
`zabbix` can't read the owning user's home by default.

Manual use: `.venv/bin/python -m scanner.auto_rescan [--force | --dry-run]`. `--dry-run` fetches, scans
and validates, and writes nothing.

### Switch-over (not done yet: units not installed, no token issued)

1. Merge the branch into the deployed checkout and rebuild + restart the app
   (`docker compose build app && docker compose up -d app`). This applies migration
   `0005_scanner_token_and_broken_workflows.sql`, which adds the `scanner` token kind and the `broken`
   diff type.
2. Issue the token straight into its file, so it never lands on a terminal:
   ```
   mkdir -p "$(dirname <token file>)" && (umask 077 && cd <crewboss checkout> && \
     docker compose exec -T app python -m app.tokens create --kind scanner --name auto-rescan --quiet \
     > <token file>)
   ```
3. `.venv/bin/python -m scanner.auto_rescan --dry-run`, then `scanner/systemd/install.sh`. The install
   script checks the venv, the token file (must exist and be mode 600) and `systemd-analyze verify`,
   then installs both units to `/etc/systemd/system` and enables the timer. `--no-start` and
   `--uninstall` are also available.
4. Check it: `systemctl list-timers crewboss-rescan.timer`,
   `journalctl -u crewboss-rescan -n 30`, `cat <state_dir>/rescan-status.json`.

## Running it

```
scanner/run.sh                          # scan the scanner.yaml repos @ origin/main, write out/
scanner/run.sh --check                  # same, but exit 1 on any schema/privacy problem instead of writing
scanner/run.sh test                     # run the pytest suite (scanner/tests/)
scanner/run.sh --diff out/latest.json   # scan, then print a change report vs an older catalog file
python -m scanner.scan --help           # every flag (custom repo paths, --primary-root for an already-exported tree, ...)
```

`--diff OLD.json` (`scanner/lib/diff.py`) prints, per workflow id: workflows added/removed entirely,
and — for a workflow present in both — which top-level fields changed (name/group/trigger/confidence/
summary), which step ids were added/removed, and for a step present in both, field changes plus
(for an `ai` step) whether `prompt_text`/`model_rule`/`max_tokens` changed. Exit status is unaffected by
`--diff` — it's a report, not a check. This is the "catalog diffs over time" changelog the dev-dashboard
plan asked for; the app's own scan history can call `diff_catalogs()` directly instead of shelling out.

`run.sh`/`scan.py` never touch the configured repos' working trees:
they run `git fetch` + `git archive <ref> | tar -x` into a throwaway temp directory (cleaned up on
exit) and scan that. Needs `pyyaml` + `pytest` (`pip install -r scanner/requirements.txt`).

## Tests

`scanner/tests/test_discover_fixtures.py` runs the discovery logic against small fixture modules under
`scanner/tests/fixtures/` that mirror the real shapes (an `INTERVAL`/`ENABLED` worker, a webhook route,
an `@executor`/`@inline_handler`/`produce_*` trio, a direct AI call and a two-pass escalation) — no real
repo needed. `test_discover_routes.py` proves the routes BFS resolves an AI function reached only
through a shared helper 1 hop from the route (the `app/kb.py`-shaped case), and that an unreachable
function resolves to nothing. `test_diff.py` exercises `diff_catalogs`/`render_report` directly
(added/removed workflows, step add/remove/change, `ai` step `prompt_text` changes). `test_schema.py`
exercises the validator + privacy sweep directly. `test_annotations_and_health_map.py` validates the
annotations file in use (the instance's, else `annotations.example.yaml`: every entry schema-clean and privacy-clean) and
cross-checks `health_map.yaml`'s (workflow, step) references against it. The checks on one install's own workflows and the
end-to-end scan of its real repo (`test_instance_annotations.py`, `test_real_scan.py`, `test_instance_export.py`) live in that
instance folder's `tests/`: `.venv/bin/python -m pytest $CREWBOSS_HOME/tests -q`.
`test_refhash_and_drift.py` covers what moves a hash and what doesn't (unrelated edits, comments, line
shifts, called helpers, `Class.method`, the non-Python line-range fallback) and the drift `problems`
(missing file/function, auto workflows never checked, unscanned roots skipped).
`test_decisions_schema.py` proves every flow rule both ways (decision/loop/scope/reachability); `test_decisions_annotations.py` covers the loader keeping `question`/`parent`/`branch`/labels and that the annotations in use validate; `test_autodecide.py` runs the auto-derivation on fixture modules (if/elif/else, match, guard clause, shared steps, helpers, depth cap) and through `build_catalog` (flag on/off, annotated untouched, rollback).
`test_state_routes.py` builds a small route tree per test: direct, 1-hop and 2-hop writes (3 hops not
followed), what's left out (GET, UI state, log-only, tables the migrations don't create, SQL only in a
docstring), entity naming/aliases and singleton folding, one auto workflow per entity through
`build_catalog`, annotations replacing the routes they claim, a webhook handler not repeated, and the
coverage report + its validator. `test_scan_real_repo.py` also checks the named sales routes
(mark-lost/reopen, stage invoice, quote cancel/retract, PO routes) are in the real catalog.
`test_auto_rescan.py` drives `auto_rescan.main()` against throwaway bare-origin git repos and a local
HTTP stand-in for the dashboard. It covers: first import with the Bearer token, the token never
printed, unchanged ticks not re-importing, a new origin commit being fetched without touching the
checkout, annotation edits triggering a rescan, failures exiting 1 with status recorded and retried,
refusal on schema problems, and `--dry-run`.

## Privacy

The catalog is built from code only. `scanner/lib/schema.py:privacy_sweep()` greps the *rendered* JSON
for anything that looks like an email address (excluding known shared-mailbox role constants like
`support@`), an `sk-ant-...` key, or a generic API-key shape, and `scan.py` fails (`--check`) or warns
on any hit. This scanner never reads a `.env` file and never connects to a database, Graph, Xero, or
any other live system — every fact it reports comes from parsing source text.
