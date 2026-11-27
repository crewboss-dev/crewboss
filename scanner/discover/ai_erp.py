"""AI call-site discovery over app/clients/claude.py (the ERP's ONE place all Anthropic calls live —
29 `.messages.create(` sites at the time this was written, per CLAUDE.md's own count).

Each public function in the file is analysed for:
  * a DIRECT `.messages.create(` call in its own body, or
  * a call to a same-module private helper (one level down — e.g. `triage_alert` -> `_triage_once`)
    that itself makes the call, optionally invoked MORE THAN ONCE with a different `model` argument
    (the Haiku-first/escalate-to-Sonnet convention: memory `default-ai-model-sonnet`).

Nothing here calls the model or reads an env var — it is pure `ast` analysis of one file, and the only
strings it emits verbatim are the `_XXX_SYSTEM` prompt constants (code, not runtime content).
"""
from __future__ import annotations

import ast

from scanner.lib import astutil as A

CLAUDE_PY = "app/clients/claude.py"


def _model_expr_text(node) -> str:
    return A.node_source(node)


def _helper_model_desc(helper_fn) -> str:
    """Describe a zero/one-arg local model-resolver function (`_model`, `_triage_model`,
    `_line_model`, ...) by reading ITS body: which app.models family it falls back to, and which env
    var pins it — the same two facts a human would look for."""
    env_var = None
    family = None
    for n in ast.walk(helper_fn):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "get":
            base = n.func.value
            if isinstance(base, ast.Attribute) and base.attr == "environ":
                if n.args:
                    env_var = A.const_str(n.args[0])
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in ("haiku", "sonnet", "opus"):
            family = n.func.attr
    if family and env_var:
        return f"{family} (app.models.{family}(); env override {env_var})"
    if family:
        return f"{family} (app.models.{family}())"
    return f"custom resolver {helper_fn.name}()"


def describe_model_rule(expr_text: str, helpers: dict) -> str:
    if expr_text in ("_model()", "()"):
        return "estate default: latest Sonnet (app.models.sonnet(); env override ANTHROPIC_MODEL)"
    if expr_text.startswith("_models.haiku"):
        return "fixed: latest Haiku (app.models.haiku())"
    if expr_text.startswith("_models.sonnet"):
        return "fixed: latest Sonnet (app.models.sonnet())"
    if expr_text.startswith("_models.opus"):
        return "fixed: latest Opus (app.models.opus(), opt-in)"
    name = expr_text.split("(")[0]
    if name in helpers:
        return _helper_model_desc(helpers[name])
    return f"expression: {expr_text}"


def _prompt_for_call(mod: A.Module, call: ast.Call, module_consts: dict):
    kw = A.call_kwargs(call)
    sysnode = kw.get("system")
    if sysnode is None:
        return None, None, None
    if isinstance(sysnode, ast.Name) and sysnode.id in module_consts:
        const_node = module_consts[sysnode.id]
        text = A.const_str(const_node)
        return sysnode.id, text, const_node
    text = A.const_str(sysnode)
    return None, text, sysnode


def _output_kind(fn_or_call_scope, call: ast.Call) -> str:
    kw = A.call_kwargs(call)
    if "tools" in kw or "tool_choice" in kw:
        return "forced-tool-schema"
    for n in ast.walk(fn_or_call_scope):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_json_obj":
            return "json-parsed-text"
    return "text"


def _build_ai_block(mod: A.Module, call: ast.Call, scope_fn, module_consts: dict, context_expr, extra_ctx=None):
    kw = A.call_kwargs(call)
    prompt_name, prompt_text, prompt_node = _prompt_for_call(mod, call, module_consts)
    max_tokens = A.const_number(kw.get("max_tokens"))
    temperature = A.const_number(kw.get("temperature"))
    tools_kw = kw.get("tools")
    tool_names = []
    if isinstance(tools_kw, ast.List):
        for el in tools_kw.elts:
            if isinstance(el, ast.Dict):
                for k, v in zip(el.keys, el.values):
                    if A.const_str(k) == "name":
                        n = A.const_str(v)
                        if n:
                            tool_names.append(n)
    return {
        "provider": "anthropic",
        "model_rule": None,   # filled by caller (needs the invocation-level model expr)
        "prompt_name": prompt_name or "(inline)",
        "prompt_location": {
            "file": CLAUDE_PY,
            "lines": mod.line_range(prompt_node) if prompt_node is not None else mod.line_range(call),
        },
        "prompt_text": prompt_text or "",
        "context_injected": A.describe_context(context_expr, extra_ctx),
        "max_tokens": max_tokens,
        "temperature": temperature,
        "output": _output_kind(scope_fn, call),
        "tools": tool_names,
    }


def _raw_content_node(call: ast.Call):
    kw = A.call_kwargs(call)
    messages = kw.get("messages")
    if isinstance(messages, ast.List) and messages.elts:
        first = messages.elts[0]
        if isinstance(first, ast.Dict):
            for k, v in zip(first.keys, first.values):
                if A.const_str(k) == "content":
                    return v
    return None


def _resolve_arg(call: ast.Call, callee_fn, param_name: str):
    params = A.func_param_names(callee_fn)
    for i, a in enumerate(call.args):
        if i < len(params) and params[i] == param_name:
            return a
    for kw in call.keywords:
        if kw.arg == param_name:
            return kw.value
    return None


