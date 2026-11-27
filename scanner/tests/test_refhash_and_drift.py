"""source_refs[].hash (scanner/lib/refhash.py) + annotation drift (scanner/lib/drift.py): the hash
moves when — and only when — the code a workflow names changes, and an annotation pointing at a
function/file that's gone comes out as `problems` on the workflow."""
import json
import textwrap
from pathlib import Path

from scanner import annotations as ann_loader
from scanner.build import attach_source_hashes, build_catalog
from scanner.lib import drift, refhash, schema

FIXTURES = Path(__file__).parent / "fixtures"

WORKER = textwrap.dedent('''\
    """A worker."""
    import os


    def _land(msg):
        return {"id": msg}


    def _unrelated():
        return 1


    def main():
        # poll then land
        for m in range(3):
            _land(m)
''')


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _wf(confidence="annotated", source_refs=None, steps=None, subproject="ERP"):
    return {
        "id": "platform.fixture-worker.run", "name": "Platform › Fixture worker › Run",
        "group": "Platform", "subproject": subproject, "summary": "s",
        "trigger": {"type": "timer", "detail": "every 60s"}, "has_ai": False,
        "steps": steps if steps is not None else [
            {"id": "timer", "kind": "trigger", "label": "Timer", "system": "internal",
             "code_ref": {"file": "app/workers/w.py", "function": "main"}},
        ],
        "edges": [], "side_effects": [],
        "source_refs": source_refs if source_refs is not None else [{"file": "app/workers/w.py", "lines": "1-16"}],
        "confidence": confidence,
    }


def _hash(root: Path, wf: dict | None = None) -> str | None:
    wf = wf or _wf()
    attach_source_hashes([wf], lambda _w: root, refhash.ModuleCache())
    return wf["source_refs"][0].get("hash")


# --- hashing ------------------------------------------------------------------------------------

def test_hash_is_present_and_well_formed(tmp_path):
    _write(tmp_path, "app/workers/w.py", WORKER)
    h = _hash(tmp_path)
    assert h and schema.HASH_RE.match(h)
    assert _hash(tmp_path) == h   # deterministic


def test_hash_ignores_unrelated_edits_comments_and_line_shifts(tmp_path):
    _write(tmp_path, "app/workers/w.py", WORKER)
    before = _hash(tmp_path)
    shifted = WORKER.replace('import os\n', 'import os\nimport sys\n\n\n# a new comment block\n')
    shifted = shifted.replace("return 1", "return 2")            # a function main() never calls
    shifted = shifted.replace("# poll then land", "# reworded comment")
    _write(tmp_path, "app/workers/w.py", shifted)
    assert _hash(tmp_path) == before


def test_hash_changes_when_the_named_function_changes(tmp_path):
    _write(tmp_path, "app/workers/w.py", WORKER)
    before = _hash(tmp_path)
    _write(tmp_path, "app/workers/w.py", WORKER.replace("range(3)", "range(4)"))
    assert _hash(tmp_path) != before


def test_hash_changes_when_a_local_helper_it_calls_changes(tmp_path):
    _write(tmp_path, "app/workers/w.py", WORKER)
    before = _hash(tmp_path)
    _write(tmp_path, "app/workers/w.py", WORKER.replace('{"id": msg}', '{"id": msg, "x": 1}'))
    assert _hash(tmp_path) != before


def test_ref_function_key_overrides_step_functions(tmp_path):
    _write(tmp_path, "app/workers/w.py", WORKER)
    wf = _wf(source_refs=[{"file": "app/workers/w.py", "function": "_unrelated"}])
    before = _hash(tmp_path, wf)
    _write(tmp_path, "app/workers/w.py", WORKER.replace("return 1", "return 2"))
    wf2 = _wf(source_refs=[{"file": "app/workers/w.py", "function": "_unrelated"}])
    assert _hash(tmp_path, wf2) != before


def test_deleted_function_changes_hash(tmp_path):
    _write(tmp_path, "app/workers/w.py", WORKER)
    before = _hash(tmp_path)
    _write(tmp_path, "app/workers/w.py", WORKER.replace("def main():", "def main_loop():"))
    assert _hash(tmp_path) not in (None, before)


