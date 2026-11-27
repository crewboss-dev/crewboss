"""The application's graded integration list (#393): the application commits its registry's export as
docs/generated/integrations.json; the scanner reads it from the same exported origin/main tree it scans and carries it
in the catalog as the top-level `integrations` (docs/catalog-schema.md), so it reaches the dashboard by the one import
path, with the scanner's role token. No file on the ERP's main = no `integrations` key, and nothing below runs.

A step names the integrations it uses in its annotation (`integrations: [ids]`, narrowing what its `system` maps to).
An id the export doesn't know is ANNOTATION DRIFT, the same as a function that no longer exists (scanner/lib/drift.py):
a `problems` entry with code `unknown_integration` on the workflow — the app shows it broken and raises a Tweak task —
and `scan --check` fails on it, so a typo is caught before the annotation is committed. Only checked when the export
exists ("couldn't look" is not "gone")."""
from __future__ import annotations

import json
from pathlib import Path

PATH = "docs/generated/integrations.json"
UNKNOWN_INTEGRATION = "unknown_integration"
GRADES = {"A", "B", "C", "D"}


def load(erp_root: Path | None) -> tuple[dict | None, list[str]]:
    """(export, problems). A missing file is (None, []); an unreadable one is a problem (the ERP's contract broke)."""
    if erp_root is None:
        return None, []
    p = Path(erp_root) / PATH
    if not p.is_file():
        return None, []
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, [f"catalog.integrations: {PATH} on the ERP's main isn't valid JSON ({exc})"]
    return doc, validate(doc)


def validate(doc) -> list[str]:
    """The shape the dashboard relies on (app/integrations.py validates the same on import)."""
    if not isinstance(doc, dict):
        return ["catalog.integrations: must be an object"]
    problems: list[str] = []
    if doc.get("version") != 1:
        problems.append(f"catalog.integrations: version {doc.get('version')!r} is not 1")
    rows = doc.get("integrations")
    if not isinstance(rows, list):
        return problems + ["catalog.integrations: 'integrations' must be a list"]
    ids = set()
    for n, r in enumerate(rows):
        where = f"catalog.integrations.integrations[{n}]"
        if not isinstance(r, dict):
            problems.append(f"{where}: must be an object")
            continue
        for k in ("id", "name", "module", "peer", "zone", "flow", "kind", "auth", "transport", "grade"):
            if not isinstance(r.get(k), str) or not r.get(k):
                problems.append(f"{where}: missing '{k}'")
        if r.get("grade") not in GRADES:
            problems.append(f"{where}: grade {r.get('grade')!r} not in A-D")
        if r.get("id") in ids:
            problems.append(f"catalog.integrations: duplicate id {r.get('id')!r}")
        ids.add(r.get("id"))
    systems = doc.get("systems", {})
    if not isinstance(systems, dict):
        problems.append("catalog.integrations: 'systems' must be an object")
    else:
        for system, sids in systems.items():
            if not isinstance(sids, list):
                problems.append(f"catalog.integrations.systems[{system}]: must be a list")
                continue
            for i in sids:
                if i not in ids:
                    problems.append(f"catalog.integrations.systems[{system}]: unknown id {i!r}")
    return problems


def known_ids(doc: dict | None) -> set[str]:
    return {r.get("id") for r in (doc or {}).get("integrations") or [] if isinstance(r, dict)}


def step_problems(wf: dict, doc: dict) -> list[dict]:
    """unknown_integration problems for one workflow's steps."""
    ids = known_ids(doc)
    out = []
    for st in wf.get("steps") or []:
        named = st.get("integrations")
        if not isinstance(named, list):
            continue
        for i in named:
            if i not in ids:
                where = f"steps[{st.get('id')}].integrations"
                out.append({"code": UNKNOWN_INTEGRATION, "where": where, "step": st.get("id"), "integration": i,
                            "message": f"{where}: integration {i!r} is not in the ERP's integrations list"})
    return out


def attach(workflows: list[dict], doc: dict | None) -> int:
    """Add unknown_integration problems to annotated workflows (after drift.attach). Returns how many."""
    if doc is None:
        return 0
    n = 0
    for wf in workflows:
        if wf.get("confidence") != "annotated":
            continue
        probs = step_problems(wf, doc)
        if probs:
            wf["problems"] = list(wf.get("problems") or []) + probs
            n += len(probs)
    return n
