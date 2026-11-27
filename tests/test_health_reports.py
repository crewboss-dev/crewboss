"""Pushed app health (task #404): servers push batches of health reports with a project-bound, write-only 'reporter'
token; the dashboard keeps the latest per (project, source) by observed_at plus a bounded history, answers each report
(accepted / duplicate / rejected) so the sender can empty its spool, raises "<project> stopped reporting" when a
source's newest OBSERVED report is too old, and feeds the ERP section from pushed reports once the ERP switches."""
import copy
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import auth, config, erp_health, health_reports, repo, tokens
from app.auth import issue_token
from app.db import get_cursor

FEED = json.loads((Path(__file__).resolve().parent.parent / "fixtures/erp-health-v1.json").read_text())
URL = "/api/health/report"


def _now():
    return datetime.now(timezone.utc)


def _report(source="core-1", observed=None, health=None, **extra):
    return {"report_id": str(uuid.uuid4()), "source": source, "observed_at": (observed or _now()).isoformat(),
            "health": health if health is not None else {"v": 1, "generated_at": _now().isoformat(), "events": []},
            **extra}


@pytest.fixture
def feeds(project):
    """The test project pushes; another project ('other') exists to prove isolation."""
    cfg = {project["slug"]: {"mode": "push", "expect_every_seconds": 120, "sources": [], "erp": False,
                             "erp_source": None}}
    health_reports.use_config(cfg)
    yield cfg
    health_reports.use_config(None)
    health_reports._watch_started.clear()


@pytest.fixture
def other():
    return repo.create_project("other-proj", "Other", "internal", "system")


def _h(token):
    return {"Authorization": f"Bearer {token}"}


def _post(client, token, reports, **params):
    return client.post(URL, json={"reports": reports}, headers=_h(token), params=params)


def _outbox():
    with get_cursor() as cur:
        cur.execute("SELECT message FROM outbox WHERE stream = 'errors' ORDER BY id")
        return [r["message"] for r in cur.fetchall()]


# --- the token: write-only, one project ---------------------------------------------------------------------------

def test_a_reporter_posts_its_projects_reports(client, project, feeds):
    tok = issue_token("reporter", "core-1", project_id=project["id"])
    r = _post(client, tok, [_report(), _report(source="web-1")])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["accepted"] == 2 and body["duplicate"] == 0 and body["rejected"] == 0
    assert [x["status"] for x in body["results"]] == ["accepted", "accepted"]
    assert sorted(x["source"] for x in health_reports.latest(project["slug"])) == ["core-1", "web-1"]


def test_a_reporter_cannot_post_another_projects_report(client, project, other, feeds):
    tok = issue_token("reporter", "core-1", project_id=project["id"])
    assert _post(client, tok, [_report()], project=other["slug"]).status_code == 404
    r = _post(client, tok, [_report(project=other["slug"])])
    assert r.json()["results"][0] == {"report_id": r.json()["results"][0]["report_id"], "status": "rejected",
                                      "reason": "project isn't the token's project"}
    assert health_reports.latest(other["slug"]) == [] and health_reports.latest(project["slug"]) == []


def test_a_reporter_token_reads_nothing_and_does_nothing_else(client, project, feeds):
    tok = issue_token("reporter", "core-1", project_id=project["id"])
    h = _h(tok)
    assert client.get("/api/health", headers=h).status_code == 403
    assert client.get("/api/tasks", headers=h).status_code == 403
    assert client.get(f"/api/projects/{project['id']}", headers=h).status_code == 403
    assert client.post("/api/tasks", json={"title": "x", "stream": "bug"}, headers=h).status_code == 403
    assert client.post("/api/catalog/import", json={}, headers=h).status_code == 403
    assert client.get("/api/satellite/snapshot", headers=h).status_code == 403
    assert client.post("/api/health/zabbix/poke", json={}, headers=h).status_code == 403
    assert auth.resolve_session_token(tok) is None               # so no MCP tool either


def test_only_a_reporter_token_may_post(client, project, feeds):
    assert client.post(URL, json={"reports": [_report()]}).status_code == 401
    assert _post(client, issue_token("all", "s", project_id=project["id"]), [_report()]).status_code == 403
    assert _post(client, issue_token("scanner", "s"), [_report()]).status_code == 403
    with pytest.raises(ValueError):
        issue_token("reporter", "loose")                         # must be bound to a project


