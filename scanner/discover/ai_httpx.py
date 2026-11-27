"""AI call-site discovery for callers that talk to `api.anthropic.com/v1/messages` over raw httpx instead of
the SDK: a signing service's classifier module and a companion app's `services/` modules.
They do not write to `ai_usage`; the scanner is read-only and does not change that.

Some of these (`draft_outreach`) build their SYSTEM PROMPT from a value passed in at call time (a
template stored in the caller's OWN database, e.g. an outreach group's configured prompt) rather than
a code constant. Per the catalog's privacy rule this scanner NEVER treats that as a printable prompt —
it records that the prompt is "caller-supplied / stored data, not a code constant" and only prints the
FIXED riders that ARE code (e.g. a `MODE_RIDERS` dict, a `JSON_OUTPUT_RIDER` constant).
"""
from __future__ import annotations

import ast

from scanner.lib import astutil as A


def _dict_get(d: ast.AST | None, key: str):
    if not isinstance(d, ast.Dict):
        return None
    for k, v in zip(d.keys, d.values):
        if A.const_str(k) == key:
            return v
    return None


def _resolve_dict_arg(fn, node):
    """`json=payload` -> the `payload = {...}` dict literal a few lines up, if there is one."""
    if isinstance(node, ast.Dict):
        return node
    if isinstance(node, ast.Name):
        resolved = A.find_last_assign(fn, node.id)
        if isinstance(resolved, ast.Dict):
            return resolved
    return node if isinstance(node, ast.Dict) else None


def _is_messages_post(call: ast.Call) -> bool:
    if not (isinstance(call.func, ast.Attribute) and call.func.attr == "post"):
        return False
    for a in list(call.args) + [kw.value for kw in call.keywords]:
        s = A.const_str(a)
        if s and "v1/messages" in s:
            return True
        if isinstance(a, ast.JoinedStr):
            for v in a.values:
                s2 = A.const_str(v)
                if s2 and "v1/messages" in s2:
                    return True
    return False


def _system_desc(mod: A.Module, module_consts: dict, node) -> tuple[str, str, dict]:
    """(prompt_name, prompt_text, prompt_location) for the `system` value of a payload dict. A bare
    module-level constant -> printed (it's code). Anything else (a parameter, an f-string built from a
    parameter, a `.format(...)` call on a template that itself takes caller data) -> NOT printed;
    described as caller-supplied so no stored/runtime prompt ever lands in the catalog."""
    if isinstance(node, ast.Name) and node.id in module_consts:
        const_node = module_consts[node.id]
        text = A.const_str(const_node)
        if text is not None:
            return node.id, text, {"file": mod.path, "lines": mod.line_range(const_node)}
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format" \
            and isinstance(node.func.value, ast.Name) and node.func.value.id in module_consts:
        const_node = module_consts[node.func.value.id]
        text = A.const_str(const_node)
        if text is not None:
            return (node.func.value.id, text, {"file": mod.path, "lines": mod.line_range(const_node)})
    if isinstance(node, ast.BinOp) or isinstance(node, ast.Call):
        # e.g. `(system_prompt or "").rstrip() + MODE_RIDERS.get(mode, "") + JSON_OUTPUT_RIDER` —
        # pull out any module-level constant names referenced and describe the rest as caller-supplied.
        consts_used = [n.id for n in ast.walk(node) if isinstance(n, ast.Name) and n.id in module_consts]
        if consts_used:
            return (", ".join(consts_used),
                    f"(caller-supplied base prompt + fixed code riders: {', '.join(consts_used)})",
                    {"file": mod.path, "lines": mod.line_range(node)})
    return "(caller-supplied)", "(prompt text is passed in by the caller / stored in the caller's own data, not a code constant)", \
        {"file": mod.path, "lines": mod.line_range(node) if node is not None else "?"}


def discover(root, file: str, provider_label: str = "sign") -> list[dict]:
    mod = A.parse_module(root, file)
    if mod is None:
        return []
    module_consts = A.module_level_assigns(mod)
    out = []
    for fn in A.top_level_functions(mod):
        calls = [c for c in ast.walk(fn) if isinstance(c, ast.Call) and _is_messages_post(c)]
        if not calls:
            continue
        call = calls[0]
        kw = A.call_kwargs(call)
        payload = _resolve_dict_arg(fn, kw.get("json"))
        model_node = _dict_get(payload, "model")
        max_tokens_node = _dict_get(payload, "max_tokens")
        system_node = _dict_get(payload, "system")
        if isinstance(system_node, ast.Name) and system_node.id not in module_consts:
            resolved = A.find_last_assign(fn, system_node.id)
            if resolved is not None:
                system_node = resolved
        messages_node = _dict_get(payload, "messages")
        tools_node = _dict_get(payload, "tools")
        content_expr = None
        if isinstance(messages_node, ast.List) and messages_node.elts:
            first = messages_node.elts[0]
            content_expr = _dict_get(first, "content") if isinstance(first, ast.Dict) else None
            if isinstance(content_expr, ast.Name):
                resolved = A.find_last_assign(fn, content_expr.id)
                content_expr = resolved if resolved is not None else content_expr
        if system_node is not None:
            prompt_name, prompt_text, prompt_loc = _system_desc(mod, module_consts, system_node)
        elif content_expr is not None and any(
                isinstance(n, ast.Name) and n.id in module_consts for n in ast.walk(content_expr)):
            # No separate `system` — the prompt is a module constant folded into the user message
            # (Sign's shape: `prompt = CLASSIFICATION_PROMPT + "\n\n..." `).
            const_name = next(n.id for n in ast.walk(content_expr)
                               if isinstance(n, ast.Name) and n.id in module_consts)
            const_node = module_consts[const_name]
            prompt_name, prompt_text, prompt_loc = (
                const_name, A.const_str(const_node) or "(non-literal)",
                {"file": mod.path, "lines": mod.line_range(const_node)})
        else:
            prompt_name, prompt_text, prompt_loc = (
                "(none)", "(no separate system prompt — instructions are folded into the user message)",
                {"file": file, "lines": mod.line_range(fn)})
        out.append({
            "func": fn.name,
            "ai": {
                "provider": "anthropic",
                "model_rule": A.node_source(model_node) or "unresolved",
                "prompt_name": prompt_name,
                "prompt_location": prompt_loc,
                "prompt_text": prompt_text,
                "context_injected": A.describe_context(content_expr, A.func_param_names(fn)),
                "max_tokens": A.const_number(max_tokens_node),
                "temperature": None,
                "output": "forced-tool-schema" if tools_node is not None else "json-parsed-text",
                "tools": [],
            },
            "code_ref": {"file": file, "lines": mod.line_range(fn), "function": fn.name},
            "escalates": False,
            "provider_label": provider_label,
        })
    return out