def _content_expr_direct(fn, call: ast.Call):
    """The user-content expression when `fn` makes the `.messages.create(` call itself — resolved
    back to its assignment if passed as a bare Name (`messages=[{"content": user}]` -> the
    `user = ...` above it), so describe_context() sees the real construction, not just an identifier."""
    node = _raw_content_node(call)
    if isinstance(node, ast.Name):
        resolved = A.find_last_assign(fn, node.id)
        if resolved is not None:
            return resolved
    return node


def _content_expr_one_level(fn, call: ast.Call, helper_fn, inner_call: ast.Call):
    """Same, when the AI call lives in a same-module helper `fn` invokes (`triage_alert` ->
    `_triage_once`): resolve the helper's content Name to ITS parameter, then to what `fn` actually
    passed for that parameter at the call site, then (if that's itself a Name) to `fn`'s assignment."""
    node = _raw_content_node(inner_call)
    if not isinstance(node, ast.Name):
        return node
    params = A.func_param_names(helper_fn)
    if node.id in params:
        arg_node = _resolve_arg(call, helper_fn, node.id)
        if isinstance(arg_node, ast.Name):
            resolved = A.find_last_assign(fn, arg_node.id)
            return resolved if resolved is not None else arg_node
        return arg_node if arg_node is not None else node
    resolved = A.find_last_assign(helper_fn, node.id)
    return resolved if resolved is not None else node


def discover(root) -> list[dict]:
    """Returns raw AI-call-site records, one per PUBLIC function in claude.py that (directly or one
    level down) reaches a `.messages.create(` call. Shape: {func, ai, invocations: [model_expr, ...],
    escalates, source_refs}. The linker (discover/link.py) attaches these to workflows; build.py turns
    each into a Step with kind='ai'."""
    mod = A.parse_module(root, CLAUDE_PY)
    if mod is None:
        return []
    module_consts = A.module_level_assigns(mod)
    all_fns = {fn.name: fn for fn in A.all_functions(mod)}
    helper_model_fns = {n: fn for n, fn in all_fns.items() if n.endswith("_model") or n == "_model"}

    # Which top-level functions make the AI call directly?
    direct_ai_fns = {}
    for fn in A.all_functions(mod):
        calls = A.calls_matching(fn, attr="create", obj_attr="messages")
        if calls:
            direct_ai_fns[fn.name] = (fn, calls[0])

    out = []
    for fn in A.top_level_functions(mod):
        if fn.name.startswith("_"):
            continue  # private helpers are only reached THROUGH a public function
        invocations = []   # list of (model_expr_text, ai_block_template, call_node, scope_fn)
        if fn.name in direct_ai_fns:
            _, call = direct_ai_fns[fn.name]
            model_expr = _model_expr_text(A.call_kwargs(call).get("model"))
            ctx_expr = _content_expr_direct(fn, call)
            block = _build_ai_block(mod, call, fn, module_consts, ctx_expr, A.func_param_names(fn))
            invocations.append((model_expr, block, call, fn))
        else:
            # One level down: calls in `fn`'s body to a same-module helper that itself has the AI call.
            for call in ast.walk(fn):
                if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
                    continue
                helper_name = call.func.id
                if helper_name not in direct_ai_fns:
                    continue
                helper_fn, inner_call = direct_ai_fns[helper_name]
                # Map this invocation's args onto the helper's parameter names to find what was
                # passed for `model` at THIS call site (the helper often takes it as a plain arg).
                model_expr = None
                params = A.func_param_names(helper_fn)
                for i, a in enumerate(call.args):
                    if i < len(params) and params[i] == "model":
                        model_expr = _model_expr_text(a)
                for kw in call.keywords:
                    if kw.arg == "model":
                        model_expr = _model_expr_text(kw.value)
                if model_expr is None:
                    model_expr = _model_expr_text(A.call_kwargs(inner_call).get("model"))
                ctx_expr = _content_expr_one_level(fn, call, helper_fn, inner_call)
                block = _build_ai_block(mod, inner_call, helper_fn, module_consts, ctx_expr,
                                         A.func_param_names(fn))
                invocations.append((model_expr, block, inner_call, helper_fn))
        if not invocations:
            continue

        distinct_models = {m for m, *_ in invocations if m}
        escalates = len(invocations) >= 1 and any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and "escalate" in n.func.id
            for n in ast.walk(fn)
        )
        base_model_expr, base_block, base_call, base_scope = invocations[0]
        if escalates and len(distinct_models) >= 2 or (escalates and len(invocations) >= 2):
            models_seen = []
            for m, *_ in invocations:
                if m and m not in models_seen:
                    models_seen.append(m)
            descs = [describe_model_rule(m, helper_model_fns) for m in models_seen] or [
                describe_model_rule(base_model_expr, helper_model_fns)]
            model_rule = "Two-pass: " + " first pass; escalates to ".join(descs) + \
                " below the confidence threshold (app.clients.claude._escalate_threshold)"
        else:
            model_rule = describe_model_rule(base_model_expr, helper_model_fns)

        ai_block = dict(base_block)
        ai_block["model_rule"] = model_rule
        out.append({
            "func": fn.name,
            "ai": ai_block,
            "code_ref": {"file": CLAUDE_PY, "lines": mod.line_range(fn), "function": fn.name},
            "call_lines": mod.line_range(base_call),
            "escalates": bool(escalates and len(invocations) >= 2),
        })
    return out
