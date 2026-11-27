"""The offline catalog export (#402, no drift): `python -m app.catalog_export --project acme-erp --ref <sha>` builds the
ERP admin site's catalog for one commit without the live dashboard, and writes exactly what the live import writes to
exports/catalog.json for the same scan. It refuses (exit 1) what the import would refuse, plus unknown integration ids
and a ref with no integrations list; bad arguments / an unknown ref exit 2. It never touches the database."""
import json
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

from app import catalog_export, catalog_import, config, db

ROOT = Path(__file__).resolve().parent.parent
EXPORT = json.loads((ROOT / "fixtures/integrations-fixture.json").read_text())
BUILT_AT = "2026-10-15T00:00:00+00:00"

WORKER = textwrap.dedent('''\
    """A worker."""


    def main():
        return 1
''')


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _repo(tmp_path: Path, with_export=True) -> tuple[Path, str]:
    """A throwaway ERP repo with one worker (+ the integrations list) committed; returns (repo, sha)."""
    repo = tmp_path / "erp"
    (repo / "app/workers").mkdir(parents=True)
    (repo / "app/workers/w.py").write_text(WORKER)
    if with_export:
        (repo / "docs/generated").mkdir(parents=True)
        (repo / "docs/generated/integrations.json").write_text(json.dumps(EXPORT))
    _git(tmp_path, "init", "-q", str(repo))
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "fixture")
    return repo, _git(repo, "rev-parse", "HEAD")


def _annotations(tmp_path: Path, step_integrations=("xero",), summary="s", group="Billing") -> Path:
    step = {"id": "call", "kind": "action", "label": "Call Xero", "system": "xero",
            "code_ref": {"file": "app/workers/w.py", "function": "main"}}
    if step_integrations is not None:
        step["integrations"] = list(step_integrations)
    wf = {"id": "billing.fixture.run", "name": "Billing › Fixture › Run", "group": group, "subproject": "ERP",
          "summary": summary, "trigger": {"type": "timer", "detail": "every 60s"}, "has_ai": False,
          "steps": [step], "side_effects": [], "source_refs": [{"file": "app/workers/w.py", "function": "main"}]}
    p = tmp_path / "annotations.yaml"
    p.write_text(yaml.safe_dump({"workflows": [wf]}))
    return p


def _args(repo, ref, annotations, out):
    return ["--project", "acme-erp", "--ref", ref, "--repo", str(repo), "--annotations", str(annotations),
            "--linkedin-repo", "", "--knowledge-repo", "", "--out", str(out)]


@pytest.fixture
def no_db(monkeypatch):
    """The CLI must not read or write the dashboard database: any connection attempt fails the test."""
    def refuse(*a, **k):
        raise AssertionError("the offline export touched the database")
    monkeypatch.setattr(db, "connect", refuse)


@pytest.fixture
def fixed_clock(monkeypatch):
    monkeypatch.setattr(catalog_export, "_now", lambda: BUILT_AT)


def test_cli_output_equals_what_the_live_import_writes(tmp_path, monkeypatch, fixed_clock, project):
    repo, sha = _repo(tmp_path)
    scanned = {}
    real = catalog_export.scan_offline

    def capture(*a, **k):
        scanned["catalog"] = real(*a, **k)
        return scanned["catalog"]

    monkeypatch.setattr(catalog_export, "scan_offline", capture)
    out = tmp_path / "cli.json"
    with monkeypatch.context() as m:                       # the CLI itself runs with the database refused
        m.setattr(db, "connect", lambda *a, **k: (_ for _ in ()).throw(AssertionError("touched the database")))
        assert catalog_export.main(_args(repo, "HEAD", _annotations(tmp_path), out)) == 0
    cli = out.read_text()

    # the same scan through the live path: import_document -> repo.import_catalog -> catalog_export.write
    live_dir = tmp_path / "exports"
    live_dir.mkdir()
    monkeypatch.setattr(config, "EXPORTS_DIR", str(live_dir))
    monkeypatch.setattr(config, "EXPORT_PROJECT", project["slug"])
    catalog_import.import_document(scanned["catalog"], project["slug"])
    assert (live_dir / "catalog.json").read_text() == cli

    doc = json.loads(cli)
    assert doc["commit"] == sha and doc["built_at"] == BUILT_AT and doc["integrations_version"] == 1
    assert set(doc) == {"version", "project", "commit", "generated_at", "built_at", "integrations_version",
                        "workflows", "skills"}
    wf = next(w for w in doc["workflows"] if w["id"] == "billing.fixture.run")
    assert wf["steps"][0]["integrations"] == ["xero"]


def test_a_tree_export_takes_its_commit_from_the_flag(tmp_path, no_db, fixed_clock):
    repo, sha = _repo(tmp_path)
    out = tmp_path / "out.json"
    args = ["--erp-tree", str(repo), "--commit", "abc123", "--annotations", str(_annotations(tmp_path)),
            "--linkedin-repo", "", "--knowledge-repo", "", "--out", str(out)]
    assert catalog_export.main(args) == 0
    assert json.loads(out.read_text())["commit"] == "abc123"
    plain = tmp_path / "plain"
    plain.mkdir()
    args[1] = str(plain)
    args.pop(2), args.pop(2)                               # no --commit, no .git: refused
    assert catalog_export.main(args) == 2


@pytest.mark.parametrize("case, code, needle", [
    ("unknown-integration", 1, "UNKNOWN INTEGRATION"),
    ("privacy", 1, "PRIVACY"),
    ("schema", 1, "SCHEMA"),
    ("no-integrations-file", 1, "integrations.json"),
    ("unknown-ref", 2, "rev-parse"),
    ("unknown-project", 2, "can't build"),
])
def test_refusals_exit_non_zero_with_a_clear_message(tmp_path, capsys, no_db, case, code, needle):
    repo, _ = _repo(tmp_path, with_export=case != "no-integrations-file")
    ann = {"unknown-integration": lambda: _annotations(tmp_path, ("xero", "xer0")),
           "privacy": lambda: _annotations(tmp_path, summary="mail jane.doe@example.com about it"),
           "schema": lambda: _annotations(tmp_path, group="Nope")}.get(case, lambda: _annotations(tmp_path))()
    args = _args(repo, "nosuchref" if case == "unknown-ref" else "HEAD", ann, tmp_path / "out.json")
    if case == "unknown-project":
        args[1] = "mallen-dashboard"
    assert catalog_export.main(args) == code
    err = capsys.readouterr().err
    assert needle in err
    assert "jane.doe" not in err                           # a privacy finding is counted, never quoted
    assert not (tmp_path / "out.json").exists()


def test_the_live_write_and_the_cli_share_one_renderer(project, tmp_path, monkeypatch, fixed_clock):
    """write() is render() on disk: nothing else shapes the file."""
    monkeypatch.setattr(config, "EXPORTS_DIR", str(tmp_path))
    monkeypatch.setattr(config, "EXPORT_PROJECT", project["slug"])
    cat = {"project": "acme-erp", "commit": "c0ffee", "generated_at": "2026-10-15T00:00:00+00:00",
           "workflows": [], "integrations": EXPORT}
    target = catalog_export.write(cat, project["slug"])
    assert target.read_text() == catalog_export.render(cat)
