"""The logic map (task #715, change #67): an app's workflows and AI processes, kept in its own repo as `.crewboss/logic/*.yaml`.

This module is PURE (no database, no web): it reads a checkout directory and answers four questions.

    load(root)             parse + validate every file -> workflows (normalised) and clear errors
    discover(root)         entry points found in the code (state-changing routes, timers/loops, webhooks, queue consumers, AI SDK
                           calls) for Python, JS/TS and Go
    check(root)            the two gate inputs: STALE (a code_ref points at code that is gone) and UNCOVERED (an entry point no
                           workflow names), plus schema errors; build_report() bundles them with the map
    diff(old, new)         what changed in the logic between two maps (workflows and steps added / removed / changed)

The format is documented in docs/logic-map.md. `python -m app.cli logic ...` and the dashboard's gate (app/logic.py) both call this."""
from __future__ import annotations

import ast
import bisect
import fnmatch
import os
import re
import stat
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import yaml

LOGIC_DIR = ".crewboss/logic"
REPORT_VERSION = 1
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$")
STEP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,60}$")
TRIGGER_TYPES = ("button", "route", "timer", "webhook", "queue", "event", "cli", "manual")
STEP_KINDS = ("action", "decision", "ai", "data", "call", "loop", "trigger")
MAX_FILE_BYTES = 512 * 1024
MAX_WORKFLOWS = 2000
MAX_STEPS = 200
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "env", "__pycache__", "tests", "test", "__tests__", "migrations", "dist", "build",
             "vendor", "static", ".claude", "worktrees", "site-packages", ".tox", ".next", "coverage", "fixtures"}
SCAN_FILE_CAP = 6000
STALE_READ_BYTES = 2 * 1024 * 1024
STALE_MAX_FILES = 3000


class LogicError(ValueError):
    """A map file that cannot be read; the message names the file and the place."""


# --- loading and validation --------------------------------------------------------------------------------------------------

def _s(v, limit: int = 600) -> str:
    if isinstance(v, bool):          # YAML 1.1 reads an unquoted yes / no as a boolean
        return "yes" if v else "no"
    return " ".join(str(v).split())[:limit] if isinstance(v, (str, int, float)) else ""


def _str_list(v, where: str, errs: list[str], limit: int = 40) -> list[str]:
    if v is None:
        return []
    if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
        errs.append(f"{where}: must be a list of non-empty text values")
        return []
    return [_s(x, 200) for x in v][:limit]


def _code_ref(v, where: str, errs: list[str]) -> dict | None:
    if v is None:
        return None
    if not isinstance(v, dict) or not isinstance(v.get("file"), str) or not v["file"].strip():
        errs.append(f"{where}: code_ref needs a 'file' (path inside the repo), optionally a 'function'")
        return None
    f = v["file"].strip()
    while f.startswith("./"):
        f = f[2:]
    if f.startswith("/") or ".." in Path(f).parts:
        errs.append(f"{where}: code_ref file {v['file']!r} must be a path inside the repo")
        return None
    out = {"file": f}
    if v.get("function") is not None:
        out["function"] = _s(v["function"], 200)
    if v.get("route") is not None:
        out["route"] = _s(v["route"], 300)
    return out


def _step(raw, where: str, errs: list[str]) -> dict | None:
    if not isinstance(raw, dict):
        errs.append(f"{where}: a step is a mapping with id, kind and label")
        return None
    sid, kind, label = raw.get("id"), raw.get("kind", "action"), raw.get("label")
    if not isinstance(sid, str) or not STEP_ID_RE.match(sid):
        errs.append(f"{where}: step needs an 'id' (letters, digits, . _ -)")
        return None
    where = f"{where} (step {sid})"
    if kind not in STEP_KINDS:
        errs.append(f"{where}: kind {kind!r} is not one of {', '.join(STEP_KINDS)}")
        return None
    if not isinstance(label, str) or not label.strip():
        errs.append(f"{where}: step needs a plain 'label'")
        return None
    out = {"id": sid, "kind": kind, "label": _s(label, 200), "detail": _s(raw.get("detail", ""), 600)}
    if kind == "decision":
        if not isinstance(raw.get("question"), str) or not raw["question"].strip():
            errs.append(f"{where}: a decision needs a 'question' (the condition, in plain words)")
        out["question"] = _s(raw.get("question", ""), 300)
    ref = _code_ref(raw.get("code_ref"), f"{where}.code_ref", errs)
    if ref:
        out["code_ref"] = ref
    out["data"] = _str_list(raw.get("data"), f"{where}.data", errs)
    out["effects"] = _str_list(raw.get("effects"), f"{where}.effects", errs)
    if kind == "ai":
        ai = raw.get("ai")
        if not isinstance(ai, dict) or not (ai.get("skill") or ai.get("prompt")):
            errs.append(f"{where}: an ai step needs an 'ai' mapping naming its 'skill' or 'prompt' (plus model and what it may do)")
            ai = {}
        out["ai"] = {"model": _s(ai.get("model", ""), 120), "skill": _s(ai.get("skill", ""), 160), "prompt": _s(ai.get("prompt", ""), 600),
                     "may": _str_list(ai.get("may"), f"{where}.ai.may", errs)}
    return out


