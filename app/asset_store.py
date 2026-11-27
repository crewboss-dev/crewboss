"""The Assets store (task #778, P1): create, new_version, get, list, versions, plus the small writers the resolver's tests and
later phases need (archive, restore, share, pin). No UI, nothing existing reads it yet (P2 adds the resolver, P3 the import).

Model (migration 0081): an `assets` row is the identity (type, owner, key) and points at its live version; every save is a new
immutable `asset_versions` row with a content hash (app/asset_hash.py). Saving identical content is a no-op. A file is a
content-addressed `asset_blobs` row on disk under config.ASSETS_DIR; a version lists its files in `asset_files`.

Rights are NOT checked here: callers (routes, CA tools, jobs) check them first (P7's app/asset_access.py). This module checks
shape: the type's schema, the scope, partitions, locked-rule pins.
"""
from __future__ import annotations

import re
import secrets
from contextlib import contextmanager
from pathlib import Path

import psycopg
import psycopg.types.json

from app import asset_hash, asset_types, config, materials
from app.db import get_cursor
from app.repo import BadRequest, Conflict, NotFound

# A skill's attachment is refused by NAME only when it looks like a key or credential file: a design skill's `tokens.css` is a plain
# file, so the materials rule (any name containing "token") is too broad here. The file's own text is checked as well.
_KEYFILE_NAME = re.compile(r"(^|[._-])(env|pem|key|p12|pfx|kdbx)$|^id_(rsa|ed25519|ecdsa)|^\.?(secrets?|credentials?)(\.|$)", re.I)
PATH_RE = re.compile(r"^[A-Za-z0-9._-][A-Za-z0-9._/-]{0,199}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
VIAS = ("web", "ca", "job", "import", "builtin", "repo")


class StaleVersion(Conflict):
    """The caller edited a version that is no longer the live one (API: 409 with the diff)."""

    def __init__(self, current_version_id: int, current_n: int):
        super().__init__(f"changed since you opened it (now v{current_n})")
        self.current_version_id, self.current_n = current_version_id, current_n


@contextmanager
def _cursor(cur):
    if cur is not None:
        yield cur
    else:
        with get_cursor() as c:
            yield c


# --- files ----------------------------------------------------------------------------------------------------------------

def _root() -> Path:
    return Path(config.ASSETS_DIR)


def put_blob(cur, raw: bytes, content_type: str, rel_path: str | None = None) -> str:
    """Store `raw` once, content-addressed, and return its sha256. The file is written to a .part and renamed into place
    BEFORE the row is inserted, so a row never points at a missing file. An existing blob (same hash) is reused."""
    import hashlib
    sha = hashlib.sha256(raw).hexdigest()
    cur.execute("SELECT 1 FROM asset_blobs WHERE sha256 = %s", (sha,))
    if cur.fetchone():
        return sha
    rel = rel_path or f"_blobs/{sha[:2]}/{sha}"
    if ".." in rel.split("/") or rel.startswith("/"):
        raise BadRequest("bad blob path")
    dest = _root() / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    tmp.write_bytes(raw)
    tmp.chmod(0o644)
    tmp.replace(dest)
    try:
        cur.execute("INSERT INTO asset_blobs (sha256, rel_path, bytes, content_type) VALUES (%s, %s, %s, %s)",
                    (sha, rel, len(raw), content_type))
    except psycopg.errors.UniqueViolation:
        raise BadRequest(f"a different file is already stored at {rel}") from None
    return sha


def read_rel(rel_path: str) -> bytes:
    """The bytes of a stored file by its recorded path; never a path outside the assets folder."""
    path = (_root() / rel_path).resolve()
    if _root().resolve() not in path.parents:
        raise NotFound("no such file")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise NotFound("the file is missing from the assets folder") from exc


def read_blob(sha256: str) -> bytes:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT rel_path FROM asset_blobs WHERE sha256 = %s", (sha256,))
        row = cur.fetchone()
    if not row:
        raise NotFound("no such file")
    return read_rel(row["rel_path"])


def _check_file(t: asset_types.AssetType, path: str, raw: bytes) -> str:
    """Content type of an upload. A material's single file follows the existing materials rules (extension allow-list, size
    cap, secrets refused); a skill's attachments are small reference files and scripts: size cap and secrets refused."""
    if t.file_policy == "one":
        return materials.check_upload(path.rsplit("/", 1)[-1], raw)
    if not raw:
        raise BadRequest(f"{path} is empty")
    if len(raw) > config.ASSET_ATTACHMENT_MAX_BYTES:
        raise BadRequest(f"{path} is over {config.ASSET_ATTACHMENT_MAX_BYTES // 1024} KB")
    if _KEYFILE_NAME.search(path.rsplit("/", 1)[-1]) or any(m in raw[:200_000] for m in materials._SECRET_BODY):
        raise BadRequest(f"{path} looks like a secret or key; secrets never go in an asset")
    return materials.guess_type(path) or "application/octet-stream"


def _prepare_files(cur, t: asset_types.AssetType, files) -> list[tuple[str, str]]:
    """[{"path", "raw"} | {"path", "sha256"}] -> [(path, sha256)], blobs stored. A `sha256` entry must already be stored."""
    files = list(files or [])
    t.check_file_count(len(files))
    out, seen = [], set()
    for f in files:
        path = f.get("path", "")
        if not PATH_RE.match(path) or ".." in path:
            raise BadRequest(f"bad file path {path!r}")
        if path in seen:
            raise BadRequest(f"{path} is listed twice")
        seen.add(path)
        if f.get("raw") is not None:
            ctype = _check_file(t, path, f["raw"])
            sha = put_blob(cur, f["raw"], ctype, f.get("rel_path"))
        else:
            sha = f.get("sha256", "")
            cur.execute("SELECT 1 FROM asset_blobs WHERE sha256 = %s", (sha,))
            if not SHA_RE.match(sha) or not cur.fetchone():
                raise BadRequest(f"{path}: that file is not stored")
        out.append((path, sha))
    for a in seen:
        for b in seen:
            if b.startswith(a + "/"):
                raise BadRequest(f"{a} is a file and also a folder holding {b}")
    return out


# --- rows -------------------------------------------------------------------------------------------------------------------

def _file_rows(cur, version_id: int) -> list[dict]:
    cur.execute("SELECT f.path, f.sha256, b.bytes, b.content_type, b.rel_path FROM asset_files f "
                "JOIN asset_blobs b ON b.sha256 = f.sha256 WHERE f.version_id = %s ORDER BY f.path", (version_id,))
    return cur.fetchall()


def _insert_version(cur, asset: dict, t: asset_types.AssetType, n: int, fields: dict, body: str, files, note, actor, via,
                    user_id, source_job_id, source_commit, approved_user_id) -> dict:
    if via not in VIAS:
        raise BadRequest(f"created_via must be one of {', '.join(VIAS)}")
    h = asset_hash.content_hash(t.key, t.schema_version, asset["key"], fields, body, files)
    cur.execute(
        "INSERT INTO asset_versions (asset_id, n, schema_version, fields, body, content_hash, note, created_via, "
        "source_job_id, source_commit, approved_user_id, created_by, created_user_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *",
        (asset["id"], n, t.schema_version, psycopg.types.json.Jsonb(fields), body or "", h, note or "", via,
         source_job_id, source_commit, approved_user_id, actor, user_id))
    v = cur.fetchone()
    for path, sha in files:
        cur.execute("INSERT INTO asset_files (version_id, path, sha256) VALUES (%s, %s, %s)", (v["id"], path, sha))
    return v


def _with_files(cur, v: dict) -> dict:
    v = dict(v)
    v["files"] = _file_rows(cur, v["id"])
    return v


def _project(cur, project_id) -> dict:
    cur.execute("SELECT id, slug, kind, partition_id FROM projects WHERE id = %s", (project_id,))
    row = cur.fetchone()
    if not row:
        raise NotFound(f"project {project_id} not found")
    return row


def _asset_row(cur, ref, lock: bool = False) -> dict:
    col = "uid" if isinstance(ref, str) else "id"
    cur.execute(f"SELECT * FROM assets WHERE {col} = %s" + (" FOR UPDATE" if lock else ""), (ref,))
    row = cur.fetchone()
    if not row:
        raise NotFound("no such asset")
    return row


def _full(cur, a: dict, n: int | None = None) -> dict:
    a = dict(a)
    if n is None:
        vid = a["current_version_id"]
        cur.execute("SELECT * FROM asset_versions WHERE id = %s", (vid,))
    else:
        cur.execute("SELECT * FROM asset_versions WHERE asset_id = %s AND n = %s", (a["id"], n))
    v = cur.fetchone()
    if not v:
        raise NotFound("no such version")
    a["version"] = _with_files(cur, v)
    cur.execute("SELECT tag FROM asset_tags WHERE asset_id = %s ORDER BY tag", (a["id"],))
    a["tags"] = [r["tag"] for r in cur.fetchall()]
    return a


# --- create / new version ---------------------------------------------------------------------------------------------------

def create(type_: str, key: str, name: str, owner_scope: str, owner_project_id: int | None, fields: dict, body: str = "",
           files=None, *, summary: str = "", origin: str = "authored", share_all: bool = False, tags=(), actor: str,
           via: str = "web", note: str = "", user_id: int | None = None, source_job_id: int | None = None,
           source_commit: str | None = None, approved_user_id: int | None = None, cur=None) -> dict:
    """Create an asset and its version 1 in one transaction; returns the asset with its `version`, `tags`."""
    t = asset_types.get(type_)
    t.check_key(key)
    fields = t.check_fields(fields or {})
    body = t.check_body(body)
    name = " ".join((name or "").split())
    if not 1 <= len(name) <= 120:
        raise BadRequest("the name is 1 to 120 characters")
    if len(summary or "") > 500:
        raise BadRequest("the summary is over 500 characters")
    if owner_scope not in ("instance", "account", "project"):
        raise BadRequest("owner_scope must be instance, account or project")
    if origin not in ("authored", "builtin", "repo", "import"):
        raise BadRequest("bad origin")
    if (origin == "builtin" and owner_scope != "instance") or (origin == "repo" and owner_scope != "project"):
        raise BadRequest("a built-in is an instance asset and a repo mirror is a project asset")
    for tag in tags:
        if not re.match(r"^[a-z0-9][a-z0-9-]{0,31}$", tag):
            raise BadRequest(f"bad tag {tag!r}")
    with _cursor(cur) as c:
        account_id = None
        if owner_scope == "instance":
            if owner_project_id is not None:
                raise BadRequest("an instance asset has no owner project")
        else:
            p = _project(c, owner_project_id)
            if owner_scope == "account" and p["kind"] != "account":
                raise BadRequest("an account asset belongs to an account")
            if owner_scope == "project" and p["kind"] == "account":
                raise BadRequest("an account's own assets are account assets")
            account_id = p["partition_id"]
        c.execute("SELECT 1 FROM assets WHERE type = %s AND COALESCE(owner_project_id, 0) = COALESCE(%s, 0) AND key = %s "
                  "AND (origin = 'builtin') = %s AND (origin = 'repo') = %s",
                  (type_, owner_project_id, key, origin == "builtin", origin == "repo"))
        if c.fetchone():
            raise BadRequest(f"there is already a {t.noun} called {key} here")
        c.execute(
            "INSERT INTO assets (uid, type, key, name, summary, owner_scope, owner_project_id, account_id, origin, "
            "share_all, locked, created_by, created_user_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "RETURNING *", (secrets.token_urlsafe(9), type_, key, name, summary or "", owner_scope, owner_project_id,
                            account_id, origin, bool(share_all), bool(fields.get("locked")), actor, user_id))
        a = c.fetchone()
        pairs = _prepare_files(c, t, files)
        v = _insert_version(c, a, t, 1, fields, body, pairs, note, actor, via, user_id, source_job_id, source_commit,
                            approved_user_id)
        c.execute("UPDATE assets SET current_version_id = %s WHERE id = %s RETURNING *", (v["id"], a["id"]))
        a = c.fetchone()
        for tag in sorted(set(tags)):
            c.execute("INSERT INTO asset_tags (asset_id, tag) VALUES (%s, %s)", (a["id"], tag))
        return _full(c, a)


