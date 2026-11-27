"""The ERP's integrations list in the catalog (#393, scanner/lib/integrations.py): read from the scanned ERP tree's
docs/generated/integrations.json into the top-level `integrations`; a step annotation naming an id the list doesn't
know is drift (`unknown_integration` on the workflow) and fails `--check` — but only when the list exists."""
import json
import textwrap
from pathlib import Path

import yaml

from scanner import scan
from scanner.lib import integrations, schema

ROOT = Path(__file__).resolve().parents[2]
EXPORT = json.loads((ROOT / "fixtures/integrations-fixture.json").read_text())

WORKER = textwrap.dedent('''\
    """A worker."""


    def main():
        return 1
''')


def _tree(tmp_path: Path, with_export=True, export=None) -> Path:
    root = tmp_path / "erp"
    (root / "app/workers").mkdir(parents=True)
    (root / "app/workers/w.py").write_text(WORKER)
    if with_export:
        (root / "docs/generated").mkdir(parents=True)
        (root / integrations.PATH).write_text(json.dumps(export if export is not None else EXPORT))
    return root


def _annotations(tmp_path: Path, step_integrations) -> Path:
    step = {"id": "call", "kind": "action", "label": "Call Xero", "system": "xero",
            "code_ref": {"file": "app/workers/w.py", "function": "main"}}
    if step_integrations is not None:
        step["integrations"] = step_integrations
    wf = {"id": "billing.fixture.run", "name": "Billing › Fixture › Run", "group": "Billing", "subproject": "ERP",
          "summary": "s", "trigger": {"type": "timer", "detail": "every 60s"}, "has_ai": False,
          "steps": [step], "side_effects": [], "source_refs": [{"file": "app/workers/w.py", "function": "main"}]}
    p = tmp_path / "annotations.yaml"
    p.write_text(yaml.safe_dump({"workflows": [wf]}))
    return p


def _wf(result, wid="billing.fixture.run"):
    return next(w for w in result.catalog["workflows"] if w["id"] == wid)


def test_the_export_rides_along_verbatim(tmp_path):
    r = scan.scan_trees(_tree(tmp_path), None, "sha", None, _annotations(tmp_path, ["xero"]))
    assert r.catalog["integrations"] == EXPORT
    assert r.ok and not r.integration_problems
    assert "problems" not in _wf(r)
    assert "7 integrations from the ERP" in r.summary()


def test_no_export_on_the_erp_main_means_no_key_and_no_check(tmp_path):
    r = scan.scan_trees(_tree(tmp_path, with_export=False), None, "sha", None, _annotations(tmp_path, ["typo"]))
    assert "integrations" not in r.catalog
    assert r.ok and not r.integration_problems and "problems" not in _wf(r)
    assert "no integrations list" in r.summary()


def test_an_unknown_id_is_drift_and_fails_the_check_only(tmp_path):
    r = scan.scan_trees(_tree(tmp_path), None, "sha", None, _annotations(tmp_path, ["xero", "xer0"]))
    probs = _wf(r)["problems"]
    assert [p["code"] for p in probs] == ["unknown_integration"]
    assert probs[0]["integration"] == "xer0" and probs[0]["step"] == "call" and "xer0" in probs[0]["message"]
    assert r.ok                                  # still importable: the dashboard shows it broken
    assert len(r.integration_problems) == 1      # ... but `scan --check` fails on it
    assert not schema.validate_catalog(r.catalog)


def test_check_exit_code(tmp_path, monkeypatch):
    erp = _tree(tmp_path)
    args = ["--primary-root", str(erp), "--secondary-repo", str(tmp_path / "none"), "--knowledge-repo", "",
            "--no-fetch", "--check"]
    assert scan.main(args + ["--annotations", str(_annotations(tmp_path, ["xero"]))]) == 0
    assert scan.main(args + ["--annotations", str(_annotations(tmp_path, ["nope"]))]) == 1


def test_a_malformed_export_is_a_schema_problem(tmp_path):
    bad = json.loads(json.dumps(EXPORT))
    bad["integrations"][0]["grade"] = "E"
    bad["systems"]["xero"] = ["missing"]
    r = scan.scan_trees(_tree(tmp_path, export=bad), None, "sha", None, _annotations(tmp_path, None))
    assert not r.ok
    assert any("grade" in p for p in r.problems) and any("unknown id 'missing'" in p for p in r.problems)
    erp = _tree(tmp_path / "x", with_export=False)
    (erp / "docs/generated").mkdir(parents=True)
    (erp / integrations.PATH).write_text("{not json")
    r = scan.scan_trees(erp, None, "sha", None, _annotations(tmp_path, None))
    assert any("isn't valid JSON" in p for p in r.problems)


def test_step_integrations_must_be_a_list_of_ids():
    problems = []
    schema.validate_step({"id": "s", "kind": "action", "label": "l", "system": "xero",
                          "code_ref": {"file": "f", "function": "g"}, "integrations": "xero"}, "p", problems)
    assert any("'integrations' must be a list" in p for p in problems)


def test_the_alarm_mailbox_is_a_role_address_not_a_person():
    assert schema.privacy_sweep('"endpoint": "alarms@example.com"') == []
    assert schema.privacy_sweep('"endpoint": "jane.doe@example.com"')
