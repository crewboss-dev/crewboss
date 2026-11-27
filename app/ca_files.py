"""Files on a CA conversation (task #727): screenshots, .md/.txt notes from other AIs, PDFs.

STORAGE is the server's own disk (config.CA_UPLOADS_DIR), the database keeps the metadata. RULES. Only these types, decided by the CONTENT (magic bytes / strict UTF-8 text), never by the extension or the browser's claim:
png, jpeg, webp, gif; PDF; and text (.md, .markdown, .txt: valid UTF-8, no NUL). Caps: image/PDF 10 MB, text 1 MB, 10 files per
message. A file is stored per person AND per conversation (ca_attachments, found by (id, user_id): another person's id is a
NotFound, so a 404). It is TEMPORARY: a file on a chat linked to a task is stripped once the task has been closed for 30 days (reopening keeps it); on
any other chat 90 days after the chat was last active (the PA's #616 fallback); one never sent goes after a day. Stripping deletes the
file and keeps a stub on the message. The runner gets them as real content blocks (runner/crewboss_runner/ca.py)."""
from __future__ import annotations

import logging
import os
import re
import secrets
from pathlib import Path, PurePosixPath

from app import config
from app.db import get_cursor
from app.repo import BadRequest, NotFound

log = logging.getLogger("crewboss.ca.files")

MAX_BYTES = {"image": 10 * 1024 * 1024, "pdf": 10 * 1024 * 1024, "text": 1024 * 1024}
MAX_UPLOAD = max(MAX_BYTES.values())
MAX_PER_MESSAGE = 10
TEXT_EXT = {".md", ".markdown", ".txt"}
ORPHAN_HOURS = 24
CHUNK = 600 * 1024   # raw bytes per runner fetch frame (base64 stays under the socket's 1 MB frame limit)


def sniff(data: bytes, filename: str) -> tuple[str, str]:
    """(kind, mime) from the content, or BadRequest naming what is accepted."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image", "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image", "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image", "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image", "image/webp"
    if data[:5] == b"%PDF-":
        return "pdf", "application/pdf"
    ext = PurePosixPath(filename).suffix.lower()
    if ext in TEXT_EXT:
        if b"\x00" in data:
            raise BadRequest("that file isn't text")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            raise BadRequest("that text file isn't UTF-8") from None
        return "text", "text/markdown" if ext in (".md", ".markdown") else "text/plain"
    raise BadRequest("I can take png, jpeg, webp or gif images, PDFs, and .md or .txt files")


def clean_name(name: str) -> str:
    base = PurePosixPath(str(name or "").replace("\\", "/")).name
    base = re.sub(r"[\x00-\x1f\x7f\"<>|:*?]", "", base).strip().lstrip(".")
    return (base or "file")[:120]


def _root() -> Path:
    return Path(config.CA_UPLOADS_DIR)


def _abs(rel: str) -> Path:
    """The file for a stored relative path, refusing anything that would leave the uploads folder."""
    root = _root().resolve()
    p = (root / rel).resolve()
    if root not in p.parents:
        raise NotFound("file not found")
    return p


def _unlink(rel: str | None) -> None:
    if not rel:
        return
    try:
        _abs(rel).unlink()
    except (FileNotFoundError, NotFound):
        pass
    except OSError:
        log.warning("ca upload: could not delete a stored file")   # never the name or content


def store(user_id: int, conversation_id: int, filename: str, data: bytes) -> dict:
    """Validate and keep one upload against the person's own conversation. -> metadata (no bytes)."""
    if not data:
        raise BadRequest("that file is empty")
    if len(data) > MAX_UPLOAD:
        raise BadRequest(f"a file is at most {MAX_UPLOAD // (1024 * 1024)} MB")
    name = clean_name(filename)
    kind, mime = sniff(data, name)
    if len(data) > MAX_BYTES[kind]:
        raise BadRequest("a text file is at most 1 MB" if kind == "text" else "an image or PDF is at most 10 MB")
    with get_cursor() as cur:
        from app import ca   # the conversation's own access rule: a task's discussion is shared by the people who may edit the task
        conv = ca._conv(cur, user_id, conversation_id, write=True, lock=True)
        if conv["archived_at"]:   # it could never be sent (review 727 #4)
            raise BadRequest("that conversation is archived")
        cur.execute("SELECT count(*) AS n FROM ca_attachments WHERE conversation_id = %s AND user_id = %s AND message_id IS NULL",
                    (conversation_id, user_id))
        if cur.fetchone()["n"] >= MAX_PER_MESSAGE:
            raise BadRequest(f"at most {MAX_PER_MESSAGE} files go with one message")
        cur.execute("SELECT COALESCE(sum(size), 0) AS b FROM ca_attachments WHERE user_id = %s AND path IS NOT NULL", (user_id,))
        if cur.fetchone()["b"] + len(data) > config.CA_UPLOAD_USER_QUOTA_MB * 1024 * 1024:   # review 727 #2: no disk fill
            raise BadRequest(f"you have {config.CA_UPLOAD_USER_QUOTA_MB} MB of files on the server already: "
                             "remove some, or wait for closed tasks to clear them")
        cur.execute("INSERT INTO ca_attachments (conversation_id, user_id, filename, mime, kind, size) VALUES (%s, %s, %s, %s, %s, %s) "
                    "RETURNING id, filename, mime, kind, size", (conversation_id, user_id, name, mime, kind, len(data)))
        row = dict(cur.fetchone())
        rel = f"{user_id}/{conversation_id}/{row['id']}-{secrets.token_hex(4)}.bin"    # the person's file name is never a path
        target = _root() / rel
        try:
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            tmp = target.with_suffix(".part")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, target)
        except OSError:
            log.exception("ca upload: could not write the file")
            raise BadRequest("I couldn't store that file on the server") from None   # the row rolls back with the transaction
        cur.execute("UPDATE ca_attachments SET path = %s WHERE id = %s", (rel, row["id"]))
        return row