def new_version(ref, fields: dict, body: str = "", files="keep", *, base_version_id: int | None = None, note: str = "",
                actor: str, via: str = "web", user_id: int | None = None, source_job_id: int | None = None,
                source_commit: str | None = None, approved_user_id: int | None = None, name: str | None = None,
                summary: str | None = None, cur=None) -> dict:
    """Save new content as version n+1 and make it the live one. Identical content (same hash) is a no-op and returns the
    current version (`created` False). `base_version_id` is the version the editor opened: if the live one moved, raises
    StaleVersion. `files`: "keep" (default) keeps the live version's files; a list replaces them (see _prepare_files)."""
    with _cursor(cur) as c:
        a = _asset_row(c, ref, lock=True)
        if a["state"] != "active":
            raise BadRequest("this asset is archived; restore it first")
        t = asset_types.get(a["type"])
        fields = t.check_fields(fields or {})
        body = t.check_body(body)
        c.execute("SELECT * FROM asset_versions WHERE id = %s", (a["current_version_id"],))
        cur_v = c.fetchone()
        if base_version_id is not None and base_version_id != cur_v["id"]:
            raise StaleVersion(cur_v["id"], cur_v["n"])
        if isinstance(files, str) and files == "keep":
            pairs = [(f["path"], f["sha256"]) for f in _file_rows(c, cur_v["id"])]
        else:
            pairs = _prepare_files(c, t, files)
        h = asset_hash.content_hash(t.key, t.schema_version, a["key"], fields, body, pairs)
        if name is not None or summary is not None:
            nm = " ".join(name.split()) if name is not None else a["name"]
            sm = summary if summary is not None else a["summary"]
            if not 1 <= len(nm) <= 120 or len(sm) > 500:
                raise BadRequest("the name is 1 to 120 characters and the summary at most 500")
            c.execute("UPDATE assets SET name = %s, summary = %s, updated_at = now() WHERE id = %s", (nm, sm, a["id"]))
        if h == cur_v["content_hash"]:
            out = _full(c, _asset_row(c, a["id"]))
            out["created"] = False
            return out
        v = _insert_version(c, a, t, cur_v["n"] + 1, fields, body, pairs, note, actor, via, user_id, source_job_id,
                            source_commit, approved_user_id)
        c.execute("UPDATE assets SET current_version_id = %s, locked = %s, updated_at = now() WHERE id = %s RETURNING *",
                  (v["id"], bool(fields.get("locked")), a["id"]))
        out = _full(c, c.fetchone())
        out["created"] = True
        return out


