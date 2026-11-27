"""Phone notifications (#487): the owner's push rule (q292) for estate problems, the grouping, push-once, the quiet
all-clear, pruning a dead subscription on 404/410, and the endpoints / service worker. pywebpush is mocked: nothing
leaves the box."""
import json
import os
import stat
from datetime import datetime, timedelta, timezone

import pytest
import pywebpush
from py_vapid import Vapid02

from app import config, push
from app.db import get_cursor

T0 = datetime(2026, 10, 19, 9, 0, tzinfo=timezone.utc)
FCM = "https://fcm.googleapis.com/fcm/send/abc123"
KEYS = {"p256dh": "BNcRdreALRFXTkOOUHK1EtK2wtaz5Ry4YfYCA_0QTpQtUbVlUls0VJXg7A8u-Ts1XbjhazAkj7I99e8QcYP7DkM",
        "auth": "tBHItJI5svbpez7KI4CCXg"}


class _Calls(list):
    fail = None  # a status the fake push service answers with, else it accepts


class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code
        self.text = ""


@pytest.fixture
def sent(tmp_path, monkeypatch):
    """Push ON: a throwaway VAPID key, one subscribed device, and pywebpush.webpush recorded instead of sent. Set
    `sent.fail = <status>` to make the push service answer that."""
    key = tmp_path / "vapid-private.pem"
    push.keygen(str(key))
    monkeypatch.setattr(config, "VAPID_KEY_FILE", str(key))
    push.subscribe({"endpoint": FCM, "keys": KEYS}, "pytest")
    calls = _Calls()

    def fake_webpush(**kw):
        calls.append(kw)
        if calls.fail:
            raise pywebpush.WebPushException("push refused", response=_Resp(calls.fail))

    monkeypatch.setattr(pywebpush, "webpush", fake_webpush)
    return calls


def _payloads(calls):
    return [json.loads(c["data"]) for c in calls]


def _problem(eventid, severity, started_at, source="zabbix", baseline=False, resolved_at=None, objectid=None,
             host="core-1", name=None):
    with get_cursor() as cur:
        cur.execute("INSERT INTO health_problems (source, eventid, objectid, host, name, severity, started_at, "
                    "resolved_at, baseline) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (source, eventid, objectid, host, name or f"problem {eventid}", severity, started_at, resolved_at,
                     baseline))


def _resolve(eventid, at, source="zabbix"):
    with get_cursor() as cur:
        cur.execute("UPDATE health_problems SET resolved_at = %s WHERE source = %s AND eventid = %s",
                    (at, source, eventid))


def _pushed_at(eventid, source="zabbix"):
    with get_cursor() as cur:
        cur.execute("SELECT pushed_at FROM health_problems WHERE source = %s AND eventid = %s", (source, eventid))
        return cur.fetchone()["pushed_at"]


# --- the rule ---------------------------------------------------------------------------------------------------------

def test_high_and_disaster_push_at_once(sent):
    _problem(1, 4, T0, name="omada-ctrl not running")
    r = push.sweep(T0 + timedelta(seconds=5))
    assert r["pushed"] == 1 and len(sent) == 1
    p = _payloads(sent)[0]
    assert p["title"] == "High: core-1" and p["body"] == "omada-ctrl not running"
    assert p["renotify"] is True and p["tag"].startswith("mdd-estate-")
    assert p["url"] == f"{config.PUBLIC_URL}/{config.HEALTH_PROJECT}/health"  # tap -> the Health page, absolute
    assert _pushed_at(1) == T0 + timedelta(seconds=5)


def test_average_pushes_only_after_15_minutes_open(sent):
    _problem(2, 3, T0)
    assert push.sweep(T0 + timedelta(minutes=14, seconds=59)).get("pushed", 0) == 0 and not sent
    assert push.sweep(T0 + timedelta(minutes=15))["pushed"] == 1 and len(sent) == 1
    assert _payloads(sent)[0]["title"] == "Average: core-1"


def test_average_resolved_inside_15_minutes_never_pushes(sent):
    _problem(3, 3, T0)
    _resolve(3, T0 + timedelta(minutes=10))
    push.sweep(T0 + timedelta(minutes=20))
    assert not sent


def test_warning_and_below_never_push_whatever_the_source(sent):
    for i, (sev, src) in enumerate([(2, "zabbix"), (1, "zabbix"), (0, "zabbix"), (2, "drift"), (2, "erp")]):
        _problem(10 + i, sev, T0 - timedelta(days=1), source=src)
    push.sweep(T0)
    assert not sent


def test_drift_and_erp_rows_push_by_severity_like_the_rest(sent):
    _problem(20, 4, T0, source="drift", host="mallen-erp")
    _problem(21, 4, T0, source="erp", host="graph-mail")
    push.sweep(T0 + timedelta(seconds=1))
    assert len(sent) == 1 and _payloads(sent)[0]["title"] == "2 estate problems · High"


