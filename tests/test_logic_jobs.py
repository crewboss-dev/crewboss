"""The logic map on the job paths (task #715): a running job records its commit's report (job MCP record_logic), every brief carries the
rule to update the map, and a job's own deploy ask goes through the gate."""
import pytest

from app import deploys, job_mcp, jobs, logic, repo
from app.db import get_cursor

from test_deploys import SHA, OTHER_SHA, _ask, depl  # noqa: F401  (fixtures/helpers)
from test_devices import est, signed_in  # noqa: F401
from test_jobs import _as_job, world  # noqa: F401
from test_web_auth import auth_on  # noqa: F401


def _report(uncovered=0):
    return {"version": 1, "workflows": [], "errors": [], "stale": [],
            "uncovered": [{"key": f"k{i}", "kind": "route", "file": "a.py", "function": "f", "name": f"POST /{i}", "line": 1} for i in range(uncovered)],
            "discovered": uncovered}


def test_the_brief_carries_the_rule_only_where_the_map_is_in_use(depl):
    c = depl["claim"]
    assert "logic_map" not in jobs.brief(c["job_id"], c["fence"])                       # gate off, nothing ever reported: not asked
    logic.record_report(c["project_id"], SHA, _report(), "ci")                          # a map has been reported
    brief = jobs.brief(c["job_id"], c["fence"])
    assert ".crewboss/logic" in brief["logic_map"] and "record_logic" in brief["logic_map"] and "crewboss logic" in brief["logic_map"]
    with get_cursor() as cur:
        cur.execute("DELETE FROM logic_reports")
        cur.execute("UPDATE projects SET logic_gate = 'enforce' WHERE id = %s", (c["project_id"],))
    assert "logic_map" in jobs.brief(c["job_id"], c["fence"])                           # gate on: asked


def test_a_job_reports_under_its_own_name_and_a_frozen_commit_is_refused(depl, monkeypatch):
    c = depl["claim"]
    _as_job(monkeypatch, c["job_token"])
    job_mcp.record_logic(c["fence"], SHA, _report())
    assert logic.get_report(c["project_id"], SHA)["reported_by"] == f"job:{c['job_id']}"
    _ask(depl)                                                                          # a production request freezes the commit
    with pytest.raises(ValueError, match="no longer change"):
        job_mcp.record_logic(c["fence"], SHA, _report())


def test_a_job_may_report_only_a_few_commits(depl, monkeypatch):
    c = depl["claim"]
    _as_job(monkeypatch, c["job_token"])
    for i in range(logic.JOB_REPORT_COMMITS):
        job_mcp.record_logic(c["fence"], f"{i:040x}", _report())
    with pytest.raises(ValueError, match="at most"):
        job_mcp.record_logic(c["fence"], f"{99:040x}", _report())
    job_mcp.record_logic(c["fence"], f"{0:040x}", _report())                            # re-reporting one of its own is fine


def test_a_job_records_the_report_of_its_own_projects_commit(depl, monkeypatch):
    c = depl["claim"]
    _as_job(monkeypatch, c["job_token"])
    out = job_mcp.record_logic(c["fence"], SHA, _report(2))
    assert out["uncovered"] == 2
    project_id = c["project_id"]
    assert len(logic.get_report(project_id, SHA)["uncovered"]) == 2
    with pytest.raises(ValueError):                                                    # a stale fence changes nothing
        job_mcp.record_logic(c["fence"] + 1, OTHER_SHA, _report())
    assert logic.get_report(project_id, OTHER_SHA) is None
    with pytest.raises(ValueError, match="40-character|sha"):
        job_mcp.record_logic(c["fence"], "short", _report())


def test_a_jobs_deploy_ask_is_refused_by_the_enforced_gate_and_passes_with_a_clean_report(depl, monkeypatch):
    c = depl["claim"]
    with get_cursor() as cur:
        cur.execute("UPDATE projects SET logic_gate = 'enforce' WHERE id = %s", (c["project_id"],))
    with pytest.raises(repo.BadRequest, match="no logic map report"):
        _ask(depl)
    logic.record_report(c["project_id"], SHA, _report(), "job:1")
    row = _ask(depl)
    assert row["state"] == "requested" and row["sha"] == SHA
