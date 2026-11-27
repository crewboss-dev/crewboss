"""Discovers connector/server.py's `@mcp.tool` functions — ~50 of them at the time this was written.
Per the brief, these are catalogued as ONE workflow entry per TOOL FAMILY (a family = the noun the
tool acts on: accounts, invoices, documents, ...), not 50 separate workflows — each family's tools
become that workflow's steps. (Same clustering logic as aios/aios_mcp — see mcp_tools.py.)
"""
from __future__ import annotations

from scanner.discover.mcp_tools import discover_families

FILE = "connector/server.py"


def discover(root) -> list[dict]:
    return discover_families(root, FILE, kind="connector_family")
