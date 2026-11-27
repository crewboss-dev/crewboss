"""Discovers app/decisions.py: the EXECUTORS / INLINE registries (button-triggered — an owner approves
a Now card) and the `produce_*` functions that populate the decision queue in the first place
(timer/event-triggered background producers).
"""
from __future__ import annotations

import ast

from scanner.lib import astutil as A

FILE = "app/decisions.py"
from scanner.lib.sqlwrites import written_tables  # noqa: E402  (one pattern for every discovery module, #52)


def _tables_written(fn) -> list[str]:
    tables = set()
    for n in ast.walk(fn):
        s = A.const_str(n)
        if s:
            tables.update(written_tables(s))
    return sorted(tables)


def discover(root) -> list[dict]:
    mod = A.parse_module(root, FILE)
    if mod is None:
        return []
    out = []
    for dtype, fn, _kw in A.find_decorated(mod, "executor"):
        out.append({
            "kind": "executor",
            "dtype": dtype,
            "func": fn.name,
            "file": FILE,
            "docstring": ast.get_docstring(fn) or "",
            "db_tables": _tables_written(fn),
            "source_refs": [{"file": FILE, "lines": mod.line_range(fn)}],
        })
    for dtype, fn, _kw in A.find_decorated(mod, "inline_handler"):
        out.append({
            "kind": "inline",
            "dtype": dtype,
            "func": fn.name,
            "file": FILE,
            "docstring": ast.get_docstring(fn) or "",
            "db_tables": _tables_written(fn),
            "source_refs": [{"file": FILE, "lines": mod.line_range(fn)}],
        })
    for fn in A.top_level_functions(mod):
        if fn.name.startswith("produce_") or fn.name.startswith("_produce_"):
            out.append({
                "kind": "producer",
                "dtype": fn.name,
                "func": fn.name,
                "file": FILE,
                "docstring": ast.get_docstring(fn) or "",
                "db_tables": _tables_written(fn),
                "source_refs": [{"file": FILE, "lines": mod.line_range(fn)}],
            })
    return out
