"""Shared `ast`-walking helpers used by every scanner/discover/*.py module.

Nothing here touches a network or a database — it is pure source-text analysis, which is what keeps
the whole scanner honest about the PRIVACY RULE in docs/catalog-schema.md (code only, never runtime
data). Every function takes an already-parsed module (or a function node inside one) and returns plain
data: node lists, dicts, strings pulled straight from source.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Module:
    """One parsed .py file, kept together with its path (relative to the repo root it was scanned
    under) and raw source so callers can slice line ranges for `source_refs`."""
    path: str            # relative path, e.g. "app/workers/token_refresh.py"
    tree: ast.Module
    source: str
    lines: list = field(default_factory=list)

    def line_range(self, node: ast.AST) -> str:
        end = getattr(node, "end_lineno", None) or node.lineno
        return f"{node.lineno}-{end}"


def parse_module(root: Path, rel_path: str) -> Module | None:
    """Parse one file under `root`. Returns None (never raises) on a syntax error or missing file —
    a bad file must not abort the whole scan."""
    full = root / rel_path
    try:
        source = full.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=rel_path)
    except (SyntaxError, OSError):
        return None
    return Module(path=rel_path, tree=tree, source=source, lines=source.splitlines())


def iter_py_files(root: Path, subdir: str, exclude_names=("__init__.py",)) -> list:
    """Sorted relative paths of every .py file directly inside `root/subdir` (non-recursive by
    default caller choice; pass subdir with trailing '/**' handling done by the caller if needed)."""
    base = root / subdir
    if not base.is_dir():
        return []
    out = []
    for p in sorted(base.glob("*.py")):
        if p.name in exclude_names:
            continue
        out.append(str(p.relative_to(root)))
    return out


def module_docstring(mod: Module) -> str:
    return ast.get_docstring(mod.tree) or ""


def top_level_functions(mod: Module):
    """def/async def directly under the module (not nested, not methods)."""
    return [n for n in mod.tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def all_functions(mod: Module):
    """Every function definition anywhere in the module, nested or not — used when a helper the
    public function calls is itself nested one level down (a common shape in app/clients/claude.py:
    `_triage_once` sits beside `triage_alert`, both top-level, but some repos nest helpers)."""
    return [n for n in ast.walk(mod.tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]


def module_level_assigns(mod: Module) -> dict:
    """name -> assigned value node, for simple top-level `NAME = ...` / `NAME: T = ...` statements.
    Implicit string concatenation (`"a" "b"`) is already one ast.Constant by the time we see it, so a
    multi-line prompt constant reads back as a single string with no extra work."""
    out = {}
    for node in mod.tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            out[node.targets[0].id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
            out[node.target.id] = node.value
    return out


def const_str(node: ast.AST | None) -> str | None:
    """The literal string value of a Constant node, or None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def const_number(node: ast.AST | None):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    return None


def node_source(node: ast.AST | None) -> str:
    """Best-effort source text for an expression — used for things like `model=_model()` where we
    want to show the RULE ("_model()") rather than resolve it at scan time."""
    if node is None:
        return ""
    try:
        return ast.unparse(node)
    except Exception:  # noqa: BLE001 — never let a formatting quirk break discovery
        return "<expr>"


def decorator_calls(fn) -> list:
    """[(decorator_name, [positional-string-args]), ...] for a function's decorators — matches
    `@executor("email.triage.case")` / `@router.post("/webhook/call")` / `@mcp.tool(title="...")`."""
    out = []
    for d in fn.decorator_list:
        if isinstance(d, ast.Call):
            name = _callable_name(d.func)
            args = [const_str(a) for a in d.args]
            kwargs = {kw.arg: const_str(kw.value) for kw in d.keywords if kw.arg}
            out.append((name, [a for a in args if a is not None], kwargs))
        elif isinstance(d, (ast.Name, ast.Attribute)):
            out.append((_callable_name(d), [], {}))
    return out


def _callable_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_callable_name(node.value)}.{node.attr}"
    return "<expr>"


def find_decorated(mod: Module, decorator_name: str):
    """[(first_string_arg_or_None, fn_node), ...] for every function decorated with
    `decorator_name` (matched on the LAST attribute segment, so `@router.post` matches
    decorator_name="post" too when the caller wants that — pass the exact name you need)."""
    out = []
    for fn in top_level_functions(mod):
        for name, args, kwargs in decorator_calls(fn):
            if name == decorator_name or name.endswith("." + decorator_name):
                out.append((args[0] if args else None, fn, kwargs))
    return out


