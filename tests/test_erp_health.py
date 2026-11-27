"""Task #196: the ERP health feed. Fixture-driven: the v1 contract shape lives in fixtures/erp-health-v1.json and a
loopback stub HTTPS server (a throwaway self-signed cert) plays the ERP, so the real client (https only, verified
TLS, token files read at use time, no redirects, error bodies) is exercised without touching the network or the ERP's
database."""
import copy
import json
import ssl
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app import config, erp_health, health, repo
from app.db import get_cursor

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
FEED = json.loads((FIXTURES / "erp-health-v1.json").read_text())
AI_CALLS = json.loads((FIXTURES / "erp-health-ai-calls-v1.json").read_text())
GEN = datetime(2026, 10, 5, 3, 0, tzinfo=timezone.utc)  # the fixture's generated_at
OWN = {"Origin": "https://crewboss.example.com", "Sec-Fetch-Site": "same-origin"}
READ_TOKEN, RESOLVE_TOKEN = "read-tok-abc123", "resolve-tok-xyz789"


# --- a loopback ERP ---------------------------------------------------------------------------------

class ErpStub:
    def __init__(self):
        self.health_body = copy.deepcopy(FEED)
        self.health_status = 200
        self.ai_calls_body = copy.deepcopy(AI_CALLS)
        self.resolve_status, self.resolve_body = 200, {"ok": True}
        self.redirect = False
        self.requests = []

    def handler(self):
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _send(self, status, body):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):  # noqa: N802
                stub.requests.append(("GET", self.path, self.headers.get("Authorization"), None))
                if stub.redirect:
                    self.send_response(302)
                    self.send_header("Location", "http://127.0.0.1:9/elsewhere")
                    self.end_headers()
                    return
                if self.path == "/api/health":
                    self._send(stub.health_status, stub.health_body)
                elif self.path.startswith("/api/health/ai-calls"):
                    self._send(200, stub.ai_calls_body)
                else:
                    self._send(404, {"detail": "not found"})

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                stub.requests.append(("POST", self.path, self.headers.get("Authorization"), body))
                self._send(stub.resolve_status, stub.resolve_body)

        return H


@pytest.fixture(scope="session")
def tls_cert(tmp_path_factory):
    """A self-signed cert for 127.0.0.1, made once per run with openssl."""
    d = tmp_path_factory.mktemp("erp-tls")
    cert, key = d / "cert.pem", d / "key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert),
                    "-days", "2", "-subj", "/CN=127.0.0.1", "-addext", "subjectAltName=IP:127.0.0.1"],
                   check=True, capture_output=True)
    return cert, key


