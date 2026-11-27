"""A thin, generic host signal for each server Crewboss deploys to (#828). No agent is installed and nothing is dialled into
the server: it is read from what the dashboard already has.

- REACHABLE: the server's newest OBSERVED health report is fresh (the dead-man rule of app/health_reports.py: younger than
  HEALTH_REPORT_DEADMAN_FACTOR x the project's expect_every_seconds). A report's `source` names the server (the deploy
  server's name, case-insensitive); only reports of projects that deploy to that server count. No report yet = "No reports".
- DISK: the OPTIONAL `host` object of a report's `health` ({disk_free_pct, disk_path, uptime_s}, validated on receipt by
  health_reports.check_host). Only reports inside their project's dead-man window count (a stale one says nothing about now);
  across them the WORST (lowest) free percentage shows, with its path. None fresh = "No data". Under LOW_DISK_PCT free is a
  finding (health_reports.check raises it once, like "stopped reporting"; it clears only above WARN_DISK_PCT).
- TLS: for each project deploying here whose health_feeds.yaml entry has an https `public_url`, the days left on the public
  certificate (app/tls_check.py). NEVER dialled while a page is drawn: the answer comes from a cache (a good one lives an hour, a bad
  one five minutes); a miss shows "Checking" and starts ONE background refresh per URL.
- LAST DEPLOY: the newest finished deploy (server_jobs state 'done') to any target on the server, if there is one.

Only the projects the VIEWER can see take part (`visible_ids`; None = all, the owner), so a designer never sees a hidden project's
reports, host name or deploys through a shared server. The caller passes the feeds config once per page (`feeds`).

summary_for() returns plain facts plus a WORD and a tone for each (ok | warn | bad | none): the page shows the word, so colour
is never the only signal."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone

from app import config, health_reports, tls_check
from app.db import get_cursor

log = logging.getLogger("crewboss.host_signal")

LOW_DISK_PCT = 10.0       # below this free is an error finding
WARN_DISK_PCT = 20.0      # below this free is a warning on the page; a finding clears only above it
TLS_OK_TTL = 3600         # a good certificate result is reused for an hour
TLS_FAIL_TTL = 300        # a bad or failed one is looked at again after five minutes
DEFAULT_EXPECT_SECONDS = 120

_tls_cache: dict[str, tuple[float, tls_check.Result]] = {}
_inflight: set[str] = set()
_lock = threading.Lock()


def _spawn(fn) -> None:
    """Run `fn` in a background thread (the tests run it inline)."""
    threading.Thread(target=fn, name="tls-check", daemon=True).start()


def _refresh(url: str) -> None:
    try:
        res = tls_check.check(url)
    except Exception:  # noqa: BLE001 — a failed look is a result, never a crash
        log.warning("tls check failed to run", exc_info=True)
        res = tls_check.Result(False, "the check could not run", None, "", time.time())
    with _lock:
        _tls_cache[url] = (time.monotonic(), res)
        _inflight.discard(url)


def _tls(url: str, now_mono: float | None = None) -> tls_check.Result | None:
    """The cached result for `url`, or None while the first look is still running. Never dials in the caller's thread. An
    expired entry is still returned (better than nothing) while one refresh per URL runs."""
    now_mono = time.monotonic() if now_mono is None else now_mono
    with _lock:
        hit = _tls_cache.get(url)
        if hit and now_mono - hit[0] < (TLS_OK_TTL if hit[1].ok else TLS_FAIL_TTL):
            return hit[1]
        start = url not in _inflight
        if start:
            _inflight.add(url)
    if start:
        try:
            _spawn(lambda: _refresh(url))
        except Exception:  # noqa: BLE001 — couldn't start a thread: try again next page
            with _lock:
                _inflight.discard(url)
            log.warning("tls check thread did not start", exc_info=True)
    return hit[1] if hit else None


def _projects_on(server_id: int, visible_ids) -> list[dict]:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT DISTINCT p.id, p.slug FROM deploy_targets t JOIN projects p ON p.id = t.project_id "
                    "WHERE t.server_id = %s AND (%s::int[] IS NULL OR p.id = ANY(%s::int[])) ORDER BY p.slug",
                    (server_id, None if visible_ids is None else list(visible_ids),
                     [] if visible_ids is None else list(visible_ids)))
        return [dict(r) for r in cur.fetchall()]


def _reports(server_name: str, project_ids: list[int]) -> list[dict]:
    if not project_ids:
        return []
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT l.*, p.slug AS project_slug FROM health_report_latest l JOIN projects p ON p.id = l.project_id "
                    "WHERE l.project_id = ANY(%s) AND lower(l.source) = lower(%s) ORDER BY l.observed_at DESC",
                    (project_ids, server_name))
        return [dict(r) for r in cur.fetchall()]


def _last_deploy(server_id: int, project_ids: list[int]) -> datetime | None:
    if not project_ids:
        return None
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT max(j.finished_at) AS at FROM server_jobs j JOIN deploy_targets t ON t.id = j.target_id "
                    "WHERE t.server_id = %s AND j.state = 'done' AND t.project_id = ANY(%s)", (server_id, project_ids))
        row = cur.fetchone()
    return row["at"] if row else None


def _limit(slug: str, feeds: dict) -> timedelta:
    cfg = feeds.get(slug) or {}
    return timedelta(seconds=(cfg.get("expect_every_seconds") or DEFAULT_EXPECT_SECONDS)
                     * config.HEALTH_REPORT_DEADMAN_FACTOR)


def _age(td: timedelta) -> str:
    m = max(int(td.total_seconds() // 60), 0)
    return f"{m} min" if m < 120 else f"{m // 60} h" if m < 2880 else f"{m // 1440} days"


def _disk(reports: list[dict], now: datetime, feeds: dict) -> dict:
    """The worst free percentage among the FRESH reports that carry one (a stale report is ignored)."""
    worst = None
    for r in reports:
        if now - r["observed_at"] > _limit(r["project_slug"], feeds):
            continue
        host = r["health"].get("host") if isinstance(r["health"], dict) else None
        pct = host.get("disk_free_pct") if isinstance(host, dict) else None
        if isinstance(pct, bool) or not isinstance(pct, (int, float)):
            continue
        if worst is None or pct < worst[0]:
            worst = (float(pct), host.get("disk_path") or "")
    if worst is None:
        return {"word": "No data", "tone": "none", "free_pct": None, "path": ""}
    pct, path = worst
    return {"word": f"Disk {pct:.0f}% free" + (f" ({path})" if path else ""), "free_pct": pct, "path": path,
            "tone": "bad" if pct < LOW_DISK_PCT else "warn" if pct < WARN_DISK_PCT else "ok"}


def _tls_summary(urls: list[str]) -> dict:
    if not urls:
        return {"word": "No public URL", "tone": "none", "days_left": None, "hosts": []}
    results = [(u, _tls(u)) for u in urls]
    done = [r for _, r in results if r is not None]
    if not done:
        return {"word": "Checking", "tone": "none", "days_left": None, "hosts": []}
    worst = min(done, key=lambda r: (r.ok, r.days_left if r.days_left is not None else -1))
    hosts = sorted({r.host for r in done if r.host})
    if not worst.ok and worst.days_left is None:
        return {"word": "TLS problem", "tone": "bad", "days_left": None, "problem": worst.problem, "hosts": hosts}
    d = worst.days_left
    return {"word": f"TLS {d} days left" if d is not None and d >= 0 else "TLS expired",
            "tone": "bad" if not worst.ok and d is not None and d < 0 else "warn" if not worst.ok else "ok",
            "days_left": d, "problem": worst.problem, "hosts": hosts}


def summary_for(server: dict, now: datetime | None = None, visible_ids=None, feeds: dict | None = None) -> dict:
    """{reachable, disk, tls, deploy}: each {word, tone, ...facts}. `server` is a commissioned_servers row (id, name);
    `visible_ids` the project ids the viewer may see (None = all); `feeds` health_reports.feeds() (read once per page)."""
    now = now or datetime.now(timezone.utc)
    feeds = health_reports.feeds() if feeds is None else feeds
    projects = _projects_on(server["id"], visible_ids)
    ids = [p["id"] for p in projects]
    reports = _reports(server["name"], ids)

    if not reports:
        reach = {"word": "No reports", "tone": "none", "observed_at": None}
    else:
        newest = reports[0]
        gap = now - newest["observed_at"]
        if gap <= _limit(newest["project_slug"], feeds):
            reach = {"word": "Reporting", "tone": "ok", "observed_at": newest["observed_at"]}
        else:
            reach = {"word": f"Silent for {_age(gap)}", "tone": "bad", "observed_at": newest["observed_at"]}

    urls = sorted({u for u in ((feeds.get(p["slug"]) or {}).get("public_url") or "" for p in projects)
                   if u and tls_check.target(u)})

    when = _last_deploy(server["id"], ids)
    deploy = ({"word": f"Deployed {_age(now - when)} ago", "tone": "ok", "at": when} if when
              else {"word": "No deploys yet", "tone": "none", "at": None})
    return {"reachable": reach, "disk": _disk(reports, now, feeds), "tls": _tls_summary(urls), "deploy": deploy}
