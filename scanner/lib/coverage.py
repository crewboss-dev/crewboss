"""Coverage: which business tables does the ERP's code write that NO workflow in the catalog covers?

Every discovery module sees one kind of surface, and annotations cover what someone chose to draw, so
a write can still sit outside every workflow. This turns that into data — the catalog's top-level
`coverage` object (docs/catalog-schema.md) — listing each uncovered table with the functions that
write it, so the dashboard can show the gap instead of the catalog quietly looking complete.

Written = a SQL string literal in any function under `app/` (sqlwrites.literal_writes; docstrings
skipped) naming a business table (sqlwrites.is_business_table: created by the ERP's migrations, not a
log/audit/read-state table). A table counts as COVERED when any workflow, annotated or auto, either
lists `db:<table>` in its side_effects or has a step whose code_ref names one of the functions that
write it. Like drift, it is a report, not a scan failure.
"""
from __future__ import annotations

import ast
from pathlib import Path

from scanner.lib import astutil as A
from scanner.lib.sqlwrites import is_business_table, literal_writes, schema_tables

SCOPE = "app"
MODULE_LEVEL = "<module>"


def _units(mod: A.Module):
    """(function name, node) for every top-level function, every class method (`Class.method`), and
    one `<module>` unit holding the module-level statements (SQL constants defined outside any def)."""
    loose = []
    for node in mod.tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node.name, node
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    yield f"{node.name}.{sub.name}", sub
        else:
            loose.append(node)
    if loose:
        yield MODULE_LEVEL, ast.Module(body=loose, type_ignores=[])


def table_writers(root) -> dict[str, list[dict]]:
    """{table: [{file, function}, ...]} for every business table written under app/ (sorted)."""
    root = Path(root)
    base = root / SCOPE
    if not base.is_dir():
        return {}
    known = schema_tables(root)
    out: dict[str, list[dict]] = {}
    for p in sorted(base.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        rel = str(p.relative_to(root))
        mod = A.parse_module(root, rel)
        if mod is None:
            continue
        for name, node in _units(mod):
            for t in literal_writes(node):
                if is_business_table(t, known):
                    out.setdefault(t, []).append({"file": rel, "function": name})
    return dict(sorted(out.items()))


def report(workflows: list[dict], writers: dict[str, list[dict]]) -> dict:
    """The catalog's `coverage` object: {scope, business_tables, covered, uncovered: [{table, writers}]}."""
    declared = set()
    pairs = set()
    for wf in workflows:
        for se in wf.get("side_effects") or []:
            if isinstance(se, str) and se.startswith("db:"):
                declared.add(se[3:])
        for st in wf.get("steps") or []:
            cr = st.get("code_ref") or {}
            if cr.get("file") and cr.get("function"):
                pairs.add((cr["file"], cr["function"]))
    uncovered = []
    for table, ws in writers.items():
        if table in declared or any((w["file"], w["function"]) in pairs for w in ws):
            continue
        uncovered.append({"table": table, "writers": ws})
    return {"scope": f"{SCOPE}/", "business_tables": len(writers), "covered": len(writers) - len(uncovered),
            "uncovered": uncovered}
