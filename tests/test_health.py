"""Phase 3 System Health: the Zabbix catch-up (bookmark + open set), baseline, escalation to the errors outbox,
the poke endpoint's auth, and the pages. A fake client stands in for Zabbix — nothing touches the network."""
import pytest

from app import config, health, repo
from app.auth import issue_token
from app.db import get_cursor


class FakeZabbix:
    """problem events keyed by eventid; a recovery is a separate event the problem's r_eventid points at."""

    def __init__(self):
        self.events = {}
        self.calls = []
        self.fail = None

    def open(self, eventid, name, severity=3, host="core-1", objectid=None, clock=1_790_000_000):
        self.events[str(eventid)] = {"eventid": str(eventid), "objectid": str(objectid or 40000 + eventid),
                                     "clock": str(clock), "name": name, "severity": str(severity),
                                     "r_eventid": "0", "value": "1", "suppressed": "0", "hosts": [{"hostid": "1", "name": host}]}

    def resolve(self, eventid, r_eventid, clock=1_790_000_600):
        self.events[str(r_eventid)] = {"eventid": str(r_eventid), "clock": str(clock), "value": "0",
                                       "r_eventid": "0", "name": "", "severity": "0"}
        self.events[str(eventid)]["r_eventid"] = str(r_eventid)

    def _problems(self):
        return [e for e in self.events.values() if e["value"] == "1"]

    def call(self, method, params):
        assert method.endswith(".get")
        self.calls.append((method, params))
        if self.fail:
            raise health.ZabbixError(self.fail)
        if method == "problem.get":
            rows = [e for e in self._problems() if e["r_eventid"] == "0"]
            if params.get("suppressed") is False:  # Zabbix: only problems not silenced by a maintenance
                rows = [e for e in rows if e.get("suppressed") != "1"]
            return [{"eventid": e["eventid"]} for e in rows]
        if "eventids" in params:
            return [dict(self.events[i]) for i in params["eventids"] if i in self.events]
        rows = sorted(self._problems(), key=lambda e: int(e["eventid"]))
        if "eventid_from" in params:
            rows = [e for e in rows if int(e["eventid"]) >= int(params["eventid_from"])]
        if params.get("sortorder") == "DESC":
            rows = rows[::-1]
        return [dict(e) for e in rows[: params.get("limit") or None]]


@pytest.fixture
def zbx(project, monkeypatch):
    monkeypatch.setattr(config, "HEALTH_PROJECT", project["slug"])
    return FakeZabbix()


def _rows():
    with get_cursor() as cur:
        cur.execute("SELECT * FROM health_problems ORDER BY eventid")
        return {r["eventid"]: r for r in cur.fetchall()}


def _errors_outbox():
    return repo.list_outbox("errors", undelivered_only=True)


def test_first_sync_is_baseline_and_escalates_nothing(zbx):
    zbx.open(100, "Reboot required", severity=3)
    zbx.open(101, "learn.example.org down", severity=4, host="mon-1")
    r = health.sync(zbx)
    assert r["ok"] and r["first"] and r["new"] == 2 and r["escalated"] == 0
    rows = _rows()
    assert all(row["baseline"] and row["resolved_at"] is None for row in rows.values())
    assert rows[101]["host"] == "mon-1"
    assert _errors_outbox() == []
    assert health.summary()["source"]["bookmark"] == 101


def test_new_problem_escalates_once_and_recovery_coalesces(zbx):
    zbx.open(100, "old", severity=3)
    health.sync(zbx)
    zbx.open(200, "Disk full", severity=4, host="core-1", objectid=555)
    r = health.sync(zbx)
    assert r["new"] == 1 and r["escalated"] == 1
    box = _errors_outbox()
    assert len(box) == 1 and box[0]["kind"] == "health_error"
    assert "core-1" in box[0]["message"] and "Disk full" in box[0]["message"] and "High" in box[0]["message"]
    assert _rows()[200]["escalated_at"] is not None
    # a second sync with nothing new pushes nothing more
    assert health.sync(zbx)["escalated"] == 0
    assert len(_errors_outbox()) == 1
    # recovery: same dedupe key, so the undelivered row now says RESOLVED rather than piling up
    zbx.resolve(200, 201, clock=1_790_000_900)
    r = health.sync(zbx)
    assert r["resolved"] == 1
    box = _errors_outbox()
    assert len(box) == 1 and "RESOLVED" in box[0]["message"]
    assert _rows()[200]["resolved_at"].timestamp() == 1_790_000_900


def test_suppressed_problem_is_not_open_and_a_new_suppressed_one_is_never_escalated(zbx):
    zbx.open(100, "VM Not running", severity=3)
    zbx.open(101, "other", severity=3)
    health.sync(zbx)
    # a maintenance starts: 100 becomes suppressed -> handled like a recovery
    zbx.events["100"]["suppressed"] = "1"
    r = health.sync(zbx)
    assert r["resolved"] == 1 and r["open"] == 1
    rows = _rows()
    assert rows[100]["resolved_at"] is not None and rows[101]["resolved_at"] is None
    assert all(p.get("suppressed") is False for m, p in zbx.calls if m == "problem.get")
    # a problem born suppressed is recorded resolved and never escalated
    zbx.open(200, "agent unavailable", severity=4)
    zbx.events["200"]["suppressed"] = "1"
    r = health.sync(zbx)
    assert r["escalated"] == 0 and _rows()[200]["resolved_at"] is not None
    assert _errors_outbox() == []


