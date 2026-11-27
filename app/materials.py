"""Materials (task #295, owner 2026-10-13): shared resources stored per account or project and inherited down the
tree, so AI sessions building any app use the real logo, colours and templates instead of generic ones.

- Files live in a plain folder (config.MATERIALS_DIR, bind-mounted from the sessions server at config.MATERIALS_HOST_DIR) as
  <project-slug>/<slot>/v<N>-<filename>; the `materials` table (migration 0025) says what each one is FOR.
- A project's effective set = its own current materials plus every ancestor's UP TO ITS ACCOUNT (task #368: never
  from above the account, i.e. never from the dashboard root, never across accounts; tree.inherit_chain); the
  NEAREST node wins per slot, so a sub-app can override `logo.primary` and inherit the rest.
- Replacing a slot keeps the old version (row + file) and links it to the new one.
- Dev-time only: apps vendor their copies at build time and never call the dashboard at runtime. Secrets are refused.
"""
from __future__ import annotations

import hashlib
import mimetypes
import re
from pathlib import Path

from app import config, tree
from app.db import get_cursor
from app.repo import BadRequest, NotFound, record_activity

KINDS = ("logo", "icon", "image", "email-template", "document-template", "font", "style", "other")
KIND_LABEL = {"logo": "Logo", "icon": "Icon", "image": "Image", "email-template": "Email template",
              "document-template": "Document template", "font": "Font", "style": "Style", "other": "Other"}
SLOT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
# What may be stored: brand files, templates, fonts, styles, documents. Anything else is refused.
ALLOWED_EXT = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp", "gif": "image/gif",
    "svg": "image/svg+xml", "ico": "image/x-icon",
    "html": "text/html", "htm": "text/html", "txt": "text/plain", "md": "text/markdown", "css": "text/css",
    "json": "application/json", "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "woff": "font/woff", "woff2": "font/woff2", "ttf": "font/ttf", "otf": "font/otf",
}
IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif", "image/svg+xml", "image/x-icon"}
_SECRET_NAME = re.compile(r"(^|[._-])(env|pem|key|p12|pfx|kdbx)$|id_(rsa|ed25519|ecdsa)|secret|token|credential",
                          re.I)
_SECRET_BODY = (b"PRIVATE KEY-----", b"BEGIN OPENSSH PRIVATE", b"aws_secret_access_key", b"xoxb-", b"ghp_",
                b"sk-ant-")


def looks_like_secret(text: str | bytes) -> bool:
    """Does this text carry a private-key block or a well-known token prefix? The body half of check_upload's secret test,
    shared with the session handoffs and shared docs (#908), which also refuse anything that looks like a secret."""
    raw = text.encode("utf-8", "replace") if isinstance(text, str) else text
    return any(m in raw for m in _SECRET_BODY)


def _root() -> Path:
    return Path(config.MATERIALS_DIR)


def safe_filename(name: str) -> str:
    base = Path(name or "").name.strip()
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip(".-")[:100]
    if not base or "." not in base:
        raise BadRequest("the file needs a name with an extension, e.g. logo.svg")
    return base


def check_upload(filename: str, raw: bytes) -> str:
    """Validate a file and return its content type, or raise BadRequest with a plain reason."""
    if not raw:
        raise BadRequest("the file is empty")
    if len(raw) > config.MATERIALS_MAX_BYTES:
        raise BadRequest(f"the file is over {config.MATERIALS_MAX_BYTES // (1024 * 1024)} MB")
    ext = filename.rsplit(".", 1)[-1].lower()
    if ext not in ALLOWED_EXT:
        raise BadRequest(f".{ext} files can't be stored as materials (logos, templates, fonts, styles and "
                         "documents only)")
    if _SECRET_NAME.search(filename) or looks_like_secret(raw):
        raise BadRequest("that looks like a secret or key: materials are shared with every session, so secrets "
                         "never go here")
    return ALLOWED_EXT[ext]


def _clean_text(value: str | None, what: str, limit: int, required: bool = True) -> str:
    v = (value or "").strip()
    if required and not v:
        raise BadRequest(f"{what} is required")
    if len(v) > limit:
        raise BadRequest(f"{what} is over {limit} characters")
    return v


def _with_paths(row: dict) -> dict:
    row = dict(row)
    row["host_path"] = str(Path(config.MATERIALS_HOST_DIR) / row["rel_path"])
    row["url"] = f"/media/materials/{row['id']}/{row['filename']}"
    row["is_image"] = row["content_type"] in IMAGE_TYPES
    row["kind_label"] = KIND_LABEL.get(row["kind"], row["kind"])
    return row