def test_baseline_and_resolved_never_push(sent):
    _problem(30, 5, T0, baseline=True)
    _problem(31, 5, T0, resolved_at=T0 + timedelta(seconds=30))
    push.sweep(T0 + timedelta(minutes=1))
    assert not sent and _pushed_at(30) is None and _pushed_at(31) is None


def test_each_problem_pushes_once(sent):
    _problem(40, 4, T0)
    push.sweep(T0 + timedelta(seconds=1))
    push.sweep(T0 + timedelta(minutes=5))
    push.sweep(T0 + timedelta(hours=3))
    assert len(sent) == 1


# --- grouping ---------------------------------------------------------------------------------------------------------

def test_problems_due_in_one_sweep_are_one_push(sent):
    _problem(50, 4, T0, host="core-1", name="omada-ctrl not running")
    _problem(51, 5, T0, host="web-1", name="caddy down")
    _problem(52, 3, T0 - timedelta(minutes=20), host="mon-1", name="disk 91%")
    push.sweep(T0 + timedelta(seconds=10))
    assert len(sent) == 1
    p = _payloads(sent)[0]
    assert p["title"] == "3 estate problems · Disaster"
    assert p["body"].splitlines() == ["web-1 · caddy down", "core-1 · omada-ctrl not running", "mon-1 · disk 91%"]


def test_a_problem_within_two_minutes_joins_the_push_quietly(sent):
    _problem(60, 4, T0, host="core-1", name="a")
    push.sweep(T0)
    _problem(61, 4, T0 + timedelta(seconds=80), host="web-1", name="b")
    push.sweep(T0 + timedelta(seconds=90))
    first, joined = _payloads(sent)
    assert joined["tag"] == first["tag"] and joined["renotify"] is False  # replaces the one notification, no new buzz
    assert joined["title"] == "2 estate problems · High" and joined["body"].splitlines() == ["core-1 · a", "web-1 · b"]
    # Past the window: a new group, its own tag, and it alerts.
    _problem(62, 4, T0 + timedelta(seconds=200))
    push.sweep(T0 + timedelta(seconds=200))
    third = _payloads(sent)[2]
    assert third["tag"] != first["tag"] and third["renotify"] is True and third["title"] == "High: core-1"


def test_a_worse_high_joiner_buzzes_again(sent):
    _problem(65, 3, T0 - timedelta(minutes=15), host="mon-1", name="disk 91%")
    push.sweep(T0)  # an Average group
    _problem(66, 4, T0 + timedelta(seconds=80), host="core-1", name="omada-ctrl not running")
    push.sweep(T0 + timedelta(seconds=90))
    first, joined = _payloads(sent)
    assert joined["tag"] == first["tag"] and joined["renotify"] is True and "silent" not in joined
    assert joined["title"] == "2 estate problems · High"
    # Same severity as the group's worst: quiet again.
    _problem(67, 4, T0 + timedelta(seconds=100), host="web-1")
    push.sweep(T0 + timedelta(seconds=110))
    assert _payloads(sent)[2]["renotify"] is False
    # Worse again (Disaster): buzzes.
    _problem(68, 5, T0 + timedelta(seconds=105), host="web-1")
    push.sweep(T0 + timedelta(seconds=115))
    assert _payloads(sent)[3]["renotify"] is True


def test_a_worse_joiner_below_high_stays_quiet(sent):
    _problem(63, 3, T0 - timedelta(minutes=15))  # Warnings never push: an Average group, then an Average joiner
    push.sweep(T0)
    _problem(64, 3, T0 - timedelta(minutes=14, seconds=30))
    push.sweep(T0 + timedelta(seconds=60))
    assert [p["renotify"] for p in _payloads(sent)] == [True, False]


def test_long_lists_are_cut_with_a_count(sent):
    for i in range(8):
        _problem(70 + i, 4, T0, host=f"h{i}")
    push.sweep(T0)
    body = _payloads(sent)[0]["body"].splitlines()
    assert len(body) == push.MAX_LINES + 1 and body[-1] == "+3 more"


# --- the all-clear ----------------------------------------------------------------------------------------------------

def test_one_quiet_resolved_push_when_every_problem_has_cleared(sent):
    _problem(80, 4, T0, host="core-1", name="a")
    _problem(81, 4, T0, host="web-1", name="b")
    push.sweep(T0)
    _resolve(80, T0 + timedelta(minutes=3))
    push.sweep(T0 + timedelta(minutes=4))
    assert len(sent) == 1  # one still open: nothing yet
    _resolve(81, T0 + timedelta(minutes=5))
    push.sweep(T0 + timedelta(minutes=6))
    push.sweep(T0 + timedelta(minutes=7))
    assert len(sent) == 2
    problem, resolved = _payloads(sent)
    assert resolved["title"] == "Resolved: 2 estate problems" and resolved["tag"] == problem["tag"]
    assert resolved["renotify"] is False and resolved["silent"] is True


