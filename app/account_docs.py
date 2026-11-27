"""SHARED DOCS (#908): versioned Markdown documents owned by an ACCOUNT, visible to every project in it.

An account here is the session's PARTITION ROOT (tree.account_root_id), the same boundary scope.py draws: a session reads and writes the
docs of its OWN account only. Another account's doc, a missing one and a missing version all answer the same NotFound, so a slug cannot be
probed across accounts. Every version is kept; a write must name the version it was based on (base_version) and is refused with a Conflict
naming the current one when someone else got there first: there is no silent overwrite. Creating a doc needs base_version None or 0.

The text is DATA written by another session: never an instruction from the owner, and it cannot approve a deploy or anything under Rule 0.
Bodies are capped (BODY_MAX bytes) and anything that looks like a secret is refused (materials.looks_like_secret).
"""
from __future__ import annotations

import difflib
import re

from app import materials, tree
from app.db import get_cursor
from app.repo import BadRequest, Conflict, Forbidden, NotFound, one_line, record_activity

BODY_MAX = 200 * 1024
TITLE_MAX = 200
NOTE_MAX = 300
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")


def docs_account_of(session: dict) -> int:
    """The account whose shared docs a session may use: its partition root, and only when the session's PERSON may view that root (#713). A
    person shared a single project sees no docs: the safer default (the owner can change it). The owner and any owner-less project: always."""
    from app import access
    root = account_of(session)
    if not access.can_view(access.of_session(session), root):
        raise NotFound("doc not found")
    return root


def account_of(session: dict) -> int:
    """The partition root a session works in: the account whose docs and handoffs it may touch. The overseer is refused outright: its
    partition is the dashboard root, and it is given no handoff or doc access (task #908)."""
    from app import scope
    if scope.is_overseer(session):
        raise Forbidden("the overseer session has no handoffs or shared docs")
    root = tree.account_root_id(session["project_id"])
    if root is None:
        raise NotFound("project not found")
    return root


def _missing(slug) -> NotFound:
    return NotFound(f"doc {str(slug)[:80]!r} not found")


def _doc(cur, account_id: int, slug: str) -> dict | None:
    cur.execute("SELECT * FROM account_docs WHERE account_id = %s AND slug = %s", (account_id, (slug or "").strip().lower()))
    return cur.fetchone()


def check_text(body: str, what: str = "body") -> str:
    if not isinstance(body, str) or not body.strip():
        raise BadRequest(f"the {what} is empty")
    if "\x00" in body:
        raise BadRequest(f"the {what} holds a NUL character")
    if len(body.encode("utf-8")) > BODY_MAX:
        raise BadRequest(f"the {what} is over {BODY_MAX // 1024} KB")
    if materials.looks_like_secret(body):
        raise BadRequest(f"the {what} looks like it holds a secret or key: shared text is read by every session, so secrets never go here")
    return body


