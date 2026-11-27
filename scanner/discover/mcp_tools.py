"""Shared `@mcp.tool` discovery — used for both connector/server.py (50 tools) and
aios/aios_mcp/server.py (13 tools). Clusters tools into FAMILIES (the noun each tool acts on) so a
50-tool file becomes a handful of workflow entries, not fifty."""
from __future__ import annotations

import ast
import re

from scanner.lib import astutil as A

_VERB_RE = re.compile(r"^(get|list|create|file|update|resolve|search|read|log|link|remember|forget|book|add|"
                      r"close|next|publish|whats)_?")

# Explicit clustering for the two known MCP tool servers (connector/server.py's ~50 tools,
# aios_mcp/server.py's ~13) — read from the actual function list rather than guessed, because the
# generic verb-strip heuristic below badly over-splits real names ("list_accounts" / "update_account"
# landing in different buckets is wrong far more often than a hand list is). Anything NOT in this map
# (a future tool) still gets a family from the generic fallback, just a coarser one — good enough for
# 'auto' confidence.
FAMILY_OVERRIDES = {
    # connector/server.py
    "get_overview": "overview & scheduling", "list_pending_activities": "overview & scheduling",
    "outstanding_at": "overview & scheduling", "next_free_slot": "overview & scheduling",
    "whats_on_at": "overview & scheduling",
    "list_accounts": "accounts & contacts", "update_account": "accounts & contacts",
    "list_contacts": "accounts & contacts", "update_contact": "accounts & contacts",
    "list_locations": "locations & devices", "get_location_devices": "locations & devices",
    "get_entity_files": "files & documents", "create_document": "files & documents",
    "file_document": "files & documents", "link_document": "files & documents",
    "list_unfiled_documents": "files & documents", "create_upload_url": "files & documents",
    "publish_pdf": "files & documents", "create_diagram": "diagrams",
    "list_quotes": "quotes", "create_quote": "quotes", "add_quote_lines": "quotes",
    "list_invoices": "invoices", "create_invoice": "invoices",
    "get_bookings": "bookings & cases", "book_case": "bookings & cases",
    "create_case": "bookings & cases", "update_case": "bookings & cases", "close_case": "bookings & cases",
    "add_case_materials": "bookings & cases", "log_time": "bookings & cases", "log_note": "bookings & cases",
    "link_conversation": "bookings & cases",
    "get_decisions": "decision queue",
    "get_emails": "email", "search_my_email": "email", "read_my_email": "email",
    "read_my_email_attachment": "email", "create_email_draft": "email",
    "list_opportunities": "opportunities", "get_opportunity": "opportunities",
    "create_opportunity": "opportunities", "update_opportunity": "opportunities",
    "create_task": "tasks", "update_task": "tasks", "list_my_tasks": "tasks",
    "get_user_persona": "persona & preferences", "remember_user_preference": "persona & preferences",
    "forget_user_preference": "persona & preferences",
    "connector_selftest": "diagnostics",
    # aios_mcp/server.py
    "log_note_aios": "activity log",
}


def family_of(fn_name: str) -> str:
    if fn_name in FAMILY_OVERRIDES:
        return FAMILY_OVERRIDES[fn_name]
    noun = _VERB_RE.sub("", fn_name)
    noun = noun.replace("entity_", "").replace("my_", "")
    words = [w for w in noun.split("_") if w]
    if words and words[-1].endswith("s") and len(words[-1]) > 3:
        words[-1] = words[-1][:-1]   # crude singularise so plural/singular don't split a family
    return " ".join(words[:2]) if words else (fn_name or "misc")


def discover_tools(root, file: str) -> list[dict]:
    mod = A.parse_module(root, file)
    if mod is None:
        return []
    tools = []
    for _arg, fn, kwargs in A.find_decorated(mod, "tool"):
        title = kwargs.get("title") or fn.name
        tools.append({
            "func": fn.name, "title": title, "family": family_of(fn.name),
            "file": file, "lines": mod.line_range(fn), "docstring": ast.get_docstring(fn) or "",
        })
    return tools


def discover_families(root, file: str, kind: str) -> list[dict]:
    tools = discover_tools(root, file)
    families: dict = {}
    for t in tools:
        families.setdefault(t["family"], []).append(t)
    out = []
    for family, members in sorted(families.items()):
        lo = min(int(m["lines"].split("-")[0]) for m in members)
        hi = max(int(m["lines"].split("-")[-1]) for m in members)
        out.append({
            "kind": kind, "family": family, "tools": members, "file": file,
            "source_refs": [{"file": file, "lines": f"{lo}-{hi}"}],
        })
    return out
