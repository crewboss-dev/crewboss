"""#828 piece 1: the ERP tags each health event kind "code_fault" | "ops". The setting erp_health_code_faults_only is OFF
by default (every event tracked and escalated, as before); ON keeps only code_fault events, a missing kind counting as ops."""
from datetime import datetime, timedelta, timezone

import pytest

from app import config, erp_health, repo
from app.db import get_cursor

T0 = datetime(2026, 11, 18, 3, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _health_project(project, monkeypatch):
    monkeypatch.setattr(config, "HEALTH_PROJECT", project["slug"])


def _ev(eid, kind="__absent__", severity="error"):
    e = {"id": eid, "severity": severity, "source": "app", "title": f"event {eid}", "detail": "",
         "first_seen": T0.isoformat(), "last_seen": T0.isoformat(), "count": 1}
    if kind != "__absent__":
        e["kind"] = kind
    return e


def _body(*events):
    return {"v": 1, "generated_at": T0.isoformat(), "events": list(events)}


def _sent():
    return sorted(m["dedupe_key"] for m in repo.list_outbox("errors", undelivered_only=True))


def _rows():
    with get_cursor() as cur:
        cur.execute("SELECT * FROM health_problems WHERE source = 'erp'")
        return {r["eventid"]: r for r in cur.fetchall()}


def _feed(*events, at=0):
    return erp_health.ingest(_body(*events), T0 + timedelta(minutes=at))


def test_default_is_off_and_everything_is_tracked_and_escalated():
    assert config.ERP_HEALTH_CODE_FAULTS_ONLY is False
    _feed()  # first good fetch = baseline
    r = _feed(_ev(1, "code_fault"), _ev(2, "ops"), _ev(3), at=2)
    assert r["new"] == 3 and r["escalated"] == 3
    assert _sent() == ["erp-event:1", "erp-event:2", "erp-event:3"]
    assert {i: row["kind"] for i, row in _rows().items()} == {1: "code_fault", 2: "ops", 3: "ops"}


def test_flag_on_keeps_only_code_faults(monkeypatch):
    monkeypatch.setattr(config, "ERP_HEALTH_CODE_FAULTS_ONLY", True)
    _feed()
    r = _feed(_ev(1, "code_fault"), _ev(2, "ops"), at=2)
    assert r["new"] == 1 and r["escalated"] == 1 and _sent() == ["erp-event:1"]
    assert set(_rows()) == {1}
    shown = erp_health.parse_snapshot(_body(_ev(1, "code_fault"), _ev(2, "ops")))["events"]
    assert [e["kind"] for e in shown] == ["code_fault"]


@pytest.mark.parametrize("kind", ["__absent__", None, "weird", 7])
def test_a_missing_or_unknown_kind_is_ops(monkeypatch, kind):
    assert erp_health.parse_snapshot(_body(_ev(1, kind)))["events"][0]["kind"] == "ops"
    monkeypatch.setattr(config, "ERP_HEALTH_CODE_FAULTS_ONLY", True)
    assert erp_health.parse_snapshot(_body(_ev(1, kind)))["events"] == []


def test_open_ops_events_dont_escalate_after_the_flag_goes_on(monkeypatch):
    _feed(_ev(2, "ops"))                                   # open at the first fetch: a baseline, never sent
    monkeypatch.setattr(config, "ERP_HEALTH_CODE_FAULTS_ONLY", True)
    r = _feed(_ev(2, "ops"), _ev(3, "ops", "critical"), at=4)
    assert r["new"] == 0 and r["escalated"] == 0 and r["open"] == 0
    assert _sent() == []
    assert _rows()[2]["resolved_at"] is not None


def test_a_hidden_event_that_was_never_sent_closes_quietly(monkeypatch):
    _feed(_ev(2, "ops"))                                   # baseline: escalated_at stays NULL
    assert _rows()[2]["escalated_at"] is None
    monkeypatch.setattr(config, "ERP_HEALTH_CODE_FAULTS_ONLY", True)
    r = _feed(_ev(2, "ops"), at=2)
    assert r["escalated"] == 0 and _sent() == [] and _rows()[2]["resolved_at"] is not None


def test_an_already_escalated_event_that_becomes_hidden_still_gets_its_resolved_push(monkeypatch):
    _feed()
    _feed(_ev(2, "ops"), at=2)                             # escalated while the flag was off
    assert _rows()[2]["escalated_at"] is not None
    monkeypatch.setattr(config, "ERP_HEALTH_CODE_FAULTS_ONLY", True)
    r = _feed(_ev(2, "ops"), at=4)                         # flag turned on: it is hidden now
    assert r["resolved"] == 1 and r["escalated"] == 1
    assert "RESOLVED" in repo.list_outbox("errors", undelivered_only=True)[0]["message"]


def test_a_code_fault_the_erp_retags_ops_still_gets_its_resolved_push(monkeypatch):
    monkeypatch.setattr(config, "ERP_HEALTH_CODE_FAULTS_ONLY", True)
    _feed()
    _feed(_ev(5, "code_fault"), at=2)
    assert _sent() == ["erp-event:5"] and _rows()[5]["escalated_at"] is not None
    r = _feed(_ev(5, "ops"), at=4)                         # re-tagged: hidden, so it leaves the open set
    assert r["resolved"] == 1 and r["escalated"] == 1
    assert "RESOLVED" in repo.list_outbox("errors", undelivered_only=True)[0]["message"]


def test_event_row_shows_a_kind_label(client, project):
    erp_health.ingest(_body(_ev(1, "code_fault"), _ev(2, "ops")), datetime.now(timezone.utc))
    html = client.get(f"/{project['slug']}/health").text
    assert 'data-kind="code_fault"' in html and "Code fault" in html and 'data-kind="ops"' in html


def test_an_untagged_event_is_not_labelled_ops_on_the_page(client, project):
    erp_health.ingest(_body(_ev(1), _ev(2, "code_fault")), datetime.now(timezone.utc))
    html = client.get(f"/{project['slug']}/health").text
    assert html.count("data-kind=") == 1 and 'data-kind="code_fault"' in html     # event 1 carried no kind: no chip
    assert erp_health.parse_snapshot(_body(_ev(1)))["events"][0]["kind"] == "ops"  # still ops for filtering
