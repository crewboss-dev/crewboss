"""#828 piece 2: the thin host signal per deploy server. `health.host` is an optional, validated part of a pushed report; a server
is Reachable while its newest OBSERVED report is fresh (the dead-man rule); disk free below 10% raises one error that clears on
recovery; TLS days come from tls_check; last deploy from the deploy records. Shown as a compact row on the Machines page, each
status a word as well as a colour."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from psycopg.types.json import Json

from app import auth, config, health_reports, host_signal, repo, tls_check
from app.db import get_cursor

from test_deploys import depl  # noqa: F401  (fixture: acme, ready server web-1, target 'prod' of project acme-app)
from test_devices import est, signed_in  # noqa: F401
from test_jobs import world  # noqa: F401
from test_web_auth import auth_on  # noqa: F401

NOW = datetime.now(timezone.utc)


def _pid():
    return repo.get_project_by_slug("acme-app")["id"]


def _put(source="web-1", observed=None, host=None, **health_extra):
    """A report as the receiver stores it (the latest per project and source)."""
    observed = observed or NOW
    health = {"v": 1, "generated_at": observed.isoformat(), "events": [], **health_extra}
    if host is not None:
        health["host"] = host
    with get_cursor() as cur:
        cur.execute("INSERT INTO health_report_latest (project_id, source, report_id, observed_at, received_at, health) "
                    "VALUES (%s, %s, %s, %s, now(), %s) ON CONFLICT (project_id, source) DO UPDATE SET "
                    "report_id = EXCLUDED.report_id, observed_at = EXCLUDED.observed_at, health = EXCLUDED.health",
                    (_pid(), source, str(uuid.uuid4()), observed, Json(health)))


@pytest.fixture
def feeds(depl, monkeypatch):  # noqa: F811
    cfg = {"acme-app": {"mode": "push", "expect_every_seconds": 120, "sources": [], "erp": False, "erp_source": None,
                        "public_url": ""}}
    health_reports.use_config(cfg)
    monkeypatch.setattr(config, "HEALTH_PROJECT", "acme-app")
    host_signal._tls_cache.clear()
    host_signal._inflight.clear()
    monkeypatch.setattr(host_signal, "_spawn", lambda fn: fn())     # the background refresh runs inline
    yield cfg
    health_reports.use_config(None)
    health_reports._watch_started.clear()


def _server(depl):  # noqa: F811
    return {"id": depl["server_id"], "name": "web-1"}


def _outbox():
    return [m["message"] for m in repo.list_outbox("errors", undelivered_only=True)]


# --- validation ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("host,ok", [
    ({"disk_free_pct": 42.5, "disk_path": "/", "uptime_s": 1000}, True),
    ({"disk_free_pct": 0}, True), ({"disk_free_pct": 100}, True), ({}, True),
    ({"disk_free_pct": -1}, False), ({"disk_free_pct": 101}, False), ({"disk_free_pct": "40"}, False),
    ({"disk_free_pct": True}, False), ({"disk_free_pct": float("nan")}, False),
    ({"disk_path": ""}, False), ({"disk_path": "x" * 201}, False), ({"disk_path": 5}, False),
    ({"uptime_s": -1}, False), ({"uptime_s": 1.5}, False), ({"uptime_s": True}, False),
    ("disk", False), ([], False), (None, False),
])
def test_host_is_validated(host, ok):
    assert (health_reports.check_host(host) == "") is ok


def test_a_report_with_a_bad_host_is_rejected_and_a_good_one_kept(client, depl, feeds):  # noqa: F811
    tok = auth.issue_token("reporter", "r", project_id=_pid())
    good = {"report_id": str(uuid.uuid4()), "source": "web-1", "observed_at": NOW.isoformat(),
            "health": {"v": 1, "events": [], "host": {"disk_free_pct": 55.0, "disk_path": "/", "uptime_s": 9}}}
    bad = {**good, "report_id": str(uuid.uuid4()), "health": {"v": 1, "events": [], "host": {"disk_free_pct": 140}}}
    r = client.post("/api/health/report", json={"reports": [good, bad]}, headers={"Authorization": f"Bearer {tok}"}).json()
    assert [x["status"] for x in r["results"]] == ["accepted", "rejected"] and "disk_free_pct" in r["results"][1]["reason"]
    assert health_reports.latest("acme-app")[0]["health"]["host"] == {"disk_free_pct": 55.0, "disk_path": "/", "uptime_s": 9}


# --- the signal ---------------------------------------------------------------------------------------------------

def test_no_report_no_deploys_no_url(depl, feeds):  # noqa: F811
    s = host_signal.summary_for(_server(depl), NOW)
    assert (s["reachable"]["word"], s["disk"]["word"], s["tls"]["word"], s["deploy"]["word"]) == (
        "No reports", "No data", "No public URL", "No deploys yet")
    assert {v["tone"] for v in s.values()} == {"none"}


def test_reachable_follows_the_dead_man_window(depl, feeds):  # noqa: F811
    _put(observed=NOW - timedelta(minutes=5))                      # 3 x 120 s = 6 min
    assert host_signal.summary_for(_server(depl), NOW)["reachable"] == {
        "word": "Reporting", "tone": "ok", "observed_at": NOW - timedelta(minutes=5)}
    _put(observed=NOW - timedelta(minutes=30))
    r = host_signal.summary_for(_server(depl), NOW)["reachable"]
    assert r["tone"] == "bad" and r["word"] == "Silent for 30 min"


def test_a_report_for_another_source_is_not_this_server(depl, feeds):  # noqa: F811
    _put(source="db01")
    assert host_signal.summary_for(_server(depl), NOW)["reachable"]["word"] == "No reports"


@pytest.mark.parametrize("pct,tone", [(55, "ok"), (15, "warn"), (9, "bad")])
def test_disk_word_and_tone(depl, feeds, pct, tone):  # noqa: F811
    _put(host={"disk_free_pct": pct, "disk_path": "/"})
    d = host_signal.summary_for(_server(depl), NOW)["disk"]
    assert d["word"] == f"Disk {pct}% free (/)" and d["tone"] == tone


def _tls_word(depl):  # noqa: F811
    """The first look only starts the refresh (it shows Checking); the next page build reads the cached answer."""
    host_signal.summary_for(_server(depl), NOW)
    return host_signal.summary_for(_server(depl), NOW)["tls"]


def test_tls_days_come_from_tls_check_and_are_cached(depl, feeds, monkeypatch):  # noqa: F811
    feeds["acme-app"]["public_url"] = "https://app.example.test"
    calls = []
    monkeypatch.setattr(tls_check, "check", lambda url: calls.append(url) or tls_check.Result(True, "", 80, "app.example.test"))
    assert host_signal.summary_for(_server(depl), NOW)["tls"]["word"] == "Checking"
    t = host_signal.summary_for(_server(depl), NOW)["tls"]
    assert (t["word"], t["tone"], t["days_left"]) == ("TLS 80 days left", "ok", 80)
    host_signal.summary_for(_server(depl), NOW)
    assert calls == ["https://app.example.test"]
    host_signal._tls_cache.clear()
    monkeypatch.setattr(tls_check, "check", lambda url: tls_check.Result(False, "expires in 3 day(s)", 3, "h"))
    assert _tls_word(depl)["tone"] == "warn"
    host_signal._tls_cache.clear()
    monkeypatch.setattr(tls_check, "check", lambda url: tls_check.Result(False, "not trusted", None, "h"))
    assert _tls_word(depl)["word"] == "TLS problem"


def test_a_plain_http_url_is_not_checked(depl, feeds, monkeypatch):  # noqa: F811
    feeds["acme-app"]["public_url"] = "http://app.example.test"
    monkeypatch.setattr(tls_check, "check", lambda url: pytest.fail("must not dial"))
    assert host_signal.summary_for(_server(depl), NOW)["tls"]["word"] == "No public URL"


def test_last_deploy_comes_from_finished_deploys_on_the_servers_targets(depl, feeds):  # noqa: F811
    with get_cursor() as cur:
        cur.execute("INSERT INTO server_jobs (account_id, project_id, target_id, target_name, sha, state, env, finished_at) "
                    "VALUES (%s, %s, %s, 'prod', %s, 'done', 'prod', %s)",
                    (depl["acme"]["id"], _pid(), depl["target"]["id"], "b" * 40, NOW - timedelta(hours=3)))
    d = host_signal.summary_for(_server(depl), NOW)["deploy"]
    assert d["word"] == "Deployed 3 h ago" and d["tone"] == "ok"


# --- the low-disk finding -----------------------------------------------------------------------------------------

def test_low_disk_is_an_error_sent_once_and_clears_on_recovery(depl, feeds):  # noqa: F811
    _put(host={"disk_free_pct": 6.0, "disk_path": "/var"})
    assert health_reports.check(NOW)["new"] == 1
    box = _outbox()
    assert len(box) == 1 and "low disk on web-1, 6% free (/var)" in box[0]
    assert health_reports.check(NOW)["new"] == 0 and len(_outbox()) == 1              # once
    _put(observed=NOW + timedelta(seconds=30), host={"disk_free_pct": 40.0, "disk_path": "/var"})
    r = health_reports.check(NOW + timedelta(seconds=40))
    assert r["resolved"] == 1
    assert len(_outbox()) == 1 and "RESOLVED" in _outbox()[0]                          # same key: the alert is replaced


def test_healthy_disk_raises_nothing(depl, feeds):  # noqa: F811
    _put(host={"disk_free_pct": 10.0})
    assert health_reports.check(NOW)["new"] == 0 and _outbox() == []


# --- the page -----------------------------------------------------------------------------------------------------

def test_the_machines_page_shows_one_compact_row_with_words(depl, feeds, client, signed_in):  # noqa: F811
    _put(observed=datetime.now(timezone.utc), host={"disk_free_pct": 8, "disk_path": "/"})   # the page uses the real clock
    r = client.get("/acme/machines?role=deploys", headers={"cookie": signed_in["cookie"]})
    assert r.status_code == 200
    html = r.text
    assert html.count("data-host-signal") == 1
    for key in ("reachable", "disk", "tls", "deploy"):
        assert f'data-signal="{key}"' in html
    assert "Reporting" in html and "Disk 8% free" in html and 'data-tone="bad"' in html and "No deploys yet" in html


# --- review fixes: hysteresis, worst fresh disk, viewer scope, no dial on the page, feeds once ------------------------

def test_low_disk_has_hysteresis_one_raise_no_clear_no_second_raise(depl, feeds):  # noqa: F811
    _put(observed=NOW, host={"disk_free_pct": 9.8, "disk_path": "/"})
    assert health_reports.check(NOW)["new"] == 1
    _put(observed=NOW + timedelta(seconds=30), host={"disk_free_pct": 10.3, "disk_path": "/"})
    r = health_reports.check(NOW + timedelta(seconds=35))
    assert r["new"] == 0 and r["resolved"] == 0                                   # above 10 but not above 20: stays open
    _put(observed=NOW + timedelta(seconds=60), host={"disk_free_pct": 9.7, "disk_path": "/"})
    r = health_reports.check(NOW + timedelta(seconds=65))
    assert r["new"] == 0 and r["resolved"] == 0
    assert len(_outbox()) == 1 and "RESOLVED" not in _outbox()[0]
    _put(observed=NOW + timedelta(seconds=90), host={"disk_free_pct": 20.5, "disk_path": "/"})
    assert health_reports.check(NOW + timedelta(seconds=95))["resolved"] == 1     # clears only above 20%


def _second_project(depl, slug="acme-two"):  # noqa: F811
    p = repo.create_project(slug, "Two", "internal", "system")
    with get_cursor() as cur:
        cur.execute("INSERT INTO deploy_targets (project_id, server_id, name, repo_path) VALUES (%s, %s, 'prod', '/srv/two')",
                    (p["id"], depl["server_id"]))
    return p


def _put_for(project_id, source="web-1", observed=None, host=None):
    observed = observed or NOW
    health = {"v": 1, "generated_at": observed.isoformat(), "events": [], "host": host}
    with get_cursor() as cur:
        cur.execute("INSERT INTO health_report_latest (project_id, source, report_id, observed_at, received_at, health) "
                    "VALUES (%s, %s, %s, %s, now(), %s) ON CONFLICT (project_id, source) DO UPDATE SET "
                    "observed_at = EXCLUDED.observed_at, health = EXCLUDED.health",
                    (project_id, source, str(uuid.uuid4()), observed, Json(health)))


def test_disk_shows_the_worst_fresh_report_with_its_path(depl, feeds):  # noqa: F811
    two = _second_project(depl)
    _put(host={"disk_free_pct": 60, "disk_path": "/"})
    _put_for(two["id"], host={"disk_free_pct": 5, "disk_path": "/data"})
    d = host_signal.summary_for(_server(depl), NOW)["disk"]
    assert d["word"] == "Disk 5% free (/data)" and d["path"] == "/data" and d["tone"] == "bad"


def test_a_stale_report_is_ignored_for_disk_and_none_fresh_is_no_data(depl, feeds):  # noqa: F811
    two = _second_project(depl)
    _put(host={"disk_free_pct": 60, "disk_path": "/"})
    _put_for(two["id"], observed=NOW - timedelta(hours=2), host={"disk_free_pct": 2, "disk_path": "/old"})
    assert host_signal.summary_for(_server(depl), NOW)["disk"]["word"] == "Disk 60% free (/)"
    _put(observed=NOW - timedelta(hours=3), host={"disk_free_pct": 60, "disk_path": "/"})
    d = host_signal.summary_for(_server(depl), NOW)["disk"]
    assert d == {"word": "No data", "tone": "none", "free_pct": None, "path": ""}


def test_only_the_viewers_projects_take_part(depl, feeds):  # noqa: F811
    two = _second_project(depl)
    _put(host={"disk_free_pct": 60, "disk_path": "/"})
    _put_for(two["id"], host={"disk_free_pct": 5, "disk_path": "/secret"})
    feeds["acme-two"] = {"mode": "push", "expect_every_seconds": 120, "public_url": "https://hidden.example.test"}
    host_signal._tls_cache["https://hidden.example.test"] = (host_signal.time.monotonic(),
                                                             tls_check.Result(True, "", 50, "hidden.example.test"))
    seen = host_signal.summary_for(_server(depl), NOW, visible_ids=[_pid()])
    assert seen["disk"]["word"] == "Disk 60% free (/)" and seen["tls"]["word"] == "No public URL"
    assert "hidden.example.test" not in str(seen) and "/secret" not in str(seen)
    all_ = host_signal.summary_for(_server(depl), NOW)
    assert all_["disk"]["path"] == "/secret" and all_["tls"]["hosts"] == ["hidden.example.test"]
    assert host_signal.summary_for(_server(depl), NOW, visible_ids=[])["reachable"]["word"] == "No reports"


def test_a_page_never_dials_a_miss_shows_checking_and_refreshes_once_per_url(depl, feeds, monkeypatch):  # noqa: F811
    feeds["acme-app"]["public_url"] = "https://app.example.test"
    spawned, dialled = [], []
    monkeypatch.setattr(host_signal, "_spawn", lambda fn: spawned.append(fn))             # captured, not run
    monkeypatch.setattr(tls_check, "check", lambda url: dialled.append(url) or tls_check.Result(True, "", 90, "app.example.test"))
    t = host_signal.summary_for(_server(depl), NOW)["tls"]
    assert t["word"] == "Checking" and t["tone"] == "none" and dialled == [] and len(spawned) == 1
    host_signal.summary_for(_server(depl), NOW)
    assert len(spawned) == 1                                                              # one at a time per URL
    spawned[0]()                                                                          # the background thread finishes
    assert dialled == ["https://app.example.test"]
    assert host_signal.summary_for(_server(depl), NOW)["tls"]["word"] == "TLS 90 days left" and len(spawned) == 1


def test_tls_cache_lives_an_hour_when_good_and_five_minutes_when_bad(depl, feeds, monkeypatch):  # noqa: F811
    spawned = []
    monkeypatch.setattr(host_signal, "_spawn", lambda fn: spawned.append(fn))
    url = "https://app.example.test"
    host_signal._tls_cache[url] = (1000.0, tls_check.Result(True, "", 90, "h"))
    assert host_signal._tls(url, 1000.0 + 3599).days_left == 90 and spawned == []
    assert host_signal._tls(url, 1000.0 + 3601).days_left == 90 and len(spawned) == 1       # stale value served, refresh started
    host_signal._inflight.clear()
    host_signal._tls_cache[url] = (1000.0, tls_check.Result(False, "not trusted", None, "h"))
    assert host_signal._tls(url, 1000.0 + 299) is not None and len(spawned) == 1
    host_signal._tls(url, 1000.0 + 301)
    assert len(spawned) == 2


def test_the_feeds_are_passed_in_not_reread_per_project(depl, feeds, monkeypatch):  # noqa: F811
    _second_project(depl)
    monkeypatch.setattr(health_reports, "feeds", lambda: pytest.fail("feeds() must not be called when passed in"))
    host_signal.summary_for(_server(depl), NOW, feeds=feeds)


def test_a_page_build_reads_the_feeds_once(depl, feeds, client, signed_in, monkeypatch):  # noqa: F811
    real, calls = health_reports.feeds, []
    monkeypatch.setattr(health_reports, "feeds", lambda: calls.append(1) or real())
    assert client.get("/acme/machines?role=deploys", headers={"cookie": signed_in["cookie"]}).status_code == 200
    assert len(calls) == 1