def add(project: dict, slot: str, kind: str, title: str, purpose: str, filename: str, raw: bytes, actor: str,
        usage_notes: str = "") -> dict:
    """Store a file in `slot` on this node. If the node already has a current one there, it becomes the previous
    version (kept, linked) and this is version N+1."""
    slot = (slot or "").strip().lower()
    if not SLOT_RE.match(slot):
        raise BadRequest("the slot is a short key like logo.primary or email.signature (a-z, 0-9, . _ -)")
    if kind not in KINDS:
        raise BadRequest(f"kind must be one of: {', '.join(KINDS)}")
    title = _clean_text(title, "title", 120)
    purpose = _clean_text(purpose, "what it's for", 500)
    usage_notes = _clean_text(usage_notes, "usage notes", 4000, required=False)
    filename = safe_filename(filename)
    content_type = check_upload(filename, raw)
    sha = hashlib.sha256(raw).hexdigest()
    with get_cursor() as cur:
        cur.execute("SELECT * FROM materials WHERE project_id = %s AND slot = %s AND replaced_at IS NULL FOR UPDATE",
                    (project["id"], slot))
        prev = cur.fetchone()
        if prev and prev["sha256"] == sha and prev["title"] == title and prev["purpose"] == purpose \
                and prev["usage_notes"] == usage_notes and prev["kind"] == kind:
            return _with_paths(prev)  # an identical re-upload is a no-op
        version = (prev["version"] + 1) if prev else 1
        rel = f"{project['slug']}/{slot}/v{version}-{filename}"
        dest = _root() / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        tmp.write_bytes(raw)
        tmp.chmod(0o644)
        tmp.replace(dest)
        if prev:
            cur.execute("UPDATE materials SET replaced_at = now() WHERE id = %s", (prev["id"],))
        cur.execute(
            "INSERT INTO materials (project_id, slot, kind, title, purpose, usage_notes, filename, rel_path, "
            "content_type, bytes, sha256, version, created_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
            (project["id"], slot, kind, title, purpose, usage_notes, filename, rel, content_type, len(raw), sha,
             version, actor))
        row = cur.fetchone()
        if prev:
            cur.execute("UPDATE materials SET replaced_by = %s WHERE id = %s", (row["id"], prev["id"]))
        record_activity(cur, project["id"], actor, "replaced" if prev else "added", "material", row["id"],
                        f"Material {slot} ({title}) v{version}")
    return _with_paths(row)


def get(material_id: int) -> dict:
    with get_cursor() as cur:
        cur.execute("SELECT * FROM materials WHERE id = %s", (material_id,))
        row = cur.fetchone()
    if not row:
        raise NotFound(f"material {material_id} not found")
    return _with_paths(row)


def read_bytes(row: dict) -> bytes:
    path = (_root() / row["rel_path"]).resolve()
    if _root().resolve() not in path.parents:
        raise NotFound("no such file")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise NotFound("the file is missing from the materials folder") from exc


def own(project_id: int, include_old: bool = False) -> list[dict]:
    with get_cursor() as cur:
        cur.execute("SELECT * FROM materials WHERE project_id = %s" + ("" if include_old else
                    " AND replaced_at IS NULL") + " ORDER BY slot, version DESC", (project_id,))
        return [_with_paths(r) for r in cur.fetchall()]


def effective(project: dict, idx: dict | None = None) -> list[dict]:
    """The project's own current materials plus every ancestor's up to its account (tree.inherit_chain), nearest
    winning per slot. Each row gains `from` (the node it's kept on) and `inherited` (True when that's an ancestor)."""
    idx = idx if idx is not None else tree.index()
    chain = tree.inherit_chain(project, idx)
    seen: dict[str, dict] = {}
    for depth, node in enumerate(chain):
        for row in own(node["id"]):
            if row["slot"] not in seen:
                row["from"] = {"id": node["id"], "slug": node["slug"], "name": node["name"]}
                row["inherited"] = depth > 0
                seen[row["slot"]] = row
    return sorted(seen.values(), key=lambda r: (r["inherited"], *_order(r)))


def _order(row: dict) -> tuple:
    """Logos first, then icons, templates, …; within a kind, slots in natural order (favicon.16 before favicon.180)."""
    natural = tuple(int(t) if t.isdigit() else t for t in re.split(r"(\d+)", row["slot"]))
    kind_rank = KINDS.index(row["kind"]) if row["kind"] in KINDS else len(KINDS)
    return kind_rank, tuple((0, t) if isinstance(t, int) else (1, t) for t in natural)


def overridden(project: dict, idx: dict | None = None) -> list[dict]:
    """Ancestors' materials this node hides with its own version of the same slot (shown so the override is visible)."""
    idx = idx if idx is not None else tree.index()
    mine = {r["slot"] for r in own(project["id"])}
    out = []
    for node in tree.inherit_chain(project, idx)[1:]:
        for row in own(node["id"]):
            if row["slot"] in mine:
                row["from"] = {"id": node["id"], "slug": node["slug"], "name": node["name"]}
                out.append(row)
    return out


def guess_type(filename: str) -> str:
    return ALLOWED_EXT.get(filename.rsplit(".", 1)[-1].lower()) or mimetypes.guess_type(filename)[0] or ""
