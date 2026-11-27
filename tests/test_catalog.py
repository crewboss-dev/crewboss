import copy
import json
from pathlib import Path

from app import repo

FIXTURE = json.loads((Path(__file__).parent.parent / "fixtures" / "catalog-fixture.json").read_text())


def test_import_first_scan_has_no_diffs(project):
    catalog = copy.deepcopy(FIXTURE)
    catalog["project"] = project["slug"]
    result = repo.import_catalog(project["slug"], catalog)
    assert result["workflow_count"] == 3
    # first scan: every workflow is new, so each shows up as "added"
    assert result["diff_count"] == 3
    diffs = repo.recent_workflow_changes(project["id"], limit=10)
    assert all(d["change_type"] == "added" for d in diffs)


def test_import_second_scan_detects_modified_added_removed(project):
    catalog = copy.deepcopy(FIXTURE)
    catalog["project"] = project["slug"]
    repo.import_catalog(project["slug"], catalog)

    catalog2 = copy.deepcopy(catalog)
    catalog2["commit"] = "deadbeef" * 5
    # modify one workflow
    catalog2["workflows"][0]["summary"] = "A changed summary for the Xero refresh workflow."
    # remove one workflow
    removed = catalog2["workflows"].pop(1)
    # add a new one
    catalog2["workflows"].append({
        "id": "platform.new-thing.do",
        "name": "Platform › New thing › Do",
        "group": "Platform",
        "subproject": "ERP",
        "summary": "A brand new workflow.",
        "trigger": {"type": "button", "detail": "manual"},
        "has_ai": False,
        "steps": [],
        "edges": [],
        "side_effects": [],
        "source_refs": [],
        "confidence": "auto",
    })

    result = repo.import_catalog(project["slug"], catalog2)
    assert result["diff_count"] == 3

    diffs = repo.recent_workflow_changes(project["id"], limit=10)
    by_type = {d["change_type"] for d in diffs}
    assert by_type == {"added", "removed", "modified"}

    modified = [d for d in diffs if d["change_type"] == "modified"][0]
    assert "summary" in modified["detail"]

    removed_diff = [d for d in diffs if d["change_type"] == "removed"][0]
    assert removed_diff["workflow_id"] == removed["id"]


def test_list_and_get_workflow(project):
    catalog = copy.deepcopy(FIXTURE)
    catalog["project"] = project["slug"]
    repo.import_catalog(project["slug"], catalog)

    workflows = repo.list_workflows(project["id"])
    assert len(workflows) == 3

    ai_only = repo.list_workflows(project["id"], ai_only=True)
    assert all(w["has_ai"] for w in ai_only)
    assert len(ai_only) == 1

    wf = repo.get_workflow(project["id"], "billing.xero-token.refresh")
    assert wf["name"].startswith("Billing")
    assert len(wf["steps"]) == 3


def test_catalog_import_via_cli(project, tmp_path):
    from app import catalog_import

    catalog = copy.deepcopy(FIXTURE)
    catalog["project"] = project["slug"]
    f = tmp_path / "scan.json"
    f.write_text(json.dumps(catalog))
    rc = catalog_import.main([str(f), "--project", project["slug"]])
    assert rc == 0
    assert repo.latest_scan(project["id"]) is not None
