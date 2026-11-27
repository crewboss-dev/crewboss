"""Discovers the plain-button business processes: every `@router.post/put/patch/delete` handler in
`app/routes/*.py` that WRITES a business table, directly or through 1-2 call hops into `app/` (the
same call-graph BFS discover/routes.py uses to find AI calls — `routes.walk_calls`).

The other discovery modules only see automation surfaces (timers, webhooks, decision types, AI call
sites, MCP tools), so a flow made of buttons — mark an opportunity lost, invoice a quote stage, raise a
PO, cancel a quote — was invisible unless someone annotated it. Here each such handler becomes one
raw route record carrying the tables it writes and the ENTITY its path addresses; build.py groups the
records into ONE auto workflow per (route file, entity) — "Sales › Quote › Actions" with one step per
route — never one workflow per route (there are ~400 of them).

Entity: the first static path segment (`/quotes/…` → Quote), plus a sub-entity when the path names
one under it — a static segment that isn't the final verb (`/quotes/{id}/line/{lid}/tick` → Quote
line, `/quotes/{id}/stage/{pid}/invoice` → Quote stage), or a final segment the same file uses as a
sub-entity elsewhere (`/cases/{id}/time` → Case time, because `/cases/{id}/time/start` exists).
ENTITY_ALIASES renames the few compositions that read badly (`po` → Purchase order).

Left out here (build.py drops the rest — routes an annotation or another discovered workflow already
claims, and the AI routes routes.py resolved):
- routes that write no business table (sqlwrites.is_business_table: created by the ERP's migrations
  and not a log/audit/read-state table);
- pure UI-state routes (UI_STATE_PATH: preferences, list filters, draft autosave, column/sort/sidebar
  state, a viewer's timezone or read marks, the login/session handshake) — matched on the path.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from scanner.discover import routes
from scanner.lib import astutil as A
from scanner.lib.sqlwrites import is_business_table, literal_writes, schema_tables

METHODS = ("post", "put", "patch", "delete")
ROUTES_DIR = "app/routes"

# Path segments that are a namespace, not an entity (`/api/…`, `/finance/supplier-invoice/…`).
NAMESPACE_SEGMENTS = {"api", "internal", "finance", "app", "v1"}

UI_STATE_PATH = re.compile(
    r"(^|/)(prefs?|preferences?|filters?|list-filters?|autosave|draft-autosave|save-draft|draft-save|"
    r"columns?|sort|sidebar|theme|collapse|collapsed|density|view-mode|timezone|read|mark-read|read-all|"
    r"app-session|login|logout)(/|$)", re.I)

# Composed entity name (lower-case, space-separated) -> display name.
ENTITY_ALIASES = {
    "po": "Purchase order",
    "quote po": "Purchase order",
    "quote contractor": "Contractor quote",
    "quote incoming": "Incoming quote",
    "case time": "Case time entry",
    "kb": "KB article",
    "calc": "Calculator request",
    "comm": "Conversation",
    "comm unfollow": "Conversation",
    "pending": "Pending match",
    "testwindow": "Test window",
    "ask": "App chat",
    "auth": "App device",
}
# Words shown in capitals wherever they appear in a composed name.
_ACRONYMS = {"kb": "KB", "cctv": "CCTV", "ai": "AI", "pdf": "PDF", "mfa": "MFA", "sla": "SLA", "po": "PO"}


def _is_param(seg: str) -> bool:
    return seg.startswith("{")


def _singular(word: str) -> str:
    w = word.lower()
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith(("sses", "xes", "ches", "shes")):
        return w[:-2]
    if w.endswith("s") and not w.endswith(("ss", "us", "is")):
        return w[:-1]
    return w


def _words(seg: str) -> str:
    return " ".join(_singular(p) for p in re.split(r"[-_]+", seg) if p)


def _segments(path: str) -> list[str]:
    segs = [s for s in path.strip("/").split("/") if s]
    while len(segs) > 1 and segs[0].lower() in NAMESPACE_SEGMENTS:
        segs = segs[1:]
    return segs


def _sub_candidate(segs: list[str]) -> str | None:
    """The sub-entity segment of a path: the first static segment after the entity that is not the
    final one (the final one is normally the verb — `/quotes/{id}/cancel`)."""
    for i, seg in enumerate(segs[1:], start=1):
        if _is_param(seg):
            continue
        return seg if i < len(segs) - 1 else None
    return None


def entity_for(path: str, known_subs: set[str], file_entities: set[str] = frozenset()) -> tuple[str, str]:
    """(entity_key, parent_key) for a route path — `('quote line', 'quote')`, `('quote', 'quote')`.
    `known_subs` are the sub-entity words the SAME file uses elsewhere, so a bare final segment that
    names one (`/cases/{id}/time`) joins it; `file_entities` are the file's top-level entities, so a
    segment naming one of those (`/locations/{id}/testwindow` in testwindows.py) IS that entity."""
    segs = _segments(path)
    if not segs or _is_param(segs[0]):
        return "", ""
    base = _words(segs[0])
    statics = [s for s in segs[1:] if not _is_param(s)]
    for seg in reversed(statics):
        if _words(seg) != base and _words(seg) in file_entities:
            return _words(seg), _words(seg)
    sub = _sub_candidate(segs)
    if sub is None and statics and _words(statics[-1]) in known_subs:
        sub = statics[-1]
    return (f"{base} {_words(sub)}" if sub else base), base


def display_name(key: str) -> str:
    if key in ENTITY_ALIASES:
        return ENTITY_ALIASES[key]
    words = [_ACRONYMS.get(w, w) for w in key.split()]
    return " ".join(words)[:1].upper() + " ".join(words)[1:]


def _route_tables(dotted: str, fn, func_index: dict, alias_maps: dict, known: set[str] | None) -> list[dict]:
    """[{table, via}] — every business table the handler (depth 0) or a function it reaches within
    routes.MAX_DEPTH writes; `via` is `module.func` for a write found in a helper, else None."""
    # The walk's visiting order isn't stable run to run (set-backed), so pick each table's `via` by rule —
    # the shallowest write wins, ties by name — and emit tables sorted: identical code, identical catalog.
    best: dict[str, tuple] = {}
    for mdotted, fname, node, depth in routes.walk_calls(dotted, fn.name, fn, func_index, alias_maps):
        for t in literal_writes(node):
            if not is_business_table(t, known):
                continue
            via = None if depth == 0 else f"{mdotted.removeprefix('app.')}.{fname}"
            rank = (depth, via or "")
            if t not in best or rank < best[t][0]:
                best[t] = (rank, via)
    return [{"table": t, "via": best[t][1]} for t in sorted(best)]


def discover(root) -> list[dict]:
    """One record per state-changing route that writes a business table:
    {kind, file, func, method, path, lines, entity, entity_name, parent_entity, docstring, tables}
    (`tables` = [{table, via}], via = `module.func` when the write is in a helper the route reaches)."""
    root = Path(root)
    base = root / ROUTES_DIR
    if not base.is_dir():
        return []
    known = schema_tables(root)
    func_index, alias_maps, _claude = routes.build_index(root)
    out = []
    for rel in A.iter_py_files(root, ROUTES_DIR):
        mod = A.parse_module(root, rel)
        if mod is None:
            continue
        dotted = routes._dotted_module(root, root / rel)
        handlers = []
        seen = set()
        for fn in A.top_level_functions(mod):
            for name, args, _kw in A.decorator_calls(fn):
                method = name.rsplit(".", 1)[-1]
                if method in METHODS and name.startswith("router") and args and fn.name not in seen:
                    seen.add(fn.name)
                    handlers.append((method, args[0], fn))
        subs = {_words(s) for _m, p, _f in handlers if (s := _sub_candidate(_segments(p)))}
        bases = {_words(segs[0]) for _m, p, _f in handlers if (segs := _segments(p)) and not _is_param(segs[0])}
        for method, path, fn in handlers:
            if UI_STATE_PATH.search(path):
                continue
            tables = _route_tables(dotted, fn, func_index, alias_maps, known)
            if not tables:
                continue
            key, parent = entity_for(path, subs, bases)
            if not key:
                key = parent = _words(Path(rel).stem)
            out.append({
                "kind": "state_route",
                "file": rel,
                "func": fn.name,
                "method": method.upper(),
                "path": path,
                "lines": mod.line_range(fn),
                "entity": key,
                "entity_name": display_name(key),
                "parent_entity": parent,
                "docstring": ast.get_docstring(fn) or "",
                "tables": tables,
            })
    return out


def label_for(func: str) -> str:
    """A route function name as a step label: `raise_stage_invoice` -> 'Raise stage invoice'."""
    words = [_ACRONYMS.get(w, w) for w in func.strip("_").split("_") if w]
    text = " ".join(words)
    return text[:1].upper() + text[1:]


def group_records(records: list[dict]) -> list[dict]:
    """Groups route records into one entry per (route file, entity name), in file then name order.
    Two entity keys aliased to one name ARE one entity (`/quotes/{id}/po/create` joins `/po/…` as
    Purchase order). A sub-entity with a single route folds into its parent entity in the same file
    (`/quotes/{id}/pdf/save` joins Quote rather than drawing a one-card 'Quote PDF' workflow); one with
    no parent there stays. -> [{file, entity, entity_name, routes: [record, ...]}]"""
    by_key: dict[tuple[str, str], dict] = {}
    for r in sorted(records, key=lambda r: (r["file"], r["entity"])):
        g = by_key.setdefault((r["file"], r["entity_name"]), {
            "file": r["file"], "entity": r["entity"], "entity_name": r["entity_name"],
            "parent": r["parent_entity"], "routes": []})
        g["routes"].append(r)
    for (file, name), g in list(by_key.items()):
        target = (file, display_name(g["parent"]))
        if len(g["routes"]) == 1 and g["entity"] != g["parent"] and target in by_key and target != (file, name):
            by_key[target]["routes"].extend(g["routes"])
            del by_key[(file, name)]
    return [{k: v for k, v in by_key[key].items() if k != "parent"} for key in sorted(by_key)]
