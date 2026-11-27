"""Integration checks on the two hand-curated files that don't need a real repo checkout: every
annotation must itself validate against the catalog schema, and every health_map.yaml entry must
point at a workflow id / step id that actually exists in annotations.yaml.

The files are the install's own ($CREWBOSS_HOME/scanner/, scanner/config.py); with none there, the shipped examples are checked."""
from pathlib import Path

import pytest
import yaml

from scanner import annotations as ann_loader
from scanner import config as scanner_config
from scanner.build import _finalize_annotation
from scanner.lib import schema

ROOT = Path(__file__).parent.parent


def _pick(real: str, example: str) -> Path:
    return Path(real) if Path(real).is_file() else ROOT / example


ANNOTATIONS = _pick(scanner_config.annotations_path(), "annotations.example.yaml")
HEALTH_MAP = _pick(scanner_config.health_map_path(), "health_map.example.yaml")


def _load_annotations():
    return [_finalize_annotation(a) for a in ann_loader.load(ANNOTATIONS)]


def test_annotations_yaml_is_not_empty():
    assert len(ann_loader.load(ANNOTATIONS)) >= 1


def test_every_annotation_validates_clean():
    workflows = _load_annotations()
    catalog = {"project": "acme-app", "commit": "test", "generated_at": "2026-09-30T00:00:00Z",
               "workflows": workflows}
    problems = schema.validate_catalog(catalog)
    assert problems == [], "\n".join(problems)


def test_annotations_are_privacy_clean():
    import json
    workflows = _load_annotations()
    rendered = json.dumps(workflows)
    findings = schema.privacy_sweep(rendered)
    assert findings == [], findings


def test_health_map_mapped_entries_point_at_real_workflow_and_step_ids():
    workflows = {w["id"]: w for w in _load_annotations()}
    data = yaml.safe_load(HEALTH_MAP.read_text())
    for entry in data.get("mapped", []):
        wf = workflows.get(entry["workflow"])
        assert wf is not None, f"health_map references unknown workflow id {entry['workflow']!r}"
        step_ids = {s["id"] for s in wf["steps"]}
        assert entry["step"] in step_ids, (
            f"health_map entry {entry} references step {entry['step']!r} not in "
            f"{entry['workflow']!r}'s steps {sorted(step_ids)}")


def test_health_map_unmapped_entries_have_a_note():
    data = yaml.safe_load(HEALTH_MAP.read_text())
    for entry in data.get("unmapped", []):
        assert entry.get("note"), f"unmapped health_map entry has no explanation: {entry}"
