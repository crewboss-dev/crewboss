"""AI Skills — derived from the catalog, read-only. An AI Skill is a reusable AI CAPABILITY built in
code (Classify email, Extract invoice, Draft reply): every kind=ai step is grouped by the code that runs
it (code_ref file + function, else its prompt name), so one skill lists EVERY workflow + step that uses
it. Nothing new is stored; it's a different view of the latest scan."""
from __future__ import annotations

import re

from app import repo

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _humanise(name: str) -> str:
    name = name.split(".")[-1].strip("_ ")
    name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name).replace("_", " ").strip()
    return (name[:1].upper() + name[1:].lower()) if name else "AI step"


def _slug(name: str) -> str:
    return _SLUG_RE.sub("-", name.lower()).strip("-") or "skill"


def skill_key(step: dict) -> tuple[str, str] | None:
    """(file, capability) for an AI step — the function that runs it, else its prompt name."""
    if step.get("kind") != "ai":
        return None
    cr = step.get("code_ref") or {}
    ai = step.get("ai") or {}
    cap = cr.get("function") or ai.get("prompt_name") or step.get("id")
    return (cr.get("file") or "", str(cap))


def derive_skills(workflows: list[dict]) -> list[dict]:
    groups: dict[tuple[str, str], dict] = {}
    for wf in workflows:
        for step in wf.get("steps", []) or []:
            key = skill_key(step)
            if not key:
                continue
            ai = step.get("ai") or {}
            g = groups.setdefault(key, {
                "file": key[0], "capability": key[1], "name": _humanise(key[1]),
                "model_rule": ai.get("model_rule"), "prompt_name": ai.get("prompt_name"),
                "prompt_location": ai.get("prompt_location"), "prompt_text": ai.get("prompt_text"),
                "output": ai.get("output"), "max_tokens": ai.get("max_tokens"), "tools": ai.get("tools") or [],
                "context_injected": ai.get("context_injected") or [], "subprojects": set(), "used_by": [],
            })
            g["subprojects"].add(wf.get("subproject") or "")
            g["used_by"].append({"workflow": wf["id"], "workflow_name": wf.get("name", wf["id"]),
                                 "group": wf.get("group"), "step": step["id"], "step_label": step.get("label", "")})
    # ids: the capability's slug, disambiguated by file when two files share a function name
    by_slug: dict[str, list] = {}
    for g in groups.values():
        by_slug.setdefault(_slug(g["name"]), []).append(g)
    out = []
    for slug, gs in by_slug.items():
        for g in gs:
            stem = _slug(g["file"].rsplit("/", 1)[-1].rsplit(".", 1)[0]) if len(gs) > 1 else ""
            g["id"] = f"{slug}-{stem}" if stem else slug
            g["subprojects"] = sorted(s for s in g["subprojects"] if s)
            out.append(g)
    return sorted(out, key=lambda g: (-len(g["used_by"]), g["name"]))


def list_skills(project_id: int) -> list[dict]:
    scan = repo.latest_scan(project_id)
    return derive_skills(scan["catalog"].get("workflows", []) if scan else [])


def skills_by_step(project_id: int, workflow_id: str) -> dict[str, dict]:
    """{step_id: {"id", "name"}} — which skill each AI step of one workflow uses (for the canvas)."""
    out = {}
    for s in list_skills(project_id):
        for u in s["used_by"]:
            if u["workflow"] == workflow_id:
                out[u["step"]] = {"id": s["id"], "name": s["name"]}
    return out
