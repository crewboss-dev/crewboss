"""Annotation drift: does every hand-written reference in an ANNOTATED workflow still point at real
code? $CREWBOSS_HOME/scanner/annotations.yaml is written against a snapshot of the repos; when a function it names is
renamed, moved or deleted the annotation silently describes code that no longer exists. This check
turns that into data — a `problems` list on the workflow (docs/catalog-schema.md), which the app shows
as `broken` and raises a Tweak task for — so a layout can't quietly go stale.

Checked, for each annotated workflow:
- every `source_refs[].file` exists (and its optional `function` still exists in it);
- every step `code_ref.file` exists, and its `code_ref.function` still exists in that file.

Non-Python files are checked for existence only. A file under a repo that wasn't scanned this run (the
LinkedIn checkout missing) is skipped rather than reported — "couldn't look" is not "gone".
Auto workflows are derived FROM the code, so they can't drift and are never checked.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from scanner.lib.refhash import ModuleCache, find_definitions, is_function_name

MISSING_FILE = "missing_file"
MISSING_FUNCTION = "missing_function"


def _problem(code: str, file: str, function: str | None, where: str, step: str | None) -> dict:
    what = f"{function}() in {file}" if function else file
    p = {"code": code, "where": where, "file": file,
         "message": f"{where}: {what} no longer exists"}
    if function:
        p["function"] = function
    if step:
        p["step"] = step
    return p


def _check_ref(root: Path | None, file: str | None, functions: list[str], where: str, step: str | None,
               cache: ModuleCache, out: list[dict]) -> None:
    if not file or root is None:
        return
    if not cache.exists(root, file):
        out.append(_problem(MISSING_FILE, file, None, where, step))
        return
    if not file.endswith(".py"):
        return
    mod = cache.module(root, file)
    if mod is None:   # a syntax error on origin/main is not annotation drift; don't guess
        return
    for fn in functions:
        if is_function_name(fn) and not find_definitions(mod, fn):
            out.append(_problem(MISSING_FUNCTION, file, fn, where, step))


def annotation_problems(wf: dict, root: Path | None, cache: ModuleCache) -> list[dict]:
    """Problems for ONE annotated workflow (empty list = every reference resolves)."""
    out: list[dict] = []
    seen = set()
    for i, ref in enumerate(wf.get("source_refs") or []):
        own = ref.get("function")
        fns = (own if isinstance(own, list) else [own]) if own else []
        _check_ref(root, ref.get("file"), fns, f"source_refs[{i}]", None, cache, out)
    for st in wf.get("steps") or []:
        cr = st.get("code_ref") or {}
        _check_ref(root, cr.get("file"), [cr.get("function")] if cr.get("function") else [],
                   f"steps[{st.get('id')}].code_ref", st.get("id"), cache, out)
    deduped = []
    for p in out:
        key = (p["code"], p["where"], p["file"], p.get("function"))
        if key not in seen:
            seen.add(key)
            deduped.append(p)
    return deduped


def attach(workflows: list[dict], root_for: Callable[[dict], Path | None], cache: ModuleCache) -> None:
    """Sets `problems` on every annotated workflow that has any; leaves the key off clean ones (and
    off every auto workflow), so a clean catalog is byte-identical to one from before this check."""
    for wf in workflows:
        if wf.get("confidence") != "annotated":
            continue
        problems = annotation_problems(wf, root_for(wf), cache)
        if problems:
            wf["problems"] = problems
        else:
            wf.pop("problems", None)
