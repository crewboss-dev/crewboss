import copy
import json
from pathlib import Path

from app import repo

FIXTURE = json.loads((Path(__file__).parent.parent / "fixtures" / "catalog-fixture.json").read_text())


def _import(project, catalog):
    c = copy.deepcopy(catalog)
    c["project"] = project["slug"]
    return repo.import_catalog(project["slug"], c)


def test_link_task_to_workflow_and_step(project):
    _import(project, FIXTURE)
    task = repo.create_task(project["id"], "Fix the Xero call", "", "should", "bug", [], {}, "owner")
    repo.link_task_workflow(task["id"], "billing.xero-token.refresh", "call-xero", "owner")

    linked = repo.tasks_for_workflow(project["id"], "billing.xero-token.refresh")
    assert len(linked) == 1
    assert linked[0]["id"] == task["id"]

    by_step = repo.tasks_for_workflow(project["id"], "billing.xero-token.refresh", step="call-xero")
    assert len(by_step) == 1
    by_other_step = repo.tasks_for_workflow(project["id"], "billing.xero-token.refresh", step="load-token")
    assert by_other_step == []


def test_get_workflow_includes_tasks(project):
    _import(project, FIXTURE)
    task = repo.create_task(project["id"], "Whole-workflow task", "", "should", "tweak", [], {}, "owner")
    repo.link_task_workflow(task["id"], "billing.xero-token.refresh", None, "owner")

    wf = repo.get_workflow(project["id"], "billing.xero-token.refresh")
    assert len(wf["tasks"]) == 1
    assert wf["tasks"][0]["title"] == "Whole-workflow task"
    assert wf["stale"] is False


def test_staleness_detected_when_hash_changes_without_other_diff(project):
    catalog1 = copy.deepcopy(FIXTURE)
    wf = catalog1["workflows"][0]
    wf["confidence"] = "annotated"
    wf["source_refs"] = [{"file": "app/xero_sync.py", "lines": "40-101", "hash": "sha256:aaa"}]
    _import(project, catalog1)

    catalog2 = copy.deepcopy(catalog1)
    catalog2["commit"] = "cccccccccccccccccccccccccccccccccccccccc"
    catalog2["workflows"][0]["source_refs"][0]["hash"] = "sha256:bbb"  # only the hash changed
    result = _import(project, catalog2)
    assert result["stale_count"] == 1

    listed = {w["id"]: w for w in repo.list_workflows(project["id"])}
    assert listed["billing.xero-token.refresh"]["stale"] is True

    # a dedup'd Tweak task should have been auto-created, linked to the workflow
    linked = repo.tasks_for_workflow(project["id"], "billing.xero-token.refresh")
    tweak_titles = [t["title"] for t in linked if t["stream"] == "tweak"]
    assert any("Update workflow layout" in t for t in tweak_titles)

    # importing again with the SAME hash should not create a second dedup task
    result2 = _import(project, catalog2)
    assert result2["stale_count"] == 0  # no change from the previous scan this time
    linked_again = repo.tasks_for_workflow(project["id"], "billing.xero-token.refresh")
    tweak_titles_again = [t["title"] for t in linked_again if t["stream"] == "tweak"]
    assert len(tweak_titles_again) == len(tweak_titles)


def test_staleness_not_flagged_when_other_fields_also_changed(project):
    catalog1 = copy.deepcopy(FIXTURE)
    wf = catalog1["workflows"][0]
    wf["confidence"] = "annotated"
    wf["source_refs"] = [{"file": "app/xero_sync.py", "lines": "40-101", "hash": "sha256:aaa"}]
    _import(project, catalog1)

    catalog2 = copy.deepcopy(catalog1)
    catalog2["commit"] = "dddddddddddddddddddddddddddddddddddddddd"
    catalog2["workflows"][0]["source_refs"][0]["hash"] = "sha256:bbb"
    catalog2["workflows"][0]["summary"] = "A different summary entirely."
    result = _import(project, catalog2)
    assert result["stale_count"] == 0  # the annotation DID change alongside the code
    listed = {w["id"]: w for w in repo.list_workflows(project["id"])}
    assert listed["billing.xero-token.refresh"]["stale"] is False
