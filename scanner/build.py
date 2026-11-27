"""Assembles the final catalog: runs every scanner/discover/* module, turns each raw record into an
AUTO Workflow skeleton, then lets $CREWBOSS_HOME/scanner/annotations.yaml REPLACE any workflow whose source file(s)
it claims with a fully hand-ordered, confidence='annotated' entry. See scanner/README.md for why the
split works this way (auto discovery proves EXISTENCE + wiring; annotations carry the step order/
naming quality the owner asked for on the ~40 workflows that matter most).
"""
from __future__ import annotations

import ast
from pathlib import Path

from scanner.lib import astutil as A
from scanner.lib import autodecide
from scanner.lib import coverage
from scanner.lib import drift
from scanner.lib import naming as N
from scanner.lib import refhash
from scanner.lib.route_ai_names import ROUTE_AI_NAMES
from scanner.discover import (ai_aios, ai_erp, ai_httpx, connector, decisions, mcp_tools, routes, state_routes,
                              webhooks, workers)

SIGN_CLAUDE = "sign/app/services/claude.py"
LINKEDIN_FILES = ["backend/app/services/anthropic.py", "backend/app/services/enrich.py", "backend/app/services/search_suggest.py"]
AIOS_MCP = "aios/aios_mcp/server.py"


def _step(sid, kind, label, *, system="internal", detail="", code_ref=None, ai=None):
    d = {"id": sid, "kind": kind, "label": label, "detail": detail, "system": system,
         "code_ref": code_ref or {}}
    if ai is not None:
        d["ai"] = ai
    return d


def _direct_ai_calls(fn, alias: str) -> set:
    """Function names on `alias` (e.g. 'claude') called directly within `fn` — used to attach an AI
    step to the worker/webhook/decision that reaches into app/clients/claude.py."""
    return {c.func.attr for c in ast.walk(fn) if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
            and isinstance(c.func.value, ast.Name) and c.func.value.id == alias}


def _ai_step_for_erp_fn(root: Path, file: str, fn_name: str, ai_index: dict, step_id="ai") -> dict | None:
    """Re-parses `file` to find `fn_name` and check which claude.py functions it calls directly,
    matching against ai_erp's index of AI-reachable functions. One extra parse per file is cheap and
    keeps discover/workers.py etc. from having to know about AI at all."""
    mod = A.parse_module(root, file)
    if mod is None:
        return None
    for fn in A.top_level_functions(mod):
        if fn.name != fn_name:
            continue
        called = _direct_ai_calls(fn, "claude")
        for name in called:
            if name in ai_index:
                rec = ai_index[name]
                return _step(step_id, "ai", f"AI: claude.{name}()", system="claude",
                             code_ref={"file": file, "lines": mod.line_range(fn), "function": fn_name},
                             ai=rec["ai"])
    return None


def _worker_workflow(raw: dict, ai_index: dict, root: Path) -> dict:
    name = raw["name"]
    group = N.WORKER_GROUP.get(name, "Platform")
    steps = [_step("trigger", "trigger", "Timer", detail=raw["trigger_detail"],
                   code_ref={"file": raw["file"], "function": raw.get("entry_function") or "main"})]
    for i, sysname in enumerate(raw["systems"]):
        steps.append(_step(f"call-{sysname}", "action", f"Call {sysname}", system=sysname,
                           code_ref={"file": raw["file"], "function": raw.get("entry_function") or "main"}))
    ai_step = None
    if raw.get("entry_function"):
        ai_step = _ai_step_for_erp_fn(root, raw["file"], raw["entry_function"], ai_index)
    if ai_step:
        steps.append(ai_step)
    for t in raw["db_tables"]:
        steps.append(_step(f"write-{t}", "action", f"Write db:{t}", system="db",
                           code_ref={"file": raw["file"], "function": raw.get("entry_function") or "main"}))
    steps.append(_step("heartbeat", "logic", "Heartbeat", system="db",
                       code_ref={"file": raw["file"], "function": raw.get("entry_function") or "main"}))
    has_ai = ai_step is not None
    return {
        "id": N.make_id(group, name, "run"), "name": N.make_name(group, name.replace("_", " "), "Run"),
        "group": group, "subproject": "ERP",
        "summary": (raw["docstring"].strip().splitlines() or [""])[0][:200],
        "trigger": {"type": "timer", "detail": raw["trigger_detail"]},
        "has_ai": has_ai, "steps": steps, "edges": [],
        "side_effects": [f"db:{t}" for t in raw["db_tables"]] + list(raw["systems"]),
        "source_refs": raw["source_refs"], "confidence": "auto",
    }


