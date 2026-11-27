"""Diffs two catalog scans (docs/catalog-schema.md shape) per workflow id — the "change history" the
owner asked for (memory: dev-dashboard's "catalog diffs over time: what changed, when, which commit").
Pure data in, plain-text report out; no I/O of its own (the caller reads both JSON files).
"""
from __future__ import annotations

_STEP_FIELDS = ("kind", "label", "detail", "system", "question", "parent", "branch")


def _step_field_changes(old_step: dict, new_step: dict) -> list[str]:
    changes = []
    for f in _STEP_FIELDS:
        if old_step.get(f) != new_step.get(f):
            changes.append(f"{f}: {old_step.get(f)!r} -> {new_step.get(f)!r}")
    old_cr, new_cr = old_step.get("code_ref") or {}, new_step.get("code_ref") or {}
    if old_cr.get("function") != new_cr.get("function") or old_cr.get("file") != new_cr.get("file"):
        changes.append(f"code_ref: {old_cr.get('file')}::{old_cr.get('function')} -> "
                       f"{new_cr.get('file')}::{new_cr.get('function')}")
    old_ai, new_ai = old_step.get("ai"), new_step.get("ai")
    if bool(old_ai) != bool(new_ai):
        changes.append("ai step " + ("added" if new_ai else "removed"))
    elif old_ai and new_ai:
        if old_ai.get("prompt_text") != new_ai.get("prompt_text"):
            changes.append(f"prompt_text changed (prompt_name={new_ai.get('prompt_name')})")
        if old_ai.get("model_rule") != new_ai.get("model_rule"):
            changes.append(f"model_rule: {old_ai.get('model_rule')!r} -> {new_ai.get('model_rule')!r}")
        if old_ai.get("max_tokens") != new_ai.get("max_tokens"):
            changes.append(f"max_tokens: {old_ai.get('max_tokens')} -> {new_ai.get('max_tokens')}")
    return changes


def diff_workflow(old_wf: dict, new_wf: dict) -> dict:
    """{} (empty) if nothing changed. Otherwise a report dict: name/group/trigger/confidence field
    changes, added/removed/changed step ids (with the change reason for each changed step)."""
    report: dict = {}
    for f in ("name", "group", "subproject", "summary", "confidence"):
        if old_wf.get(f) != new_wf.get(f):
            report.setdefault("fields", []).append(f"{f}: {old_wf.get(f)!r} -> {new_wf.get(f)!r}")
    if old_wf.get("trigger") != new_wf.get("trigger"):
        report.setdefault("fields", []).append(f"trigger: {old_wf.get('trigger')} -> {new_wf.get('trigger')}")
    old_e = {(e.get("from"), e.get("to"), e.get("label")) for e in old_wf.get("edges") or []}
    new_e = {(e.get("from"), e.get("to"), e.get("label")) for e in new_wf.get("edges") or []}
    if old_e != new_e:
        fmt = lambda t: f"{t[0]}->{t[1]}" + (f" [{t[2]}]" if t[2] else "")   # noqa: E731
        bits = [f"+{fmt(t)}" for t in sorted(new_e - old_e, key=str)] + \
               [f"-{fmt(t)}" for t in sorted(old_e - new_e, key=str)]
        report.setdefault("fields", []).append("edges: " + ", ".join(bits))

    old_steps = {s["id"]: s for s in old_wf.get("steps") or []}
    new_steps = {s["id"]: s for s in new_wf.get("steps") or []}
    added = sorted(set(new_steps) - set(old_steps))
    removed = sorted(set(old_steps) - set(new_steps))
    changed = {}
    for sid in sorted(set(old_steps) & set(new_steps)):
        diffs = _step_field_changes(old_steps[sid], new_steps[sid])
        if diffs:
            changed[sid] = diffs
    if added:
        report["steps_added"] = added
    if removed:
        report["steps_removed"] = removed
    if changed:
        report["steps_changed"] = changed
    return report


def diff_catalogs(old: dict, new: dict) -> dict:
    """{"added": [id, ...], "removed": [id, ...], "changed": {id: diff_workflow(...)}}"""
    old_by_id = {w["id"]: w for w in old.get("workflows") or []}
    new_by_id = {w["id"]: w for w in new.get("workflows") or []}
    added = sorted(set(new_by_id) - set(old_by_id))
    removed = sorted(set(old_by_id) - set(new_by_id))
    changed = {}
    for wid in sorted(set(old_by_id) & set(new_by_id)):
        d = diff_workflow(old_by_id[wid], new_by_id[wid])
        if d:
            changed[wid] = d
    return {"added": added, "removed": removed, "changed": changed}


def render_report(diff: dict, old_commit: str = "", new_commit: str = "") -> str:
    lines = []
    header = "Catalog diff"
    if old_commit or new_commit:
        header += f" ({old_commit[:12] or '?'} -> {new_commit[:12] or '?'})"
    lines.append(header)
    lines.append(f"  {len(diff['added'])} workflow(s) added, {len(diff['removed'])} removed, "
                 f"{len(diff['changed'])} changed")
    for wid in diff["added"]:
        lines.append(f"  + {wid}")
    for wid in diff["removed"]:
        lines.append(f"  - {wid}")
    for wid, d in diff["changed"].items():
        lines.append(f"  ~ {wid}")
        for f in d.get("fields", []):
            lines.append(f"      field  {f}")
        for sid in d.get("steps_added", []):
            lines.append(f"      step + {sid}")
        for sid in d.get("steps_removed", []):
            lines.append(f"      step - {sid}")
        for sid, reasons in d.get("steps_changed", {}).items():
            for r in reasons:
                lines.append(f"      step ~ {sid}: {r}")
    return "\n".join(lines)
