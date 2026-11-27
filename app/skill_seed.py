"""The built-in skills (task #718): the generic skills that ship with Crewboss (app/builtin_skills/<key>/SKILL.md), kept as built-in
assets: instance scope, origin 'builtin', layer 0, reaching every project. A skill of the same key installed globally, in an account
or in a project overrides the built-in for that layer and below. Nothing instance-specific belongs here: those skills are imported
through the Skills page.

seed() runs at start-up and is idempotent: a missing built-in is created, a changed one becomes a new version (so history shows the
upgrade), an unchanged one is left alone."""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from app import asset_hash, asset_store, asset_types, skill_md

log = logging.getLogger("crewboss.skills")
DIR = Path(__file__).parent / "builtin_skills"
ACTOR = "crewboss"


def _load() -> list[dict]:
    out = []
    for d in sorted(p for p in DIR.iterdir() if p.is_dir()):
        md = d / "SKILL.md"
        if not md.is_file():
            continue
        front, body = skill_md.parse(md.read_text(encoding="utf-8"))
        key = d.name
        files = [{"path": f.relative_to(d).as_posix(), "raw": f.read_bytes()} for f in sorted(d.rglob("*"))
                 if f.is_file() and f.name != "SKILL.md"]
        out.append({"key": key, "name": key.replace("-", " ").capitalize(), "description": " ".join(str(front.get("description", "")).split()),
                    "body": body.lstrip("\n"), "files": files})
    return out


def seed() -> dict:
    """-> {"created": [...], "updated": [...]}"""
    created, updated = [], []
    for s in _load():
        fields = {"description": s["description"], "license": {"status": "authored"}}
        existing = asset_store.find("skill", s["key"], None, origin="builtin")
        if existing is None:
            asset_store.create("skill", s["key"], s["name"], "instance", None, fields, s["body"], s["files"], summary=s["description"][:500],
                               origin="builtin", share_all=True, actor=ACTOR, via="builtin", note="Ships with Crewboss")
            created.append(s["key"])
            continue
        t = asset_types.get("skill")
        pairs = [(f["path"], hashlib.sha256(f["raw"]).hexdigest()) for f in s["files"]]
        h = asset_hash.content_hash(t.key, t.schema_version, s["key"], fields, s["body"], pairs)
        if existing["version"]["content_hash"] != h and existing["state"] == "active":
            asset_store.new_version(existing["id"], fields, s["body"], s["files"], note="Updated with Crewboss", actor=ACTOR, via="builtin",
                                    summary=s["description"][:500])
            updated.append(s["key"])
    if created or updated:
        log.info("built-in skills: created %s, updated %s", created, updated)
    return {"created": created, "updated": updated}