def test_class_method_reference(tmp_path):
    _write(tmp_path, "app/x.py", "class Svc:\n    def run(self):\n        return 1\n")
    wf = _wf(source_refs=[{"file": "app/x.py"}],
             steps=[{"id": "t", "kind": "trigger", "label": "T", "system": "internal",
                     "code_ref": {"file": "app/x.py", "function": "Svc.run"}}])
    before = _hash(tmp_path, wf)
    assert drift.annotation_problems(wf, tmp_path, refhash.ModuleCache()) == []
    _write(tmp_path, "app/x.py", "class Svc:\n    def run(self):\n        return 2\n")
    assert _hash(tmp_path, _wf(source_refs=[{"file": "app/x.py"}], steps=wf["steps"])) != before


def test_line_range_fallback_for_non_python(tmp_path):
    script = "\n".join(f"echo {i}" for i in range(1, 11)) + "\n"
    _write(tmp_path, "run.sh", script)
    wf = _wf(source_refs=[{"file": "run.sh", "lines": "3-5"}], steps=[
        {"id": "t", "kind": "trigger", "label": "T", "system": "internal",
         "code_ref": {"file": "run.sh", "function": "main"}}])
    before = _hash(tmp_path, wf)
    _write(tmp_path, "run.sh", script.replace("echo 9", "echo nine"))       # outside 3-5
    assert _hash(tmp_path, _wf(source_refs=[{"file": "run.sh", "lines": "3-5"}], steps=wf["steps"])) == before
    _write(tmp_path, "run.sh", script.replace("echo 4", "echo four"))       # inside 3-5
    assert _hash(tmp_path, _wf(source_refs=[{"file": "run.sh", "lines": "3-5"}], steps=wf["steps"])) != before


def test_missing_file_gets_no_hash(tmp_path):
    assert _hash(tmp_path) is None


def test_hashing_does_not_mutate_the_loaded_annotation(tmp_path):
    _write(tmp_path, "app/workers/w.py", WORKER)
    ref = {"file": "app/workers/w.py", "lines": "1-16"}
    wf = _wf(source_refs=[ref])
    attach_source_hashes([wf], lambda _w: tmp_path, refhash.ModuleCache())
    assert "hash" not in ref and "hash" in wf["source_refs"][0]


# --- drift ------------------------------------------------------------------------------------

def test_no_problems_when_everything_resolves(tmp_path):
    _write(tmp_path, "app/workers/w.py", WORKER)
    assert drift.annotation_problems(_wf(), tmp_path, refhash.ModuleCache()) == []


def test_missing_step_function_is_reported(tmp_path):
    _write(tmp_path, "app/workers/w.py", WORKER.replace("def main():", "def main_loop():"))
    probs = drift.annotation_problems(_wf(), tmp_path, refhash.ModuleCache())
    assert [(p["code"], p["step"], p["function"]) for p in probs] == [("missing_function", "timer", "main")]
    assert "main() in app/workers/w.py" in probs[0]["message"]


def test_missing_file_is_reported_once_per_reference(tmp_path):
    probs = drift.annotation_problems(_wf(), tmp_path, refhash.ModuleCache())
    assert {p["where"] for p in probs} == {"source_refs[0]", "steps[timer].code_ref"}
    assert all(p["code"] == "missing_file" for p in probs)


def test_source_ref_function_is_checked(tmp_path):
    _write(tmp_path, "app/workers/w.py", WORKER)
    wf = _wf(source_refs=[{"file": "app/workers/w.py", "function": "gone"}])
    probs = drift.annotation_problems(wf, tmp_path, refhash.ModuleCache())
    assert [(p["code"], p["where"]) for p in probs] == [("missing_function", "source_refs[0]")]


def test_attach_only_touches_annotated_and_skips_unscanned_roots(tmp_path):
    auto = _wf(confidence="auto")
    ann = _wf()
    linked = _wf(subproject="LinkedIn")
    root_for = lambda w: None if w["subproject"] == "LinkedIn" else tmp_path   # noqa: E731
    drift.attach([auto, ann, linked], root_for, refhash.ModuleCache())
    assert "problems" not in auto            # auto workflows can't drift
    assert ann["problems"]                   # file missing under the scanned root
    assert "problems" not in linked          # root not scanned this run: unknown, not broken