def _webhook_workflow(raw: dict, ai_index: dict, root: Path) -> dict:
    group = N.WEBHOOK_GROUP.get(raw["path"], "Platform")
    steps = [_step("trigger", "trigger", "Webhook", detail=f"POST {raw['path']}",
                   code_ref={"file": raw["file"], "function": raw["func"]})]
    for sysname in raw["systems"]:
        steps.append(_step(f"call-{sysname}", "action", f"Call {sysname}", system=sysname,
                           code_ref={"file": raw["file"], "function": raw["func"]}))
    ai_step = _ai_step_for_erp_fn(root, raw["file"], raw["func"], ai_index)
    if ai_step:
        steps.append(ai_step)
    for t in raw["db_tables"]:
        steps.append(_step(f"write-{t}", "action", f"Write db:{t}", system="db",
                           code_ref={"file": raw["file"], "function": raw["func"]}))
    thing = raw["path"].replace("/webhook/", "").replace("/", " ") or "generic"
    return {
        "id": N.make_id(group, thing, "receive"), "name": N.make_name(group, thing, "Receive"),
        "group": group, "subproject": "ERP",
        "summary": (raw["docstring"].strip().splitlines() or [f"Webhook {raw['path']}"])[0][:200],
        "trigger": {"type": "webhook", "detail": f"POST {raw['path']}"},
        "has_ai": ai_step is not None, "steps": steps, "edges": [],
        "side_effects": [f"db:{t}" for t in raw["db_tables"]] + list(raw["systems"]),
        "source_refs": raw["source_refs"], "confidence": "auto",
    }


def _decision_workflow(raw: dict, ai_index: dict, root: Path) -> dict:
    group = N.DECISION_GROUP.get(raw["dtype"], "Platform")
    trigger = {"type": "button", "detail": f"decision type '{raw['dtype']}'"} if raw["kind"] in ("executor", "inline") \
        else {"type": "event", "detail": "queue producer (called from casegen/mailrouter/etc.)"}
    steps = [_step("trigger", "trigger", "Approve" if trigger["type"] == "button" else "Produce",
                   detail=trigger["detail"], code_ref={"file": raw["file"], "function": raw["func"]})]
    ai_step = _ai_step_for_erp_fn(root, raw["file"], raw["func"], ai_index)
    if ai_step:
        steps.append(ai_step)
    for t in raw["db_tables"]:
        steps.append(_step(f"write-{t}", "action", f"Write db:{t}", system="db",
                           code_ref={"file": raw["file"], "function": raw["func"]}))
    thing = raw["dtype"].replace(".", " ") if raw["kind"] != "producer" else raw["dtype"].replace("_", " ")
    action = "execute" if raw["kind"] == "executor" else ("apply" if raw["kind"] == "inline" else "produce")
    return {
        "id": N.make_id(group, thing, action), "name": N.make_name(group, thing, action.title()),
        "group": group, "subproject": "ERP",
        "summary": (raw["docstring"].strip().splitlines() or [raw["func"]])[0][:200],
        "trigger": trigger, "has_ai": ai_step is not None, "steps": steps, "edges": [],
        "side_effects": [f"db:{t}" for t in raw["db_tables"]],
        "source_refs": raw["source_refs"], "confidence": "auto",
    }


def _family_workflow(raw: dict, subproject: str) -> dict:
    group = "Platform"
    steps = [_step("trigger", "trigger", "MCP tool call", detail="invoked by an operator's AI client",
                   code_ref={"file": raw["file"], "function": "(any tool in this family)"})]
    for t in raw["tools"]:
        steps.append(_step(A.slugify(t["func"]), "action", t["title"], system="mcp",
                           code_ref={"file": t["file"], "lines": t["lines"], "function": t["func"]}))
    return {
        "id": N.make_id(group, f"{subproject.lower()}-mcp-{raw['family']}", "call"),
        "name": N.make_name(group, f"{subproject} MCP › {raw['family']}", "Call"),
        "group": group, "subproject": subproject,
        "summary": f"MCP tool family '{raw['family']}' ({len(raw['tools'])} tool(s)): "
                   + ", ".join(t["func"] for t in raw["tools"]),
        "trigger": {"type": "api", "detail": "MCP tool call"},
        "has_ai": False, "steps": steps, "edges": [],
        "side_effects": [], "source_refs": raw["source_refs"], "confidence": "auto",
    }


