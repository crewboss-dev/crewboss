"""Pushed app health (task #404, owner 2026-10-15: "each server should host its own health checks and push those to dev,
with a cache period in case the connection is down or dev is down"). Contract: docs/health-report-contract.md.

- RECEIVE: POST /api/health/report with a project-bound, write-only 'reporter' token. The body is a BATCH,
  {"reports": [{report_id, source, observed_at, health}, ...]} (<= HEALTH_REPORT_MAX_BATCH reports,
  <= HEALTH_REPORT_MAX_BYTES), so a server can replay its spool after an outage. Each report is checked on its own and
  answered on its own: accepted, duplicate (that report_id was already stored: idempotent, safe to resend) or
  rejected (permanently: don't resend). The sender drops accepted + duplicate from its spool.
- STORE: every accepted report goes into health_reports (the timeline, HEALTH_REPORT_HISTORY_DAYS of observed_at,
  pruned on write). The LATEST state per (project, source) is the report with the newest observed_at — a backfilled
  older report lands in the history only.
- USE: a project whose CREWBOSS_HOME/health_feeds.yaml mode is `push` is read from these reports. With `erp: true` a report that
  becomes the latest is ingested exactly like a fetched ERP feed (app/erp_health.py ingest: events, failures, …). A
  linked admin site's catalog check (app/drift.py) reads current(): the pushed report in push mode, else the fetched
  ERP snapshot.
- HOST (#828, optional): a report's `health.host` = {disk_free_pct, disk_path, uptime_s} is validated (check_host) and kept
  as sent. app/host_signal.py shows it per deploy server; check() raises "low disk on <source>" (an error, once, below
  host_signal.LOW_DISK_PCT free) and clears it when a newer report is back above the line.
- DEAD-MAN: check() raises "<project> stopped reporting (<source>)" — an error, sent once to the errors session —
  when a watched source's newest OBSERVED report is older than DEADMAN_FACTOR x expect_every_seconds (so after a
  replay the gap shows as real history, not a blank). Tracked in health_problems (source 'reports')."""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml
from psycopg.types.json import Json

from app import config, roles
from app.db import get_cursor

log = logging.getLogger("crewboss.health_reports")

SOURCE = "reports"
SOURCE_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")
FUTURE_SKEW = timedelta(minutes=5)


