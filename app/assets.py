"""Cache-busting for /static. Every static <link>/<script> in a template goes through `asset(path)`, which
appends ?v=<short content hash>. The hash is computed once per file when the app starts (the image is
immutable, so a rebuild = a restart = new hashes). /static answers a versioned URL with a year-long
immutable cache and an unversioned one with no-cache, so a rebuilt app.css can never be shadowed by a stale
copy the browser cached heuristically (the owner hit exactly that: new .popover markup, old CSS)."""
from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.parse import parse_qs

from fastapi.staticfiles import StaticFiles

STATIC_DIR = Path("app/static")
MISSING = "0"  # a file that isn't there (e.g. the built CSS in a source checkout): still a token, never cached forever
_hashes: dict[str, str] = {}


def file_hash(rel: str) -> str:
    try:
        return hashlib.sha256((STATIC_DIR / rel).read_bytes()).hexdigest()[:12]
    except OSError:
        return MISSING


def compute_all() -> dict[str, str]:
    """Hash every file under app/static (called at startup)."""
    _hashes.clear()
    if STATIC_DIR.is_dir():
        for f in STATIC_DIR.rglob("*"):
            if f.is_file():
                rel = f.relative_to(STATIC_DIR).as_posix()
                _hashes[rel] = file_hash(rel)
    return dict(_hashes)


def version(rel: str) -> str:
    rel = rel.lstrip("/")
    if rel not in _hashes:
        _hashes[rel] = file_hash(rel)
    return _hashes[rel]


def asset(rel: str) -> str:
    """'css/app.css' -> '/static/css/app.css?v=3f2a…'"""
    rel = rel.lstrip("/")
    return f"/static/{rel}?v={version(rel)}"


class VersionedStaticFiles(StaticFiles):
    """StaticFiles + Cache-Control: immutable for ?v=… URLs (the version changes when the file does),
    no-cache (revalidate every time) for anything else."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.status_code in (200, 304):
            query = parse_qs(scope.get("query_string", b"").decode("latin-1"))
            if query.get("v", [""])[0]:
                response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            else:
                response.headers["Cache-Control"] = "no-cache"
        return response


compute_all()
