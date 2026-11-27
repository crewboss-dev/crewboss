"""Loads $CREWBOSS_HOME/scanner/annotations.yaml — a list of hand-curated Workflow objects (see
docs/catalog-schema.md) keyed by their own `id`. build.py merges these in wholesale, marks them
`confidence: annotated`, and drops the auto-discovered entry for whichever source file(s) each one
claims (its own `source_refs` + every step's `code_ref.file`)."""
from __future__ import annotations

from pathlib import Path

import yaml


class MissingAnnotations(FileNotFoundError):
    """The annotations file is absent. An install that names repos to scan must have one (or say annotations are optional)."""


def load(path: str | Path, required: bool = False) -> list[dict]:
    p = Path(path)
    if not p.exists():
        if required:
            raise MissingAnnotations(f"annotations file {p} does not exist: an empty catalog would replace the real one "
                                     f"(set `annotations: optional` in scanner.yaml if this install has none)")
        return []
    data = yaml.safe_load(p.read_text()) or {}
    workflows = data.get("workflows") or []
    seen = set()
    for wf in workflows:
        if "id" not in wf:
            raise ValueError(f"annotations.yaml: entry missing 'id': {wf!r}")
        if wf["id"] in seen:
            raise ValueError(f"annotations.yaml: duplicate id '{wf['id']}'")
        seen.add(wf["id"])
    return workflows
