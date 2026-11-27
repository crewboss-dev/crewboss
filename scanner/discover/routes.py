"""Resolves the REAL trigger for a `claude.py` AI function that no worker/webhook/decision reaches:
which `@router.get/post` handler in `app/routes/*.py` calls it, directly or through 1-2 levels of
same-file or cross-module calls (`route -> app.kb.draft() -> claude.draft_kb_article()` is 2 hops).

This is a small generic call-graph BFS over the WHOLE `app/` tree (not just `app/routes/`), because
several AI functions are called from a shared helper module (`app/kb.py`, `app/persona.py`,
`app/scheduling_approval.py`, `app/convodocs.py`, `app/worksummary.py`) that a route calls into — the
route itself never mentions `claude` by name. Depth is capped at 2 edges from the route handler, per
the brief ("following calls 1-2 levels").
"""
from __future__ import annotations

import ast
from pathlib import Path

from scanner.lib import astutil as A

MAX_DEPTH = 2


def _dotted_module(root: Path, path: Path) -> str:
    rel = path.relative_to(root).with_suffix("")
    return ".".join(rel.parts)


def _all_app_files(root: Path) -> list[Path]:
    base = root / "app"
    if not base.is_dir():
        return []
    return [p for p in base.rglob("*.py") if "__pycache__" not in p.parts]


def build_index(root: Path):
    """-> (func_index, alias_maps, claude_aliases)
    func_index[dotted_module] = {func_name: FunctionDef}
    alias_maps[dotted_module] = {local_alias: dotted_module_it_points_at}
    claude_aliases[dotted_module] = local name bound to app.clients.claude in that file, or None
    """
    func_index: dict[str, dict] = {}
    alias_maps: dict[str, dict] = {}
    claude_aliases: dict[str, str | None] = {}
    for p in _all_app_files(root):
        rel = str(p.relative_to(root))
        mod = A.parse_module(root, rel)
        if mod is None:
            continue
        dotted = _dotted_module(root, p)
        func_index[dotted] = {fn.name: fn for fn in A.top_level_functions(mod)}
        aliases: dict[str, str] = {}
        claude_alias = None
        for node in ast.walk(mod.tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app"):
                for a in node.names:
                    local = a.asname or a.name
                    target = f"{node.module}.{a.name}"
                    aliases[local] = target
                    if target == "app.clients.claude":
                        claude_alias = local
        alias_maps[dotted] = aliases
        claude_aliases[dotted] = claude_alias
    return func_index, alias_maps, claude_aliases


def _direct_claude_calls(fn, claude_alias: str | None) -> set[str]:
    if not claude_alias:
        return set()
    return {c.func.attr for c in ast.walk(fn) if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
            and isinstance(c.func.value, ast.Name) and c.func.value.id == claude_alias}


def _local_calls(fn, local_names: set[str]) -> set[str]:
    """Same-module functions this one reaches — a direct call (`f(...)`) OR a bare reference used as a
    callback (`background.add_task(f, ...)`, `executor.submit(f)`) — both mean "this eventually runs"."""
    return {n.id for n in ast.walk(fn) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
            and n.id in local_names}


def _cross_module_calls(fn, alias_map: dict, func_index: dict) -> list[tuple[str, str]]:
    """Same idea across modules: `kb.produce_kb_drafts(...)` or a bare `kb.produce_kb_drafts` reference
    passed to a scheduler/background-task runner."""
    out = []
    for n in ast.walk(fn):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and isinstance(n.ctx, ast.Load):
            dotted = alias_map.get(n.value.id)
            if dotted and dotted in func_index and n.attr in func_index[dotted]:
                out.append((dotted, n.attr))
    return out


def walk_calls(dotted: str, fname: str, fn, func_index: dict, alias_maps: dict, max_depth: int = MAX_DEPTH):
    """BFS from one function over the `app/` call graph: yields (dotted_module, func_name, node, depth)
    for the start function (depth 0) and every same-module or cross-module function it reaches —
    called or passed as a bare callback reference — within `max_depth` edges. Each function is yielded
    once, at its shortest depth. Shared by find_routes_for (AI functions) and discover/state_routes.py
    (table writes)."""
    visited = {(dotted, fname)}
    frontier = [(dotted, fname, fn, 0)]
    while frontier:
        mdotted, name, node, depth = frontier.pop(0)
        yield mdotted, name, node, depth
        if depth >= max_depth:
            continue
        local_names = set(func_index.get(mdotted, {}).keys())
        for lname in _local_calls(node, local_names) - {name}:
            key = (mdotted, lname)
            if key not in visited:
                visited.add(key)
                frontier.append((mdotted, lname, func_index[mdotted][lname], depth + 1))
        for (xdotted, xfunc) in _cross_module_calls(node, alias_maps.get(mdotted, {}), func_index):
            key = (xdotted, xfunc)
            if key not in visited:
                visited.add(key)
                frontier.append((xdotted, xfunc, func_index[xdotted][xfunc], depth + 1))


def find_routes_for(target_funcs: set[str], root: Path) -> dict[str, dict]:
    """target_funcs: claude.py function names to resolve. Returns {func_name: {method, path, route_func,
    route_file, hops: [(module, func), ...]}} for every one a route reaches within MAX_DEPTH — the
    FIRST (shortest-path, then file-order) route found wins if more than one calls the same function."""
    func_index, alias_maps, claude_aliases = build_index(root)
    routes_dir = root / "app" / "routes"
    resolved: dict[str, dict] = {}
    if not routes_dir.is_dir():
        return resolved

    for rel in sorted(str(p.relative_to(root)) for p in routes_dir.glob("*.py")):
        mod = A.parse_module(root, rel)
        if mod is None:
            continue
        dotted = _dotted_module(root, root / rel)
        for method in ("get", "post"):
            for path, fn, _kw in A.find_decorated(mod, method):
                if not path:
                    continue
                # BFS from this one route handler.
                for mdotted, fname, node, _depth in walk_calls(dotted, fn.name, fn, func_index, alias_maps):
                    calias = claude_aliases.get(mdotted)
                    for name in _direct_claude_calls(node, calias):
                        if name in target_funcs and name not in resolved:
                            resolved[name] = {
                                "method": method.upper(), "path": path,
                                "route_func": fn.name, "route_file": rel,
                                "hop_module": mdotted, "hop_func": fname,
                            }
    return resolved