# --- reads ------------------------------------------------------------------------------------------------------------------

def get(ref, *, n: int | None = None, cur=None) -> dict:
    """An asset (by id or uid) with its live version (or version `n`), that version's files, and its tags."""
    with _cursor(cur) as c:
        return _full(c, _asset_row(c, ref), n)


def find(type_: str, key: str, owner_project_id: int | None = None, *, origin: str | None = None, cur=None) -> dict | None:
    with _cursor(cur) as c:
        c.execute("SELECT id FROM assets WHERE type = %s AND key = %s AND COALESCE(owner_project_id, 0) = COALESCE(%s, 0) "
                  "AND (%s::text IS NULL OR origin = %s) ORDER BY id LIMIT 1", (type_, key, owner_project_id, origin, origin))
        row = c.fetchone()
        return _full(c, _asset_row(c, row["id"])) if row else None


def list_(*, type_: str | None = None, owner_project_id: int | None = None, account_id: int | None = None,
          owner_scope: str | None = None, state: str | None = "active", cur=None) -> list[dict]:
    """Assets (no bodies) with the live version's n and hash, ordered by type, key."""
    where, args = [], []
    for col, val in (("a.type", type_), ("a.owner_project_id", owner_project_id), ("a.account_id", account_id),
                     ("a.owner_scope", owner_scope), ("a.state", state)):
        if val is not None:
            where.append(f"{col} = %s")
            args.append(val)
    with _cursor(cur) as c:
        c.execute("SELECT a.*, v.n, v.content_hash FROM assets a LEFT JOIN asset_versions v ON v.id = a.current_version_id"
                  + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY a.type, a.key, a.id", args)
        return c.fetchall()


def versions(ref, *, with_body: bool = False, cur=None) -> list[dict]:
    """Every version, newest first, each with its files."""
    with _cursor(cur) as c:
        a = _asset_row(c, ref)
        cols = "*" if with_body else ("id, asset_id, n, schema_version, fields, content_hash, note, created_via, "
                                      "source_job_id, source_commit, approved_user_id, created_by, created_user_id, created_at")
        c.execute(f"SELECT {cols} FROM asset_versions WHERE asset_id = %s ORDER BY n DESC", (a["id"],))
        return [_with_files(c, v) for v in c.fetchall()]


# --- state, shares, pins ------------------------------------------------------------------------------------------------------

def archive(ref, actor: str, reason: str = "", user_id: int | None = None, cur=None) -> dict:
    with _cursor(cur) as c:
        a = _asset_row(c, ref, lock=True)
        c.execute("UPDATE assets SET state = 'archived', archived_at = now(), archived_by_user_id = %s, archive_reason = %s, "
                  "updated_at = now() WHERE id = %s RETURNING *", (user_id, (reason or "")[:500], a["id"]))
        return _full(c, c.fetchone())


def restore(ref, cur=None) -> dict:
    with _cursor(cur) as c:
        a = _asset_row(c, ref, lock=True)
        c.execute("UPDATE assets SET state = 'active', archived_at = NULL, archived_by_user_id = NULL, archive_reason = '', "
                  "updated_at = now() WHERE id = %s RETURNING *", (a["id"],))
        return _full(c, c.fetchone())


def set_share_all(ref, share_all: bool, cur=None) -> None:
    with _cursor(cur) as c:
        a = _asset_row(c, ref)
        c.execute("UPDATE assets SET share_all = %s, updated_at = now() WHERE id = %s", (bool(share_all), a["id"]))


def share(ref, target_project_id: int, user_id: int | None = None, cur=None) -> None:
    """Reach one more project. An instance asset may reach any project or account; any other asset only a project in its own
    account (the schema refuses the rest; this gives the plain message first). Use share_all for 'every project'."""
    with _cursor(cur) as c:
        a = _asset_row(c, ref)
        p = _project(c, target_project_id)
        if a["owner_scope"] != "instance":
            if p["partition_id"] != a["account_id"]:
                raise BadRequest("an asset can't be shared outside its own account")
            if p["kind"] == "account":
                raise BadRequest("to reach every project in the account, turn on share-with-all instead")
            if p["id"] == a["owner_project_id"]:
                raise BadRequest("the owner already has it")
        c.execute("INSERT INTO asset_shares (asset_id, target_project_id, shared_by_user_id) VALUES (%s, %s, %s) "
                  "ON CONFLICT DO NOTHING", (a["id"], p["id"], user_id))


def unshare(ref, target_project_id: int, cur=None) -> None:
    with _cursor(cur) as c:
        a = _asset_row(c, ref)
        c.execute("DELETE FROM asset_shares WHERE asset_id = %s AND target_project_id = %s", (a["id"], target_project_id))


def pin(ref, project_id: int, version_n: int, reason: str = "", user_id: int | None = None, cur=None) -> None:
    """Hold an asset at version `version_n` on a project. A locked rule can't be pinned, nor can a project pin its own asset."""
    with _cursor(cur) as c:
        a = _asset_row(c, ref)
        if a["state"] != "active":
            raise BadRequest("this asset is archived")
        if a["locked"]:
            raise BadRequest("a locked rule can't be pinned")
        if a["owner_project_id"] == project_id:
            raise BadRequest("a project edits its own asset; pinning it would mean nothing")
        p = _project(c, project_id)
        if a["owner_scope"] != "instance" and p["partition_id"] != a["account_id"]:
            raise BadRequest("an asset can't be pinned outside its own account")
        c.execute("SELECT id FROM asset_versions WHERE asset_id = %s AND n = %s", (a["id"], version_n))
        v = c.fetchone()
        if not v:
            raise NotFound(f"no version {version_n}")
        c.execute("INSERT INTO asset_pins (asset_id, project_id, version_id, reason, pinned_by_user_id) "
                  "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (asset_id, project_id) DO UPDATE SET version_id = EXCLUDED.version_id, "
                  "reason = EXCLUDED.reason, pinned_by_user_id = EXCLUDED.pinned_by_user_id, pinned_at = now()",
                  (a["id"], project_id, v["id"], (reason or "")[:300], user_id))


def unpin(ref, project_id: int, cur=None) -> None:
    with _cursor(cur) as c:
        a = _asset_row(c, ref)
        c.execute("DELETE FROM asset_pins WHERE asset_id = %s AND project_id = %s", (a["id"], project_id))
