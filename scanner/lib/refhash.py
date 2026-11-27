"""Content hashes for `source_refs[].hash` (docs/catalog-schema.md) and the function lookups that the
annotation drift check (scanner/lib/drift.py) shares with them.

WHAT is hashed, per source_ref, in order of preference:

1. **Function bodies (AST).** The functions the workflow names in that file — the ref's own optional
   `function` key, else every step `code_ref.function` pointing at the same file. Each function is
   reduced to `ast.dump(node, include_attributes=False)`: line numbers, comments and formatting are
   gone, so an edit ELSEWHERE in the file (or a blank line added above the function, which shifts
   every hand-written `lines` range) does not change the hash — only a change to the code of the
   functions this workflow actually runs does. A named function that no longer exists contributes a
   `missing:<name>` marker, so a deletion changes the hash too (drift.py reports it as `broken`).
   The module's own top-level helpers those functions call (transitively, by bare name) are hashed
   with them — a change to `_land()` that `main()` delegates to is a change to the workflow — but a
   function the workflow never reaches is not.
2. **The line range**, for a ref whose file isn't Python / doesn't parse / names no function: the
   `lines: "a-b"` slice with trailing whitespace stripped.
3. **The whole file**, when there is no line range either.

A ref whose file doesn't exist in the scanned tree gets NO hash (and drift.py reports it).

Hashes are `sha256:<hex>`, prefixed by an algorithm tag inside the digest input (`HASH_VERSION`) so a
future change to what's hashed is a deliberate, visible bump. NOTE: `ast.dump` output can shift across
Python minor versions — run the scanner from ONE interpreter (the repo's .venv); a Python upgrade will
change every function hash once, which the app reads as "layout may be stale" on annotated workflows.
"""
from __future__ import annotations

import ast
import hashlib
import re
from pathlib import Path

from scanner.lib import astutil as A

HASH_VERSION = "refhash-v1"
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
_DEF_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def is_function_name(name: str | None) -> bool:
    """True for a real `name` / `Class.method` reference; False for the scanner's own placeholders
    such as '(any tool in this family)'."""
    return bool(name) and bool(_IDENT_RE.match(name))


class ModuleCache:
    """Parses each (root, file) at most once per scan. `None` = missing file or not parseable."""

    def __init__(self):
        self._mods: dict = {}
        self._texts: dict = {}

    def module(self, root: Path, rel: str) -> A.Module | None:
        key = (str(root), rel)
        if key not in self._mods:
            self._mods[key] = A.parse_module(root, rel) if rel.endswith(".py") else None
        return self._mods[key]

    def text(self, root: Path, rel: str) -> str | None:
        key = (str(root), rel)
        if key not in self._texts:
            try:
                self._texts[key] = (root / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                self._texts[key] = None
        return self._texts[key]

    def exists(self, root: Path, rel: str) -> bool:
        return (root / rel).is_file()


def find_definitions(mod: A.Module, name: str) -> list:
    """Nodes defining `name` in `mod`. A bare name matches a top-level def/class first, else any
    nested def/method of that name; `Class.method` matches that method on that (any-depth) class."""
    if "." in name:
        cls_name, _, rest = name.partition(".")
        out = []
        for cls in (n for n in ast.walk(mod.tree) if isinstance(n, ast.ClassDef) and n.name == cls_name):
            fake = A.Module(path=mod.path, tree=ast.Module(body=cls.body, type_ignores=[]), source="")
            out += find_definitions(fake, rest)
        return out
    top = [n for n in mod.tree.body if isinstance(n, _DEF_TYPES) and n.name == name]
    if top:
        return top
    return [n for n in ast.walk(mod.tree) if isinstance(n, _DEF_TYPES) and n.name == name]


def _digest(parts: list[str]) -> str:
    h = hashlib.sha256()
    h.update(HASH_VERSION.encode())
    for p in parts:
        h.update(b"\x00")
        h.update(p.encode("utf-8", errors="replace"))
    return "sha256:" + h.hexdigest()


def local_callees(mod: A.Module, nodes: list) -> dict:
    """Transitive closure of the module's OWN top-level functions reached from `nodes` by a bare-name
    call (`_land(msg)`), as {name: node}. The helpers a workflow's function delegates to are part of
    what the workflow runs; functions it never calls are not."""
    tops = {n.name: n for n in mod.tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    found: dict = {}
    stack = list(nodes)
    while stack:
        node = stack.pop()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
                callee = tops.get(sub.func.id)
                if callee is not None and sub.func.id not in found:
                    found[sub.func.id] = callee
                    stack.append(callee)
    return found


def hash_functions(mod: A.Module, names: list[str]) -> str:
    parts = []
    named_nodes = []
    for name in sorted(set(names)):
        nodes = find_definitions(mod, name)
        if not nodes:
            parts.append(f"missing:{name}")
            continue
        named_nodes += nodes
        for node in nodes:
            parts.append(f"def:{name}")
            parts.append(ast.dump(node, include_attributes=False))
    named = set(names)
    for cname, node in sorted(local_callees(mod, named_nodes).items()):
        if cname in named:
            continue
        parts.append(f"callee:{cname}")
        parts.append(ast.dump(node, include_attributes=False))
    return _digest(parts)


def _parse_lines(lines: str | None) -> tuple[int, int] | None:
    if not lines:
        return None
    m = re.match(r"^\s*(\d+)\s*(?:-\s*(\d+))?\s*$", str(lines))
    if not m:
        return None
    a = int(m.group(1))
    b = int(m.group(2) or a)
    return (min(a, b), max(a, b))


def hash_text(text: str, lines: str | None) -> str:
    rows = text.splitlines()
    rng = _parse_lines(lines)
    if rng:
        rows = rows[rng[0] - 1:rng[1]]
        tag = f"lines:{rng[0]}-{rng[1]}"
    else:
        tag = "file"
    return _digest([tag] + [r.rstrip() for r in rows])


def ref_functions(wf: dict, ref: dict) -> list[str]:
    """The function names a source_ref stands for: its own `function` (str or list), else every
    step code_ref.function pointing at the same file."""
    own = ref.get("function")
    if own:
        names = own if isinstance(own, list) else [own]
    else:
        names = [(st.get("code_ref") or {}).get("function") for st in wf.get("steps") or []
                 if (st.get("code_ref") or {}).get("file") == ref.get("file")]
    return sorted({n for n in names if is_function_name(n)})


def source_ref_hash(root: Path | None, wf: dict, ref: dict, cache: ModuleCache) -> str | None:
    """The hash for one source_ref (see the module docstring), or None when the file isn't there."""
    file = ref.get("file")
    if root is None or not file or not cache.exists(root, file):
        return None
    names = ref_functions(wf, ref)
    mod = cache.module(root, file)
    if mod is not None and names:
        return hash_functions(mod, names)
    text = cache.text(root, file)
    if text is None:
        return None
    return hash_text(text, ref.get("lines"))
