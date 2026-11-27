"""Asset types (task #778, P1): a code registry, not a table, so a new type or an add-on's type needs no migration.

An asset is a building block that agents and apps share. Each TYPE says what its key looks like, what its `fields` must hold
(a JSON Schema), whether it has a text body or files, and whether jobs receive it. Initial types:
  skill     a build skill: instructions for agents that design and build (SKILL.md body + attachments)
  rule      a rule or AI instruction, optionally LOCKED so nothing below it can override it
  material  a brand/template file kept per slot (logo.primary, email.signature)
App AI features (the catalog's AI steps) are NOT assets: they stay a view derived from the code.

A version stores the `schema_version` it was written under; `upgrade` maps older fields forward when read, so an old version is
never rewritten.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

import jsonschema

from app.repo import BadRequest

TYPE_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
JOB_KIND = {"type": "string", "pattern": r"^[a-z][a-z0-9-]{0,31}$"}
MATERIAL_KINDS = ("logo", "icon", "image", "email-template", "document-template", "font", "style", "other")

SKILL_KEY_RE = r"^[a-z0-9][a-z0-9-]{0,63}$"
RULE_KEY_RE = r"^[a-z0-9][a-z0-9._-]{0,120}$"
MATERIAL_KEY_RE = r"^[a-z0-9][a-z0-9._-]{0,63}$"

SKILL_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["description"],
    "properties": {
        "description": {"type": "string", "minLength": 1, "maxLength": 1024},
        "job_kinds": {"type": "array", "items": JOB_KIND, "uniqueItems": True, "maxItems": 32},
        "allowed_tools": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 100},
                          "uniqueItems": True, "maxItems": 64},
        # the key of the skill this one is built on: its text is put in front of this one's when a job gets it (#718)
        "base": {"type": "string", "pattern": SKILL_KEY_RE},
        # where the skill's licence stands. authored = written here; permissive = a recognised permissive licence;
        # unknown = none found, so the skill is not used until a person confirms it (confirmed_by / confirmed_at, a new version)
        "license": {"type": "object", "additionalProperties": False, "required": ["status"], "properties": {
            "status": {"enum": ["authored", "permissive", "unknown"]},
            "spdx": {"type": "string", "maxLength": 64},
            "evidence": {"type": "string", "maxLength": 100},
            "confirmed_by": {"type": "integer"},
            "confirmed_at": {"type": "string", "maxLength": 40},
        }},
        # a person read an imported skill and let jobs use it (a new version, like a licence confirmation)
        "review": {"type": "object", "additionalProperties": False, "properties": {
            "reviewed_by": {"type": "integer"},
            "reviewed_at": {"type": "string", "maxLength": 40},
        }},
        # attribution of an imported skill
        "source": {"type": "object", "additionalProperties": False, "properties": {
            "via": {"enum": ["github", "upload"]},
            "url": {"type": "string", "maxLength": 500},
            "ref": {"type": "string", "maxLength": 100},
            "author": {"type": "string", "maxLength": 200},
            "fetched_at": {"type": "string", "maxLength": 40},
            "notes": {"type": "array", "items": {"type": "string", "maxLength": 300}, "maxItems": 20},
        }},
    },
}
RULE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["kind", "severity"],
    "properties": {
        "kind": {"enum": ["rule", "instruction"]},
        "severity": {"enum": ["must", "should"]},
        "job_kinds": {"type": "array", "items": JOB_KIND, "uniqueItems": True, "maxItems": 32},
        "locked": {"type": "boolean"},
        "lock_reason": {"type": "string", "maxLength": 500},
        # the key of the locked rule this one tightens; a same-key rule below a locked one is delivered only with this
        "extends": {"type": "string", "pattern": RULE_KEY_RE},
    },
    "allOf": [{"if": {"properties": {"locked": {"const": True}}, "required": ["locked"]},
               "then": {"required": ["lock_reason"], "properties": {"lock_reason": {"minLength": 1}}}}],
}
MATERIAL_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["kind", "title", "purpose"],
    "properties": {
        "kind": {"enum": list(MATERIAL_KINDS)},
        "title": {"type": "string", "minLength": 1, "maxLength": 120},
        "purpose": {"type": "string", "minLength": 1, "maxLength": 500},
        "usage_notes": {"type": "string", "maxLength": 4000},
    },
}


@dataclass(frozen=True)
class AssetType:
    key: str
    label: str
    noun: str
    schema_version: int
    fields_schema: dict
    key_pattern: str
    has_body: bool
    file_policy: str                    # 'none' | 'attachments' | 'one'
    body_max: int = 0                   # characters
    delivers_to_jobs: bool = False
    upgrade: Callable[[dict, int], dict] = field(default=lambda fields, from_version: fields)

    def check_key(self, key: str) -> str:
        if not isinstance(key, str) or not re.match(self.key_pattern, key):
            raise BadRequest(f"a {self.noun} key must match {self.key_pattern}")
        return key

    def check_fields(self, fields: dict) -> dict:
        if not isinstance(fields, dict):
            raise BadRequest("fields must be an object")
        validator = jsonschema.Draft202012Validator(self.fields_schema)
        errors = sorted(validator.iter_errors(fields), key=lambda e: list(e.path))
        if errors:
            e = errors[0]
            where = ".".join(str(p) for p in e.path) or "fields"
            raise BadRequest(f"{self.noun} {where}: {e.message}")
        return fields

    def check_body(self, body: str) -> str:
        body = body or ""
        if not isinstance(body, str):
            raise BadRequest("body must be text")
        if body and not self.has_body:
            raise BadRequest(f"a {self.noun} has no text body")
        if len(body) > self.body_max:
            raise BadRequest(f"the {self.noun} text is over {self.body_max} characters")
        return body

    def check_file_count(self, n: int) -> None:
        if self.file_policy == "none" and n:
            raise BadRequest(f"a {self.noun} takes no files")
        if self.file_policy == "one" and n != 1:
            raise BadRequest(f"a {self.noun} takes exactly one file")
        if self.file_policy == "attachments" and n > 32:
            raise BadRequest(f"a {self.noun} takes at most 32 attachments")


TYPES: dict[str, AssetType] = {}


def register(t: AssetType) -> AssetType:
    """Add a type (add-ons call this at import). A key already registered is refused: types are never replaced."""
    if not TYPE_RE.match(t.key):
        raise ValueError(f"bad asset type key {t.key!r}")
    if t.key in TYPES:
        raise ValueError(f"asset type {t.key!r} is already registered")
    if t.file_policy not in ("none", "attachments", "one"):
        raise ValueError("file_policy must be none, attachments or one")
    TYPES[t.key] = t
    return t


def get(type_: str) -> AssetType:
    try:
        return TYPES[type_]
    except KeyError:
        raise BadRequest(f"unknown asset type {type_!r} (known: {', '.join(sorted(TYPES))})") from None


register(AssetType("skill", "Skill", "skill", 1, SKILL_SCHEMA, SKILL_KEY_RE, True, "attachments", 256 * 1024, True))
register(AssetType("rule", "Rule", "rule", 1, RULE_SCHEMA, RULE_KEY_RE, True, "none", 32 * 1024, True))
register(AssetType("material", "Material", "material", 1, MATERIAL_SCHEMA, MATERIAL_KEY_RE, False, "one", 0, False))
