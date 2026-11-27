"""Discovers the webhook-triggered flows in app/routes/webhook.py — 9 `@router.post("/webhook/...")`
handlers at the time this was written. Auto mode records the route path, the app.clients systems it
calls, and any DB tables it writes; the real step order for each is expected from annotations.yaml.
"""
from __future__ import annotations

import ast

from scanner.lib import astutil as A

FILE = "app/routes/webhook.py"
from scanner.lib.sqlwrites import written_tables  # noqa: E402  (one pattern for every discovery module, #52)


def discover(root) -> list[dict]:
    mod = A.parse_module(root, FILE)
    if mod is None:
        return []
    out = []
    for path, fn, _kwargs in A.find_decorated(mod, "post"):
        if not path or not path.startswith("/webhook"):
            continue
        systems = set()
        for alias in A.imported_client_modules(mod):
            if A.calls_to(fn, alias):
                systems.add(alias)
        db_tables = set()
        for n in ast.walk(fn):
            s = A.const_str(n)
            if s:
                db_tables.update(written_tables(s))
        out.append({
            "kind": "webhook",
            "path": path,
            "func": fn.name,
            "file": FILE,
            "docstring": ast.get_docstring(fn) or "",
            "systems": sorted(systems),
            "db_tables": sorted(db_tables),
            "source_refs": [{"file": FILE, "lines": mod.line_range(fn)}],
        })
    # GET-registered webhooks (none currently, but the generic /webhook/{source} catch-all is POST)
    return out