def _ai_only_workflow(raw: dict, group: str, subproject: str, system: str) -> dict:
    steps = [
        _step("trigger", "trigger", "Invoked", detail="see annotations.yaml for the real caller",
              code_ref=raw["code_ref"]),
        _step("ai", "ai", f"AI: {raw['func']}()", system=system, code_ref=raw["code_ref"], ai=raw["ai"]),
    ]
    return {
        "id": N.make_id(group, raw["func"].replace("_", "-"), "run"),
        "name": N.make_name(group, raw["func"].replace("_", " "), "Run"),
        "group": group, "subproject": subproject,
        "summary": f"AI call site: {raw['func']}() ({raw['code_ref']['file']})",
        "trigger": {"type": "event", "detail": "unresolved trigger — auto discovery only"},
        "has_ai": True, "steps": steps, "edges": [],
        "side_effects": [], "source_refs": [{"file": raw["code_ref"]["file"], "lines": raw["code_ref"]["lines"]}],
        "confidence": "auto",
    }


def _route_ai_workflow(raw: dict, route: dict) -> dict:
    """A claude.py AI function resolved to a real `@router.get/post` caller by discover/routes.py —
    button-triggered, named/grouped from ROUTE_AI_NAMES (scanner/lib/route_ai_names.py), and confidence
    stays 'auto' (it's discovered, not hand-ordered) even though it now carries a real trigger."""
    func = raw["func"]
    group, thing, action, subproject = ROUTE_AI_NAMES.get(
        func, ("Platform", func.replace("_", " "), "Run", "ERP"))
    detail = f"{route['method']} {route['path']}"
    hop_note = ""
    if (route["hop_module"], route["hop_func"]) != (route["route_file"], route["route_func"]):
        hop_note = f" (via {route['hop_module']}.{route['hop_func']}())"
    steps = [
        _step("trigger", "trigger", "Click", detail=f"{detail}{hop_note}",
              code_ref={"file": route["route_file"], "function": route["route_func"]}),
        _step("ai", "ai", f"AI: claude.{func}()", system="claude", code_ref=raw["code_ref"], ai=raw["ai"]),
    ]
    return {
        "id": N.make_id(group, thing, action),
        "name": N.make_name(group, thing, action),
        "group": group, "subproject": subproject,
        "summary": f"AI call site claude.{func}() — reached from {detail}{hop_note}.",
        "trigger": {"type": "button", "detail": detail},
        "has_ai": True, "steps": steps, "edges": [],
        "side_effects": [], "source_refs": [{"file": raw["code_ref"]["file"], "lines": raw["code_ref"]["lines"]},
                                             {"file": route["route_file"]}],
        "confidence": "auto",
    }


