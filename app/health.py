"""System Health (phase 3): production problems pulled from Zabbix over a READ-ONLY API token.

Two ways in, one sync:
- PULL: a background loop (config.HEALTH_POLL_SECONDS) calls sync().
- POKE: Zabbix media type 49 POSTs {"eventid": …} to /api/health/zabbix/poke when a problem opens or clears.
  The poke body is never trusted: it only triggers sync(), which asks Zabbix itself what changed.

sync() catches up by BOOKMARK: every PROBLEM event (value=1) in the watched host groups with an eventid above the
highest one already seen, plus problem.get's open set to notice recoveries. The first sync records what is already
open as BASELINE (shown, never escalated). After that a new problem at or above HEALTH_ESCALATE_MIN_SEVERITY is
pushed once to the health project's errors outbox (dedupe key zabbix:<triggerid>, so a flapping trigger coalesces
while undelivered), and its recovery is pushed the same way.

Nothing here writes to Zabbix: the client refuses any method that isn't a *.get, and the token can't anyway."""
from __future__ import annotations

import json
import logging
import threading
import urllib.request
from datetime import datetime, timedelta, timezone

from app import config, repo, roles
from app.db import get_cursor

log = logging.getLogger("crewboss.health")

SOURCE = "zabbix"
SEVERITY = {0: "Not classified", 1: "Information", 2: "Warning", 3: "Average", 4: "High", 5: "Disaster"}
# design-system status tokens, worst first (colour always paired with the word).
SEVERITY_CLASSES = {
    5: "bg-status-disaster/15 text-status-disaster font-semibold",
    4: "bg-status-high/15 text-status-high font-semibold",
    3: "bg-status-avg/15 text-charcoal dark:text-status-avg font-semibold",
    2: "bg-status-warn/15 text-charcoal dark:text-status-warn",
    1: "bg-charcoal/[.06] text-charcoal/70",
    0: "bg-charcoal/[.06] text-charcoal/70",
}
EVENT_BATCH = 500  # events per catch-up call; a bigger backlog continues on the next sync


class ZabbixError(Exception):
    pass