def fetch(user_id: int, attachment_id: int) -> dict:
    """The file with its bytes, only for the person who owns it; anyone else gets NotFound (a 404). A stripped file is gone too."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT id, conversation_id, user_id, message_id, filename, mime, kind, size, path FROM ca_attachments WHERE id = %s", (attachment_id,))
        row = cur.fetchone()
        if row and row["user_id"] != user_id and row["path"]:   # another person's file: only once SENT, inside a task discussion you can view
            from app import ca
            try:
                ca._conv(cur, user_id, row["conversation_id"])
            except NotFound:
                row = None
            if row and row["message_id"] is None:
                row = None
    if not row or not row["path"]:
        raise NotFound(f"file {attachment_id} not found")
    row = dict(row)
    row.pop("user_id", None)
    row.pop("message_id", None)
    try:
        row["data"] = _abs(row.pop("path")).read_bytes()
    except OSError:
        raise NotFound(f"file {attachment_id} not found") from None
    return row


def discard(user_id: int, attachment_id: int) -> None:
    """Remove a file that is not yet sent (the chip's x). A sent file stays with its message until the sweep strips it."""
    with get_cursor() as cur:
        cur.execute("DELETE FROM ca_attachments WHERE id = %s AND user_id = %s AND message_id IS NULL RETURNING path", (attachment_id, user_id))
        row = cur.fetchone()
        if not row:
            raise NotFound(f"file {attachment_id} not found")
    _unlink(row["path"])


def claim_for_message(cur, user_id: int, conversation_id: int, message_id: int, ids) -> list[dict]:
    """Attach the (still unsent) uploads `ids` to a message. Every id must be this person's, in this conversation, unsent:
    otherwise BadRequest and nothing is sent."""
    ids = [i for i in (ids if isinstance(ids, list) else []) if isinstance(i, int) and not isinstance(i, bool)]
    if not ids:
        return []
    if len(set(ids)) > MAX_PER_MESSAGE:
        raise BadRequest(f"at most {MAX_PER_MESSAGE} files go with one message")
    cur.execute("UPDATE ca_attachments SET message_id = %s WHERE id = ANY(%s) AND user_id = %s AND conversation_id = %s AND message_id IS NULL "
                "AND path IS NOT NULL RETURNING id, filename, mime, kind, size", (message_id, list(set(ids)), user_id, conversation_id))
    rows = [dict(r) for r in cur.fetchall()]
    if len(rows) != len(set(ids)):
        raise BadRequest("one of the attached files is gone: attach it again")
    return sorted(rows, key=lambda r: ids.index(r["id"]))


def for_message(cur, message_id: int) -> list[dict]:
    cur.execute("SELECT id, filename, mime, kind, size FROM ca_attachments WHERE message_id = %s AND path IS NOT NULL ORDER BY id", (message_id,))
    return [dict(r) for r in cur.fetchall()]


def chunk_for_turn(device_id: int, turn_id: int, fence: int, attachment_id: int, part: int) -> dict:
    """One slice (base64) of a file attached to the message this running turn answers, for the runner that holds the turn."""
    import base64
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT t.user_message_id FROM ca_turns t WHERE t.id = %s AND t.state = 'running' AND t.fence = %s AND t.device_id = %s",
                    (turn_id, fence, device_id))
        t = cur.fetchone()
        if not t:
            raise NotFound("turn is not running")
        cur.execute("SELECT path, size FROM ca_attachments WHERE id = %s AND message_id = %s AND path IS NOT NULL",
                    (attachment_id, t["user_message_id"]))
        row = cur.fetchone()
    if not row:
        raise NotFound("file not found")
    try:
        with open(_abs(row["path"]), "rb") as fh:
            fh.seek(part * CHUNK)
            piece = fh.read(CHUNK)
    except OSError:
        raise NotFound("file not found") from None
    return {"data": base64.b64encode(piece).decode("ascii"), "last": (part + 1) * CHUNK >= row["size"]}


