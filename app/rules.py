"""Rules & AI Instructions inventory — READ-ONLY. What steers the workflows: a Rule is a deterministic
WHEN/IF -> THEN policy at a step; an AI Instruction is a plain-language policy tuning an AI Skill's
judgement (optionally naming the skill it tunes); a built-in is a rule hard-coded in the code. Nothing
here edits one: "Request a change" opens a blank request pre-linked to it.

Source: the latest catalog scan's `rules` array (docs/catalog-schema.md) once the scanner reports it.
Until then the page is empty — the fixture (fixtures/rules-fixture.json) is loaded ONLY when
CREWBOSS_RULES_FIXTURE names it (tests, screenshots), so fixture rows never pass for real ones.
Hit counts arrive in phase 3 (health wiring); until then every hit count is None ("—")."""
from __future__ import annotations

import json
import os
import re

from app import repo

VALID_KIND = ("rule", "instruction", "builtin")
KIND_LABEL = {"rule": "Rule", "instruction": "AI Instruction", "builtin": "Built-in"}
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,120}$")


def _fixture_rules() -> list[dict]:
    path = os.environ.get("CREWBOSS_RULES_FIXTURE")
    if not path:
        return []
    with open(path, encoding="utf-8") as fh:
        return json.load(fh).get("rules", [])


def _clean(r: dict) -> dict | None:
    """Keep the documented shape only; drop anything malformed rather than fail the page."""
    if not isinstance(r, dict) or not _ID_RE.match(str(r.get("id", ""))) or r.get("kind") not in VALID_KIND:
        return None
    ws = r.get("workflow_step") if isinstance(r.get("workflow_step"), dict) else None
    src = r.get("source") if isinstance(r.get("source"), dict) else {}
    return {
        "id": r["id"],
        "name": str(r.get("name") or r["id"])[:200],
        "kind": r["kind"],
        "enabled": bool(r.get("enabled", True)),
        "workflow_step": {"workflow": str(ws.get("workflow", "")), "step": str(ws.get("step", "") or "")} if ws else None,
        "conditions": str(r.get("conditions") or "")[:500],
        "action": str(r.get("action") or r.get("guidance") or "")[:500],
        "source_file": str(src.get("file") or r.get("source_file") or "")[:300],
        "skill": str(r.get("skill") or "")[:120] if r["kind"] == "instruction" else "",
        "hits": None,  # phase 3
    }


def all_rules(project_id: int) -> tuple[list[dict], str]:
    """(rules, origin) — origin is 'catalog', 'fixture' or 'none'."""
    scan = repo.latest_scan(project_id)
    return from_raw(scan["catalog"].get("rules") if scan else None)


def from_raw(raw_rules) -> tuple[list[dict], str]:
    """(rules, origin) from a scan's `rules` array (None = no scan / none reported). Split out so a caller that already holds the array (the Assets pages
    select just that slice of every project's scan in one query) does not load the whole catalog again."""
    if isinstance(raw_rules, list):
        raw, origin = raw_rules, "catalog"
    else:
        raw = _fixture_rules()
        origin = "fixture" if raw else "none"
    rules = [c for c in (_clean(r) for r in raw) if c]
    return sorted(rules, key=lambda r: (r["name"].lower(), r["id"])), origin


def list_rules(project_id: int, kind: str | None = None, show_disabled: bool = False,
               workflow: str | None = None, step: str | None = None,
               skill: str | None = None) -> tuple[list[dict], str]:
    rules, origin = all_rules(project_id)
    if skill:
        rules = [r for r in rules if r["skill"] == skill]
    if kind:
        rules = [r for r in rules if r["kind"] == kind]
    if not show_disabled:
        rules = [r for r in rules if r["enabled"]]
    if workflow:
        rules = [r for r in rules if r["workflow_step"] and r["workflow_step"]["workflow"] == workflow]
    if step:
        rules = [r for r in rules if r["workflow_step"] and r["workflow_step"]["step"] == step]
    return rules, origin


def get_rule(project_id: int, rule_id: str) -> dict:
    for r in all_rules(project_id)[0]:
        if r["id"] == rule_id:
            return r
    raise repo.NotFound(f"rule {rule_id!r} not found")


def step_counts(project_id: int, workflow_id: str) -> dict[str, dict]:
    """{step_id: {"rules": n, "instructions": n}} for the canvas cards — enabled only; built-ins count
    as rules."""
    out: dict[str, dict] = {}
    for r in all_rules(project_id)[0]:
        ws = r["workflow_step"]
        if not r["enabled"] or not ws or ws["workflow"] != workflow_id or not ws["step"]:
            continue
        c = out.setdefault(ws["step"], {"rules": 0, "instructions": 0})
        c["instructions" if r["kind"] == "instruction" else "rules"] += 1
    return out
