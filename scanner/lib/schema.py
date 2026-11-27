"""Validator for the catalog contract in docs/catalog-schema.md — hand-written (no jsonschema
dependency) because the shape is small and stable, and because we want the PRIVACY check to run in
the same pass. Raises SchemaError with every problem found (not just the first) so `--check` gives a
useful report in one run.
"""
from __future__ import annotations

import re

GROUPS = {
    "Sales", "Service", "Monitoring", "Billing", "Email and AiOS", "Remote access",
    "Documents and Sign", "Platform", "Infrastructure",
}
# "Infrastructure" = host-level automations (systemd timers etc.) whose code lives in the
# host-automation repo (infra/...), scanned as the third root — see build.py's root_for.
SUBPROJECTS = {"ERP", "AiOS", "Sign", "LinkedIn", "Portal", "Connector", "Infrastructure"}
TRIGGER_TYPES = {"timer", "webhook", "email", "button", "event", "api"}
STEP_KINDS = {"trigger", "precode", "ai", "logic", "action", "decision", "loop"}
SCOPE_KINDS = {"decision", "loop"}   # steps that may be a `parent` (a scope container on the canvas)
CONFIDENCE = {"auto", "annotated"}
PROBLEM_CODES = {"missing_file", "missing_function", "unknown_integration"}
HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_UNIT_RE = re.compile(r"^[A-Za-z0-9:_.@\\-]+\.(timer|service|path|socket)$")   # a systemd unit a box job runs as
ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*\.[a-z0-9]+(-[a-z0-9]+)*\.[a-z0-9]+(-[a-z0-9]+)*$")

# Privacy sweep — flags things that look like runtime data leaking into a code-derived catalog.
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_SK_KEY_RE = re.compile(r"sk-ant-[A-Za-z0-9\-_]{10,}")
_GENERIC_KEY_RE = re.compile(r"\b(xox[baprs]-|ghp_|AKIA)[A-Za-z0-9]{8,}")
# Code constants that are legitimately allowed to contain an '@' or a company-domain-looking string —
# the scanner only ever WRITES these from source, but a reviewer grepping the output should not have
# to special-case them by hand, so they are excluded up front.
_ALLOWED_AT_STRINGS = {
    "support@", "info@", "accounts@", "sign@", "admin@",  # shared-mailbox / role addresses, not people
    "alarms@",  # the alarm-monitoring shared mailbox (named in the ERP's integrations list, #393)
}


