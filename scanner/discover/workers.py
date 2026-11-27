"""Discovers the 13 ERP workers under app/workers/*.py — each a sleep-loop container, 1:1 with a
compose.yaml service. Auto mode produces a MINIMAL skeleton (trigger + one step per system touched);
the real step order for every worker is expected to come from $CREWBOSS_HOME/scanner/annotations.yaml (the owner's
own worked example, `billing.xero-token.refresh`, is one of these files) — see scanner/README.md.
"""
from __future__ import annotations

import ast
import re

from scanner.lib import astutil as A
from scanner.lib.sqlwrites import written_tables

SUBDIR = "app/workers"

_INTERVAL_RE = re.compile(r"(INTERVAL|_SECONDS)\s*=\s*(?:float|int)\(\s*os\.environ\.get\(\s*"
                          r"[\"']([A-Z0-9_]+)[\"']\s*,\s*[\"']([0-9.]+)[\"']")
_ENABLED_RE = re.compile(r"([A-Z0-9_]*ENABLED[A-Z0-9_]*)\s*=\s*os\.environ\.get\(\s*[\"']([A-Z0-9_]+)[\"']")


def _timer_detail(mod: A.Module) -> tuple[str, str | None]:
    """('every Ns (env VAR, default Ns)', gating-env-var-or-None) from the module SOURCE (regex,
    not exec — these are always simple `os.environ.get("X", "600")` one-liners in this codebase)."""
    m = _INTERVAL_RE.search(mod.source)
    detail = f"every {m.group(3)}s (env {m.group(2)})" if m else "periodic loop"
    gate = None
    em = _ENABLED_RE.search(mod.source)
    if em:
        gate = em.group(2)
    return detail, gate


def discover(root) -> list[dict]:
    out = []
    for rel in A.iter_py_files(root, SUBDIR):
        mod = A.parse_module(root, rel)
        if mod is None:
            continue
        name = rel.split("/")[-1][:-3]
        detail, gate = _timer_detail(mod)
        systems = sorted(A.imported_client_modules(mod))
        fn_names = {fn.name for fn in A.top_level_functions(mod)}
        entry_fn = "run_once" if "run_once" in fn_names else ("main" if "main" in fn_names else None)
        entry_node = None
        for fn in A.top_level_functions(mod):
            if fn.name == entry_fn:
                entry_node = fn
        db_tables = set()
        if entry_node is not None:
            for n in ast.walk(entry_node):
                s = A.const_str(n) if isinstance(n, ast.Constant) else None
                if s:
                    db_tables.update(written_tables(s))
        out.append({
            "kind": "worker",
            "name": name,
            "file": rel,
            "docstring": A.module_docstring(mod),
            "trigger_detail": detail,
            "gate_env": gate,
            "systems": systems,
            "db_tables": sorted(db_tables),
            "source_refs": [{"file": rel, "lines": "1-" + str(len(mod.lines))}],
            "entry_function": entry_fn,
        })
    return out