def _state_route_workflows(records: list[dict], claimed_pairs: set[tuple[str, str]], taken_ids: set[str]) -> list[dict]:
    """discover/state_routes.py's plain-button routes -> ONE auto workflow per (route file, entity),
    one step per route ("Sales › Quote › Actions": Cancel quote, Retract quote, …). A route whose
    (file, function) an annotation or another discovered workflow already names is left out — the same
    `_claimed_pairs` rule webhooks/decisions use — and an entity with nothing left disappears. Ids that
    collide (the same entity in two route files, or an existing workflow) get the file stem appended."""
    kept = [r for r in records if (r["file"], r["func"]) not in claimed_pairs]
    groups = state_routes.group_records(kept)
    by_id: dict[str, list[dict]] = {}
    for g in groups:
        stem = Path(g["file"]).stem
        g["group"] = N.ROUTE_ENTITY_GROUP.get(g["entity"]) or N.ROUTE_FILE_GROUP.get(stem, "Platform")
        g["thing"] = g["entity_name"]
        by_id.setdefault(N.make_id(g["group"], g["thing"], "actions"), []).append(g)
    for wid, gs in by_id.items():
        gs.sort(key=lambda g: (-len(g["routes"]), g["file"]))
        for i, g in enumerate(gs):
            if i > 0 or wid in taken_ids:
                g["thing"] = f"{g['entity_name']} ({Path(g['file']).stem.replace('_', ' ')})"

    out = []
    for g in groups:
        file, thing, group = g["file"], g["thing"], g["group"]
        steps = [_step("trigger", "trigger", "Click",
                       detail="any one of the actions below — each is its own button/route, not a sequence",
                       code_ref={"file": file, "function": "(any route in this group)"})]
        tables: list[str] = []
        for r in g["routes"]:
            writes = []
            for t in r["tables"]:
                writes.append(f"db:{t['table']}" + (f" (via {t['via']}())" if t["via"] else ""))
                if t["table"] not in tables:
                    tables.append(t["table"])
            steps.append(_step(A.slugify(r["func"]), "action", state_routes.label_for(r["func"]), system="db",
                               detail=f"{r['method']} {r['path']} — writes " + ", ".join(writes),
                               code_ref={"file": file, "lines": r["lines"], "function": r["func"]}))
        n = len(g["routes"])
        out.append({
            "id": N.make_id(group, thing, "actions"), "name": N.make_name(group, thing, "Actions"),
            "group": group, "subproject": "ERP",
            "summary": f"{n} state-changing route(s) on {g['entity_name']} records in {file} — each step is "
                       f"a separate staff action, not a sequence.",
            "trigger": {"type": "button", "detail": f"staff action — {n} route(s) in {file}"},
            "has_ai": False, "steps": steps, "edges": [],
            "side_effects": [f"db:{t}" for t in sorted(tables)],
            "source_refs": [{"file": file}], "confidence": "auto",
        })
    return out


def _claimed_files(annotations: list[dict]) -> set[str]:
    """Every file ANY annotation touches — used for workers, where one file is one conceptual worker
    (even split across several annotated workflows), so file-level claiming is exactly the right grain."""
    files = set()
    for wf in annotations:
        for sr in wf.get("source_refs") or []:
            if sr.get("file"):
                files.add(sr["file"])
        for st in wf.get("steps") or []:
            cr = st.get("code_ref") or {}
            if cr.get("file"):
                files.add(cr["file"])
    return files


def _claimed_pairs(annotations: list[dict]) -> set[tuple[str, str]]:
    """(file, function) pairs from every step's code_ref — used for webhooks and decisions, where
    MANY unrelated handlers/dtypes/producers share ONE file (app/routes/webhook.py,
    app/decisions.py). File-level claiming there would silently drop every unannotated handler in the
    same file the moment ONE sibling got annotated (a real bug this fix closes: 6 `produce_*` decision
    workflows were vanishing entirely, neither annotated nor auto, because every OTHER dtype in
    app/decisions.py is annotated)."""
    pairs = set()
    for wf in annotations:
        for st in wf.get("steps") or []:
            cr = st.get("code_ref") or {}
            if cr.get("file") and cr.get("function"):
                pairs.add((cr["file"], cr["function"]))
    return pairs


def _finalize_annotation(wf: dict) -> dict:
    out = dict(wf)
    out.setdefault("edges", [])
    out.setdefault("side_effects", [])
    out["has_ai"] = any(s.get("kind") == "ai" for s in out.get("steps", []))
    out["confidence"] = "annotated"
    for st in out.get("steps", []):
        st.setdefault("detail", "")
        st.setdefault("system", "internal")
        st.setdefault("code_ref", {})
    return out


def attach_source_hashes(workflows: list[dict], root_for, cache: "refhash.ModuleCache") -> None:
    """Adds `hash` to every source_ref it can (scanner/lib/refhash.py says what is hashed). Builds
    NEW ref dicts rather than mutating in place — annotated workflows share their ref dicts with the
    loaded annotations.yaml data."""
    for wf in workflows:
        root = root_for(wf)
        refs = []
        for ref in wf.get("source_refs") or []:
            ref = {k: v for k, v in ref.items() if k != "hash"}
            h = refhash.source_ref_hash(root, wf, ref, cache)
            if h:
                ref["hash"] = h
            refs.append(ref)
        wf["source_refs"] = refs