def calls_matching(node: ast.AST, *, attr: str | None = None, obj_attr: str | None = None):
    """Every ast.Call under `node` whose func is `<obj>.<attr>` (obj_attr=None) or `<obj>.<obj_attr>.
    <attr>` (e.g. attr='create', obj_attr='messages' matches `client.messages.create(...)`)."""
    out = []
    for n in ast.walk(node):
        if not isinstance(n, ast.Call) or not isinstance(n.func, ast.Attribute):
            continue
        if attr is not None and n.func.attr != attr:
            continue
        if obj_attr is not None:
            base = n.func.value
            if not (isinstance(base, ast.Attribute) and base.attr == obj_attr):
                continue
        out.append(n)
    return out


def calls_to(node: ast.AST, module_alias: str, func_name: str | None = None):
    """Every call shaped `module_alias.func_name(...)` (func_name=None matches any attr on that
    alias) — used to find `xero.refresh(...)`, `claude.triage_alert(...)`, `ops.record(...)`."""
    out = []
    for n in ast.walk(node):
        if not isinstance(n, ast.Call) or not isinstance(n.func, ast.Attribute):
            continue
        base = n.func.value
        if isinstance(base, ast.Name) and base.id == module_alias:
            if func_name is None or n.func.attr == func_name:
                out.append(n)
    return out


def call_kwargs(call: ast.Call) -> dict:
    return {kw.arg: kw.value for kw in call.keywords if kw.arg}


def imported_client_modules(mod: Module) -> set:
    """Names imported `from app.clients import X[, Y...]` or `from app.clients import X as Y` (alias
    tracked as the LOCAL name used in the body) — the systems a worker/route/decision touches."""
    out = set()
    for node in ast.walk(mod.tree):
        if isinstance(node, ast.ImportFrom) and node.module and (
                node.module == "app.clients" or node.module.startswith("app.clients.")):
            if node.module == "app.clients":
                for alias in node.names:
                    out.add(alias.asname or alias.name)
            else:
                tail = node.module.split(".")[-1]
                out.add(tail)
    return out


_IDENT_STOPWORDS = {
    "self", "cur", "conn", "str", "int", "float", "bool", "dict", "list", "json", "os", "log", "logging",
    "None", "True", "False", "get", "strip", "join", "format", "isinstance", "len", "range", "text",
    "resp", "client", "out", "row", "rows", "user", "content", "prompt", "msg", "message",
}


def humanize_ident(name: str) -> str:
    return name.replace("_", " ").strip()


def describe_context(expr: ast.AST | None, extra_names: list[str] | None = None) -> list[str]:
    """Best-effort, CODE-ONLY description of what a prompt's user-content is built from: for each
    `<name>.get("key")` / `<name>["key"]` under `expr`, "<name> <key>"; for each remaining bare
    parameter Name referenced, just its (humanised) name. Never returns a literal value — only
    identifiers and string KEYS that are themselves part of the source (dict/attr access), which the
    catalog schema treats as code, not runtime content."""
    if expr is None:
        return sorted({humanize_ident(n) for n in (extra_names or [])})
    described = []
    seen = set()

    def add(s: str):
        if s and s not in seen:
            seen.add(s)
            described.append(s)

    for n in ast.walk(expr):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "get" and n.args:
            key = const_str(n.args[0])
            base = _callable_name(n.func.value) if isinstance(n.func.value, (ast.Name, ast.Attribute)) else None
            if key and base:
                add(f"{humanize_ident(base)} {humanize_ident(key)}")
        elif isinstance(n, ast.Subscript):
            key = const_str(n.slice) if isinstance(n.slice, ast.Constant) else None
            base = n.value
            if key and isinstance(base, (ast.Name, ast.Attribute)):
                add(f"{humanize_ident(_callable_name(base))} {humanize_ident(key)}")

    for n in ast.walk(expr):
        if isinstance(n, ast.Name) and n.id not in _IDENT_STOPWORDS and not n.id.startswith("_"):
            add(humanize_ident(n.id))

    for extra in (extra_names or []):
        add(humanize_ident(extra))

    return described[:10]


def find_last_assign(fn, name: str) -> ast.AST | None:
    """The value expression of the LAST top-level assignment to `name` inside `fn`'s body (walks
    straight-line + simple if/try blocks) — used to trace `user = f"...{alert['x']}..."` back from a
    `messages=[{"role": "user", "content": user}]` call that just passes the name."""
    found = None
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            if node.targets[0].id == name:
                found = node.value
    return found


def func_param_names(fn) -> list[str]:
    args = fn.args
    names = [a.arg for a in args.posonlyargs] + [a.arg for a in args.args] + [a.arg for a in args.kwonlyargs]
    return [n for n in names if n != "self"]


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower()).strip("-")