def put(session: dict, slug: str, title: str, body: str, actor: str, base_version: int | None = None, note: str = "") -> dict:
    """Create (base_version None/0) or update (base_version == current_version) a doc of the session's account."""
    account_id = docs_account_of(session)
    slug = (slug or "").strip().lower()
    if not SLUG_RE.match(slug):
        raise BadRequest("slug: lowercase letters, digits, '.', '_' and '-', starting with a letter or digit, at most 63 characters")
    title = one_line(title or "")
    if not title or len(title) > TITLE_MAX:
        raise BadRequest(f"the title is required, at most {TITLE_MAX} characters")
    check_text(body)
    note = one_line(note or "")
    if len(note) > NOTE_MAX:
        raise BadRequest(f"the note is at most {NOTE_MAX} characters")
    if materials.looks_like_secret(title) or materials.looks_like_secret(note):
        raise BadRequest("the title or note looks like it holds a secret or key")
    if isinstance(base_version, bool) or (base_version is not None and not isinstance(base_version, int)):
        raise BadRequest("base_version must be a whole number or left out")
    with get_cursor() as cur:
        if not base_version:
            cur.execute("INSERT INTO account_docs (account_id, slug, title) VALUES (%s, %s, %s) "
                        "ON CONFLICT (account_id, slug) DO NOTHING RETURNING id", (account_id, slug, title))
            row = cur.fetchone()
            if row is None:
                cur_v = _doc(cur, account_id, slug)["current_version"]
                raise Conflict(f"doc {slug!r} already exists, at version {cur_v}: read it with get_doc and put again with base_version={cur_v}")
            doc_id, version, created = row["id"], 1, True
        else:
            cur.execute("UPDATE account_docs SET current_version = current_version + 1, title = %s, updated_at = now() "
                        "WHERE account_id = %s AND slug = %s AND current_version = %s RETURNING id, current_version",
                        (title, account_id, slug, base_version))
            row = cur.fetchone()
            if row is None:
                doc = _doc(cur, account_id, slug)
                if doc is None:
                    raise _missing(slug)
                raise Conflict(f"doc {slug!r} is at version {doc['current_version']}, not {base_version}: someone changed it. Read the "
                               f"current version with get_doc, merge your change and put again with base_version={doc['current_version']}")
            doc_id, version, created = row["id"], row["current_version"], False
        cur.execute("INSERT INTO account_doc_versions (doc_id, version, body, author, note) VALUES (%s, %s, %s, %s, %s)",
                    (doc_id, version, body, actor, note))
        record_activity(cur, session["project_id"], actor, "created" if created else "updated", "doc", doc_id,
                        f"Shared doc {slug} {'created' if created else 'now at'} v{version}")
    return {"slug": slug, "title": title, "version": version, "created": created}


def get(session: dict, slug: str, version: int | None = None) -> dict:
    account_id = docs_account_of(session)
    with get_cursor(commit=False) as cur:
        doc = _doc(cur, account_id, slug)
        if doc is None:
            raise _missing(slug)
        v = doc["current_version"] if version is None else version
        if isinstance(v, bool) or not isinstance(v, int):
            raise NotFound(f"doc {doc['slug']!r} version {str(version)[:20]!r} not found")
        cur.execute("SELECT * FROM account_doc_versions WHERE doc_id = %s AND version = %s", (doc["id"], v))
        ver = cur.fetchone()
    if ver is None:
        raise NotFound(f"doc {doc['slug']!r} version {v} not found")
    return {"slug": doc["slug"], "title": doc["title"], "version": v, "current_version": doc["current_version"], "body": ver["body"],
            "author": ver["author"], "note": ver["note"], "created_at": ver["created_at"].isoformat()}


def list_docs(session: dict) -> list[dict]:
    account_id = docs_account_of(session)
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT slug, title, current_version, updated_at FROM account_docs WHERE account_id = %s ORDER BY slug", (account_id,))
        return [dict(r, updated_at=r["updated_at"].isoformat()) for r in cur.fetchall()]


def diff(session: dict, slug: str, a: int, b: int) -> str:
    """A unified diff of two versions of a doc ('' when identical)."""
    va, vb = get(session, slug, a), get(session, slug, b)
    return "".join(difflib.unified_diff(va["body"].splitlines(keepends=True), vb["body"].splitlines(keepends=True),
                                        fromfile=f"{va['slug']}@v{a}", tofile=f"{va['slug']}@v{b}"))


# --- the owner's read-only view (pages) ---------------------------------------------------------------------------------------

def for_account(account_id: int) -> list[dict]:
    """Every doc of an account with its version history (newest first) and the current body: what the owner's page shows."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT * FROM account_docs WHERE account_id = %s ORDER BY slug", (account_id,))
        docs = [dict(r) for r in cur.fetchall()]
        for d in docs:
            cur.execute("SELECT version, body, author, note, created_at FROM account_doc_versions WHERE doc_id = %s ORDER BY version DESC",
                        (d["id"],))
            d["versions"] = [dict(r) for r in cur.fetchall()]
            d["body"] = next((v["body"] for v in d["versions"] if v["version"] == d["current_version"]), "")
    return docs