# Auto-derived decisions (scanner/lib/autodecide.py) are ON by default: over the real ERP they are
# silent today (see scanner/README.md), and any workflow they would leave schema-invalid is reverted
# rather than allowed to block an import. `python -m scanner.scan --no-auto-decisions` turns them off.
AUTO_DECISIONS_DEFAULT = True


def add_auto_decisions(autos_with_fn: list, root: Path) -> int:
    """For each (auto workflow, file, function) the discovery already analysed, let autodecide turn a
    real if/elif/else or match between different side-effect steps into decision cards. Returns how
    many workflows gained at least one decision. Never touches an annotated workflow. A rewrite that
    fails schema validation is rolled back to the plain auto workflow — auto_rescan refuses to import
    a catalog with any schema problem, so a derivation bug must never be able to stall the dashboard."""
    import copy

    from scanner.lib import schema

    modules: dict = {}
    n = 0
    for wf, file, func in autos_with_fn:
        if not file or not func:
            continue
        if file not in modules:
            modules[file] = A.parse_module(root, file)
        mod = modules[file]
        if mod is None:
            continue
        fns = {f.name: f for f in A.top_level_functions(mod)}
        fn = fns.get(func)
        if fn is None:
            continue
        before = copy.deepcopy(wf)
        try:
            added = autodecide.apply(wf, fn, file=file, func_name=func, module_functions=fns)
        except Exception:  # noqa: BLE001 — best effort: a derivation bug leaves the plain workflow
            added = 0
            wf.clear()
            wf.update(before)
        if not added:
            continue
        problems: list = []
        schema.validate_workflow(wf, problems)
        if problems:
            wf.clear()
            wf.update(before)
            continue
        n += 1
    return n


