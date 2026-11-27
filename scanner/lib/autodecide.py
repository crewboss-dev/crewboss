"""Auto-derived decisions for AUTO workflows (docs/catalog-schema.md "Auto-derived decisions").

An auto workflow is a flat list of side-effect steps the scanner already detects in ONE function —
`write-<table>` (an INSERT/UPDATE/DELETE string literal), `call-<system>` (a call on an imported
app.clients alias), `ai` (a call on the `claude` alias). This module looks inside that function for an
`if`/`elif`/`else` or `match` whose branches reach DIFFERENT such steps, and turns it into a decision
step with one labelled edge per branch — the label is the condition's own source text, shortened.
Everything it emits comes from source text (the privacy rule holds: no runtime values).

Rules that keep it honest and readable:
  * a branch counts only if it reaches at least one side-effect step; a decision needs >= 2 such
    branches whose step sets differ — a guard clause (`if not x: return`) never becomes a card;
  * a step that turns up in more than one place (both branches write `cases`, or the write also
    happens outside the `if`) cannot sit inside one branch, so it is hoisted out to just after the
    decision (where the paths rejoin); a decision left with < 2 qualifying branches after that is
    dissolved back into a straight run;
  * at most MAX_DEPTH levels of nesting (default 2);
  * loops/with/try bodies are walked in place (no loop cards are invented — that is the annotation's
    job); a call to a same-module helper (or a def nested in the function) reaches what that helper
    reaches directly, one level deep, at the call site.

`apply()` rewrites the workflow in place (adds the decision steps, `parent`/`branch` on the steps
inside a branch, and the full explicit `edges` the schema requires once a decision exists) and returns
how many decisions it added; 0 means the workflow is left exactly as it was.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field

MAX_DEPTH = 2
LABEL_MAX = 40
from scanner.lib.sqlwrites import written_tables  # noqa: E402  (one pattern for every discovery module, #52)
_WS_RE = re.compile(r"\s+")


@dataclass
class Effect:
    step_id: str
    pos: tuple


@dataclass
class Decision:
    pos: tuple
    lines: str
    kind: str                                   # "if" | "match"
    head: str                                   # shortened first condition / match subject
    branches: list = field(default_factory=list)   # [(label, [items])]


def _short(text: str) -> str:
    text = _WS_RE.sub(" ", text).strip()
    return text if len(text) <= LABEL_MAX else text[: LABEL_MAX - 1].rstrip() + "…"


def _src(node) -> str:
    try:
        return ast.unparse(node)
    except Exception:  # noqa: BLE001 — never let a formatting quirk break discovery
        return "<expr>"


class _Detector:
    """Finds, in source order, which of the workflow's own side-effect steps a subtree reaches.

    A call to a helper defined in the same module (or nested in the function itself) counts as
    reaching whatever that helper reaches DIRECTLY — one level only, placed at the call site. That is
    the same grain the step list itself was built at (a worker's `call-xero` step exists because the
    module imports xero, whichever helper actually makes the call)."""

    def __init__(self, step_ids: set, claude_alias: str = "claude", helpers: dict | None = None):
        self.step_ids = step_ids
        self.claude_alias = claude_alias
        self.helpers = helpers or {}
        self._helper_cache: dict = {}
        self._skip_defs: set = set()

    def _direct(self, node, skip_nested_defs: bool) -> list:
        found = []
        stack = [node]
        while stack:
            n = stack.pop()
            if skip_nested_defs and n is not node and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue            # a nested def's body runs where it is CALLED, not where it is defined
            stack.extend(ast.iter_child_nodes(n))
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                for t in written_tables(n.value):
                    cand = f"write-{t.lower()}"
                    if cand in self.step_ids:
                        found.append(Effect(cand, (n.lineno, n.col_offset)))
                continue
            if not isinstance(n, ast.Call):
                continue
            sid = None
            if isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name):
                alias = n.func.value.id
                if alias == self.claude_alias and "ai" in self.step_ids:
                    sid = "ai"
                elif f"call-{alias}" in self.step_ids:
                    sid = f"call-{alias}"
            if sid:
                found.append(Effect(sid, (n.lineno, n.col_offset)))
        return found

    def effects(self, node) -> list:
        found = self._direct(node, skip_nested_defs=True)
        for n in ast.walk(node):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) and n is not node:
                continue
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in self.helpers:
                name = n.func.id
                if name not in self._helper_cache:
                    self._helper_cache[name] = {e.step_id for e in self._direct(self.helpers[name], False)}
                for sid in sorted(self._helper_cache[name]):
                    found.append(Effect(sid, (n.lineno, n.col_offset)))
        found.sort(key=lambda e: e.pos)
        return found


def _if_branches(node: ast.If) -> list:
    """[(label, body_stmts)] for an if/elif/.../else chain."""
    out = [(f"if {_short(_src(node.test))}", node.body)]
    cur = node
    while len(cur.orelse) == 1 and isinstance(cur.orelse[0], ast.If):
        cur = cur.orelse[0]
        out.append((f"elif {_short(_src(cur.test))}", cur.body))
    if cur.orelse:
        out.append(("else", cur.orelse))
    return out


def _match_branches(node) -> list:
    out = []
    for case in node.cases:
        label = f"case {_src(case.pattern)}"
        if case.guard is not None:
            label += f" if {_src(case.guard)}"
        out.append((_short(label), case.body))
    return out


def _flat_ids(items) -> list:
    ids = []
    for it in items:
        if isinstance(it, Effect):
            ids.append(it.step_id)
        else:
            for _label, sub in it.branches:
                ids.extend(_flat_ids(sub))
    return ids


def _qualifies(dec: Decision) -> bool:
    sets = [frozenset(_flat_ids(sub)) for _label, sub in dec.branches]
    sets = [s for s in sets if s]
    return len(sets) >= 2 and len(set(sets)) >= 2


def _scan(stmts, det: _Detector, depth: int) -> list:
    items: list = []
    for st in stmts:
        if isinstance(st, ast.If) or (hasattr(ast, "Match") and isinstance(st, ast.Match)):
            is_if = isinstance(st, ast.If)
            head_node = st.test if is_if else st.subject
            items.extend(det.effects(head_node))          # side effects inside the condition itself
            branches = _if_branches(st) if is_if else _match_branches(st)
            if depth < MAX_DEPTH:
                dec = Decision(pos=(st.lineno, st.col_offset), lines=f"{st.lineno}-{st.end_lineno or st.lineno}",
                               kind="if" if is_if else "match",
                               head=_short(_src(head_node)),
                               branches=[(label, _scan(body, det, depth + 1)) for label, body in branches])
                if _qualifies(dec):
                    items.append(dec)
                    continue
                for _label, sub in dec.branches:             # not a real choice: flatten in order
                    items.extend(sub)
            else:
                for _label, body in branches:
                    items.extend(_scan(body, det, depth))
            continue
        if isinstance(st, (ast.For, ast.AsyncFor, ast.While)):
            items.extend(det.effects(st.iter if not isinstance(st, ast.While) else st.test))
            items.extend(_scan(st.body, det, depth))
            items.extend(_scan(st.orelse, det, depth))
            continue
        if isinstance(st, (ast.With, ast.AsyncWith)):
            for w in st.items:
                items.extend(det.effects(w.context_expr))
            items.extend(_scan(st.body, det, depth))
            continue
        if isinstance(st, ast.Try) or (hasattr(ast, "TryStar") and isinstance(st, ast.TryStar)):
            items.extend(_scan(st.body, det, depth))
            for h in st.handlers:
                items.extend(_scan(h.body, det, depth))
            items.extend(_scan(st.orelse, det, depth))
            items.extend(_scan(st.finalbody, det, depth))
            continue
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue                                   # runs where it is called (see _Detector)
        items.extend(det.effects(st))
    return items


def _normalise(items: list) -> list:
    """Hoist any step that occurs in more than one place out of the branches (to just AFTER the
    top-level decision that holds its first occurrence — the paths rejoin and then it happens), drop
    repeats, and dissolve decisions that no longer choose between different steps. Repeats until
    stable."""
    for _ in range(10):
        counts: dict = {}
        for sid in _flat_ids(items):
            counts[sid] = counts.get(sid, 0) + 1
        multi = {sid for sid, n in counts.items() if n > 1}
        changed = False

        def strip_branches(seq):
            """Remove every `multi` step from inside decisions (top-level runs are left alone)."""
            out = []
            for it in seq:
                if isinstance(it, Decision):
                    it.branches = [(label, strip_inside(sub)) for label, sub in it.branches]
                out.append(it)
            return out

        def strip_inside(seq):
            out = []
            for it in seq:
                if isinstance(it, Effect):
                    if it.step_id not in multi:
                        out.append(it)
                else:
                    it.branches = [(label, strip_inside(sub)) for label, sub in it.branches]
                    out.append(it)
            return out

        if multi:
            new_top = []
            placed = set()
            for it in items:
                new_top.append(it)
                if isinstance(it, Decision):
                    inside = [sid for sid in _flat_ids([it]) if sid in multi and sid not in placed]
                    for sid in dict.fromkeys(inside):          # first-seen order, once each
                        new_top.append(Effect(sid, it.pos))
                        placed.add(sid)
            items = _dedupe_all(strip_branches(new_top))
            changed = True

        def dissolve(seq):
            nonlocal changed
            out = []
            for it in seq:
                if isinstance(it, Decision):
                    it.branches = [(label, dissolve(sub)) for label, sub in it.branches]
                    if not _qualifies(it):
                        changed = True
                        for _label, sub in it.branches:
                            out.extend(sub)
                        continue
                out.append(it)
            return out

        items = _dedupe_all(dissolve(items))
        if not changed:
            break
    return items


def _count_decisions(items) -> int:
    n = 0
    for it in items:
        if isinstance(it, Decision):
            n += 1
            for _label, sub in it.branches:
                n += _count_decisions(sub)
    return n


def derive(fn, step_ids: set, module_functions: dict | None = None) -> list:
    """The normalised item tree for `fn` (Effects and Decisions, in source order). `module_functions`
    = {name: def} of the module's top-level functions, followed one level (see _Detector)."""
    helpers = {k: v for k, v in (module_functions or {}).items() if v is not fn}
    for n in ast.walk(fn):                       # defs nested inside fn, called by name
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not fn:
            helpers[n.name] = n
    det = _Detector(step_ids, helpers=helpers)
    items = _scan(fn.body, det, 0)
    return _normalise(_dedupe_all(items))