def test_a_token_bound_to_sources_rejects_the_others(client, project, feeds):
    tok = issue_token("reporter", "core-1", project_id=project["id"], sources=["core-1"])
    body = _post(client, tok, [_report("core-1"), _report("web-1")]).json()
    assert [x["status"] for x in body["results"]] == ["accepted", "rejected"]
    assert "may not report for source 'web-1'" in body["results"][1]["reason"]


def test_tokens_cli_issues_a_reporter_for_one_project(capsys, project):
    assert tokens.main(["create", "--kind", "reporter", "--quiet"]) == 2           # needs --project
    assert tokens.main(["create", "--kind", "bugs", "--source", "x", "--quiet"]) == 2
    assert tokens.main(["create", "--kind", "reporter", "--project", project["slug"], "--source", "core-1",
                        "--quiet"]) == 0
    row = auth.resolve_token(capsys.readouterr().out.strip().splitlines()[-1])
    assert row["stream"] == "reporter" and row["project_id"] == project["id"] and row["sources"] == ["core-1"]


# --- the batch: idempotent, newest observed wins, limits -------------------------------------------------------------

def test_a_resent_batch_is_all_duplicates(client, project, feeds):
    tok = issue_token("reporter", "core-1", project_id=project["id"])
    reports = [_report(), _report()]
    assert _post(client, tok, reports).json()["accepted"] == 2
    again = _post(client, tok, reports).json()
    assert again["duplicate"] == 2 and again["accepted"] == 0
    same = _report()
    assert [x["status"] for x in _post(client, tok, [same, same]).json()["results"]] == ["accepted", "duplicate"]


def test_latest_is_the_newest_observed_not_the_newest_arrival(client, project, feeds):
    tok = issue_token("reporter", "core-1", project_id=project["id"])
    new = _report(observed=_now() - timedelta(minutes=1))
    old = _report(observed=_now() - timedelta(hours=2))       # a spool replayed after an outage
    assert _post(client, tok, [new]).json()["results"][0]["latest"] is True
    res = _post(client, tok, [old]).json()["results"][0]
    assert res["status"] == "accepted" and res["latest"] is False
    (row,) = health_reports.latest(project["slug"])
    assert str(row["report_id"]) == new["report_id"]
    with get_cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM health_reports")
        assert cur.fetchone()["n"] == 2                        # the old one is in the history


def test_history_is_bounded(client, project, feeds):
    tok = issue_token("reporter", "core-1", project_id=project["id"])
    _post(client, tok, [_report()])
    with get_cursor() as cur:
        cur.execute("UPDATE health_reports SET observed_at = now() - interval '8 days'")
    _post(client, tok, [_report()])
    with get_cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM health_reports")
        assert cur.fetchone()["n"] == 1


@pytest.mark.parametrize("breakage, reason", [
    (lambda r: r.update(report_id="not-a-uuid"), "report_id must be a UUID"),
    (lambda r: r.update(source="Core 01"), "source must be"),
    (lambda r: r.update(observed_at="2026-10-15T10:00:00"), "with a zone"),
    (lambda r: r.update(observed_at=(_now() + timedelta(hours=1)).isoformat()), "in the future"),
    (lambda r: r.update(observed_at=(_now() - timedelta(days=8)).isoformat()), "older than the 7-day history"),
    (lambda r: r.update(health={"v": 2}), "unsupported feed version"),
    (lambda r: r.update(health=[1]), "isn't a JSON object"),
    (lambda r: r["health"].update(app_sha="not hex!"), "app_sha"),
    (lambda r: r["health"].update(catalog={"built_at": "x"}), "health.catalog"),
])
def test_each_bad_report_is_rejected_alone(client, project, feeds, breakage, reason):
    tok = issue_token("reporter", "core-1", project_id=project["id"])
    bad = _report()
    breakage(bad)
    body = _post(client, tok, [bad, _report()]).json()
    assert body["results"][0]["status"] == "rejected" and reason in body["results"][0]["reason"]
    assert body["results"][1]["status"] == "accepted"