def build_catalog(erp_root: Path, linkedin_root: Path | None, annotations: list[dict],
                   erp_sha: str, linkedin_sha: str | None, generated_at: str,
                   auto_decisions: bool = AUTO_DECISIONS_DEFAULT,
                   knowledge_root: Path | None = None, knowledge_sha: str | None = None,
                   project: str = "", labels: dict | None = None) -> dict:
    N.load_overrides()   # the instance's group tables ($CREWBOSS_HOME/scanner/naming.yaml); absent = none
    ai_erp_raw = ai_erp.discover(erp_root)
    ai_index = {r["func"]: r for r in ai_erp_raw}

    worker_raw = workers.discover(erp_root)
    webhook_raw = webhooks.discover(erp_root)
    decision_raw = decisions.discover(erp_root)
    connector_raw = connector.discover(erp_root)
    aios_mcp_raw = mcp_tools.discover_families(erp_root, AIOS_MCP, kind="aios_mcp_family")
    ai_aios_raw = ai_aios.discover(erp_root)
    ai_sign_raw = ai_httpx.discover(erp_root, SIGN_CLAUDE, "sign")
    ai_linkedin_raw = []
    if linkedin_root is not None:
        for f in LINKEDIN_FILES:
            ai_linkedin_raw += ai_httpx.discover(linkedin_root, f, "linkedin")

    claimed = _claimed_files(annotations)
    claimed_pairs = _claimed_pairs(annotations)

    autos = []
    with_fn = []   # (auto workflow, file, function) — what add_auto_decisions may look inside
    for r in worker_raw:
        if r["file"] in claimed:
            continue
        autos.append(_worker_workflow(r, ai_index, erp_root))
        with_fn.append((autos[-1], r["file"], r.get("entry_function")))
    for r in webhook_raw:
        if (r["file"], r["func"]) in claimed_pairs:
            continue
        autos.append(_webhook_workflow(r, ai_index, erp_root))
        with_fn.append((autos[-1], r["file"], r["func"]))
    for r in decision_raw:
        if (r["file"], r["func"]) in claimed_pairs:
            continue
        autos.append(_decision_workflow(r, ai_index, erp_root))
        with_fn.append((autos[-1], r["file"], r["func"]))
    if auto_decisions:
        add_auto_decisions(with_fn, erp_root)
    for r in connector_raw:
        autos.append(_family_workflow(r, "Connector"))
    for r in aios_mcp_raw:
        autos.append(_family_workflow(r, "AiOS"))
    for r in ai_aios_raw:
        if r["code_ref"]["file"] in claimed:
            continue
        autos.append(_ai_only_workflow(r, "Email and AiOS", "AiOS", "claude"))
    for r in ai_sign_raw:
        if r["code_ref"]["file"] in claimed:
            continue
        autos.append(_ai_only_workflow(r, "Documents and Sign", "Sign", "claude"))
    for r in ai_linkedin_raw:
        if r["code_ref"]["file"] in claimed:
            continue
        autos.append(_ai_only_workflow(r, "Sales", "LinkedIn", "claude"))

    annotated = [_finalize_annotation(a) for a in annotations]

    # Fallback for AI call sites in claude.py that NEITHER a surviving auto worker/webhook/decision
    # reaches NOR any annotation names (e.g. compose_quote/draft_kb_article/recommend_* — called from
    # interactive app/routes/*.py, which this scanner does not walk generically): rather than let
    # them vanish from the catalog, emit one generic auto entry per unattached function so every one
    # of claude.py's AI call sites is counted somewhere (scanner/README.md calls this out — the
    # trigger on these is genuinely unresolved without annotating the calling route).
    attached_claude_funcs = {
        st["code_ref"].get("function")
        for wf in (autos + annotated) for st in wf.get("steps", [])
        if st.get("kind") == "ai" and (st.get("code_ref") or {}).get("file") == ai_erp.CLAUDE_PY
    }
    unattached = [r for r in ai_erp_raw if r["func"] not in attached_claude_funcs]
    # Before falling back to a bare "Platform, unresolved trigger" entry, walk app/routes/*.py (1-2
    # call levels — discover/routes.py) for a real `@router.get/post` handler that reaches each
    # unattached function: that gives it a genuine button trigger + the right group (ROUTE_AI_NAMES).
    route_matches = routes.find_routes_for({r["func"] for r in unattached}, erp_root) if unattached else {}
    for r in unattached:
        route = route_matches.get(r["func"])
        if route:
            autos.append(_route_ai_workflow(r, route))
        else:
            autos.append(_ai_only_workflow(r, "Platform", "ERP", "claude"))

    # Plain-button business processes (discover/state_routes.py): every state-changing route that
    # writes a business table, grouped one workflow per (route file, entity). Runs LAST so a route any
    # annotation or already-built auto workflow names — a webhook handler, the route an AI function was
    # resolved to above — is claimed and left out (annotations REPLACE the routes they claim).
    claimed_by_any = _claimed_pairs(autos + annotated)
    autos += _state_route_workflows(state_routes.discover(erp_root), claimed_by_any,
                                    {w["id"] for w in autos + annotated})

    # De-dup by id (an annotation always wins over an auto entry of the same id, and a duplicate
    # WITHIN one side is a bug in that side rather than something to silently drop).
    by_id: dict = {}
    for wf in autos + annotated:
        by_id[wf["id"]] = wf   # annotated processed second → wins on collision
    workflows = sorted(by_id.values(), key=lambda w: w["id"])

    # Which tree an annotated workflow's file paths are relative to. Infrastructure workflows (host
    # automations: systemd timers, watchdog scripts) live in the host-automation repo's infra/ — nothing
    # is AUTO-discovered there, it is annotation-only, scanned so hashes + drift work. A root that wasn't
    # scanned this run is None → hashing and drift skip it ("couldn't look" is not "gone").
    def root_for(wf: dict) -> Path | None:
        sub = wf.get("subproject")
        if sub == "LinkedIn":
            return linkedin_root
        if sub == "Infrastructure":
            return knowledge_root
        return erp_root

    cache = refhash.ModuleCache()
    attach_source_hashes(workflows, root_for, cache)
    drift.attach(workflows, root_for, cache)

    # Which business tables the ERP writes that NO workflow (annotated or auto) covers, with the
    # functions that write them — the gap list the dashboard can show (scanner/lib/coverage.py).
    cov = coverage.report(workflows, coverage.table_writers(erp_root))

    # keyed by each repo's label (scanner/config.py labels(): the checkout's folder name)
    labels = {"primary": "primary", "secondary": "secondary", "knowledge": "knowledge", **(labels or {})}
    commits = {labels["primary"]: erp_sha}
    if linkedin_sha:
        commits[labels["secondary"]] = linkedin_sha
    if knowledge_sha:
        commits[labels["knowledge"]] = knowledge_sha
    return {
        "project": project,
        "commit": erp_sha,
        "commits": commits,
        "generated_at": generated_at,
        "workflows": workflows,
        "coverage": cov,
    }