def test_below_threshold_and_baseline_recovery_stay_quiet(zbx):
    zbx.open(100, "baseline", severity=4)
    health.sync(zbx)
    zbx.open(300, "Security updates pending", severity=2)
    assert health.sync(zbx)["escalated"] == 0
    zbx.resolve(100, 301)
    r = health.sync(zbx)
    assert r["resolved"] == 1 and r["escalated"] == 0
    assert _errors_outbox() == []


def test_problem_that_opened_and_cleared_between_syncs_is_recorded_resolved_not_sent(zbx):
    health.sync(zbx)  # empty first sync
    zbx.open(400, "blip", severity=5)
    zbx.resolve(400, 401, clock=1_790_000_060)
    r = health.sync(zbx)
    assert r["new"] == 1 and r["escalated"] == 0
    assert _rows()[400]["resolved_at"].timestamp() == 1_790_000_060
    assert _errors_outbox() == []


def test_sync_failure_is_recorded_not_raised(zbx, monkeypatch):
    monkeypatch.setattr(health, "is_configured", lambda: True)
    zbx.fail = "No permissions"
    r = health.sync(zbx)
    assert r == {"ok": False, "error": "problem.get: No permissions"} or not r["ok"]
    s = health.summary()
    assert s["state"] == "error" and "No permissions" in s["source"]["last_error"]
    zbx.fail = None
    assert health.sync(zbx)["ok"]
    assert health.summary()["state"] == "ok"


def test_client_refuses_writes():
    with pytest.raises(ValueError):
        health.ZabbixClient("http://x", "t").call("host.update", {})


def test_not_configured_without_token_file(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ZABBIX_TOKEN_FILE", str(tmp_path / "missing"))
    assert health.client_from_config() is None
    assert health.sync() == {"ok": False, "error": "not configured"}
    assert health.slot()["label"] == "not connected"


def test_escalation_skipped_when_health_project_missing(zbx, monkeypatch):
    monkeypatch.setattr(config, "HEALTH_PROJECT", "no-such-project")
    health.sync(zbx)
    zbx.open(500, "down", severity=5)
    assert health.sync(zbx)["escalated"] == 0


# --- the poke endpoint ------------------------------------------------------------------------------

def test_poke_requires_the_zabbix_role_token(client, project):
    url = "/api/health/zabbix/poke"
    assert client.post(url, json={"eventid": "1"}).status_code == 401
    session = issue_token("bugs", "t", project_id=project["id"])
    assert client.post(url, json={"eventid": "1"}, headers={"Authorization": f"Bearer {session}"}).status_code == 403
    scanner = issue_token("scanner", "s")
    assert client.post(url, json={"eventid": "1"}, headers={"Authorization": f"Bearer {scanner}"}).status_code == 403
    tok = issue_token("zabbix", "zabbix-poke")
    h = {"Authorization": f"Bearer {tok}"}
    r = client.post(url, json={"eventid": "4535791", "event_value": "1", "anything": "<script>"}, headers=h)
    assert r.status_code == 202
    assert health.summary()["source"]["last_poke_at"] is not None
    assert client.post(url, json={"eventid": "1; drop table"}, headers=h).status_code == 422


def test_zabbix_token_cannot_act_as_a_session(client, project):
    tok = issue_token("zabbix", "zabbix-poke")
    r = client.post("/api/tasks", json={"title": "x", "stream": "bug"}, headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403


# --- pages ------------------------------------------------------------------------------------------

def test_health_page_lists_open_problems(client, zbx, monkeypatch):
    monkeypatch.setattr(health, "is_configured", lambda: True)
    zbx.open(100, "Reboot required", severity=3, host="pbx-1")
    health.sync(zbx)
    html = client.get(f"/{config.HEALTH_PROJECT}/health").text
    assert "Reboot required" in html and "pbx-1" in html and "baseline" in html
    assert 'data-health-state="ok"' in html
    assert "phase 3" not in html


def test_health_page_other_project_has_no_source(client, zbx):
    other = repo.create_project("other-proj", "Other", "internal", "system")
    html = client.get(f"/{other['slug']}/health").text
    assert "No health source for this project yet" in html


def test_home_and_overview_show_the_slot(client, zbx, monkeypatch):
    monkeypatch.setattr(health, "is_configured", lambda: True)
    health.sync(zbx)
    home = client.get("/").text
    assert "Health &amp; alerts: all clear" in home
    assert "connects in phase 3" not in home.replace("Business pulse", "")
    overview = client.get(f"/{config.HEALTH_PROJECT}").text           # Overview merged into Summary (#773)
    assert "Failing now" in overview and "all clear" in overview


# --- standing responsibilities head the briefing ------------------------------------------------------

def test_responsibilities_always_head_the_ground_briefing(project):
    r = repo.create_knowledge(project["id"], "responsibility", "Own the Zabbix integration", "detail", [], "owner",
                              False, "owner", summary="Funnel + templates for every device connected.")
    for i in range(7):  # more decisions than the briefing's 5 most recent
        repo.create_knowledge(project["id"], "decision", f"Decision {i}", "", [], "", False, "owner", summary="s")
    g = repo.build_ground("bugs", project["slug"])
    assert [x["title"] for x in g["responsibilities"]] == ["Own the Zabbix integration"]
    text = repo.render_ground_text(g)
    assert "YOU ARE RESPONSIBLE FOR" in text and f"[k{r['id']}] Own the Zabbix integration" in text
    assert text.index("YOU ARE RESPONSIBLE FOR") < text.index("ASSIGNED TO YOU")
    repo.archive_knowledge(r["id"], "no longer", "owner")
    assert repo.build_ground("bugs", project["slug"])["responsibilities"] == []