def _workflow(raw, where: str, errs: list[str]) -> dict | None:
    if not isinstance(raw, dict):
        errs.append(f"{where}: a workflow is a mapping")
        return None
    wid = raw.get("id")
    if not isinstance(wid, str) or not ID_RE.match(wid):
        errs.append(f"{where}: workflow needs an 'id' (lower-case letters, digits, . _ -)")
        return None
    where = f"workflow {wid!r} ({where})"
    for key in ("name", "summary"):
        if not isinstance(raw.get(key), str) or not raw[key].strip():
            errs.append(f"{where}: needs a '{key}'")
    trig = raw.get("trigger")
    if not isinstance(trig, dict) or trig.get("type") not in TRIGGER_TYPES:
        errs.append(f"{where}: trigger needs a 'type' of {', '.join(TRIGGER_TYPES)} and a 'detail'")
        trig = {}
    trigger = {"type": trig.get("type", ""), "detail": _s(trig.get("detail", ""), 300), "control": _s(trig.get("control", ""), 200),
               "route": _s(trig.get("route", ""), 300)}
    tref = _code_ref(trig.get("code_ref"), f"{where}.trigger.code_ref", errs)
    if tref:
        trigger["code_ref"] = tref
    steps_raw = raw.get("steps")
    steps: list[dict] = []
    if not isinstance(steps_raw, list) or not steps_raw:
        errs.append(f"{where}: needs a non-empty 'steps' list")
    else:
        if len(steps_raw) > MAX_STEPS:
            errs.append(f"{where}: more than {MAX_STEPS} steps; split the workflow")
        seen: set[str] = set()
        for i, s in enumerate(steps_raw[:MAX_STEPS]):
            st = _step(s, f"{where}.steps[{i}]", errs)
            if st:
                if st["id"] in seen:
                    errs.append(f"{where}: step id {st['id']!r} is used twice")
                seen.add(st["id"])
                steps.append(st)
    edges: list[dict] = []
    ids = {s["id"] for s in steps}
    for i, e in enumerate(raw.get("edges") or []):
        if not isinstance(e, dict) or e.get("from") not in ids | {"trigger"} or e.get("to") not in ids:
            errs.append(f"{where}.edges[{i}]: 'from' and 'to' must name steps of this workflow (or 'trigger' as the start)")
            continue
        edges.append({"from": e["from"], "to": e["to"], "label": _s(e.get("label", ""), 60)})
    covers = []
    for i, c in enumerate(raw.get("covers") or []):
        ref = _code_ref(c, f"{where}.covers[{i}]", errs)
        if ref:
            covers.append(ref)
    return {"id": wid, "name": _s(raw.get("name", wid), 200), "summary": _s(raw.get("summary", ""), 800), "trigger": trigger,
            "steps": steps, "edges": edges, "data": _str_list(raw.get("data"), f"{where}.data", errs), "covers": covers}