def test_envelope_and_size_limits(client, project, feeds, monkeypatch):
    tok = issue_token("reporter", "core-1", project_id=project["id"])
    assert client.post(URL, json={"report": []}, headers=_h(tok)).status_code == 400
    assert client.post(URL, json={"reports": []}, headers=_h(tok)).status_code == 400
    assert client.post(URL, content=b"{nope", headers=_h(tok)).status_code == 400
    monkeypatch.setattr(config, "HEALTH_REPORT_MAX_BATCH", 2)
    assert _post(client, tok, [_report(), _report(), _report()]).status_code == 413
    monkeypatch.setattr(config, "HEALTH_REPORT_MAX_BYTES", 300)
    big = _report(health={"v": 1, "events": [], "notes": "x" * 400})
    assert _post(client, tok, [big]).status_code == 413
    assert health_reports.latest(project["slug"]) == []


# --- the dead-man --------------------------------------------------------------------------------------------------

def test_a_source_that_stops_reporting_raises_the_alarm_once_then_recovers(client, project, feeds, monkeypatch):
    monkeypatch.setattr(config, "HEALTH_PROJECT", project["slug"])
    tok = issue_token("reporter", "core-1", project_id=project["id"])
    _post(client, tok, [_report(observed=_now() - timedelta(minutes=7))])     # 3 x 2 min = 6 min allowed
    r = health_reports.check()
    assert r["new"] == 1 and r["escalated"] == 1
    (alarm,) = health_reports.summary()["open"]
    assert alarm["name"].startswith(f"{project['slug']} stopped reporting (core-1): last report observed 7 min")
    assert health_reports.check()["escalated"] == 0                         # once
    # the server comes back and replays its spool: the newest observed report clears it
    _post(client, tok, [_report(observed=_now() - timedelta(minutes=4)), _report(observed=_now())])
    assert health_reports.check()["resolved"] == 1
    assert health_reports.summary()["open"] == []
    assert len(_outbox()) == 1 and "RESOLVED" in _outbox()[0]              # same dedupe key, replaced


def test_a_late_arrival_of_an_old_report_does_not_count_as_alive(client, project, feeds):
    """Received now, observed 10 min ago: the dead-man reads observed_at."""
    tok = issue_token("reporter", "core-1", project_id=project["id"])
    _post(client, tok, [_report(observed=_now() - timedelta(minutes=10))])
    assert health_reports.check()["new"] == 1


def test_an_expected_source_that_never_reported(project, feeds):
    feeds[project["slug"]]["sources"] = ["core-1"]
    health_reports._watch_started[project["slug"]] = _now() - timedelta(minutes=10)
    health_reports.check()
    (alarm,) = health_reports.summary()["open"]
    assert "stopped reporting (core-1): no report yet" in alarm["name"]


def test_pull_projects_are_not_watched(project):
    health_reports.use_config({project["slug"]: {"mode": "pull", "expect_every_seconds": 120, "sources": [],
                                                 "erp": True, "erp_source": None}})
    try:
        assert health_reports.check()["watched"] == []
    finally:
        health_reports.use_config(None)


# --- the ERP, once it pushes ---------------------------------------------------------------------------------------

def test_pushed_erp_reports_feed_the_erp_section_and_nothing_is_fetched(client, project, feeds, monkeypatch):
    monkeypatch.setattr(config, "HEALTH_PROJECT", project["slug"])
    monkeypatch.setattr(config, "ERP_HEALTH_URL", "https://erp.example.invalid")
    feeds[project["slug"]].update(erp=True, erp_source="core-1")
    assert erp_health.pushed() and not erp_health.is_pulling() and erp_health.is_configured()
    tok = issue_token("reporter", "core-1", project_id=project["id"])
    feed = copy.deepcopy(FEED)
    feed["generated_at"] = _now().isoformat()
    feed["app_sha"] = "a" * 40
    feed["catalog"] = {"commit": "b" * 40, "built_at": "2026-10-15T00:00:00Z"}
    assert _post(client, tok, [_report(health=feed)]).json()["accepted"] == 1
    s = erp_health.summary()
    assert s["state"] == "ok" and s["pushed"] and 9101 in {e["id"] for e in s["events"]}
    body, _, _ = health_reports.current(project["slug"])
    assert body["app_sha"] == "a" * 40                       # the admin-catalog check reads this one
    _post(client, tok, [_report(source="web-1", health=copy.deepcopy(feed))])   # not the ERP source: stored only
    html = client.get(f"/{project['slug']}/health").text
    assert "data-app-reports" in html and f'data-report-source="{project["slug"]}:core-1"' in html
