"""Which tables a SQL string WRITES (INSERT INTO / UPDATE / DELETE FROM), for the discovery modules.

One place for the pattern (task #52). Two bugs it fixes:
- a table name with digits (e.g. `audit2`) was cut at the first digit by the old `[a-z_]+`;
- `ON CONFLICT (…) DO UPDATE SET …` (an upsert's second half) was read as a write to a table called "SET",
  which put a false write step on the Syncro webhook workflow. The upsert's table is already counted by its
  INSERT INTO.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

_WRITE_RE = re.compile(r"\b(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+(?:ONLY\s+)?([A-Za-z_][A-Za-z0-9_]*)", re.I)
_DO_BEFORE = re.compile(r"\bDO\s*$", re.I)


def written_tables(sql: str) -> list[str]:
    """Lower-cased table names, in order of appearance, without duplicates."""
    out: list[str] = []
    for m in _WRITE_RE.finditer(sql or ""):
        verb, name = m.group(1), m.group(2)
        if verb.upper() == "UPDATE" and (name.upper() == "SET" or _DO_BEFORE.search(sql[: m.start()])):
            continue  # ON CONFLICT … DO UPDATE SET: not a separate write
        name = name.lower()
        if name not in out:
            out.append(name)
    return out


# ── Business tables (discover/state_routes.py + lib/coverage.py) ──────────────────────────────────────
# A table counts as a BUSINESS table when the ERP's own migrations create it (so prose like "Update the
# account" in a comment string can't invent a table called "the") and it isn't one of the platform's
# bookkeeping tables below — logs, audit trails, usage counters, per-viewer read/ack state. Those are
# written by nearly every flow as a side effect and would drown out the real business writes.
MIGRATIONS_DIR = "db/migrations"

NON_BUSINESS_TABLES = {
    "system_events", "record_history", "ai_usage", "ai_calls", "scheduler_runs", "sync_state",
    "notification_reads", "follower_acks", "dev_access_audit", "credential_reveals",
    "portal_link_failures", "portal_mfa_attempts", "connection_launch_reconnects",
    "push_subscriptions", "app_push_tokens",   # push-delivery plumbing, not a business record
}

_CREATE_RE = re.compile(r"\bCREATE\s+(?:UNLOGGED\s+)?TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_][\w.]*)", re.I)
_DROP_RE = re.compile(r"\bDROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?([A-Za-z_][\w.]*)", re.I)
_RENAME_RE = re.compile(r"\bALTER\s+TABLE\s+(?:IF\s+EXISTS\s+)?([A-Za-z_][\w.]*)\s+RENAME\s+TO\s+([A-Za-z_]\w*)", re.I)
_SQL_COMMENT_RE = re.compile(r"--[^\n]*")


def _bare(name: str) -> str:
    return name.split(".")[-1].lower()


def schema_tables(root) -> set[str] | None:
    """Tables that exist after replaying `db/migrations/*.sql` in file order (CREATE adds, DROP removes,
    ALTER … RENAME TO renames). None when the tree has no migrations dir — callers then accept any
    table name rather than filter everything out."""
    base = Path(root) / MIGRATIONS_DIR
    if not base.is_dir():
        return None
    tables: set[str] = set()
    for p in sorted(base.glob("*.sql")):
        try:
            sql = _SQL_COMMENT_RE.sub("", p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        events = [(m.start(), "create", _bare(m.group(1)), None) for m in _CREATE_RE.finditer(sql)]
        events += [(m.start(), "drop", _bare(m.group(1)), None) for m in _DROP_RE.finditer(sql)]
        events += [(m.start(), "rename", _bare(m.group(1)), _bare(m.group(2))) for m in _RENAME_RE.finditer(sql)]
        for _pos, kind, name, new in sorted(events):
            if kind == "create":
                tables.add(name)
            elif kind == "drop":
                tables.discard(name)
            elif name in tables:
                tables.discard(name)
                tables.add(new)
    return tables


def is_business_table(name: str, known: set[str] | None) -> bool:
    if name in NON_BUSINESS_TABLES:
        return False
    return known is None or name in known


def literal_writes(node) -> list[str]:
    """Tables written by the SQL string literals under an AST node, in order, without duplicates.
    Docstrings are skipped (they describe SQL in prose: "the UPDATE re-asserts …"); f-string pieces
    are read like any other literal, so `f"UPDATE quotes SET {col}=%s"` still counts."""
    docstrings = set()
    for n in ast.walk(node):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)) and n.body:
            first = n.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstrings.add(id(first.value))
    out: list[str] = []
    for n in ast.walk(node):
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings:
            for t in written_tables(n.value):
                if t not in out:
                    out.append(t)
    return out