REASON_TASK = "removed after task closed"
REASON_IDLE = "removed after {days} days idle"

_STRIP_SQL = """
SELECT a.id, a.path, (t.id IS NOT NULL) AS task_closed
FROM ca_attachments a
JOIN ca_conversations c ON c.id = a.conversation_id
LEFT JOIN tasks t ON t.id = CASE WHEN c.entity_kind = 'task' AND c.entity_id ~ '^[0-9]+$' THEN c.entity_id::int END
WHERE a.removed_at IS NULL AND a.message_id IS NOT NULL AND (
        (t.id IS NOT NULL AND t.status = 'done' AND t.done_at < now() - make_interval(days => %(closed)s))
     OR (t.id IS NULL AND c.updated_at < now() - make_interval(days => %(idle)s)))
ORDER BY a.id
LIMIT 500
"""


def sweep() -> dict:
    """The scheduler's pass (idempotent; logs counts only, never names or content):
      1. an upload attached but never sent, older than a day: deleted outright;
      2. a SENT file on a chat linked to a task that has been done for more than CA_UPLOAD_TASK_CLOSED_DAYS (30) days (a task reopened
         since is not done, so its files stay), or on a chat with no live task link idle for CA_UPLOAD_IDLE_DAYS (90): the file is deleted
         and the row kept as a stub on the message, so the transcript still reads. A chat linked to an OPEN task is never stripped."""
    out = {"orphans": 0, "stripped": 0}
    with get_cursor() as cur:
        cur.execute("DELETE FROM ca_attachments WHERE message_id IS NULL AND created_at < now() - make_interval(hours => %s) RETURNING path", (ORPHAN_HOURS,))
        gone = cur.fetchall()
    for r in gone:
        _unlink(r["path"])
    out["orphans"] = len(gone)
    with get_cursor() as cur:
        cur.execute(_STRIP_SQL, {"closed": config.CA_UPLOAD_TASK_CLOSED_DAYS, "idle": config.CA_UPLOAD_IDLE_DAYS})
        rows = cur.fetchall()
        for r in rows:
            reason = REASON_TASK if r["task_closed"] else REASON_IDLE.format(days=config.CA_UPLOAD_IDLE_DAYS)
            cur.execute("UPDATE ca_attachments SET path = NULL, removed_at = now(), removed_reason = %s WHERE id = %s AND removed_at IS NULL", (reason, r["id"]))
    for r in rows:
        _unlink(r["path"])
    out["stripped"] = len(rows)
    if out["orphans"] or out["stripped"]:
        log.info("ca uploads swept: %d unsent deleted, %d stripped", out["orphans"], out["stripped"])
    return out