def test_resolved_push_can_be_switched_off(sent, monkeypatch):
    monkeypatch.setattr(config, "PUSH_RESOLVED", False)
    _problem(85, 4, T0)
    push.sweep(T0)
    _resolve(85, T0 + timedelta(minutes=1))
    push.sweep(T0 + timedelta(minutes=2))
    assert len(sent) == 1


# --- repeats ----------------------------------------------------------------------------------------------------------

def test_a_flapping_trigger_pushes_at_most_once_an_hour(sent):
    _problem(90, 4, T0, objectid=777)
    push.sweep(T0)
    _resolve(90, T0 + timedelta(minutes=2))
    _problem(91, 4, T0 + timedelta(minutes=5), objectid=777)  # the same trigger fires again
    push.sweep(T0 + timedelta(minutes=6))
    payloads = _payloads(sent)
    assert [p["title"] for p in payloads] == ["High: core-1", "Resolved: core-1"]  # no second problem push
    push.sweep(T0 + timedelta(minutes=59))
    assert len(sent) == 2
    push.sweep(T0 + timedelta(minutes=61))  # still open an hour on: now it pushes, once
    push.sweep(T0 + timedelta(minutes=62))
    assert len(sent) == 3 and _payloads(sent)[2]["title"] == "High: core-1"


def test_a_reopened_drift_finding_is_a_new_occurrence(sent):
    _problem(95, 4, T0, source="drift", host="mallen-erp")
    push.sweep(T0)
    _resolve(95, T0 + timedelta(minutes=5), source="drift")
    push.sweep(T0 + timedelta(minutes=6))  # the all-clear
    # drift.track re-opens the same row with a fresh started_at.
    with get_cursor() as cur:
        cur.execute("UPDATE health_problems SET resolved_at = NULL, started_at = %s WHERE source = 'drift' "
                    "AND eventid = 95", (T0 + timedelta(hours=2),))
    push.sweep(T0 + timedelta(hours=2, seconds=30))
    assert [p["title"] for p in _payloads(sent)] == ["High: mallen-erp", "Resolved: mallen-erp", "High: mallen-erp"]


def test_an_erp_row_reopened_by_a_feed_gap_does_not_push_again(sent):
    _problem(96, 4, T0, source="erp")
    push.sweep(T0)
    _resolve(96, T0 + timedelta(minutes=1), source="erp")
    with get_cursor() as cur:  # erp_health re-opens without touching started_at
        cur.execute("UPDATE health_problems SET resolved_at = NULL WHERE source = 'erp' AND eventid = 96")
    push.sweep(T0 + timedelta(hours=3))
    assert [p["title"] for p in _payloads(sent)] == ["High: core-1"]


# --- sending ----------------------------------------------------------------------------------------------------------

def test_webpush_gets_the_vapid_object_ttl_and_timeout(sent):
    _problem(100, 4, T0)
    push.sweep(T0)
    kw = sent[0]
    assert isinstance(kw["vapid_private_key"], Vapid02)  # never the PEM string
    assert kw["ttl"] == 86400 and kw["timeout"] == 10
    assert kw["vapid_claims"] == {"sub": config.VAPID_SUB} and config.VAPID_SUB.startswith("mailto:")
    assert kw["subscription_info"] == {"endpoint": FCM, "keys": KEYS}


def test_a_gone_subscription_is_pruned_on_410(sent):
    sent.fail = 410
    _problem(110, 4, T0)
    push.sweep(T0)
    assert push.status()["devices"] == 0
    assert _pushed_at(110) is not None  # pushed once, never retried
    with get_cursor() as cur:
        cur.execute("SELECT result FROM estate_pushes")
        assert cur.fetchone()["result"] == "sent 0, failed 0, pruned 1"


def test_a_failed_push_is_recorded_not_pruned(sent):
    sent.fail = 500
    r = push.deliver(push.test_payload())
    assert r == {"sent": 0, "failed": 1, "pruned": 0}
    with get_cursor() as cur:
        cur.execute("SELECT last_error, last_ok_at FROM push_subscriptions")
        row = cur.fetchone()
    assert row["last_error"].startswith("HTTP 500") and row["last_ok_at"] is None


def test_nothing_is_marked_while_no_device_is_subscribed(sent):
    push.unsubscribe(FCM)
    _problem(120, 4, T0)
    assert push.sweep(T0)["off"] == "no device subscribed"
    assert _pushed_at(120) is None
    push.subscribe({"endpoint": FCM, "keys": KEYS})
    push.sweep(T0 + timedelta(seconds=30))  # the first phone gets what is due now
    assert len(sent) == 1