def _dedupe_all(items: list) -> list:
    """Within any ONE straight run (a branch or the top level), a step called twice in a row is one
    card. Repeats across different places are handled by _normalise's hoist."""
    out, seen = [], set()
    for it in items:
        if isinstance(it, Effect):
            if it.step_id in seen:
                continue
            seen.add(it.step_id)
            out.append(it)
        else:
            it.branches = [(label, _dedupe_all(sub)) for label, sub in it.branches]
            out.append(it)
    return out


def apply(wf: dict, fn, *, file: str, func_name: str, module_functions: dict | None = None) -> int:
    """Rewrite auto workflow `wf` in place with the decisions found in `fn`. Returns the count added."""
    steps = wf.get("steps") or []
    by_id = {s["id"]: s for s in steps}
    effect_ids = {s["id"] for s in steps if s.get("kind") in ("action", "ai") and s["id"] != "trigger"}
    items = derive(fn, effect_ids, module_functions)
    n_dec = _count_decisions(items)
    if not n_dec:
        return 0

    placed = set(_flat_ids(items))
    trigger = next((s for s in steps if s.get("kind") == "trigger"), None)
    tail = [s for s in steps if s.get("kind") == "logic" and s["id"] == "heartbeat"]
    unlocated = [Effect(s["id"], (0, 0)) for s in steps
                 if s is not trigger and s not in tail and s["id"] not in placed]
    top = unlocated + items + [Effect(s["id"], (10**9, 0)) for s in tail]

    new_steps = [trigger] if trigger else []
    edges: list = []
    counter = [0]

    def entry(it):
        return it.step_id if isinstance(it, Effect) else it._sid

    def emit(seq, parent, branch):
        for it in seq:
            if isinstance(it, Effect):
                st = dict(by_id[it.step_id])
                st.pop("parent", None)
                st.pop("branch", None)
                if parent:
                    st["parent"] = parent
                    if branch is not None:
                        st["branch"] = branch
                new_steps.append(st)
            else:
                counter[0] += 1
                it._sid = f"decide-{counter[0]}"
                labels, used = [], set()
                for label, _sub in it.branches:
                    base, k = label, 2
                    while label in used:
                        label = f"{base} ({k})"
                        k += 1
                    used.add(label)
                    labels.append(label)
                it.branches = [(labels[i], sub) for i, (_l, sub) in enumerate(it.branches)]
                what = "if/elif/else" if it.kind == "if" else "match"
                st = {"id": it._sid, "kind": "decision",
                      "label": f"{'Match' if it.kind == 'match' else 'Condition'} (line {it.pos[0]})",
                      "question": f"{'match ' if it.kind == 'match' else ''}{it.head}?",
                      "detail": f"auto-derived from the {what} at {file}:{it.pos[0]} — branch labels are "
                                f"the condition source, not reviewed by a person",
                      "system": "internal",
                      "code_ref": {"file": file, "lines": it.lines, "function": func_name}}
                if parent:
                    st["parent"] = parent
                    if branch is not None:
                        st["branch"] = branch
                new_steps.append(st)
                for label, sub in it.branches:
                    emit(sub, it._sid, label)

    emit(top, None, None)

    def link(seq, nxt):
        for i, it in enumerate(seq):
            after = entry(seq[i + 1]) if i + 1 < len(seq) else nxt
            if isinstance(it, Effect):
                if after:
                    edges.append({"from": it.step_id, "to": after})
                continue
            empty = []
            for label, sub in it.branches:
                if sub:
                    edges.append({"from": it._sid, "to": entry(sub[0]), "label": label})
                    link(sub, after)
                else:
                    empty.append(label)
            if empty and after:
                edges.append({"from": it._sid, "to": after, "label": " / ".join(empty)})

    if trigger and top:
        edges.append({"from": trigger["id"], "to": entry(top[0])})
    link(top, None)
    wf["steps"] = new_steps
    wf["edges"] = edges
    return n_dec
