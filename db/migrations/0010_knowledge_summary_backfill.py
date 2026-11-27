"""Backfill knowledge.summary for entries written before summaries existed (0009).

summary = the first meaningful paragraph of the body, trimmed at a sentence boundary to 200 characters,
ending in '…' if cut (app.knowledge_text.derive_summary). Every row this fills is marked needs_review so a
session can write a better one later (update_knowledge over MCP). Runs once, inside the migration's
transaction; rows that already have a summary are untouched."""
from app.knowledge_text import derive_summary


def backfill(conn) -> int:
    rows = conn.execute("SELECT id, body FROM knowledge WHERE summary = '' ORDER BY id").fetchall()
    for row in rows:
        conn.execute(
            "UPDATE knowledge SET summary = %s, needs_review = true WHERE id = %s",
            (derive_summary(row["body"]), row["id"]),
        )
    return len(rows)


def migrate(conn) -> None:
    backfill(conn)