class ReportError(Exception):
    """The whole request is refused (status 400 or 413). The message is safe to return."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------------------------

def load_config(path: str | None = None) -> dict[str, dict]:
    """CREWBOSS_HOME/health_feeds.yaml as {slug: {mode, expect_every_seconds, sources, erp, erp_source}}."""
    p = Path(path or config.HEALTH_FEEDS_FILE)
    try:
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        doc = {}   # no health_feeds.yaml in CREWBOSS_HOME: no project feeds
    except (OSError, yaml.YAMLError) as exc:
        log.warning("health feeds config %s unreadable: %s", p, exc)
        doc = {}
    out = {}
    for slug, c in (doc.get("projects") or {}).items():
        if not isinstance(c, dict):
            continue
        out[str(slug)] = {"mode": "push" if c.get("mode") == "push" else "pull",
                          "expect_every_seconds": int(c.get("expect_every_seconds") or 120),
                          "sources": [str(x) for x in c.get("sources") or []],
                          "erp": bool(c.get("erp")), "erp_source": c.get("erp_source"),
                          "public_url": str(c["public_url"]).strip() if c.get("public_url") else ""}
    return out


_cfg_override: dict | None = None   # tests set this through use_config()


def feeds() -> dict[str, dict]:
    return _cfg_override if _cfg_override is not None else load_config()


def use_config(cfg: dict | None) -> None:
    global _cfg_override
    _cfg_override = cfg


def mode(slug: str) -> str | None:
    c = feeds().get(slug)
    return c["mode"] if c else None


# ---------------------------------------------------------------------------------------------
# validate + receive
# ---------------------------------------------------------------------------------------------

def _dt(v) -> datetime | None:
    if not isinstance(v, str) or not v.strip():
        return None
    s = v.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else None   # a time without a zone is ambiguous: refused


HOST_KEYS = {"disk_free_pct": "a number from 0 to 100", "disk_path": "a path of at most 200 characters",
             "uptime_s": "a whole number of seconds, 0 or more"}


def check_host(host) -> str:
    """'' when health.host is valid, else why not. Every key is optional; an unknown key is ignored (kept, never read)."""
    if not isinstance(host, dict):
        return "health.host must be an object"
    pct = host.get("disk_free_pct")
    if "disk_free_pct" in host and (isinstance(pct, bool) or not isinstance(pct, (int, float))
                                    or pct != pct or not 0 <= pct <= 100):
        return f"health.host.disk_free_pct must be {HOST_KEYS['disk_free_pct']}"
    path = host.get("disk_path")
    if "disk_path" in host and (not isinstance(path, str) or not path or len(path) > 200):
        return f"health.host.disk_path must be {HOST_KEYS['disk_path']}"
    up = host.get("uptime_s")
    if "uptime_s" in host and (isinstance(up, bool) or not isinstance(up, int) or up < 0):
        return f"health.host.uptime_s must be {HOST_KEYS['uptime_s']}"
    return ""


def check_report(item, token: dict, now: datetime) -> tuple[dict | None, str]:
    """(normalised report, '') or (None, why it's rejected). Rejections are permanent: the sender must not resend."""
    from app import erp_health

    if not isinstance(item, dict):
        return None, "a report must be a JSON object"
    try:
        rid = str(uuid.UUID(str(item.get("report_id"))))
    except (ValueError, TypeError, AttributeError):
        return None, "report_id must be a UUID"
    if "project" in item and item["project"] != token["project"]["slug"]:
        return None, "project isn't the token's project"
    src = item.get("source")
    if not isinstance(src, str) or not SOURCE_RE.match(src):
        return None, "source must be a lower-case name: a-z 0-9 . _ - (1-63 chars)"
    if token.get("sources") and src not in token["sources"]:
        return None, f"this token may not report for source {src!r}"
    observed = _dt(item.get("observed_at"))
    if observed is None:
        return None, "observed_at must be an ISO 8601 time with a zone"
    if observed > now + FUTURE_SKEW:
        return None, "observed_at is in the future (check the sender's clock)"
    if observed < now - timedelta(days=config.HEALTH_REPORT_HISTORY_DAYS):
        return None, f"observed_at is older than the {config.HEALTH_REPORT_HISTORY_DAYS}-day history"
    health = item.get("health")
    try:
        erp_health.parse_snapshot(health)
    except erp_health.FeedError as exc:
        return None, f"health: {exc}"
    if "app_sha" in health and not (isinstance(health["app_sha"], str) and SHA_RE.match(health["app_sha"])):
        return None, "health.app_sha must be a hex commit sha"
    if "catalog" in health:
        cat = health["catalog"]
        if not isinstance(cat, dict) or not (isinstance(cat.get("commit"), str) and SHA_RE.match(cat["commit"])):
            return None, "health.catalog must be {commit: <hex sha>, built_at: <iso>}"
        if "built_at" in cat and _dt(cat["built_at"]) is None:
            return None, "health.catalog.built_at must be an ISO 8601 time with a zone"
    if "host" in health:
        why = check_host(health["host"])
        if why:
            return None, why
    return {"report_id": rid, "source": src, "observed_at": observed, "health": health}, ""


def parse_body(raw: bytes) -> list:
    """The batch envelope, or ReportError (the whole request is refused)."""
    if len(raw) > config.HEALTH_REPORT_MAX_BYTES:
        raise ReportError(f"body larger than {config.HEALTH_REPORT_MAX_BYTES} bytes: send a smaller batch", 413)
    try:
        body = json.loads(raw)
    except ValueError:
        raise ReportError("body isn't JSON") from None
    if not isinstance(body, dict) or not isinstance(body.get("reports"), list):
        raise ReportError('body must be {"reports": [...]}')
    if not body["reports"]:
        raise ReportError("reports is empty")
    if len(body["reports"]) > config.HEALTH_REPORT_MAX_BATCH:
        raise ReportError(f"more than {config.HEALTH_REPORT_MAX_BATCH} reports in one batch", 413)
    return body["reports"]


def receive(token: dict, raw: bytes, now: datetime | None = None) -> dict:
    """Store one batch for the token's project. {"results": [{report_id, status, reason?, latest?}], counts}.
    Reports are applied oldest observed_at first, so a replayed spool moves the latest state forward in order."""
    now = now or datetime.now(timezone.utc)
    items = parse_body(raw)
    project = token["project"]
    cfg = feeds().get(project["slug"]) or {}
    results: list[dict | None] = [None] * len(items)
    good = []
    for i, item in enumerate(items):
        rep, why = check_report(item, token, now)
        if rep is None:
            rid = item.get("report_id") if isinstance(item, dict) and isinstance(item.get("report_id"), str) else None
            results[i] = {"report_id": rid, "status": "rejected", "reason": why}
        else:
            good.append((i, rep))
    good.sort(key=lambda x: x[1]["observed_at"])
    seen_in_batch: set[str] = set()
    to_ingest = []
    with get_cursor() as cur:
        for i, rep in good:
            if rep["report_id"] in seen_in_batch:
                results[i] = {"report_id": rep["report_id"], "status": "duplicate"}
                continue
            seen_in_batch.add(rep["report_id"])
            cur.execute("INSERT INTO health_reports (project_id, source, report_id, observed_at, received_at, health, "
                        "token_id) VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (project_id, report_id) DO NOTHING "
                        "RETURNING id", (project["id"], rep["source"], rep["report_id"], rep["observed_at"], now,
                                         Json(rep["health"]), token.get("id")))
            if cur.fetchone() is None:
                results[i] = {"report_id": rep["report_id"], "status": "duplicate"}
                continue
            cur.execute("INSERT INTO health_report_latest (project_id, source, report_id, observed_at, received_at, "
                        "health) VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (project_id, source) DO UPDATE SET "
                        "report_id = EXCLUDED.report_id, observed_at = EXCLUDED.observed_at, received_at = "
                        "EXCLUDED.received_at, health = EXCLUDED.health "
                        "WHERE health_report_latest.observed_at < EXCLUDED.observed_at RETURNING report_id",
                        (project["id"], rep["source"], rep["report_id"], rep["observed_at"], now, Json(rep["health"])))
            latest = cur.fetchone() is not None
            results[i] = {"report_id": rep["report_id"], "status": "accepted", "latest": latest}
            if latest and cfg.get("erp") and cfg.get("mode") == "push" \
                    and cfg.get("erp_source") in (None, rep["source"]) and roles.is_role(project, "health"):
                to_ingest.append(rep)
        cur.execute("DELETE FROM health_reports WHERE project_id = %s AND observed_at < %s",
                    (project["id"], now - timedelta(days=config.HEALTH_REPORT_HISTORY_DAYS)))
    if to_ingest:
        from app import erp_health
        for rep in to_ingest:   # oldest first: events open and close in the order they were observed
            try:
                erp_health.ingest(rep["health"], now)
            except Exception:  # noqa: BLE001 — the report is stored; a tracking bug must not lose it
                log.exception("erp ingest of pushed report %s", rep["report_id"])
    counts = {k: sum(1 for r in results if r["status"] == k) for k in ("accepted", "duplicate", "rejected")}
    return {"results": results, **counts}


# ---------------------------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------------------------

def latest(project_slug: str) -> list[dict]:
    """The latest report per source of a project, newest observed first."""
    with get_cursor() as cur:
        cur.execute("SELECT l.* FROM health_report_latest l JOIN projects p ON p.id = l.project_id WHERE p.slug = %s "
                    "ORDER BY l.observed_at DESC", (project_slug,))
        return cur.fetchall()


def current(project_slug: str) -> tuple[dict | None, datetime | None, str]:
    """(health body, when observed, why-not) — the report that stands for the project now: in push mode the newest
    latest-per-source report (preferring one that carries app_sha/catalog); in pull mode the fetched ERP snapshot."""
    from app import erp_health

    cfg = feeds().get(project_slug)
    if cfg and cfg["mode"] == "push":
        rows = latest(project_slug)
        if not rows:
            return None, None, "no health report has been pushed yet"
        best = next((r for r in rows if isinstance(r["health"], dict)
                     and ("app_sha" in r["health"] or "catalog" in r["health"])), rows[0])
        return best["health"], best["observed_at"], ""
    if project_slug == roles.slug("health") and cfg and cfg.get("erp"):
        with get_cursor() as cur:
            cur.execute("SELECT snapshot, generated_at FROM health_sources WHERE source = %s", (erp_health.SOURCE,))
            row = cur.fetchone()
        if not row or not isinstance(row["snapshot"], dict):
            return None, None, "the ERP health feed hasn't been fetched yet"
        return row["snapshot"], row["generated_at"], ""
    return None, None, "its health isn't reaching the dashboard (CREWBOSS_HOME/health_feeds.yaml)"


# ---------------------------------------------------------------------------------------------
# dead-man
# ---------------------------------------------------------------------------------------------

_watch_started: dict[str, datetime] = {}


def _age(td: timedelta) -> str:
    m = int(td.total_seconds() // 60)
    return f"{m} min" if m < 120 else f"{m // 60} h" if m < 2880 else f"{m // 1440} days"


def _low_disk_findings(now: datetime) -> list[dict]:
    """An error per push-mode source whose newest report says its disk is below LOW_DISK_PCT free (#828). It is the last
    known state: a silent server is the dead-man's to report. HYSTERESIS: once open it clears only above WARN_DISK_PCT, so a
    disk hovering around the line raises once rather than flapping."""
    from app import drift, host_signal

    with get_cursor(commit=False) as cur:
        cur.execute("SELECT eventid FROM health_problems WHERE source = %s AND resolved_at IS NULL", (SOURCE,))
        open_ids = {r["eventid"] for r in cur.fetchall()}
    out = []
    for slug, cfg in sorted(feeds().items()):
        if cfg["mode"] != "push":
            continue
        for r in latest(slug):
            host = r["health"].get("host") if isinstance(r["health"], dict) else None
            pct = host.get("disk_free_pct") if isinstance(host, dict) else None
            key = f"lowdisk:{slug}:{r['source']}"
            if isinstance(pct, bool) or not isinstance(pct, (int, float)):
                continue
            was_open = drift.eventid(key) in open_ids
            if pct < host_signal.LOW_DISK_PCT or (was_open and pct <= host_signal.WARN_DISK_PCT):
                where = f" ({host['disk_path']})" if host.get("disk_path") else ""
                out.append({"key": key, "level": drift.ERROR, "host": slug,
                            "title": f"{slug}: low disk on {r['source']}, {pct:.0f}% free{where}"})
    return out


def check(now: datetime | None = None) -> dict:
    """Raise / clear "<project> stopped reporting (<source>)" for every push-mode project. Never raises."""
    from app import drift

    now = now or datetime.now(timezone.utc)
    try:
        findings, rows_out = [], []
        for slug, cfg in sorted(feeds().items()):
            if cfg["mode"] != "push":
                continue
            limit = timedelta(seconds=cfg["expect_every_seconds"] * config.HEALTH_REPORT_DEADMAN_FACTOR)
            rows = {r["source"]: r for r in latest(slug)
                    if r["observed_at"] > now - timedelta(days=config.HEALTH_REPORT_HISTORY_DAYS)}
            started = _watch_started.setdefault(slug, now)
            watched = sorted(set(rows) | set(cfg["sources"]))
            if not watched and now - started > limit:
                findings.append({"key": f"stopped:{slug}", "level": drift.ERROR, "host": slug,
                                 "title": f"{slug} stopped reporting: no health report has arrived"})
            for src in watched:
                row = rows.get(src)
                gap = now - (row["observed_at"] if row else started)
                rows_out.append({"project": slug, "source": src, "observed_at": row and row["observed_at"],
                                 "received_at": row and row["received_at"], "stale": gap > limit})
                if gap > limit:
                    seen = f"last report observed {_age(gap)} ago" if row else "no report yet"
                    findings.append({"key": f"stopped:{slug}:{src}", "level": drift.ERROR, "host": slug,
                                     "title": f"{slug} stopped reporting ({src}): {seen}"})
        findings += _low_disk_findings(now)
        with get_cursor() as cur:
            inserted, raised, resolved = drift.track(cur, SOURCE, findings, now)
        pushed = drift.escalate(SOURCE, "Health reports", inserted + raised, resolved)
        return {"ok": True, "watched": rows_out, "new": len(inserted), "resolved": len(resolved), "escalated": pushed}
    except Exception as exc:  # noqa: BLE001
        log.exception("health reports check")
        return {"ok": False, "error": exc.__class__.__name__}


def summary(now: datetime | None = None) -> dict:
    """The Health page's 'App reports' lines: every push-mode project's sources, plus the open alarms."""
    now = now or datetime.now(timezone.utc)
    rows = []
    for slug, cfg in sorted(feeds().items()):
        limit = timedelta(seconds=cfg["expect_every_seconds"] * config.HEALTH_REPORT_DEADMAN_FACTOR)
        latest_rows = latest(slug)
        if cfg["mode"] != "push":   # still fetched; a server already pushing (a trial before the switch) shows too
            rows.append({"project": slug, "mode": "pull", "source": None})
        elif not latest_rows:
            rows.append({"project": slug, "mode": "push", "source": None, "observed_at": None, "stale": True})
        for r in latest_rows:
            rows.append({"project": slug, "mode": "push", "source": r["source"], "observed_at": r["observed_at"],
                         "received_at": r["received_at"], "stale": now - r["observed_at"] > limit})
    with get_cursor() as cur:
        cur.execute("SELECT * FROM health_problems WHERE source = %s AND resolved_at IS NULL ORDER BY host, name",
                    (SOURCE,))
        open_rows = cur.fetchall()
    return {"rows": rows, "open": open_rows}