@pytest.fixture
def erp(project, monkeypatch, tmp_path, tls_cert):
    """A configured feed pointing at the stub over https: URL + both token files (0600), health project = the test
    project. The client trusts ONLY the stub's cert (verification stays on)."""
    cert, key = tls_cert
    stub = ErpStub()
    server = ThreadingHTTPServer(("127.0.0.1", 0), stub.handler())
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(str(cert), str(key))
    server.socket = server_ctx.wrap_socket(server.socket, server_side=True)
    monkeypatch.setattr(erp_health, "_ssl_context", lambda: ssl.create_default_context(cafile=str(cert)))
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    read, resolve = tmp_path / "erp-health-read-token", tmp_path / "erp-health-resolve-token"
    read.write_text(READ_TOKEN + "\n")
    resolve.write_text(RESOLVE_TOKEN + "\n")
    read.chmod(0o600)
    resolve.chmod(0o600)
    monkeypatch.setattr(config, "ERP_HEALTH_URL", f"https://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setattr(config, "ERP_HEALTH_READ_TOKEN_FILE", str(read))
    monkeypatch.setattr(config, "ERP_HEALTH_RESOLVE_TOKEN_FILE", str(resolve))
    monkeypatch.setattr(config, "HEALTH_PROJECT", project["slug"])
    stub.read_file, stub.resolve_file = read, resolve
    yield stub
    server.shutdown()
    server.server_close()


@pytest.fixture
def unconfigured(project, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ERP_HEALTH_URL", "")
    monkeypatch.setattr(config, "ERP_HEALTH_READ_TOKEN_FILE", str(tmp_path / "missing-read"))
    monkeypatch.setattr(config, "ERP_HEALTH_RESOLVE_TOKEN_FILE", str(tmp_path / "missing-resolve"))
    monkeypatch.setattr(config, "HEALTH_PROJECT", project["slug"])


def _fresh(stub):
    """Make the stub's snapshot current, for page tests that run on the real clock."""
    stub.health_body["generated_at"] = datetime.now(timezone.utc).isoformat()


def _errors_outbox():
    return repo.list_outbox("errors", undelivered_only=True)


def _event(eid, severity="error", title="Something broke", source="invoicing"):
    return {"id": eid, "severity": severity, "source": source, "title": title, "detail": "d",
            "first_seen": "2026-10-05T02:59:00+00:00", "last_seen": "2026-10-05T03:00:00+00:00", "count": 1}


def _rows():
    with get_cursor() as cur:
        cur.execute("SELECT * FROM health_problems WHERE source = 'erp' ORDER BY eventid")
        return {r["eventid"]: r for r in cur.fetchall()}


# --- the parser --------------------------------------------------------------------------------------

def test_parser_reads_the_v1_shape():
    s = erp_health.parse_snapshot(copy.deepcopy(FEED))
    assert s["generated_at"] == GEN and s["missing"] == [] and s["events_present"]
    assert [e["id"] for e in s["events"]] == [9101, 9102, 9103]  # critical, error, warning
    crit = s["events"][0]
    assert crit["level"] == 5 and crit["source"] == "backup" and crit["title"] == "Nightly restic backup failed"
    assert '"status": 400' in s["events"][1]["detail"] and s["events"][1]["count"] == 4  # jsonb detail -> text
    f = {x["key"]: x for x in s["failures"]}
    assert [x["key"] for x in s["failures"]][:2] == ["invoices_failed", "provision_errors"]
    assert f["invoices_failed"]["count"] == 2 and f["invoices_failed"]["label"] == "Failed invoices"
    assert f["invoices_failed"]["entries"][0]["label"] == "INV-0412 · Acme Pty Ltd"
    assert f["invoices_failed"]["entries"][0]["detail"] == "Xero 400 ValidationException"
    hb = {h["name"]: h for h in s["heartbeats"]}
    assert hb["worker:invoicing"]["red"] and not hb["worker:casegen"]["red"]
    assert s["heartbeats"][0]["name"] == "worker:invoicing"  # late first
    assert s["webhooks"][0]["source"] == "stripe" and s["webhooks"][0]["errors"] == 1  # failing first
    assert s["xero"]["breaker"] == "closed" and s["xero"]["tone"] == "ok" and s["xero"]["pushes_today"] == 17
    u = s["ai_usage"]
    assert [p[0] for p in u["periods"]] == ["Today", "Month"] and "Input tokens" in u["columns"]
    assert u["tables"][0]["label"] == "By model" and u["tables"][0]["rows"][0][0] == "claude-sonnet"


def test_parser_missing_optional_sections_are_named_not_all_clear():
    body = copy.deepcopy(FEED)
    for k in ("xero", "events", "ai_usage"):
        del body[k]
    s = erp_health.parse_snapshot(body)
    assert set(s["missing"]) == {"xero", "events", "ai_usage"}
    assert s["events_present"] is False and s["events"] == [] and s["xero"] is None and s["ai_usage"] is None
    assert s["failures"] and s["heartbeats"]


@pytest.mark.parametrize("v", [2, None, "1", True, 0])
def test_parser_refuses_an_unknown_version(v):
    body = copy.deepcopy(FEED)
    if v is None:
        del body["v"]
    else:
        body["v"] = v
    with pytest.raises(erp_health.FeedError, match="unsupported feed version"):
        erp_health.parse_snapshot(body)
    with pytest.raises(erp_health.FeedError):
        erp_health.parse_snapshot([body])


def test_parser_tolerates_level_alias_and_a_breaker_object():
    body = {"v": 1, "generated_at": "2026-10-05T03:00:00Z",
            "events": [{"id": "7", "level": "CRITICAL", "message": "boom"}, {"title": "no id"}],
            "heartbeats": [{"entity": "worker:a", "age_minutes": 130}], "webhooks": [],
            "xero": {"breaker": {"state": "open", "until": "03:10"}}}
    s = erp_health.parse_snapshot(body)
    assert [(e["id"], e["level"], e["title"]) for e in s["events"]] == [(7, 5, "boom")]
    assert [(h["name"], h["red"]) for h in s["heartbeats"]] == [("worker:a", True)]
    assert s["webhooks"] == [] and "webhooks" not in s["unreadable"]  # present and empty = really none
    assert s["xero"]["tone"] == "bad" and ("Until", "03:10") in s["xero"]["extra"]


def test_parser_wrong_type_sections_are_invalid_never_empty():
    body = copy.deepcopy(FEED)
    body["events"] = {"count": 3, "items": []}
    body["heartbeats"] = {"count": 1, "items": []}
    body["webhooks"] = {"zabbix": 10}
    body["xero"] = ["closed"]
    s = erp_health.parse_snapshot(body)
    assert s["invalid"] == ["events", "heartbeats", "webhooks", "xero"] and s["missing"] == []
    assert s["events_present"] is False and s["events"] == [] and s["heartbeats"] == [] and s["webhooks"] == []
    assert s["xero"] is None and s["failures"]  # the valid sections still read


# --- sync: snapshot, dedupe, recovery -----------------------------------------------------------------

def test_first_fetch_is_baseline_then_new_errors_escalate_once_and_recover(erp):
    r = erp_health.sync(now=GEN)
    assert r["ok"] and r["first"] and r["new"] == 3 and r["escalated"] == 0
    assert all(row["baseline"] for row in _rows().values())
    assert _errors_outbox() == []
    # the stored snapshot keeps the ERP's key order (JSON, not JSONB): Today before Month, as the ERP sent them
    assert [p[0] for p in erp_health.summary(now=GEN)["snapshot"]["ai_usage"]["periods"]] == ["Today", "Month"]
    # the auth header carried the READ token, and only to /api/health
    assert erp.requests[-1][:3] == ("GET", "/api/health", f"Bearer {READ_TOKEN}")

    erp.health_body["events"] += [_event(9200, "critical", "Disk full on core-1"), _event(9201, "warning")]
    r = erp_health.sync(now=GEN + timedelta(minutes=2))
    assert r["new"] == 2 and r["escalated"] == 1  # the warning only shows
    box = _errors_outbox()
    assert len(box) == 1 and box[0]["dedupe_key"] == "erp-event:9200" and box[0]["kind"] == "health_error"
    assert "critical" in box[0]["message"] and "Disk full on core-1" in box[0]["message"]
    assert _rows()[9200]["escalated_at"] is not None

    assert erp_health.sync(now=GEN + timedelta(minutes=4))["escalated"] == 0  # nothing new: nothing sent
    assert len(_errors_outbox()) == 1

    erp.health_body["events"] = [e for e in erp.health_body["events"] if e["id"] != 9200]
    r = erp_health.sync(now=GEN + timedelta(minutes=6))
    assert r["resolved"] == 1 and r["escalated"] == 1
    box = _errors_outbox()
    assert len(box) == 1 and "RESOLVED" in box[0]["message"] and box[0]["dedupe_key"] == "erp-event:9200"
    assert _rows()[9200]["resolved_at"] is not None


def test_baseline_and_warning_recoveries_stay_quiet(erp):
    erp_health.sync(now=GEN)
    erp.health_body["events"] = []
    r = erp_health.sync(now=GEN + timedelta(minutes=2))
    assert r["resolved"] == 3 and r["escalated"] == 0
    assert _errors_outbox() == []


def test_a_snapshot_without_events_leaves_the_open_set_alone(erp):
    erp_health.sync(now=GEN)
    del erp.health_body["events"]
    r = erp_health.sync(now=GEN + timedelta(minutes=2))
    assert r["ok"] and r["resolved"] == 0
    assert all(row["resolved_at"] is None for row in _rows().values())


def _keys_feed(project, monkeypatch, age: timedelta):
    """The Dashboard's own platform key reads for the health project's store, last good `age` ago (#562)."""
    monkeypatch.setattr(config, "PLATFORM_KEY_STORES", {project["slug"]: {
        "url": "https://keys.test.invalid", "key_id": "dash-keys-1", "key_file": "/nonexistent/x", "server": "core-1"}})
    with get_cursor() as cur:
        cur.execute("INSERT INTO health_sources (source, last_ok_at) VALUES (%s, %s)",
                    (f"keys:{project['slug']}", datetime.now(timezone.utc) - age))


def _key_events():
    return [dict(_event(9301, "error", "Key MCP_CLIENT_SECRET is OVERDUE for rotation (2026-10-26)", "keyclock"),
                 dedup_key="key_rotation:MCP_CLIENT_SECRET", category="secrets"),
            dict(_event(9302, "warning", "Key AZURE_MAPS_KEY is due for rotation", "other"),
                 dedup_key="key_rotation:AZURE_MAPS_KEY"),
            dict(_event(9303, "error", "Key OLD_KEY is OVERDUE for rotation", "keyclock")),
            dict(_event(9304, "error", "Anthropic refused ANTHROPIC_API_KEY (401)", "keyhealth"),
                 dedup_key="key_refused:ANTHROPIC_API_KEY"),
            _event(9305, "critical", "Disk full on core-1", "backup")]


def test_erp_key_rotation_reminders_come_through_while_the_dashboards_are_stale(erp, project, monkeypatch):
    """#562 review: the ERP stays the backup. With no good platform keys read for over an hour (or none at all), its
    key_rotation events are imported as before."""
    from app import push
    _keys_feed(project, monkeypatch, timedelta(minutes=61))
    erp_health.sync(now=GEN)
    erp.health_body["events"] += _key_events()
    r = erp_health.sync(now=GEN + timedelta(minutes=2))
    rows = _rows()
    assert all(rows[i]["resolved_at"] is None for i in (9301, 9302, 9303, 9304, 9305))
    assert r["new"] == 5
    shown = {e["id"] for e in erp_health.summary(now=GEN + timedelta(minutes=2))["events"]}
    assert {9301, 9302, 9303, 9304, 9305} <= shown
    assert ("erp", 9301) in {(p["source"], p["eventid"]) for p in push.due_problems(GEN + timedelta(minutes=3))}


def test_erp_key_rotation_reminders_are_dropped_and_everything_else_comes_through(erp, project, monkeypatch):
    """#562: while the Dashboard's own platform key reminders are live (a good read within the hour), the ERP's
    key_rotation:<NAME> events (its key clock) never reach Health, health_problems or the push. A refused key
    (key_refused:) and the rest still do."""
    from app import push
    _keys_feed(project, monkeypatch, timedelta(minutes=5))
    erp_health.sync(now=GEN)
    with get_cursor() as cur:   # one stored before this change: it resolves on the next snapshot
        cur.execute("INSERT INTO health_problems (source, eventid, host, name, severity, started_at, baseline) "
                    "VALUES ('erp', 9303, 'keyclock', 'Key OLD_KEY is OVERDUE for rotation', 4, %s, false)", (GEN,))
    rotation = dict(_event(9301, "error", "Key MCP_CLIENT_SECRET is OVERDUE for rotation (2026-10-26)", "keyclock"),
                    dedup_key="key_rotation:MCP_CLIENT_SECRET", category="secrets")
    rotation_no_source = dict(_event(9302, "warning", "Key AZURE_MAPS_KEY is due for rotation", "other"),
                              dedup_key="key_rotation:AZURE_MAPS_KEY")
    clock_without_key = dict(_event(9303, "error", "Key OLD_KEY is OVERDUE for rotation", "keyclock"))
    refused = dict(_event(9304, "error", "Anthropic refused ANTHROPIC_API_KEY (401)", "keyhealth"),
                   dedup_key="key_refused:ANTHROPIC_API_KEY")
    other = _event(9305, "critical", "Disk full on core-1", "backup")
    erp.health_body["events"] += [rotation, rotation_no_source, clock_without_key, refused, other]

    r = erp_health.sync(now=GEN + timedelta(minutes=2))
    rows = _rows()
    assert 9301 not in rows and 9302 not in rows
    assert rows[9303]["resolved_at"] is not None
    assert rows[9304]["resolved_at"] is None and rows[9305]["resolved_at"] is None
    assert r["new"] == 2 and r["escalated"] == 2            # the refused key and the disk, nothing else
    assert {b["dedupe_key"] for b in _errors_outbox()} == {"erp-event:9304", "erp-event:9305"}
    shown = {e["id"] for e in erp_health.summary(now=GEN + timedelta(minutes=2))["events"]}
    assert {9304, 9305} <= shown and not shown & {9301, 9302, 9303}
    due = {(p["source"], p["eventid"]) for p in push.due_problems(GEN + timedelta(minutes=3))}
    assert ("erp", 9304) in due and ("erp", 9305) in due
    assert not due & {("erp", 9301), ("erp", 9302), ("erp", 9303)}


def test_unknown_version_is_a_failed_fetch_and_keeps_the_last_snapshot(erp):
    erp_health.sync(now=GEN)
    erp.health_body["v"] = 2
    r = erp_health.sync(now=GEN + timedelta(minutes=1))
    assert not r["ok"] and "unsupported feed version 2" in r["error"]
    s = erp_health.summary(now=GEN + timedelta(minutes=1))
    assert s["state"] == "stale" and len(s["events"]) == 3  # the last good snapshot still shows
    assert all(row["resolved_at"] is None for row in _rows().values())


# --- freshness: stale and down --------------------------------------------------------------------------

def test_old_snapshot_and_failed_fetch_show_stale(erp):
    erp_health.sync(now=GEN)
    assert erp_health.summary(now=GEN + timedelta(minutes=5))["state"] == "ok"
    assert erp_health.summary(now=GEN + timedelta(minutes=11))["state"] == "stale"
    erp.health_status = 503
    r = erp_health.sync(now=GEN + timedelta(minutes=3))
    assert not r["ok"] and r["error"].startswith("HTTP 503")
    s = erp_health.summary(now=GEN + timedelta(minutes=3))
    assert s["state"] == "stale" and s["source"]["last_error"].startswith("HTTP 503")
    assert READ_TOKEN not in s["source"]["last_error"]


def test_feed_down_escalates_once_then_recovers(erp):
    erp_health.sync(now=GEN)
    erp.health_status = 500
    assert not erp_health.sync(now=GEN + timedelta(minutes=10))["down_escalated"]  # stale, not down yet
    r = erp_health.sync(now=GEN + timedelta(minutes=16))
    assert r["down_escalated"]
    box = _errors_outbox()
    assert len(box) == 1 and box[0]["dedupe_key"] == "erp-feed:down" and "DOWN for 16 min" in box[0]["message"]
    assert erp_health.summary(now=GEN + timedelta(minutes=16))["state"] == "down"
    assert not erp_health.sync(now=GEN + timedelta(minutes=18))["down_escalated"]  # once
    assert len(_errors_outbox()) == 1

    erp.health_status = 200
    erp.health_body["generated_at"] = (GEN + timedelta(minutes=20)).isoformat()
    r = erp_health.sync(now=GEN + timedelta(minutes=20))
    assert r["ok"] and r["down_escalated"]
    box = _errors_outbox()
    assert len(box) == 1 and "back up" in box[0]["message"]  # coalesced on the same key while undelivered
    assert erp_health.summary(now=GEN + timedelta(minutes=20))["source"]["down_escalated_at"] is None


def test_feed_that_never_answered_goes_down_from_its_first_failure(erp):
    erp.health_status = 401
    erp.health_body = {"detail": "bad token"}
    r = erp_health.sync(now=GEN)
    assert not r["ok"] and r["error"] == "HTTP 401: bad token"
    assert erp_health.summary(now=GEN + timedelta(minutes=1))["state"] == "stale"
    assert erp_health.sync(now=GEN + timedelta(minutes=16))["down_escalated"]


def test_redirect_is_refused_so_the_token_never_follows_it(erp):
    erp.redirect = True
    r = erp_health.sync(now=GEN)
    assert not r["ok"] and "redirect" in r["error"]
    assert [req[1] for req in erp.requests] == ["/api/health"]


def test_not_configured_does_nothing(unconfigured):
    assert erp_health.client_from_config() is None
    assert erp_health.sync() == {"ok": False, "error": "not configured"}
    assert erp_health.summary()["state"] == "off"
    assert erp_health.slot()["label"] == "not connected"


def test_url_without_read_token_is_not_configured(erp):
    erp.read_file.write_text("")
    assert not erp_health.is_configured()
    erp.read_file.write_text(READ_TOKEN)  # read at use time: no restart needed
    assert erp_health.is_configured()


# --- Resolve -----------------------------------------------------------------------------------------------

def test_resolve_success_uses_the_resolve_token_and_prunes_the_event(erp):
    erp_health.sync(now=GEN)
    assert erp_health.resolve_event(9102, now=GEN) == {"ok": True}
    method, path, auth, body = erp.requests[-1]
    assert (method, path, auth) == ("POST", "/api/health/events/9102/resolve", f"Bearer {RESOLVE_TOKEN}")
    assert body == {"resolved_by": erp_health.RESOLVED_BY}
    assert [e["id"] for e in erp_health.summary(now=GEN)["events"]] == [9101, 9103]


def test_resolve_4xx_shows_the_erps_reason(erp):
    erp_health.sync(now=GEN)
    erp.resolve_status, erp.resolve_body = 403, {"detail": "token lacks health:resolve"}
    r = erp_health.resolve_event(9102, now=GEN)
    assert not r["ok"] and "HTTP 403" in r["error"] and "token lacks health:resolve" in r["error"]
    assert RESOLVE_TOKEN not in r["error"]
    erp.resolve_status, erp.resolve_body = 404, {"detail": "no such open event"}
    assert "already resolved" in erp_health.resolve_event(9102, now=GEN)["error"]
    assert 9102 in [e["id"] for e in erp_health.summary(now=GEN)["events"]]  # not pruned on failure


def test_resolve_without_its_token_errors_and_sends_nothing(erp):
    erp_health.sync(now=GEN)
    erp.resolve_file.unlink()
    n = len(erp.requests)
    r = erp_health.resolve_event(9102, now=GEN)
    assert not r["ok"] and "resolve token isn't configured" in r["error"]
    assert len(erp.requests) == n


def test_resolve_button_htmx_folds_the_row_away_and_failure_returns_it(client, erp):
    _fresh(erp)
    erp_health.sync()
    url = f"/{config.HEALTH_PROJECT}/health/erp/events/9102/resolve"
    r = client.post(url, headers={**OWN, "HX-Request": "true"})
    # #582: success = an EMPTY answer (fold.js folds the row away in place) + a toast; never the row, never a redirect
    assert r.status_code == 200 and r.text == "" and "mdd:flash" in r.headers["HX-Trigger"]
    assert "9102 resolved" in r.headers["HX-Trigger"]
    erp.resolve_status, erp.resolve_body = 409, {"detail": "already resolved by mike"}
    html = client.post(f"/{config.HEALTH_PROJECT}/health/erp/events/9101/resolve",
                       headers={**OWN, "HX-Request": "true"}).text
    assert "data-resolve-error" in html and "already resolved by mike" in html and "data-resolve-button" in html


def test_resolve_button_without_token_and_plain_post_flashes(client, erp):
    _fresh(erp)
    erp_health.sync()
    erp.resolve_file.write_text("")
    r = client.post(f"/{config.HEALTH_PROJECT}/health/erp/events/9101/resolve", headers=OWN, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/{config.HEALTH_PROJECT}/health"
    assert "resolve token" in r.cookies.get("crewboss_flash", "")


def test_resolve_route_is_only_on_the_health_project(client, erp):
    other = repo.create_project("other-proj", "Other", "internal", "system")
    r = client.post(f"/{other['slug']}/health/erp/events/1/resolve", headers=OWN, follow_redirects=False)
    assert r.status_code in (303, 404)
    assert not [req for req in erp.requests if req[0] == "POST"]


# --- pages ---------------------------------------------------------------------------------------------------

def test_health_page_without_the_feed(client, unconfigured):
    html = client.get(f"/{config.HEALTH_PROJECT}/health").text
    assert "Zabbix" in html  # the Zabbix section is untouched
    assert 'data-erp-state="off"' in html and "ERP feed not connected" in html
    assert "data-erp-event" not in html and "data-erp-failures" not in html
    calls = client.get(f"/{config.HEALTH_PROJECT}/health?view=ai-calls").text
    assert "ERP feed not connected" in calls and "data-ai-call" not in calls


def test_health_page_with_the_feed(client, erp):
    _fresh(erp)
    erp_health.sync()
    html = client.get(f"/{config.HEALTH_PROJECT}/health").text
    assert 'data-erp-state="ok"' in html and "ERP feed · Connected" in html
    assert html.index("Zabbix") < html.index('id="erp-health-h"')  # ERP sits under Zabbix
    assert 'data-erp-event="9101"' in html and "Nightly restic backup failed" in html and "baseline" in html
    assert html.count("data-resolve-button") == 3
    assert 'data-failure="invoices_failed"' in html and "INV-0412 · Acme Pty Ltd" in html
    assert 'data-heartbeat="worker:invoicing" data-late' in html and "5h 0m ago" in html
    assert 'data-webhook="stripe"' in html and "1 failed" in html
    assert 'data-xero-breaker="closed"' in html
    assert "By model" in html and "81,234" in html
    assert 'data-health-view="ai-calls"' in html


def test_health_page_names_a_missing_section(client, erp):
    _fresh(erp)
    del erp.health_body["xero"]
    erp_health.sync()
    html = client.get(f"/{config.HEALTH_PROJECT}/health").text
    assert "Not in this feed." in html and "data-xero-breaker" not in html


def test_health_page_stale_feed_still_shows_the_last_snapshot(client, erp):
    _fresh(erp)
    erp_health.sync()
    erp.health_status = 502
    erp_health.sync()
    html = client.get(f"/{config.HEALTH_PROJECT}/health").text
    assert 'data-erp-state="stale"' in html and "Last fetch failed: HTTP 502" in html
    assert 'data-erp-event="9101"' in html


def test_ai_calls_view_pages_by_id(client, erp):
    html = client.get(f"/{config.HEALTH_PROJECT}/health?view=ai-calls&before=6000&limit=3").text
    assert 'data-ai-call="5003"' in html and "casegen.triage" in html and "overloaded" in html
    assert "view=ai-calls&before=5001&limit=3" in html  # Older -> next_before, limit kept
    assert "Newest" in html and "data-filters-button" in html
    assert erp.requests[-1][1] == "/api/health/ai-calls?limit=3&before=6000"
    assert erp.requests[-1][2] == f"Bearer {READ_TOKEN}"
    client.get(f"/{config.HEALTH_PROJECT}/health?view=ai-calls&limit=99999")
    assert erp.requests[-1][1] == f"/api/health/ai-calls?limit={erp_health.AI_CALLS_MAX_LIMIT}"


def test_other_project_health_page_has_no_erp_section(client, erp):
    other = repo.create_project("other-proj", "Other", "internal", "system")
    html = client.get(f"/{other['slug']}/health").text
    assert "No health source for this project yet" in html and "data-erp-section" not in html


# --- the home slot: worst of Zabbix + ERP ---------------------------------------------------------------------

def test_combined_slot_takes_the_worst():
    z = {"state": "ok", "open": 0, "worst": None, "label": "all clear", "project": "p"}
    off = {"state": "off", "open": 0, "worst": None, "label": "not connected"}
    assert erp_health.combined_slot(z, off) is z
    e = {"state": "ok", "open": 2, "worst": 5, "label": "2 open events"}
    c = erp_health.combined_slot(z, e)
    assert c == {"state": "ok", "open": 2, "worst": 5, "label": "Zabbix all clear · ERP 2 open events", "project": "p"}
    down = {"state": "down", "open": 0, "worst": None, "label": "feed down"}
    assert erp_health.combined_slot(z, down)["state"] == "error"
    zoff = {"state": "off", "open": 0, "worst": None, "label": "not connected", "project": "p"}
    assert erp_health.combined_slot(zoff, e)["label"] == "ERP 2 open events"
    zerr = {"state": "error", "open": 1, "worst": 3, "label": "sync error · 1 open", "project": "p"}
    stale = {"state": "stale", "open": 0, "worst": None, "label": "feed stale"}
    assert erp_health.combined_slot(zerr, stale)["state"] == "error"


def test_home_slot_shows_the_erp_feed(client, erp, project, monkeypatch):
    monkeypatch.setattr(health, "is_configured", lambda: False)
    _fresh(erp)
    erp_health.sync()
    home = client.get("/").text
    assert "Health &amp; alerts: ERP 2 open errors" in home  # the critical + the error; the warning doesn't count
    assert 'class="w-2 h-2 rounded-full bg-status-disaster"' in home  # a critical event is open
    from app.auth import issue_token
    assert client.get("/api/health").status_code == 401  # no token, no feed (task #368)
    # a session of the health project's own account may read it
    h = {"Authorization": f"Bearer {issue_token('all', 'hp', project_id=project['id'])}"}
    api = client.get("/api/health", headers=h).json()
    assert api["erp"]["state"] == "ok" and api["slot_all"]["open"] == 2 and api["slot_all"]["worst"] == 5


def test_global_plan_counts_erp_errors_not_warnings(erp):
    from app import plan as plan_module

    erp_health.sync(now=GEN)  # fixture: one critical, one error, one warning
    with get_cursor() as cur:
        rows = plan_module._open_health(cur)
    assert sorted((r["source"], r["eventid"]) for r in rows) == [("erp", 9101), ("erp", 9102)]


# --- review fixes (task #196 review) ---------------------------------------------------------------------------

def test_slot_counts_only_error_and_critical(client, erp, monkeypatch):
    """A permanent info row (the ERP's pbx-push "pbx_push:ok") and warnings list on the page but never turn the
    roll-ups red."""
    monkeypatch.setattr(health, "is_configured", lambda: False)
    _fresh(erp)
    erp.health_body["events"] = [_event(1, "info", "pbx_push:ok", "pbx_push"), _event(2, "warning", "slow cycle")]
    erp_health.sync()
    sl = erp_health.slot()
    assert sl == {"state": "ok", "open": 0, "worst": None, "label": "all clear"}
    home = client.get("/").text
    assert "Health &amp; alerts: ERP all clear" in home and "rounded-full bg-status-ok" in home
    page = client.get(f"/{config.HEALTH_PROJECT}/health").text
    assert "pbx_push:ok" in page and 'data-erp-event="2"' in page
    overview = client.get(f"/{config.HEALTH_PROJECT}").text           # Overview merged into Summary (#773)
    tile = overview.split("Failing now", 1)[1][:300]
    assert '>0</div>' in tile and "text-flame" not in tile and "all clear" in tile


def test_truncated_events_insert_new_but_never_recover(erp):
    erp_health.sync(now=GEN)
    erp.health_body["events"] = [_event(9300, "critical", "New and capped")]
    erp.health_body["events_truncated"] = True
    r = erp_health.sync(now=GEN + timedelta(minutes=2))
    assert r["new"] == 1 and r["resolved"] == 0 and r["escalated"] == 1
    assert all(row["resolved_at"] is None for row in _rows().values())
    assert [b["dedupe_key"] for b in _errors_outbox()] == ["erp-event:9300"]  # no false RESOLVED
    del erp.health_body["events_truncated"]
    r = erp_health.sync(now=GEN + timedelta(minutes=4))  # the full list again: now the missing ones really recovered
    assert r["resolved"] == 3


def test_plain_post_flash_survives_a_non_latin1_erp_detail(client, erp):
    _fresh(erp)
    erp_health.sync()
    erp.resolve_status, erp.resolve_body = 409, {"detail": "already resolved \u2014 by Zo\u00eb \u2713"}
    r = client.post(f"/{config.HEALTH_PROJECT}/health/erp/events/9102/resolve", headers=OWN, follow_redirects=False)
    assert r.status_code == 303
    flash = r.cookies.get("crewboss_flash", "")
    assert "already resolved - by Zoe" in flash and flash.isascii()
    html = client.get(f"/{config.HEALTH_PROJECT}/health").text  # the client kept the cookie; the page shows it once
    assert "already resolved - by Zoe" in html


def test_wrong_type_events_section_never_shows_all_clear(client, erp, monkeypatch):
    monkeypatch.setattr(health, "is_configured", lambda: False)
    _fresh(erp)
    erp_health.sync()
    _fresh(erp)
    erp.health_body["events"] = {"count": 3, "items": []}
    erp.health_body["heartbeats"] = {"count": 1, "items": []}
    erp.health_body["webhooks"] = {"count": 1, "items": []}
    r = erp_health.sync()
    assert r["ok"] and r["resolved"] == 0
    assert all(row["resolved_at"] is None for row in _rows().values())  # tracked open set untouched
    s = erp_health.summary()
    assert s["state"] == "stale" and erp_health.slot(s)["label"] == "events unreadable"
    html = client.get(f"/{config.HEALTH_PROJECT}/health").text
    assert "All clear" not in html and "Not in the v1 shape, so not shown: events, heartbeats, webhooks" in html
    for sec in ("events", "heartbeats", "webhooks"):
        assert f'data-unreadable="{sec}"' in html
    assert "None in the last 24h" not in html
    assert "ERP all clear" not in client.get("/").text


def test_severity_follows_the_erp_and_a_rise_to_error_escalates_once(erp):
    erp_health.sync(now=GEN)  # 9103 is a baseline WARNING
    erp.health_body["events"][2]["severity"] = "error"
    r = erp_health.sync(now=GEN + timedelta(minutes=2))
    assert r["upgraded"] == 1 and r["escalated"] == 1
    box = _errors_outbox()
    assert [b["dedupe_key"] for b in box] == ["erp-event:9103"] and "rose to error" in box[0]["message"]
    assert _rows()[9103]["severity"] == 4 and _rows()[9103]["escalated_at"] is not None
    erp.health_body["events"][2]["severity"] = "critical"
    assert erp_health.sync(now=GEN + timedelta(minutes=4))["escalated"] == 0  # once
    assert _rows()[9103]["severity"] == 5
    erp.health_body["events"][2]["severity"] = "warning"  # a drop just follows, quietly
    assert erp_health.sync(now=GEN + timedelta(minutes=6))["escalated"] == 0
    assert _rows()[9103]["severity"] == 2
    erp.health_body["events"] = erp.health_body["events"][:2]
    erp_health.sync(now=GEN + timedelta(minutes=8))
    box = _errors_outbox()
    assert len(box) == 1 and "RESOLVED" in box[0]["message"]  # the recovery follows on the same key


def test_a_baseline_error_that_stays_an_error_is_not_sent(erp):
    erp_health.sync(now=GEN)
    erp.health_body["events"][1]["severity"] = "critical"  # error -> critical: already past the line, not a crossing
    assert erp_health.sync(now=GEN + timedelta(minutes=2))["escalated"] == 0


def test_a_non_https_url_is_refused(client, erp):
    url = config.ERP_HEALTH_URL.replace("https://", "http://")
    config.ERP_HEALTH_URL = url  # monkeypatched by the fixture: restored after the test
    n = len(erp.requests)
    assert erp_health.client_from_config() is None and not erp_health.is_configured()
    assert erp_health.sync() == {"ok": False, "error": "not configured"}
    s = erp_health.summary()
    assert s["state"] == "off" and "https://" in s["config_problem"] and erp_health.slot(s)["label"] == "https required"
    html = client.get(f"/{config.HEALTH_PROJECT}/health").text
    assert "data-erp-config-problem" in html and "cleartext" in html
    assert not erp_health.resolve_event(9101)["ok"]
    assert len(erp.requests) == n  # the token never went out


def test_tls_is_verified(erp, monkeypatch):
    monkeypatch.setattr(erp_health, "_ssl_context", ssl.create_default_context)  # system CAs: the stub isn't trusted
    r = erp_health.sync(now=GEN)
    assert not r["ok"] and "can't reach the ERP" in r["error"]
    assert erp.requests == []


def test_resolve_is_refused_for_an_id_not_open_in_the_fresh_snapshot(client, erp):
    _fresh(erp)
    erp_health.sync()
    url = f"/{config.HEALTH_PROJECT}/health/erp/events/%s/resolve"
    r = client.post(url % 424242, headers={**OWN, "HX-Request": "true"})
    assert r.status_code == 404 and "Not an open ERP event" in r.text
    assert "mdd:flash" in r.headers.get("HX-Trigger", "")
    assert client.post(url % 424242, headers=OWN, follow_redirects=False).status_code == 404
    # stale snapshot: even a listed id is refused
    erp.health_status = 503
    erp_health.sync()
    assert client.post(url % 9101, headers=OWN, follow_redirects=False).status_code == 404
    assert erp_health.resolve_event(9101)["status"] == 404
    assert not [req for req in erp.requests if req[0] == "POST"]  # nothing was sent to the ERP


# --- ai_models (acme-erp #516, dash #517) ------------------------------------------------------------

def test_parser_reads_ai_models_and_splits_live_from_history_drift():
    m = erp_health.parse_snapshot(copy.deepcopy(FEED))["ai_models"]
    assert [g for g, _ in m["groups"]] == ["Writing for clients", "Email triage"]  # feed (registry) order
    rows = {w["key"]: w for _, ws in m["groups"] for w in ws}
    assert rows["compose_quote"]["drift"] == "ok" and rows["compose_quote"]["client_facing"]
    assert rows["compose_quote"]["model"] == "claude-sonnet-5-5" and rows["compose_quote"]["aud"] == pytest.approx(0.79)
    # off-model calls only before the 7-day window: history, not live
    assert rows["classify_email"]["drift"] == "history"
    assert rows["classify_email"]["used"][0]["model"] == "claude-haiku-4-5"  # most calls first
    # off-model used yesterday: live; unpriced use gives no A$ rather than 0
    assert rows["triage_escalated"]["drift"] == "live" and rows["triage_escalated"]["aud"] is None
    assert rows["triage_escalated"]["source"] == "env" and rows["triage_escalated"]["pin"] == "TRIAGE_MODEL"
    t = m["totals"]
    assert (t["workflows"], t["client_facing"], t["drift_live"], t["drift_history"]) == (3, 1, 1, 1)
    assert t["calls"] == 11 + 1267 + 40 + 48
    assert m["unattributed"][0]["job"] == "-c:?" and m["fx"]["rate"] == pytest.approx(1.4318)


def test_ai_models_links_only_to_the_admin_site():
    m = erp_health.parse_snapshot(copy.deepcopy(FEED))["ai_models"]
    rows = {w["key"]: w for _, ws in m["groups"] for w in ws}
    assert rows["compose_quote"]["admin_url"].startswith("https://erp.example.com/admin/models")
    assert rows["triage_escalated"]["admin_url"] is None  # javascript: dropped
    for bad in ("http://admin.example.com/x", "https://admin.example.com/models",   # a separate admin host is not trusted
                "https://erp.example.com/admin.evil.com/", 'https://erp.example.com/admin/"><x',
                "https://erp.example.com/models", "https://erp.example.com.evil.com/admin/",
                "http://erp.example.com/admin/models"):
        assert erp_health._admin_link(bad) is None


def test_ai_models_links_follow_admin_to_the_erp():
    # acme-erp #587: the ERP's admin lives at <ERP_URL>admin/ now; no other host is accepted.
    link = f"{config.ERP_URL}admin/models?edit=x#wf-x"
    assert erp_health._admin_link(link) == link


def test_ai_models_missing_or_wrong_type_is_named_never_empty():
    body = copy.deepcopy(FEED)
    del body["ai_models"]
    s = erp_health.parse_snapshot(body)
    assert "ai_models" in s["missing"] and s["ai_models"] is None
    body["ai_models"] = []
    s = erp_health.parse_snapshot(body)
    assert "ai_models" in s["invalid"] and s["ai_models"] is None


def test_health_page_shows_ai_models(client, erp):
    _fresh(erp)
    today = datetime.now(timezone.utc).date().isoformat()
    erp.health_body["ai_models"]["workflows"][2]["used_30d"][0]["last_used"] = today  # live on the real clock
    erp_health.sync()
    html = client.get(f"/{config.HEALTH_PROJECT}/health").text
    assert "data-erp-ai-models" in html and "Quote drafting (Overview &amp; Approach)" in html
    assert 'data-wf="classify_email" data-drift="history"' in html
    assert 'data-wf="triage_escalated" data-drift="live"' in html
    assert 'data-drift-live="1"' in html and "Change in ERP Admin" in html
    assert "javascript:" not in html


def test_ai_models_without_an_aud_rate_shows_usd_not_zero_aud():
    body = copy.deepcopy(FEED)
    body["ai_models"]["fx"] = None
    for w in body["ai_models"]["workflows"]:
        for u in w["used_30d"]:
            u["aud"] = None
    m = erp_health.parse_snapshot(body)["ai_models"]
    assert m["currency"] == "US$" and m["totals"]["usd"] == pytest.approx(0.55 + 2.0 + 0.1)
    assert erp_health.parse_snapshot(copy.deepcopy(FEED))["ai_models"]["currency"] == "A$"
