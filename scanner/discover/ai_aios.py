"""AI call-site discovery for the AiOS email-triage skill (aios/.claude/skills/email-triage/{classify,
draft}.py). Same idea as discover/ai_erp.py — find the `.../v1/messages` call, the system prompt, the
schema, the model rule — but the shape differs in two ways worth naming rather than hiding:

  1. There is no SDK here (`llm.py` uses raw httpx, like graph.py) and every call FORCES a tool schema
     (`call_tool`/`call_tool_chat` in aios/.claude/skills/email-triage/llm.py) — output is always
     "forced-tool-schema", never free text.
  2. The public functions (`classify_email`, `classify_sales_reply`, `draft_reply`) take the AI caller
     as an INJECTABLE PARAMETER (`caller=_call_claude` / `caller=_default_caller`) for testability, and
     call it by that parameter name, not by the helper's real name — so the generic "call a same-module
     function that itself has the AI call" match discover/ai_erp.py uses does not fire. Rather than
     build a second generic dataflow resolver for three functions, the wiring below is EXPLICIT (see
     _WIRING) — scanner/README.md lists this as a known simplification.

Prompts here are built at RUNTIME from a role/guidance/truncated flag (`_system_prompt(role, ...)`),
not static module constants like claude.py's `_XXX_SYSTEM` — so `prompt_text` records the STATIC
clauses ast can find (e.g. `_TRUNCATED_CLAUSE`) plus a note that the rest is role-templated, rather
than a full literal (there isn't one).
"""
from __future__ import annotations

import ast

from scanner.lib import astutil as A

CLASSIFY = "aios/.claude/skills/email-triage/classify.py"
DRAFT = "aios/.claude/skills/email-triage/draft.py"

# public function -> (its file, the direct-AI helper it calls via an injectable `caller=` param,
# the local function that builds `system`, the local function that builds `schema`)
_WIRING = {
    "classify_email":        (CLASSIFY, "_call_claude", "_system_prompt", "_tool_schema"),
    "classify_sales_reply":  (CLASSIFY, "_call_claude", "_sales_reply_system_prompt", "_sales_reply_schema"),
    "draft_reply":           (DRAFT, "_default_caller", "_system", "_schema"),
}


def _dict_return_key(fn, key: str) -> str | None:
    """If `fn`'s body is (essentially) `return {...}`, the literal value of {...}[key]."""
    for node in ast.walk(fn):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            for k, v in zip(node.value.keys, node.value.values):
                if A.const_str(k) == key:
                    return A.const_str(v)
    return None


def _static_prompt_fragments(fn) -> str:
    """Any module-level ALL-CAPS string constant referenced inside `fn` (e.g. `_TRUNCATED_CLAUSE`,
    `llm.UNTRUSTED_NOTICE`) — the parts of an otherwise-templated prompt that ARE fixed text."""
    frags = []
    for node in ast.walk(fn):
        if isinstance(node, ast.Attribute) and node.attr.isupper():
            frags.append(f"{A.node_source(node)}")
        if isinstance(node, ast.Name) and node.id.isupper():
            frags.append(node.id)
    seen, out = set(), []
    for f in frags:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return ", ".join(out)


def _model_rule(fn, helper_fn, mod_llm_names: set) -> str:
    uses_escalate = any(
        isinstance(n, ast.Attribute) and n.attr in ("escalate_model", "escalate_threshold")
        for n in ast.walk(fn))
    if uses_escalate:
        return ("Two-pass: Haiku first pass (llm.default_model()); escalates to Sonnet "
                "(llm.escalate_model()) when confidence < llm.escalate_threshold() (default 0.7), "
                "or unconditionally on a truncated email body")
    return "llm.default_model() — env ANTHROPIC_MODEL, fallback claude-haiku-4-5"


def discover(root) -> list[dict]:
    out = []
    mods = {}
    for f in (CLASSIFY, DRAFT):
        m = A.parse_module(root, f)
        if m is not None:
            mods[f] = m
    for pub_name, (file, helper_name, sys_fn_name, schema_fn_name) in _WIRING.items():
        mod = mods.get(file)
        if mod is None:
            continue
        fns = {fn.name: fn for fn in A.top_level_functions(mod)}
        fn = fns.get(pub_name)
        helper_fn = fns.get(helper_name)
        sys_fn = fns.get(sys_fn_name)
        schema_fn = fns.get(schema_fn_name)
        if fn is None or helper_fn is None:
            continue
        direct_calls = A.calls_to(helper_fn, "llm", "call_tool") + A.calls_to(helper_fn, "llm", "call_tool_chat")
        max_tokens = None
        if direct_calls:
            kw = A.call_kwargs(direct_calls[0])
            max_tokens = A.const_number(kw.get("max_tokens"))
        tool_name = _dict_return_key(schema_fn, "name") if schema_fn else None
        static_frags = _static_prompt_fragments(sys_fn) if sys_fn else ""
        prompt_text = (
            f"(dynamic — role-templated by {sys_fn_name}(); fixed clauses included verbatim: "
            f"{static_frags})" if static_frags else
            f"(dynamic — built per call by {sys_fn_name}(); no fixed literal text)"
        )
        ai_block = {
            "provider": "anthropic",
            "model_rule": _model_rule(fn, helper_fn, set()),
            "prompt_name": sys_fn_name,
            "prompt_location": {"file": file, "lines": mod.line_range(sys_fn) if sys_fn else mod.line_range(fn)},
            "prompt_text": prompt_text,
            "context_injected": A.describe_context(None, A.func_param_names(fn)),
            "max_tokens": max_tokens,
            "temperature": None,
            "output": "forced-tool-schema",
            "tools": [tool_name] if tool_name else [],
        }
        out.append({
            "func": pub_name,
            "ai": ai_block,
            "code_ref": {"file": file, "lines": mod.line_range(fn), "function": pub_name},
            "escalates": "escalate" in ast.dump(fn),
        })
    return out
