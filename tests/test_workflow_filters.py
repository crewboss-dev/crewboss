"""Workflows page: filter by trigger source and by downstream impact (the owner's discovery-tool brief)."""
import json
from pathlib import Path

import pytest

from app import config, repo



def _wf(i, trigger, effects, has_ai=False):
    return {"id": f"t.wf-{i}", "name": f"WF {i} {trigger}", "group": "Service", "summary": "", "has_ai": has_ai,
            "confidence": "auto", "trigger": {"type": trigger, "detail": ""}, "steps": [], "edges": [],
            "side_effects": effects, "source_refs": []}


@pytest.fixture
def catalog(project):
    cat = {"project": project["slug"], "commit": "abc123", "generated_at": "2026-10-01T00:00:00Z", "workflows": [
        _wf(1, "timer", ["db:invoices", "xero"]),
        _wf(2, "webhook", ["db:cases"]),
        _wf(3, "button", []),
        _wf(4, "event", ["graph", "db:notifications"], has_ai=True),
    ]}
    repo.import_catalog(project["slug"], cat)
    return project


def test_impacts_classify_side_effects():
    assert config.workflow_impacts([]) == {"none"}
    assert config.workflow_impacts(["db:invoices", "xero"]) == {"data", "external", "money"}
    assert config.workflow_impacts(["db:cases"]) == {"data", "cases"}
    assert config.workflow_impacts(["graph"]) == {"external", "notify"}


def test_repo_filters(catalog):
    ids = lambda **kw: [w["id"] for w in repo.list_workflows(catalog["id"], **kw)]
    assert ids(trigger="timer") == ["t.wf-1"]
    assert ids(impact="money") == ["t.wf-1"]
    assert ids(impact="none") == ["t.wf-3"]
    assert ids(impact="notify", ai_only=True) == ["t.wf-4"]
    assert ids(trigger="webhook", impact="money") == []


def test_page_filters_and_chips(client, catalog):
    html = client.get("/test-proj/workflows?trigger=timer&impact=money").text
    main = html.split("<main", 1)[1]
    assert "WF 1 timer" in main and "WF 2 webhook" not in main
    assert 'aria-label="Remove filter Schedule"' in main and 'aria-label="Remove filter Billing &amp; money"' in main
    assert 'href="/test-proj/workflows?impact=money"' in main  # removing the trigger keeps the impact
    assert "Starts with" in main and "What it changes" in main
    html = client.get("/test-proj/workflows?trigger=webhook&impact=money").text
    assert "No workflows match these filters." in html
    # junk values are ignored, never an error
    assert client.get("/test-proj/workflows?trigger=x&impact=y").status_code == 200
