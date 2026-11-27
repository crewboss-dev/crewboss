"""Decisions/loops in the annotations: the loader + build finalisation keep the new keys, and the annotation file in use
(the instance's, else the example) validates clean with them. The checks on one install's own workflows live in that
instance folder (tests/test_instance_annotations.py there)."""
import textwrap

from scanner import annotations as ann_loader
from scanner.build import _finalize_annotation
from scanner.lib import schema
from scanner.tests.test_annotations_and_health_map import ANNOTATIONS

_FIXTURE_YAML = textwrap.dedent("""
    workflows:
      - id: billing.fixture-token.refresh
        name: "Billing › Fixture token › Refresh"
        group: Billing
        subproject: ERP
        summary: fixture
        trigger: {type: timer, detail: "every 600s"}
        steps:
          - {id: timer, kind: trigger, label: Timer, code_ref: {file: app/workers/f.py, function: main}}
          - {id: each-row, kind: loop, label: "For each row", detail: "every stored token row",
             code_ref: {file: app/workers/f.py, function: run_once}}
          - {id: check, kind: decision, label: "Near expiry?", question: "Within 15 min of expiry?",
             parent: each-row, code_ref: {file: app/workers/f.py, function: _near}}
          - {id: refresh, kind: action, label: Refresh, parent: check, branch: "Yes",
             code_ref: {file: app/workers/f.py, function: refresh}}
          - {id: heartbeat, kind: logic, label: Heartbeat, code_ref: {file: app/workers/f.py, function: main}}
        edges:
          - {from: timer, to: each-row}
          - {from: each-row, to: check}
          - {from: each-row, to: heartbeat}
          - {from: check, to: refresh, label: "Yes"}
          - {from: check, to: heartbeat, label: "No"}
        source_refs: [{file: app/workers/f.py}]
""")


def _validate(workflows):
    return schema.validate_catalog({"project": "p", "commit": "c", "generated_at": "g", "workflows": workflows})


def test_loader_and_finalise_keep_question_parent_branch_and_labels(tmp_path):
    p = tmp_path / "annotations.yaml"
    p.write_text(_FIXTURE_YAML)
    wf = _finalize_annotation(ann_loader.load(p)[0])
    steps = {s["id"]: s for s in wf["steps"]}
    assert steps["check"]["kind"] == "decision"
    assert steps["check"]["question"] == "Within 15 min of expiry?"
    assert steps["check"]["parent"] == "each-row"
    assert steps["refresh"]["parent"] == "check" and steps["refresh"]["branch"] == "Yes"
    assert steps["each-row"]["kind"] == "loop" and steps["each-row"]["detail"] == "every stored token row"
    labels = {(e["from"], e["to"]): e.get("label") for e in wf["edges"]}
    assert labels[("check", "refresh")] == "Yes" and labels[("check", "heartbeat")] == "No"
    assert wf["confidence"] == "annotated"
    assert _validate([wf]) == []


def test_loader_output_fails_validation_when_a_branch_label_drifts(tmp_path):
    p = tmp_path / "annotations.yaml"
    p.write_text(_FIXTURE_YAML.replace('branch: "Yes"', 'branch: "Yep"'))
    wf = _finalize_annotation(ann_loader.load(p)[0])
    assert any("has branch 'Yep'" in prob for prob in _validate([wf]))


# ---- the annotations file in use ---------------------------------------------------------------------

def test_annotations_in_use_validate_clean_with_decisions():
    wfs = [_finalize_annotation(w) for w in ann_loader.load(ANNOTATIONS)]
    assert wfs, "no annotations to check"
    assert _validate(wfs) == []