def test_problems_validate_against_schema(tmp_path):
    ann = _wf()
    drift.attach([ann], lambda _w: tmp_path, refhash.ModuleCache())
    catalog = {"project": "p", "commit": "c", "generated_at": "g", "workflows": [ann]}
    assert schema.validate_catalog(catalog) == []
    assert schema.privacy_sweep(json.dumps(catalog)) == []


def test_schema_rejects_problems_on_auto_and_bad_hash():
    wf = _wf(confidence="auto")
    wf["problems"] = [{"code": "missing_file", "message": "x"}]
    wf["source_refs"] = [{"file": "a.py", "hash": "md5:nope"}]
    problems = schema.validate_catalog({"project": "p", "commit": "c", "generated_at": "g", "workflows": [wf]})
    assert any("only valid on an annotated" in p for p in problems)
    assert any("not 'sha256:" in p for p in problems)


# --- end to end over the fixture tree -------------------------------------------------------------

def test_build_catalog_hashes_every_ref_and_flags_a_broken_annotation(tmp_path):
    annotation = _wf(steps=[
        {"id": "timer", "kind": "trigger", "label": "Timer", "system": "internal",
         "code_ref": {"file": "app/workers/fixture_worker.py", "function": "main"}},
        {"id": "gone", "kind": "action", "label": "Gone", "system": "db",
         "code_ref": {"file": "app/workers/fixture_worker.py", "function": "no_such_function"}},
    ], source_refs=[{"file": "app/workers/fixture_worker.py", "lines": "1-5"}])
    catalog = build_catalog(FIXTURES, None, [annotation], "sha", None, "2026-09-30T00:00:00Z")
    refs = [r for w in catalog["workflows"] for r in w["source_refs"]]
    assert refs and all(r.get("hash") for r in refs)
    broken = [w for w in catalog["workflows"] if w.get("problems")]
    assert [w["id"] for w in broken] == ["platform.fixture-worker.run"]
    assert broken[0]["problems"][0]["function"] == "no_such_function"
    assert schema.validate_catalog(catalog) == []


def test_real_annotations_file_loads_unchanged_by_hashing():
    """attach_source_hashes must not write hashes back into the loaded annotations list."""
    anns = ann_loader.load(Path(__file__).parent.parent / "annotations.yaml")
    build_catalog(FIXTURES, None, anns, "sha", None, "2026-09-30T00:00:00Z")
    assert not any("hash" in r for a in anns for r in a.get("source_refs") or [])


def test_infrastructure_refs_resolve_against_the_knowledge_root(tmp_path):
    """Infrastructure workflows (host automations) point into the host-automation repo, the third root: hashed
    and drift-checked THERE, never against the ERP tree; with no knowledge root they're skipped."""
    knowledge = tmp_path / "knowledge"
    (knowledge / "infra" / "wd").mkdir(parents=True)
    (knowledge / "infra" / "wd" / "wd.sh").write_text("#!/bin/sh\necho ok\n")
    ann = dict(_wf(subproject="Infrastructure", steps=[
        {"id": "timer", "kind": "trigger", "label": "Timer", "system": "internal",
         "code_ref": {"file": "infra/wd/wd.sh", "function": "main"}},
        {"id": "gone", "kind": "action", "label": "Gone", "system": "internal",
         "code_ref": {"file": "infra/wd/gone.timer", "function": "unit"}},
    ], source_refs=[{"file": "infra/wd/wd.sh"}]), id="infrastructure.wd.heal",
        name="Infrastructure › Wd › Heal", group="Infrastructure")
    cat = build_catalog(FIXTURES, None, [ann], "sha", None, "g", knowledge_root=knowledge, knowledge_sha="k1")
    (wf,) = [w for w in cat["workflows"] if w["id"] == "infrastructure.wd.heal"]
    assert wf["source_refs"][0].get("hash", "").startswith("sha256:")
    assert [(p["code"], p["file"]) for p in wf["problems"]] == [("missing_file", "infra/wd/gone.timer")]
    assert cat["commits"]["knowledge"] == "k1"
    assert schema.validate_catalog(cat) == []
    cat = build_catalog(FIXTURES, None, [ann], "sha", None, "g")   # knowledge repo not scanned this run
    (wf,) = [w for w in cat["workflows"] if w["id"] == "infrastructure.wd.heal"]
    assert "problems" not in wf and "hash" not in wf["source_refs"][0]