class SchemaError(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("\n".join(problems))
        self.problems = problems


def _req(d: dict, key: str, path: str, problems: list[str]):
    if key not in d:
        problems.append(f"{path}: missing required key '{key}'")
        return None
    return d[key]


def validate_step(step: dict, path: str, problems: list[str]):
    for k in ("id", "kind", "label", "system", "code_ref"):
        _req(step, k, path, problems)
    if step.get("kind") not in STEP_KINDS:
        problems.append(f"{path}: step kind '{step.get('kind')}' not in {sorted(STEP_KINDS)}")
    cr = step.get("code_ref") or {}
    for k in ("file", "function"):
        if k not in cr:
            problems.append(f"{path}.code_ref: missing '{k}'")
    if "integrations" in step and not (isinstance(step["integrations"], list)
                                       and all(_nonempty_str(i) for i in step["integrations"])):
        problems.append(f"{path}: 'integrations' must be a list of integration ids")
    if step.get("kind") == "decision" and not _nonempty_str(step.get("question")):
        problems.append(f"{path}: kind=decision needs a plain-words 'question'")
    if step.get("kind") == "loop" and not _nonempty_str(step.get("detail")):
        problems.append(f"{path}: kind=loop needs a 'detail' naming the collection it iterates")
    if "branch" in step and "parent" not in step:
        problems.append(f"{path}: 'branch' without a 'parent'")
    if step.get("kind") == "ai":
        ai = step.get("ai")
        if not ai:
            problems.append(f"{path}: kind=ai but no 'ai' block")
        else:
            for k in ("provider", "model_rule", "prompt_location", "output"):
                if k not in ai:
                    problems.append(f"{path}.ai: missing '{k}'")


def _nonempty_str(v) -> bool:
    return isinstance(v, str) and bool(v.strip())


def _reachable(starts, adj: dict, blocked: str | None = None) -> set:
    """Step ids reachable from `starts` (inclusive) along `adj`, never passing THROUGH `blocked`."""
    seen, stack = set(), [s for s in starts if s != blocked]
    while stack:
        sid = stack.pop()
        if sid in seen:
            continue
        seen.add(sid)
        stack.extend(n for n in adj.get(sid, ()) if n != blocked and n not in seen)
    return seen


def validate_flow(wf: dict, path: str, problems: list[str]):
    """The control-flow rules (docs/catalog-schema.md "Decisions, loops and scopes"): only enforced
    once a workflow has a decision or a loop — a straight run, or the older partial-edge annotations,
    are left exactly as they were. Assumes validate_workflow already reported unknown edge endpoints
    (edges naming an unknown step are ignored here rather than double-reported)."""
    steps = [s for s in (wf.get("steps") or []) if isinstance(s, dict) and s.get("id")]
    by_id = {s["id"]: s for s in steps}
    edges = [e for e in (wf.get("edges") or []) if isinstance(e, dict)
             and e.get("from") in by_id and e.get("to") in by_id]
    adj: dict = {}
    out_edges: dict = {}
    for e in edges:
        adj.setdefault(e["from"], []).append(e["to"])
        out_edges.setdefault(e["from"], []).append(e)

    # parent / branch — structural, checked whenever present
    children: dict = {}
    for s in steps:
        if "parent" not in s:
            continue
        sid, pid = s["id"], s.get("parent")
        where = f"{path}: step '{sid}'"
        if pid == sid:
            problems.append(f"{where} is its own parent")
            continue
        parent = by_id.get(pid)
        if parent is None:
            problems.append(f"{where} has parent '{pid}' which is not a step")
            continue
        if parent.get("kind") not in SCOPE_KINDS:
            problems.append(f"{where} has parent '{pid}' of kind '{parent.get('kind')}' — "
                            f"a parent must be a decision or a loop")
            continue
        children.setdefault(pid, []).append(s)
        if parent.get("kind") == "decision":
            labels = {e.get("label") for e in out_edges.get(pid, [])}
            if not _nonempty_str(s.get("branch")):
                problems.append(f"{where} is inside decision '{pid}' but has no 'branch'")
            elif s["branch"] not in labels:
                problems.append(f"{where} has branch '{s['branch']}' but decision '{pid}' has no "
                                f"outgoing edge labelled that (labels: {sorted(l for l in labels if l)})")
        elif "branch" in s:
            problems.append(f"{where} is inside loop '{pid}' and must not carry a 'branch'")
    # no parent cycles (a → b → a)
    for s in steps:
        seen, cur = set(), s
        while cur is not None and "parent" in cur:
            if cur["id"] in seen:
                problems.append(f"{path}: parent chain through step '{s['id']}' is a cycle")
                break
            seen.add(cur["id"])
            cur = by_id.get(cur.get("parent"))

    scoped = [s for s in steps if s.get("kind") in SCOPE_KINDS]
    if not scoped:
        return

    # A workflow with ANY decision or loop spells out its whole flow as edges.
    if not edges:
        problems.append(f"{path}: has a decision/loop but no 'edges' — the whole flow (branches and "
                        f"rejoins) must be given explicitly")
        return
    for s in scoped:
        sid, where = s["id"], f"{path}: {s.get('kind')} '{s['id']}'"
        outs = out_edges.get(sid, [])
        if s.get("kind") == "decision":
            labelled = [e for e in outs if _nonempty_str(e.get("label"))]
            if len(labelled) < 2:
                problems.append(f"{where} needs >= 2 labelled outgoing edges (has {len(labelled)})")
            if len(labelled) != len(outs):
                problems.append(f"{where} has an outgoing edge with no label")
            labels = [e["label"] for e in labelled]
            if len(set(labels)) != len(labels):
                problems.append(f"{where} has duplicate branch labels {labels}")
            for e in outs:   # an edge straight into one of this decision's own branch steps
                tgt = by_id[e["to"]]
                if tgt.get("parent") == sid and tgt.get("branch") != e.get("label"):
                    problems.append(f"{where}: edge labelled '{e.get('label')}' enters step '{tgt['id']}' "
                                    f"which is in branch '{tgt.get('branch')}'")
        else:
            kids = {c["id"] for c in children.get(sid, [])}
            if not kids:
                problems.append(f"{where} has no body — no step names it as 'parent'")
            elif not any(e["to"] in kids for e in outs):
                problems.append(f"{where} has no edge into its body")

    # Every step inside a scope must actually be reached from that scope: a decision's branch step
    # from the edge carrying its branch label, a loop's body step from the loop.
    for pid, kids in children.items():
        parent = by_id[pid]
        if parent.get("kind") == "decision":
            for c in kids:
                starts = [e["to"] for e in out_edges.get(pid, []) if e.get("label") == c.get("branch")]
                if starts and c["id"] not in _reachable(starts, adj, blocked=pid):
                    problems.append(f"{path}: step '{c['id']}' (branch '{c.get('branch')}' of '{pid}') "
                                    f"is not reachable from that branch's edge")
        else:
            reach = _reachable(adj.get(pid, []), adj, blocked=pid)
            for c in kids:
                if c["id"] not in reach:
                    problems.append(f"{path}: step '{c['id']}' (body of loop '{pid}') is not reachable "
                                    f"from the loop")

    # Every non-trigger step reachable from the entry: the first step (the canvas synthesises a
    # trigger in front of it when it isn't one) plus any trigger-kind step (a mid-flow human approval).
    roots = {steps[0]["id"]} | {s["id"] for s in steps if s.get("kind") == "trigger"}
    reach = _reachable(roots, adj)
    for s in steps:
        if s["id"] not in reach:
            problems.append(f"{path}: step '{s['id']}' is not reachable from the trigger")


def validate_workflow(wf: dict, problems: list[str]):
    wid = wf.get("id", "<no id>")
    path = f"workflow[{wid}]"
    for k in ("id", "name", "group", "subproject", "summary", "trigger", "steps", "confidence"):
        _req(wf, k, path, problems)
    if "id" in wf and not ID_RE.match(wf["id"]):
        problems.append(f"{path}: id '{wf['id']}' is not lower-case group.thing.action")
    if "name" in wf and " › " not in wf["name"]:
        problems.append(f"{path}: name '{wf['name']}' is not 'Group › Thing › Action'")
    if wf.get("group") not in GROUPS:
        problems.append(f"{path}: group '{wf.get('group')}' not in {sorted(GROUPS)}")
    if wf.get("subproject") not in SUBPROJECTS:
        problems.append(f"{path}: subproject '{wf.get('subproject')}' not in {sorted(SUBPROJECTS)}")
    trig = wf.get("trigger") or {}
    if trig.get("type") not in TRIGGER_TYPES:
        problems.append(f"{path}.trigger: type '{trig.get('type')}' not in {sorted(TRIGGER_TYPES)}")
    if wf.get("confidence") not in CONFIDENCE:
        problems.append(f"{path}: confidence '{wf.get('confidence')}' not in {sorted(CONFIDENCE)}")
    steps = wf.get("steps") or []
    if not steps:
        problems.append(f"{path}: no steps")
    step_ids = set()
    for i, step in enumerate(steps):
        validate_step(step, f"{path}.steps[{i}]", problems)
        sid = step.get("id")
        if sid in step_ids:
            problems.append(f"{path}: duplicate step id '{sid}'")
        step_ids.add(sid)
    for e in wf.get("edges") or []:
        if e.get("from") not in step_ids or e.get("to") not in step_ids:
            problems.append(f"{path}: edge {e} references an unknown step id")
    validate_flow(wf, path, problems)
    for i, ref in enumerate(wf.get("source_refs") or []):
        if not isinstance(ref, dict) or not ref.get("file"):
            problems.append(f"{path}.source_refs[{i}]: missing 'file'")
        elif "hash" in ref and not HASH_RE.match(str(ref["hash"])):
            problems.append(f"{path}.source_refs[{i}]: hash {ref['hash']!r} is not 'sha256:<64 hex>'")
    if "problems" in wf:
        probs = wf["problems"]
        if not isinstance(probs, list):
            problems.append(f"{path}: 'problems' must be a list")
        else:
            if wf.get("confidence") != "annotated":
                problems.append(f"{path}: 'problems' is only valid on an annotated workflow")
            for i, pr in enumerate(probs):
                if not isinstance(pr, dict) or pr.get("code") not in PROBLEM_CODES or not pr.get("message"):
                    problems.append(f"{path}.problems[{i}]: needs code in {sorted(PROBLEM_CODES)} + message")
    if "box_jobs" in wf:
        jobs = wf["box_jobs"]
        if not isinstance(jobs, list):
            problems.append(f"{path}: 'box_jobs' must be a list")
        else:
            for i, job in enumerate(jobs):
                if not isinstance(job, dict) or not _nonempty_str(job.get("unit")) or not _UNIT_RE.match(job["unit"]):
                    problems.append(f"{path}.box_jobs[{i}]: needs a systemd 'unit' name, e.g. 'acme-backup.timer'")
                elif "hosts" in job and not (isinstance(job["hosts"], list) and job["hosts"]
                                             and all(_nonempty_str(h) for h in job["hosts"])):
                    problems.append(f"{path}.box_jobs[{i}]: 'hosts' must be a non-empty list of server names "
                                    f"(servers.yml), or left out for every server")
    has_ai_step = any(s.get("kind") == "ai" for s in steps)
    if wf.get("has_ai") and not has_ai_step:
        problems.append(f"{path}: has_ai=true but no step has kind='ai'")
    if has_ai_step and not wf.get("has_ai"):
        problems.append(f"{path}: an 'ai' step exists but has_ai is not true")


def validate_coverage(cov, problems: list[str]):
    """The optional top-level `coverage` object (scanner/lib/coverage.py): business tables the code
    writes that no workflow covers, each with its writing functions."""
    if not isinstance(cov, dict):
        problems.append("catalog.coverage: must be an object")
        return
    for k in ("business_tables", "covered"):
        if not isinstance(cov.get(k), int):
            problems.append(f"catalog.coverage: '{k}' must be an integer")
    unc = cov.get("uncovered")
    if not isinstance(unc, list):
        problems.append("catalog.coverage: 'uncovered' must be a list")
        return
    for i, entry in enumerate(unc):
        path = f"catalog.coverage.uncovered[{i}]"
        if not isinstance(entry, dict) or not _nonempty_str(entry.get("table")):
            problems.append(f"{path}: needs a 'table'")
            continue
        writers = entry.get("writers")
        if not isinstance(writers, list) or not writers or not all(
                isinstance(w, dict) and w.get("file") and w.get("function") for w in writers):
            problems.append(f"{path}: 'writers' must be a non-empty list of {{file, function}}")


def validate_catalog(catalog: dict) -> list[str]:
    """Returns a list of problems (empty = valid). Never raises; `--check` decides what to do."""
    problems: list[str] = []
    for k in ("project", "commit", "generated_at", "workflows"):
        if k not in catalog:
            problems.append(f"catalog: missing required key '{k}'")
    if "coverage" in catalog:
        validate_coverage(catalog["coverage"], problems)
    if "integrations" in catalog:
        from scanner.lib.integrations import validate as validate_integrations
        problems.extend(validate_integrations(catalog["integrations"]))
    seen_ids = set()
    for wf in catalog.get("workflows") or []:
        validate_workflow(wf, problems)
        wid = wf.get("id")
        if wid in seen_ids:
            problems.append(f"catalog: duplicate workflow id '{wid}'")
        seen_ids.add(wid)
    return problems


def privacy_sweep(text: str) -> list[str]:
    """Flags anything in the *rendered* catalog JSON that looks like runtime data rather than code:
    an email address (other than a known shared-mailbox role constant), an Anthropic key, or a
    generic API-key-shaped token. Returns human-readable findings; empty = clean."""
    findings = []
    for m in _EMAIL_RE.finditer(text):
        addr = m.group(0)
        local = addr.split("@", 1)[0].lower() + "@"
        if local in _ALLOWED_AT_STRINGS:
            continue
        findings.append(f"possible email address in output: {addr!r}")
    for m in _SK_KEY_RE.finditer(text):
        findings.append(f"possible Anthropic API key in output: {m.group(0)[:12]}...")
    for m in _GENERIC_KEY_RE.finditer(text):
        findings.append(f"possible API key/token in output: {m.group(0)[:12]}...")
    return findings
