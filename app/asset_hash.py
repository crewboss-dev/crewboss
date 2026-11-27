"""The canonical form and content hash of an asset version (task #778, P1). The spec is docs/asset-hash.md; the runner
re-implements it to check what it delivers, so the golden vectors in tests/test_assets_store.py must never change.

hash = SHA-256 (hex) over the UTF-8 bytes of the compact JSON of
    {"type", "schema_version", "key", "fields", "body", "files": [[path, sha256], ...]}
with object keys sorted, no insignificant whitespace, non-ASCII written as itself, every string (keys too) NFC-normalised,
CR LF and lone CR turned into LF inside every string, files sorted by path. Floats are refused (fields are strings, booleans,
integers, lists and objects): their text form differs between languages.
"""
from __future__ import annotations

import hashlib
import json
import unicodedata


def _canon(value):
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
    if isinstance(value, bool) or value is None or isinstance(value, int):
        return value
    if isinstance(value, float):
        raise ValueError("floats are not allowed in asset fields")
    if isinstance(value, (list, tuple)):
        return [_canon(v) for v in value]
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise ValueError("asset field names must be strings")
            nk = _canon(k)
            if nk in out:
                raise ValueError(f"field name {k!r} appears twice after normalisation")
            out[nk] = _canon(v)
        return out
    raise ValueError(f"cannot canonicalise {type(value).__name__}")


def canonical_json(type_: str, schema_version: int, key: str, fields: dict, body: str, files=()) -> str:
    """`files` is an iterable of (path, sha256) pairs (or dicts with those keys)."""
    pairs = sorted((f["path"], f["sha256"]) if isinstance(f, dict) else (f[0], f[1]) for f in files)
    doc = {"type": type_, "schema_version": int(schema_version), "key": key, "fields": fields or {}, "body": body or "",
           "files": [list(p) for p in pairs]}
    return json.dumps(_canon(doc), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(type_: str, schema_version: int, key: str, fields: dict, body: str, files=()) -> str:
    return hashlib.sha256(canonical_json(type_, schema_version, key, fields, body, files).encode("utf-8")).hexdigest()