def load(root: str | Path) -> dict:
    """Read every `.crewboss/logic/*.yaml|yml` under `root`. A file holds one workflow (a mapping with an id) or a `workflows:` list,
    and may add an `ignore:` list ({file: <glob>, reason}) for entry points that are deliberately not mapped.
    Returns {"workflows": [...], "ignore": [...], "errors": [text, ...], "files": [names]}; never raises for a bad file."""
    root = Path(root)
    d = root / LOGIC_DIR
    out = {"workflows": [], "ignore": [], "errors": [], "files": []}
    if d.is_symlink() or not d.is_dir():
        return out
    seen: dict[str, str] = {}
    for p in sorted(x for x in list(d.glob("*.yaml")) + list(d.glob("*.yml")) if _regular(x)):
        rel = f"{LOGIC_DIR}/{p.name}"
        out["files"].append(p.name)
        errs: list[str] = out["errors"]
        try:
            if os.lstat(p).st_size > MAX_FILE_BYTES:
                errs.append(f"{rel}: file is larger than {MAX_FILE_BYTES // 1024} KB; split it")
                continue
            doc = yaml.safe_load(p.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            at = f" at line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""
            problem = getattr(exc, "problem", None) or "the file is not valid YAML"
            errs.append(f"{rel}: not valid YAML{at}: {problem}")
            continue
        except (OSError, UnicodeDecodeError) as exc:
            errs.append(f"{rel}: cannot be read ({exc.__class__.__name__})")
            continue
        if doc is None:
            continue
        if not isinstance(doc, dict):
            errs.append(f"{rel}: must be a mapping (one workflow, or `workflows:` list)")
            continue
        items = doc.get("workflows") if "workflows" in doc else ([doc] if "id" in doc else [])
        if "workflows" not in doc and "id" not in doc and "ignore" not in doc:
            errs.append(f"{rel}: expected a workflow (with an 'id') or a 'workflows:' list")
        if not isinstance(items, list):
            errs.append(f"{rel}: 'workflows' must be a list")
            items = []
        for i, raw in enumerate(items):
            wf = _workflow(raw, f"{rel} workflows[{i}]", errs)
            if wf:
                if wf["id"] in seen:
                    errs.append(f"{rel}: workflow id {wf['id']!r} is already defined in {seen[wf['id']]}")
                    continue
                seen[wf["id"]] = p.name
                out["workflows"].append(wf)
        for i, ig in enumerate(doc.get("ignore") or []):
            if not isinstance(ig, dict) or not isinstance(ig.get("file"), str) or not _s(ig.get("reason", "")):
                errs.append(f"{rel}: ignore[{i}] needs a 'file' (path or glob) and a 'reason'")
                continue
            out["ignore"].append({"file": ig["file"].strip().removeprefix("./"), "reason": _s(ig["reason"], 200),
                                  "kind": _s(ig.get("kind", ""), 30)})
        if len(out["workflows"]) > MAX_WORKFLOWS:
            errs.append(f"more than {MAX_WORKFLOWS} workflows; the map is too large")
            break
    return out


# --- discovery ---------------------------------------------------------------------------------------------------------------

_STATE_METHODS = {"post", "put", "patch", "delete"}
_TIMER_DECOS = {"scheduled_job", "periodic_task", "repeat_every", "cron", "scheduled"}
_QUEUE_DECOS = {"shared_task", "consumer", "subscriber", "on_message", "actor", "task_handler"}
_AI_CHAINS = (("messages", "create"), ("messages", "stream"), ("completions", "create"), ("responses", "create"), ("generate_content",),
              ("generateContent",))


def _entry(kind: str, file: str, function: str, name: str, line: int) -> dict:
    return {"key": f"{kind}|{file}|{function}|{name}", "kind": kind, "file": file, "function": function, "name": name, "line": line}


def _chain(node) -> tuple[str, ...]:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return tuple(reversed(parts))


class _Calls(ast.NodeVisitor):
    """Collect what a function does itself (not what its nested functions do)."""

    def __init__(self):
        self.ai = False
        self.loop = False
        self.sleeps = False

    def visit_FunctionDef(self, node):  # nested defs are their own entries
        pass

    visit_AsyncFunctionDef = visit_FunctionDef
    visit_Lambda = visit_FunctionDef

    def visit_Call(self, node):
        ch = _chain(node.func)
        if any(ch[-len(c):] == c for c in _AI_CHAINS if len(ch) >= len(c)):
            self.ai = True
        if ch and ch[-1] == "sleep":
            self.sleeps = True
        self.generic_visit(node)

    def visit_While(self, node):
        if isinstance(node.test, ast.Constant) and node.test.value is True:
            self.loop = True
        self.generic_visit(node)


def _py_entries(rel: str, text: str) -> list[dict]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return []
    out: list[dict] = []

    def walk(body, prefix: str):
        for node in body:
            if isinstance(node, ast.ClassDef):
                walk(node.body, f"{prefix}{node.name}.")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                fn = f"{prefix}{node.name}"
                seen_kind = False
                for dec in node.decorator_list:
                    call = dec if isinstance(dec, ast.Call) else None
                    f = call.func if call else dec
                    attr = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
                    path = ""
                    if call and call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
                        path = call.args[0].value
                    if attr in _STATE_METHODS and call is not None and (path or call.args or call.keywords):
                        kind = "webhook" if "hook" in path.lower() else "route"
                        out.append(_entry(kind, rel, fn, f"{attr.upper()} {path}", node.lineno))
                        seen_kind = True
                    elif attr == "route" and call is not None:
                        methods = []
                        for kw in call.keywords:
                            if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                                methods = [e.value.upper() for e in kw.value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
                        changing = [m for m in methods if m != "GET"]
                        if changing:
                            out.append(_entry("webhook" if "hook" in path.lower() else "route", rel, fn, f"{'/'.join(changing)} {path}", node.lineno))
                            seen_kind = True
                    elif attr in _TIMER_DECOS:
                        out.append(_entry("timer", rel, fn, attr, node.lineno))
                        seen_kind = True
                    elif attr in _QUEUE_DECOS or (attr == "task" and call is not None):
                        out.append(_entry("queue", rel, fn, attr, node.lineno))
                        seen_kind = True
                v = _Calls()
                for stmt in node.body:
                    v.visit(stmt)
                if v.ai:
                    out.append(_entry("ai", rel, fn, "AI SDK call", node.lineno))
                if v.loop and v.sleeps and not seen_kind:
                    out.append(_entry("timer", rel, fn, "background loop", node.lineno))
                walk(node.body, f"{fn}.")
    walk(tree.body, "")
    # module-level AI calls (a script)
    mod = _Calls()
    for stmt in tree.body:
        if not isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            mod.visit(stmt)
    if mod.ai:
        out.append(_entry("ai", rel, "<module>", "AI SDK call", 1))
    return out


_Q = r"[^'\"`\n]"       # a quoted path never runs past its line, and is bounded: no regex here can go quadratic on hostile input
_JS_RULES = (
    ("route", re.compile(r"\b(?:app|router|server|api|r|route|fastify|express)\.(post|put|patch|delete)\([ \t]{0,20}(['\"`])(" + _Q + r"{1,300})\2", re.I), "{m1} {g3}"),
    ("route", re.compile(r"@(Post|Put|Patch|Delete)\([ \t]{0,20}(?:(['\"`])(" + _Q + r"{0,300})\2)?"), "{m1} {g3}"),
    ("timer", re.compile(r"\b(cron\.schedule|new[ \t]{1,20}CronJob|schedule\.scheduleJob|agenda\.define)\("), "{m0}"),
    ("queue", re.compile(r"\b(new[ \t]{1,20}Worker|\.process|\.consume|\.subscribe)\([ \t]{0,20}(['\"`])(" + _Q + r"{1,300})\2"), "{m0} {g3}"),
    ("ai", re.compile(r"\.(messages\.create|messages\.stream|chat\.completions\.create|responses\.create|generateContent)\("), "AI SDK call"),
)
_GQ = r"[^\"\n]"
_GO_RULES = (
    ("route", re.compile(r"\.(?:HandleFunc|Handle)\([ \t]{0,20}\"((?:(?:POST|PUT|PATCH|DELETE)[ \t]{1,20})?" + _GQ + r"{1,300})\""), "{g1}"),
    ("route", re.compile(r"\.(POST|PUT|PATCH|DELETE|Post|Put|Patch|Delete)\([ \t]{0,20}\"(" + _GQ + r"{1,300})\""), "{m1} {g2}"),
    ("timer", re.compile(r"\.(?:AddFunc|NewTicker|Every)\("), "timer"),
    ("queue", re.compile(r"\.(?:Subscribe|QueueSubscribe|Consume)\([ \t]{0,20}\"(" + _GQ + r"{1,300})\""), "{g1}"),
    ("ai", re.compile(r"\b(?:Messages\.New|Chat\.Completions\.New|GenerateContent)\("), "AI SDK call"),
)
_JS_FN = re.compile(r"(?:function\s+(\w+)|(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?(?:function|\())")
_GO_FN = re.compile(r"^func\s+(?:\([^)]*\)\s*)?(\w+)")


MAX_MATCHES = 5000
MAX_LINE = 2000


def _generic_entries(rel: str, text: str, rules, fn_re, go: bool) -> list[dict]:
    nl: list[int] = []                                  # offsets of every newline: a match's line is one bisect, not a rescan
    i = text.find("\n")
    while i != -1:
        nl.append(i)
        i = text.find("\n", i + 1)
    fn_lines: list[int] = []
    fn_names: list[str] = []
    for n, ln in enumerate(text.split("\n"), 1):
        m = fn_re.search(ln[:MAX_LINE])
        if m:
            fn_lines.append(n)
            fn_names.append(next((g for g in m.groups() if g), ""))
    out: list[dict] = []
    for kind, rx, tmpl in rules:
        for count, m in enumerate(rx.finditer(text)):
            if count >= MAX_MATCHES:
                break
            line = bisect.bisect_right(nl, m.start()) + 1
            k = bisect.bisect_right(fn_lines, line) - 1
            fn = fn_names[k] if k >= 0 else ""
            g = list(m.groups()) + [""] * 3
            name = " ".join(tmpl.format(m0=m.group(0).split("(")[0].strip(), m1=(g[0] or "").upper() if kind == "route" else (g[0] or ""),
                                        g1=g[0] or "", g2=g[1] or "", g3=g[2] or "").split())
            kind_use = "webhook" if kind == "route" and not go and re.search(r"hook", name, re.I) else kind
            out.append(_entry(kind_use, rel, fn, name, line))
    return out


def _regular(p: Path) -> bool:
    """A plain file, not a symlink, FIFO or device: the code under a scan is untrusted, and reading a FIFO would hang."""
    try:
        return stat.S_ISREG(os.lstat(p).st_mode)
    except OSError:
        return False


def _walk_files(root: Path):
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        for fname in sorted(filenames):
            n += 1
            if n > SCAN_FILE_CAP:
                return
            p = Path(dirpath) / fname
            if _regular(p):
                yield p


def discover(root: str | Path) -> list[dict]:
    """Every entry point found in the code under `root`: [{key, kind (route|webhook|timer|queue|ai), file, function, name, line}]."""
    root = Path(root)
    out: list[dict] = []
    for p in _walk_files(root):
        ext = p.suffix.lower()
        name = p.name
        if ext == ".py":
            if name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py" or name == "setup.py":
                continue
            fn = lambda rel, text: _py_entries(rel, text)  # noqa: E731
        elif ext in (".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx"):
            if ".test." in name or ".spec." in name or name.endswith(".min.js") or name.endswith(".d.ts"):
                continue
            fn = lambda rel, text: _generic_entries(rel, text, _JS_RULES, _JS_FN, False)  # noqa: E731
        elif ext == ".go":
            if name.endswith("_test.go"):
                continue
            fn = lambda rel, text: _generic_entries(rel, text, _GO_RULES, _GO_FN, True)  # noqa: E731
        else:
            continue
        try:
            if os.lstat(p).st_size > 1024 * 1024:
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        out.extend(fn(p.relative_to(root).as_posix(), text))
    seen: set[str] = set()
    uniq = []
    for e in out:
        if e["key"] not in seen:
            seen.add(e["key"])
            uniq.append(e)
    return sorted(uniq, key=lambda e: (e["file"], e["line"], e["name"]))


# --- coverage and staleness --------------------------------------------------------------------------------------------------

def refs_of(wf: dict) -> list[dict]:
    """Every code location a workflow names: its trigger, its steps and its `covers` list."""
    refs = []
    t = wf.get("trigger") or {}
    if t.get("code_ref"):
        refs.append(t["code_ref"])
    if t.get("route"):
        refs.append({"file": (t.get("code_ref") or {}).get("file", ""), "route": t["route"]})
    for s in wf.get("steps", []):
        if s.get("code_ref"):
            refs.append(s["code_ref"])
    refs.extend(wf.get("covers", []))
    return refs


def _norm_route(r: str) -> str:
    return " ".join((r or "").upper().split())


def _covered(entry: dict, refs: list[dict]) -> bool:
    fn_last = entry["function"].split(".")[-1]
    for r in refs:
        same_file = r.get("file") == entry["file"]
        fn = r.get("function")
        if same_file and fn == "*":
            return True
        if same_file and fn and fn.split(".")[-1] == fn_last and entry["function"] != "<module>":
            return True
        if same_file and fn == "<module>" and entry["function"] == "<module>":
            return True
        route = r.get("route")
        if route and _norm_route(route) == _norm_route(entry["name"]) and (not r.get("file") or same_file):
            return True
    return False


def _ignored(entry: dict, ignore: list[dict]) -> bool:
    return any(fnmatch.fnmatch(entry["file"], ig["file"]) and (not ig.get("kind") or ig["kind"] == entry["kind"]) for ig in ignore)


def _has_function(text: str, name: str) -> bool:
    last = name.split(".")[-1]
    return bool(last) and re.search(rf"(?<![\w$]){re.escape(last)}(?![\w$])", text) is not None


def stale_refs(root: str | Path, workflows: list[dict]) -> list[dict]:
    """[{workflow, step, file, function, why}] for every code_ref whose file is gone or no longer holds the named function."""
    root = Path(root)
    real_root = root.resolve()
    out, cache = [], {}
    for wf in workflows:
        pairs = [("trigger", (wf.get("trigger") or {}).get("code_ref"))] + [(s["id"], s.get("code_ref")) for s in wf.get("steps", [])] \
            + [("covers", c) for c in wf.get("covers", [])]
        for where, ref in pairs:
            if not ref:
                continue
            f = ref["file"]
            if f not in cache:
                p = root / f
                try:
                    inside = p.resolve().is_relative_to(real_root)
                    ok = inside and _regular(p) and os.lstat(p).st_size <= STALE_READ_BYTES and len(cache) < STALE_MAX_FILES
                    cache[f] = p.read_text(encoding="utf-8", errors="replace") if ok else None
                except (OSError, ValueError):
                    cache[f] = None
            text = cache[f]
            fn = ref.get("function") or ""
            if text is None:
                out.append({"workflow": wf["id"], "step": where, "file": f, "function": fn, "why": "the file no longer exists"})
            elif fn and fn not in ("*", "<module>") and not _has_function(text, fn):
                out.append({"workflow": wf["id"], "step": where, "file": f, "function": fn, "why": f"{fn} is no longer in {f}"})
    return out


def check(root: str | Path) -> dict:
    """{"map": load(), "stale": [...], "uncovered": [...], "ignored": n, "discovered": n}. Uncovered = discovered and neither named by
    a workflow nor under an `ignore` rule."""
    m = load(root)
    found = discover(root)
    refs = [r for wf in m["workflows"] for r in refs_of(wf)]
    uncovered, ignored = [], 0
    for e in found:
        if _covered(e, refs):
            continue
        if _ignored(e, m["ignore"]):
            ignored += 1
            continue
        uncovered.append(e)
    return {"map": m, "stale": stale_refs(root, m["workflows"]), "uncovered": uncovered, "ignored": ignored, "discovered": len(found)}


def build_report(root: str | Path, sha: str = "") -> dict:
    """The document the dashboard gate stores per commit (POST /api/logic/report, the job MCP's record_logic)."""
    c = check(root)
    return {"version": REPORT_VERSION, "sha": sha, "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "workflows": c["map"]["workflows"], "errors": c["map"]["errors"], "stale": c["stale"],
            "uncovered": [{k: e[k] for k in ("key", "kind", "file", "function", "name", "line")} for e in c["uncovered"]],
            "ignored": c["ignored"], "discovered": c["discovered"]}


# --- the diff ----------------------------------------------------------------------------------------------------------------

def _step_sig(s: dict) -> dict:
    return {k: s.get(k) for k in ("kind", "label", "detail", "question", "code_ref", "data", "effects", "ai")}


def _wf_changes(old: dict, new: dict) -> list[dict]:
    ch: list[dict] = []
    for field, word in (("name", "name"), ("summary", "summary"), ("trigger", "trigger")):
        if old.get(field) != new.get(field):
            ch.append({"what": f"{word} changed", "step": "", "detail": ""})
    if sorted(old.get("data", [])) != sorted(new.get("data", [])):
        ch.append({"what": "data touched changed", "step": "", "detail": ""})
    o = {s["id"]: s for s in old.get("steps", [])}
    n = {s["id"]: s for s in new.get("steps", [])}
    for sid, s in n.items():
        if sid not in o:
            ch.append({"what": "step added", "step": sid, "detail": s["label"]})
        elif _step_sig(o[sid]) != _step_sig(s):
            fields = [k for k, v in _step_sig(s).items() if v != _step_sig(o[sid]).get(k)]
            ch.append({"what": "step changed", "step": sid, "detail": f"{s['label']} ({', '.join(fields)})"})
    for sid, s in o.items():
        if sid not in n:
            ch.append({"what": "step removed", "step": sid, "detail": s["label"]})
    if old.get("edges") != new.get("edges") and not any(c["what"].startswith("step") for c in ch):
        ch.append({"what": "flow changed", "step": "", "detail": ""})
    return ch


def diff(old: list[dict], new: list[dict]) -> dict:
    """What changed in the logic: {added: [{id, name}], removed: [{id, name}], changed: [{id, name, changes}], summary: {...}}."""
    o = {w["id"]: w for w in old or []}
    n = {w["id"]: w for w in new or []}
    added = [{"id": i, "name": w["name"], "steps": len(w.get("steps", []))} for i, w in n.items() if i not in o]
    removed = [{"id": i, "name": w["name"], "steps": len(w.get("steps", []))} for i, w in o.items() if i not in n]
    changed = []
    for i, w in n.items():
        if i in o:
            ch = _wf_changes(o[i], w)
            if ch:
                changed.append({"id": i, "name": w["name"], "changes": ch})
    return {"added": added, "removed": removed, "changed": changed,
            "summary": {"added": len(added), "removed": len(removed), "changed": len(changed),
                        "steps": sum(len(c["changes"]) for c in changed)}}


# --- the slim form (CA tool, export) -----------------------------------------------------------------------------------------

def slim(workflows: list[dict]) -> list[dict]:
    """The small form for teaching an agent or an app's own admin page: what starts each workflow, its steps and their side effects."""
    out = []
    for w in workflows:
        t = w.get("trigger") or {}
        out.append({"id": w["id"], "name": w["name"], "summary": w["summary"],
                    "trigger": {k: t[k] for k in ("type", "detail", "control", "route") if t.get(k)},
                    "data": w.get("data", []),
                    "steps": [{"id": s["id"], "kind": s["kind"], "label": s["label"],
                               **({"question": s["question"]} if s.get("question") else {}),
                               **({"effects": s["effects"]} if s.get("effects") else {}),
                               **({"data": s["data"]} if s.get("data") else {}),
                               **({"ai": {k: v for k, v in s["ai"].items() if v}} if s.get("ai") else {})} for s in w.get("steps", [])]})
    return out


# --- importing today's catalog -----------------------------------------------------------------------------------------------

_TRIGGER_MAP = {"timer": "timer", "webhook": "webhook", "email": "event", "button": "button", "event": "event", "api": "route"}
_KIND_MAP = {"precode": "action", "logic": "action", "trigger": "trigger"}


def _slug_id(text: str) -> str:
    out = re.sub(r"[^a-z0-9._-]+", "-", (text or "").lower()).strip("-._")
    return out[:80] or "workflow"


def from_catalog(catalog: dict) -> list[dict]:
    """Convert a scanner catalog (docs/catalog-schema.md: `workflows` with trigger, steps, edges, side_effects) into logic map workflows,
    ready to dump as `.crewboss/logic/*.yaml`. A one-off migration: what the catalog cannot say (button names, per-step effects) is left empty."""
    out = []
    for wf in catalog.get("workflows") or []:
        if not isinstance(wf, dict) or not wf.get("id"):
            continue
        t = wf.get("trigger") or {}
        steps = []
        for st in wf.get("steps") or []:
            kind = _KIND_MAP.get(st.get("kind"), st.get("kind") if st.get("kind") in STEP_KINDS else "action")
            if kind == "trigger":
                continue
            step = {"id": st["id"], "kind": kind, "label": st.get("label") or st["id"]}
            if st.get("detail"):
                step["detail"] = st["detail"]
            if kind == "decision":
                step["question"] = st.get("question") or st.get("label") or st["id"]
            cr = st.get("code_ref") or {}
            if cr.get("file"):
                step["code_ref"] = {k: cr[k] for k in ("file", "function") if cr.get(k)}
            if kind == "ai":
                ai = st.get("ai") or {}
                step["ai"] = {"model": ai.get("model_rule") or "", "skill": ai.get("prompt_name") or cr.get("function") or step["label"],
                              "may": [str(x) for x in ai.get("tools") or []]}
            steps.append(step)
        ids = {s["id"] for s in steps}
        edges = [{"from": "trigger" if (e.get("from") not in ids) else e["from"], "to": e["to"], "label": e.get("label", "")}
                 for e in wf.get("edges") or [] if e.get("to") in ids]
        node = {"id": _slug_id(wf["id"]), "name": wf.get("name") or wf["id"], "summary": wf.get("summary") or wf.get("name") or wf["id"],
                "trigger": {"type": _TRIGGER_MAP.get(t.get("type"), "event"), "detail": t.get("detail") or ""},
                "data": [str(x) for x in wf.get("side_effects") or []], "steps": steps}
        refs = [r for r in wf.get("source_refs") or [] if isinstance(r, dict) and r.get("file")]
        if refs and not steps:
            node["steps"] = [{"id": "run", "kind": "action", "label": wf.get("name") or "Run"}]
        if refs:
            node["covers"] = [{"file": r["file"], "function": "*"} for r in refs]
        if edges:
            node["edges"] = edges
        if node["steps"]:
            out.append(node)
    return out


def dump_workflows(workflows: list[dict]) -> str:
    return yaml.safe_dump({"workflows": workflows}, sort_keys=False, allow_unicode=True, width=120)


# --- the picture -------------------------------------------------------------------------------------------------------------

NODE_W, NODE_H, GAP_X, GAP_Y, PAD = 232, 58, 28, 40, 16


def layout(wf: dict) -> dict:
    """Place a workflow's trigger and steps in rows (a step sits one row below its latest predecessor) for an inline SVG.
    Without `edges` the steps run straight on, in order. Returns {nodes, edges, width, height}."""
    steps = wf.get("steps", [])
    ids = [s["id"] for s in steps]
    edges = [dict(e) for e in wf.get("edges") or []]
    if not edges:
        chain = ["trigger", *ids]
        edges = [{"from": a, "to": b, "label": ""} for a, b in zip(chain, chain[1:])]
    elif not any(e["from"] == "trigger" for e in edges) and ids:
        has_in = {e["to"] for e in edges}
        edges = [{"from": "trigger", "to": i, "label": ""} for i in ids if i not in has_in][:1] + edges
    level = {"trigger": 0}
    for _ in range(len(ids) + 2):          # relax longest paths; a cycle stops growing after len(ids) rounds
        for e in edges:
            if e["from"] in level and level[e["from"]] + 1 > level.get(e["to"], -1) and level[e["from"]] < len(ids) + 1:
                level[e["to"]] = level[e["from"]] + 1
    for i in ids:
        level.setdefault(i, max(level.values()) + 1)
    rows: dict[int, list[str]] = {}
    for nid in ["trigger", *ids]:
        rows.setdefault(level[nid], []).append(nid)
    widest = max(len(r) for r in rows.values())
    width = PAD * 2 + widest * NODE_W + (widest - 1) * GAP_X
    by_id = {s["id"]: s for s in steps}
    nodes, pos = [], {}
    for lv in sorted(rows):
        row = rows[lv]
        row_w = len(row) * NODE_W + (len(row) - 1) * GAP_X
        x0 = (width - row_w) / 2
        for j, nid in enumerate(row):
            x, y = x0 + j * (NODE_W + GAP_X), PAD + lv * (NODE_H + GAP_Y)
            pos[nid] = (x, y)
            if nid == "trigger":
                t = wf.get("trigger") or {}
                label, kind, sub = (t.get("control") or t.get("detail") or t.get("type") or "Starts"), "trigger", t.get("type", "")
            else:
                s = by_id[nid]
                label, kind, sub = s["label"], s["kind"], (s.get("question") or "")
            nodes.append({"id": nid, "kind": kind, "x": x, "y": y, "w": NODE_W, "h": NODE_H, "sub": (sub[:27] + "…") if len(sub) > 28 else sub,
                          "lines": textwrap.wrap(label, 30)[:2] or [""], "effects": len(by_id[nid].get("effects", [])) if nid != "trigger" else 0})
    out_edges = []
    for e in edges:
        if e["from"] in pos and e["to"] in pos:
            (x1, y1), (x2, y2) = pos[e["from"]], pos[e["to"]]
            out_edges.append({"x1": x1 + NODE_W / 2, "y1": y1 + NODE_H, "x2": x2 + NODE_W / 2, "y2": y2, "label": e.get("label", ""),
                              "back": y2 <= y1})
    height = PAD * 2 + (max(level.values()) + 1) * NODE_H + max(level.values()) * GAP_Y
    return {"nodes": nodes, "edges": out_edges, "width": width, "height": height}