def test_no_key_means_push_off_never_a_crash(client, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "VAPID_KEY_FILE", str(tmp_path / "missing.pem"))
    _problem(130, 5, T0)
    assert push.sweep(T0) == {"ok": True, "off": "no VAPID key on the server"} and _pushed_at(130) is None
    st = client.get("/push/status").json()
    assert st["enabled"] is False and st["reason"] == "no VAPID key on the server"
    assert client.get("/push/key").status_code == 503
    r = client.post("/push/test")
    assert r.status_code == 503 and "no VAPID key" in r.json()["message"]
    (tmp_path / "dir.pem").mkdir()  # what docker leaves when the file was missing at `compose up`
    monkeypatch.setattr(config, "VAPID_KEY_FILE", str(tmp_path / "dir.pem"))
    assert push.status()["enabled"] is False
    (tmp_path / "junk.pem").write_text("not a key")
    monkeypatch.setattr(config, "VAPID_KEY_FILE", str(tmp_path / "junk.pem"))
    assert push.status()["reason"] == "the VAPID key file on the server is not a usable key"


# --- endpoints ----------------------------------------------------------------------------------------------------------

def test_subscribe_test_and_unsubscribe_endpoints(client, sent):
    push.unsubscribe(FCM)
    key = client.get("/push/key")
    assert key.status_code == 200 and len(key.text) == 87  # 65 raw bytes, base64url without padding
    other = "https://updates.push.services.mozilla.com/wpush/v2/xyz"
    r = client.post("/push/subscribe", json={"endpoint": other, "keys": KEYS}, headers={"user-agent": "Android"})
    assert r.json() == {"ok": True, "devices": 1}
    assert client.post("/push/subscribe", json={"endpoint": other, "keys": KEYS}).json()["devices"] == 1  # upsert
    assert client.get("/push/status", params={"endpoint": other}).json()["this_device"] is True
    assert client.get("/push/status", params={"endpoint": FCM}).json()["this_device"] is False  # pruned/never stored
    r = client.post("/push/test").json()
    assert r["message"] == "Sent 1, failed 0, pruned 0." and len(sent) == 1
    assert _payloads(sent)[0]["title"] == "Crewboss test" and _payloads(sent)[0]["renotify"] is True
    sent.fail = 404
    assert client.post("/push/test").json()["message"] == "Sent 0, failed 0, pruned 1."
    assert client.post("/push/test").json()["message"] == "No device is subscribed: Enable first."
    client.post("/push/subscribe", json={"endpoint": other, "keys": KEYS})
    assert client.post("/push/unsubscribe", json={"endpoint": other}).json() == {"ok": True, "removed": True,
                                                                                "devices": 0}


@pytest.mark.parametrize("endpoint", ["http://fcm.googleapis.com/x", "https://192.0.2.5/x", "https://localhost/x",
                                      "https://core-1.internal/x", "https://nodots/x", "", None, 42])
def test_subscribe_refuses_what_is_not_a_push_service(client, endpoint):
    r = client.post("/push/subscribe", json={"endpoint": endpoint, "keys": KEYS})
    assert r.status_code == 400 and push.status()["devices"] == 0


def test_subscribe_refuses_bad_keys(client):
    r = client.post("/push/subscribe", json={"endpoint": FCM, "keys": {"p256dh": "<script>", "auth": "x"}})
    assert r.status_code == 400


def test_push_writes_are_same_origin_only(client):
    r = client.post("/push/subscribe", json={"endpoint": FCM, "keys": KEYS},
                    headers={"origin": "https://evil.example", "sec-fetch-site": "cross-site"})
    assert r.status_code == 403


def test_service_worker_is_served_from_the_root(client):
    r = client.get("/sw.js")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/javascript")
    assert r.headers["service-worker-allowed"] == "/" and r.headers["cache-control"] == "no-cache"
    body = r.text
    for needle in ("skipWaiting", "clients.claim", "'push'", "notificationclick", "showNotification", "renotify"):
        assert needle in body, needle


def test_no_notification_panel_the_browser_asks(client):
    # Owner 11-09: no Enable / Disable / Test panel in the account menu; push.js asks through the browser's own prompt.
    html = client.get("/").text
    assert "data-push-action" not in html and "Phone notifications" not in html
    assert "/static/js/push.js?v=" in html


# --- the key ----------------------------------------------------------------------------------------------------------

def test_keygen_writes_a_0600_key_once(tmp_path):
    path = tmp_path / "sub" / "vapid-private.pem"
    made, pub = push.keygen(str(path))
    assert made and stat.S_IMODE(os.stat(path).st_mode) == 0o600
    before = path.read_bytes()
    made2, pub2 = push.keygen(str(path))
    assert not made2 and pub2 == pub and path.read_bytes() == before  # never replaced
    assert isinstance(Vapid02.from_pem(before), Vapid02)