class ZabbixClient:
    """Minimal JSON-RPC client. Read-only by construction: only *.get methods are ever sent."""

    def __init__(self, url: str, token: str, timeout: float = 15.0):
        self.url, self._token, self.timeout = url, token, timeout

    def call(self, method: str, params: dict):
        if not method.endswith(".get"):
            raise ValueError(f"read-only client: {method!r} is not a *.get method")
        req = urllib.request.Request(
            self.url,
            data=json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode(),
            headers={"Content-Type": "application/json-rpc", "Authorization": f"Bearer {self._token}"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            body = json.load(resp)
        if "error" in body:
            err = body["error"]
            raise ZabbixError(f"{method}: {err.get('data') or err.get('message') or err}")
        return body["result"]


def client_from_config() -> ZabbixClient | None:
    try:
        with open(config.ZABBIX_TOKEN_FILE, encoding="utf-8") as fh:
            token = fh.read().strip()
    except OSError:
        return None
    return ZabbixClient(config.ZABBIX_API_URL, token) if token else None


def is_configured() -> bool:
    return client_from_config() is not None


# ---------------------------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------------------------

_lock = threading.Lock()
_rerun = threading.Event()


def sync(client: ZabbixClient | None = None) -> dict:
    """One catch-up against Zabbix. Concurrent callers (a poke during a poll) don't queue up: the running sync
    simply goes round once more. Never raises: a failure is recorded on the source and returned."""
    client = client or client_from_config()
    if client is None:
        return {"ok": False, "error": "not configured"}
    if not _lock.acquire(blocking=False):
        _rerun.set()
        return {"ok": True, "queued": True}
    try:
        while True:
            _rerun.clear()
            result = _sync_once(client)
            if not _rerun.is_set():
                return result
    finally:
        _lock.release()


def _ts(clock) -> datetime:
    return datetime.fromtimestamp(int(clock), tz=timezone.utc)


def _host(ev: dict) -> str:
    hosts = ev.get("hosts") or []
    return (hosts[0].get("name") or hosts[0].get("host") or "") if hosts else ""


def _event_params(**extra) -> dict:
    params = {"groupids": list(config.ZABBIX_GROUPIDS), "source": 0, "object": 0,
              "output": ["eventid", "objectid", "clock", "name", "severity", "r_eventid", "value"],
              "selectHosts": ["hostid", "name"]}
    params.update(extra)
    return params


def _recovery_times(client: ZabbixClient, r_eventids: set[str]) -> dict[str, datetime]:
    ids = sorted(e for e in r_eventids if e and e != "0")
    if not ids:
        return {}
    rows = client.call("event.get", {"eventids": ids, "output": ["eventid", "clock"]})
    return {r["eventid"]: _ts(r["clock"]) for r in rows}


def _sync_once(client: ZabbixClient) -> dict:
    now = datetime.now(timezone.utc)
    try:
        with get_cursor() as cur:
            cur.execute("INSERT INTO health_sources (source) VALUES (%s) ON CONFLICT DO NOTHING", (SOURCE,))
            cur.execute("SELECT bookmark FROM health_sources WHERE source = %s", (SOURCE,))
            bookmark = cur.fetchone()["bookmark"]
            cur.execute("SELECT eventid FROM health_problems WHERE source = %s AND resolved_at IS NULL", (SOURCE,))
            stored_open = {str(r["eventid"]) for r in cur.fetchall()}
            cur.execute("SELECT eventid FROM health_problems WHERE source = %s", (SOURCE,))
            stored_all = {str(r["eventid"]) for r in cur.fetchall()}

        first = bookmark is None
        # Catch-up BEFORE the open set: a problem that opens between the two calls is then still in the open
        # set, and one that clears between them is correctly read as resolved.
        events: dict[str, dict] = {}
        if not first:
            for ev in client.call("event.get", _event_params(
                    eventid_from=str(int(bookmark) + 1), value=1,
                    sortfield=["eventid"], sortorder="ASC", limit=EVENT_BATCH)):
                events[ev["eventid"]] = ev
        # suppressed=False: a problem silenced by a Zabbix maintenance is not open production trouble; one that
        # becomes suppressed drops out of this set and is handled like a recovery below.
        open_rows = client.call("problem.get", {"groupids": list(config.ZABBIX_GROUPIDS), "source": 0,
                                                "object": 0, "suppressed": False,
                                                "output": ["eventid"]})
        open_ids = {r["eventid"] for r in open_rows}
        # Open problems we have never recorded (the first sync, or older than the bookmark after a gap).
        missing = sorted(open_ids - stored_all - set(events))
        if missing:
            for ev in client.call("event.get", _event_params(eventids=missing)):
                events[ev["eventid"]] = ev

        # Recorded-open problems that dropped out of the open set have recovered; ask when.
        gone = sorted(stored_open - open_ids)
        gone_r: dict[str, str] = {}
        if gone:
            for ev in client.call("event.get", {"eventids": gone, "output": ["eventid", "r_eventid"]}):
                gone_r[ev["eventid"]] = ev.get("r_eventid") or "0"
        rec = _recovery_times(client, {e.get("r_eventid", "0") for e in events.values()} | set(gone_r.values()))

        if first:
            latest = client.call("event.get", {"groupids": list(config.ZABBIX_GROUPIDS), "source": 0, "object": 0,
                                               "value": 1, "output": ["eventid"], "sortfield": ["eventid"],
                                               "sortorder": "DESC", "limit": 1})
            new_bookmark = max([int(e) for e in open_ids] + [int(r["eventid"]) for r in latest] + [0])
        else:
            new_bookmark = max([int(bookmark)] + [int(e) for e in events])
    except Exception as exc:  # noqa: BLE001 — a sync failure is state to show, never a crash
        msg = str(exc)[:300] or exc.__class__.__name__
        log.warning("zabbix sync failed: %s", msg)
        with get_cursor() as cur:
            cur.execute("INSERT INTO health_sources (source) VALUES (%s) ON CONFLICT DO NOTHING", (SOURCE,))
            cur.execute("UPDATE health_sources SET last_sync_at = %s, last_error = %s WHERE source = %s",
                        (now, msg, SOURCE))
        return {"ok": False, "error": msg}

    inserted, resolved = [], []
    with get_cursor() as cur:
        for ev in events.values():
            r_id = ev.get("r_eventid") or "0"
            resolved_at = rec.get(r_id) or (now if r_id != "0" or ev["eventid"] not in open_ids else None)
            cur.execute(
                "INSERT INTO health_problems (source, eventid, objectid, host, name, severity, started_at, "
                "resolved_at, baseline) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (source, eventid) DO NOTHING RETURNING *",
                (SOURCE, int(ev["eventid"]), int(ev.get("objectid") or 0) or None, _host(ev)[:200],
                 (ev.get("name") or "")[:500], int(ev.get("severity") or 0), _ts(ev["clock"]), resolved_at, first),
            )
            row = cur.fetchone()
            if row:
                inserted.append(row)
        for eventid in gone:
            cur.execute(
                "UPDATE health_problems SET resolved_at = %s WHERE source = %s AND eventid = %s "
                "AND resolved_at IS NULL RETURNING *",
                (rec.get(gone_r.get(eventid, "0")) or now, SOURCE, int(eventid)),
            )
            row = cur.fetchone()
            if row:
                resolved.append(row)
        if any(not r["baseline"] and r["resolved_at"] is None for r in inserted):
            repo.mark_plan_stale(cur, None, None, "new production problem", actor="system")  # task #129
        cur.execute("UPDATE health_sources SET bookmark = %s, last_sync_at = %s, last_ok_at = %s, "
                    "last_error = NULL WHERE source = %s", (new_bookmark, now, now, SOURCE))

    escalated = _escalate(inserted, resolved)
    return {"ok": True, "first": first, "new": len(inserted), "resolved": len(resolved),
            "escalated": escalated, "open": len(open_ids), "bookmark": new_bookmark}


def _health_project() -> dict | None:
    try:
        return repo.get_project_by_slug(roles.slug("health"))
    except repo.NotFound:
        return None


def _escalate(inserted: list[dict], resolved: list[dict]) -> int:
    """New, still-open, non-baseline problems at/above the threshold -> the health project's errors outbox; the
    recovery of one that was escalated -> the same dedupe key. Returns how many rows were pushed."""
    project = _health_project()
    if project is None:
        return 0
    pushed = 0
    for row in inserted:
        if row["baseline"] or row["resolved_at"] is not None \
                or row["severity"] < config.HEALTH_ESCALATE_MIN_SEVERITY:
            continue
        msg = (f"Zabbix problem on {row['host'] or 'unknown host'}: {row['name']} "
               f"({SEVERITY.get(row['severity'], row['severity'])}, event {row['eventid']}) "
               f"- see /{project['slug']}/health")
        _push(project, row, msg)
        pushed += 1
    for row in resolved:
        if row["escalated_at"] is None:
            continue
        _push(project, row, f"Zabbix problem on {row['host'] or 'unknown host'} RESOLVED: {row['name']} "
                            f"(event {row['eventid']})", mark=False)
        pushed += 1
    return pushed


def _push(project: dict, row: dict, message: str, mark: bool = True) -> None:
    repo.push_outbox("errors", "health_error", message[:400], project_id=project["id"],
                     dedupe_key=f"zabbix:{row['objectid'] or row['eventid']}")
    if mark:
        with get_cursor() as cur:
            cur.execute("UPDATE health_problems SET escalated_at = now() WHERE source = %s AND eventid = %s",
                        (row["source"], row["eventid"]))


def note_poke() -> None:
    with get_cursor() as cur:
        cur.execute("INSERT INTO health_sources (source, last_poke_at) VALUES (%s, now()) "
                    "ON CONFLICT (source) DO UPDATE SET last_poke_at = now()", (SOURCE,))


# ---------------------------------------------------------------------------------------------
# reads for the pages
# ---------------------------------------------------------------------------------------------

def _stale_after() -> timedelta:
    return timedelta(seconds=max(config.HEALTH_POLL_SECONDS, 60) * 3)


def summary(resolved_days: int = 7, resolved_limit: int = 25) -> dict:
    """Everything the Health page needs. `state` is 'off' (no token), 'waiting' (never synced), 'error' (last
    sync failed), 'stale' (no good sync lately) or 'ok'."""
    with get_cursor() as cur:
        cur.execute("SELECT * FROM health_sources WHERE source = %s", (SOURCE,))
        src = cur.fetchone() or {}
        cur.execute("SELECT * FROM health_problems WHERE source = %s AND resolved_at IS NULL "
                    "ORDER BY severity DESC, started_at", (SOURCE,))
        open_rows = cur.fetchall()
        cur.execute("SELECT * FROM health_problems WHERE source = %s AND resolved_at > now() - make_interval("
                    "days => %s) ORDER BY resolved_at DESC LIMIT %s", (SOURCE, resolved_days, resolved_limit))
        resolved_rows = cur.fetchall()
    configured = is_configured()
    now = datetime.now(timezone.utc)
    if not configured:
        state = "off"
    elif not src.get("last_sync_at"):
        state = "waiting"
    elif src.get("last_error"):
        state = "error"
    elif not src.get("last_ok_at") or now - src["last_ok_at"] > _stale_after():
        state = "stale"
    else:
        state = "ok"
    counts = {s: 0 for s in SEVERITY}
    for r in open_rows:
        counts[r["severity"]] = counts.get(r["severity"], 0) + 1
    return {
        "state": state,
        "configured": configured,
        "source": src,
        "open": open_rows,
        "resolved": resolved_rows,
        "counts": counts,
        "worst": max((r["severity"] for r in open_rows), default=None),
        "escalate_min": config.HEALTH_ESCALATE_MIN_SEVERITY,
        "project": roles.slug("health"),
        "groups": config.ZABBIX_GROUPIDS,
        "poll_seconds": config.HEALTH_POLL_SECONDS,
    }


def slot(s: dict | None = None) -> dict:
    """One line for the global home / project cards: {state, open, worst, label}."""
    s = s or summary(resolved_limit=0)
    n = len(s["open"])
    if s["state"] == "off":
        label = "not connected"
    elif s["state"] == "waiting":
        label = "first sync pending"
    elif s["state"] in ("error", "stale"):
        label = f"sync {s['state']}" + (f" · {n} open" if n else "")
    else:
        label = f"{n} open problem{'s' if n != 1 else ''}" if n else "all clear"
    return {"state": s["state"], "open": n, "worst": s["worst"], "label": label, "project": s["project"]}
