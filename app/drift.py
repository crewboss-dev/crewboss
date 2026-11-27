"""No drift (#402, owner q213: "anything any agent changes must be updated"). Checks that the code, Crewboss
and each project's admin site agree, and puts what doesn't on the Health page. Config: CREWBOSS_HOME/drift.yaml (reviewed; a new
linked admin site is config, not code).

- ADMIN CATALOG BEHIND (per linked admin site): its health feed reports `app_sha` (the deployed commit) and
  `catalog: {commit, built_at}` (the commit its admin catalog was built from). The dashboard never calls the admin site:
  it reads whichever health report is current for the project (pushed, #404, or the ERP feed it still fetches; see
  app/health_reports.py current()). The one exception (#828): a site may name a slim `version_url`, read with the ERP health read
  token and ONLY when it is on the ERP's own origin (scheme, host and port of ERP_HEALTH_URL); the answer is used only when it
  has both app_sha and catalog_sha, else the report is. Then, in its read-only copy of the project's repo:
    git diff --name-only <catalog.commit> <app_sha> -- <catalog_paths> ':(exclude)<exclude_paths>'
  non-empty = an ERROR ("behind"); a commit the copy doesn't have = a WARNING ("can't verify"); fields absent = "not
  reported", no alert. catalog.commit != app_sha alone is normal (the release commits the catalog one commit later).
  A WARNING when the project's latest scan of main has catalog-relevant commits the running app doesn't, first seen by
  the dashboard more than `undeployed_hours` ago (pushed but not deployed is fine short-term).

- AUTOMATION ON A SERVER WITH NO DIAGRAM: Zabbix (read-only token) holds each estate server's systemd timers in the
  item systemd.unit.discovery[timer] (the server-health template, Zabbix agent 2's systemd plugin). Each timer must
  be named by an annotation's `box_jobs` ($CREWBOSS_HOME/scanner/annotations.yaml → the latest scans) or be an OS timer in
  CREWBOSS_HOME/drift.yaml; anything else is a WARNING "Automation on <hosts> with no diagram: <unit>" (one per unit). Servers come from
  the estate servers.yml (name ↔ zabbix_host). Cron jobs aren't checked yet.

Findings are tracked like the other sources, in health_problems (source 'drift', eventid = a stable hash of the
finding's key): errors go once to the health project's errors outbox (dedupe drift:<eventid>) and their recovery
follows; warnings only show. A check that couldn't run keeps its previous findings ("couldn't look" is not "fixed").
The last run's detail lives in health_sources('drift').snapshot for the page."""
from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from psycopg.types.json import Json

from app import config, repo, roles
from app.db import get_cursor

log = logging.getLogger("crewboss.drift")

SOURCE = "drift"
ERROR, WARNING = 4, 2
GIT_TIMEOUT = 20
BEHIND_TITLE = "Admin site shows workflows from an older version than what's running"


# ---------------------------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------------------------

def load_config(path: str | None = None) -> dict:
    """CREWBOSS_HOME/drift.yaml as {admin_sites: {slug: site}, os_timers: [unit, ...]}; {} parts when unreadable (logged)."""
    p = Path(path or config.DRIFT_FILE)
    try:
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        doc = {}   # no drift.yaml in CREWBOSS_HOME: nothing linked
    except (OSError, yaml.YAMLError) as exc:
        log.warning("drift config %s unreadable: %s", p, exc)
        doc = {}
    sites = {}
    for slug, site in (doc.get("admin_sites") or {}).items():
        if not isinstance(site, dict):
            continue
        sites[str(slug)] = {
            "repo": site.get("repo"),
            "catalog_paths": [str(x) for x in site.get("catalog_paths") or []],
            "exclude_paths": [str(x) for x in site.get("exclude_paths") or []],
            "undeployed_hours": float(site.get("undeployed_hours") or 24),
            "version_url": (str(site["version_url"]).strip() or None) if site.get("version_url") else None,
            "version": str(site["version"]).strip().lower() if site.get("version") else None,
        }
    return {"admin_sites": sites, "os_timers": sorted({str(u) for u in doc.get("os_timers") or []})}


# ---------------------------------------------------------------------------------------------
# git, read-only, in the project's mounted .git
# ---------------------------------------------------------------------------------------------

class GitUnavailable(Exception):
    """The repo copy can't be read (not mounted, no git). The message is safe to show."""


def _git(repo_dir: str, *args: str) -> subprocess.CompletedProcess:
    git_dir = Path(repo_dir) / ".git"
    if not git_dir.exists():
        raise GitUnavailable(f"the dashboard can't read {repo_dir} (its .git isn't mounted)")
    try:
        return subprocess.run(["git", "-c", "safe.directory=*", f"--git-dir={git_dir}", *args],
                              capture_output=True, text=True, timeout=GIT_TIMEOUT)
    except FileNotFoundError as exc:
        raise GitUnavailable("git isn't installed in the dashboard") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitUnavailable(f"git timed out in {repo_dir}") from exc


_known: set[tuple[str, str]] = set()


def has_commit(repo_dir: str, sha: str) -> bool:
    """Is the commit in the copy? Only a yes is cached (a fetch can bring a missing one in)."""
    if (repo_dir, sha) in _known:
        return True
    ok = _git(repo_dir, "cat-file", "-e", f"{sha}^{{commit}}").returncode == 0
    if ok:
        if len(_known) > 5000:
            _known.clear()
        _known.add((repo_dir, sha))
    return ok


def pathspec(site: dict) -> list[str]:
    return list(site["catalog_paths"]) + [f":(exclude){p.rstrip('/')}" for p in site["exclude_paths"]]


def changed_files(repo_dir: str, a: str, b: str, site: dict) -> list[str]:
    """Catalog-relevant files that differ between two known commits."""
    r = _git(repo_dir, "diff", "--name-only", a, b, "--", *pathspec(site))
    if r.returncode != 0:
        raise GitUnavailable(f"git diff failed: {r.stderr.strip()[-200:]}")
    return [f for f in r.stdout.splitlines() if f]


def first_relevant_commit(repo_dir: str, base: str, head: str, site: dict) -> str | None:
    """The oldest commit in base..head that touches a catalog-relevant path, or None."""
    r = _git(repo_dir, "log", "--reverse", "--format=%H", f"{base}..{head}", "--", *pathspec(site))
    if r.returncode != 0:
        raise GitUnavailable(f"git log failed: {r.stderr.strip()[-200:]}")
    return next((line for line in r.stdout.splitlines() if line), None)


_ancestry: dict[tuple[str, str, str], bool] = {}


def _contains(repo_dir: str, commit: str, tip: str) -> bool:
    """Is `commit` in `tip`'s history? Cached: two commits' ancestry never changes."""
    key = (repo_dir, commit, tip)
    if key not in _ancestry:
        if len(_ancestry) > 5000:
            _ancestry.clear()
        _ancestry[key] = _git(repo_dir, "merge-base", "--is-ancestor", commit, tip).returncode == 0
    return _ancestry[key]


# ---------------------------------------------------------------------------------------------
# admin catalog behind
# ---------------------------------------------------------------------------------------------

def _short(sha) -> str:
    return str(sha or "")[:7] or "?"


def _feed_body(slug: str) -> tuple[dict | None, datetime | None, str]:
    """(health body, as of, why-not): whichever report is current for the project (app/health_reports.py current():
    the pushed report in push mode, the fetched ERP snapshot in pull mode). The admin site is never called here (the optional slim version endpoint is _slim_version)."""
    from app import health_reports

    return health_reports.current(slug)


VERSION_PATH = "/api/health/release"


def version_url(site: dict) -> str | None:
    """The slim version endpoint of a site (#828), or None to use the health report. An explicit `version_url` wins;
    `version: erp` derives <ERP_HEALTH_URL>/api/health/release (the ERP's endpoint, behind the feed's own bearer token)."""
    if site.get("version_url"):
        return site["version_url"]
    if site.get("version") == "erp" and config.ERP_HEALTH_URL:
        base = config.ERP_HEALTH_URL.rstrip("/")
        base = base[: -len("/api/health")] if base.endswith("/api/health") else base
        return base + VERSION_PATH
    return None


def _slim_version(site: dict) -> tuple[dict, datetime] | None:
    """({app_sha, catalog: {commit}}, as of) read from the site's version endpoint, shaped like a health body so the check
    below is one path; None when none is configured or it can't be read or lacks an app_sha (then the health report is used)."""
    from app import erp_health

    url = version_url(site)
    if not url:
        return None
    if not erp_health.same_origin(url):   # the ERP's read token goes to the ERP's own origin and nowhere else
        log.warning("version_url host %r is not the ERP's own address; the token is not sent, using the health report",
                    urlsplit(url).hostname or "?")
        return None
    try:
        got = erp_health.fetch_json(url)
    except Exception as exc:  # noqa: BLE001 — an unreachable endpoint falls back to the report, never breaks the run
        log.warning("version endpoint unreadable (%s); using the health report", exc.__class__.__name__)
        return None
    app_sha, cat_sha = got.get("app_sha"), got.get("catalog_sha")
    if not all(isinstance(v, str) and v.strip() for v in (app_sha, cat_sha)):
        return None   # a half answer (one sha) is not enough to compare: the report path decides
    return {"app_sha": app_sha.strip(), "catalog": {"commit": cat_sha.strip()}}, datetime.now(timezone.utc)


def _scan_commits(project_slug: str) -> list[tuple[str, datetime]]:
    """(commit, first seen) per distinct commit of the project's scans in the last 60 days, oldest first."""
    with get_cursor() as cur:
        cur.execute("SELECT s.commit, min(s.created_at) AS seen FROM catalog_scans s JOIN projects p "
                    "ON p.id = s.project_id WHERE p.slug = %s AND s.created_at > now() - interval '60 days' "
                    "GROUP BY s.commit ORDER BY seen", (project_slug,))
        return [(r["commit"], r["seen"]) for r in cur.fetchall()]


def check_admin_site(slug: str, site: dict, now: datetime) -> dict:
    """One linked admin site. Returns {project, state, word, …, findings}. state: off (no feed) | not_reported |
    ok | behind | unverified."""
    out = {"project": slug, "state": "off", "word": "", "app_sha": None, "catalog_commit": None,
           "catalog_built_at": None, "feed_generated_at": None, "files": [], "pending": None, "findings": []}
    slim = _slim_version(site)
    body, generated_at, why = (slim[0], slim[1], "") if slim else _feed_body(slug)
    out["feed_generated_at"] = generated_at.isoformat() if generated_at else None
    if body is None:
        out["word"] = f"Not checked: {why}."
        return out
    app_sha = body.get("app_sha") if isinstance(body.get("app_sha"), str) and body.get("app_sha").strip() else None
    cat = body.get("catalog") if isinstance(body.get("catalog"), dict) else {}
    cat_commit = cat.get("commit") if isinstance(cat.get("commit"), str) and cat.get("commit").strip() else None
    out.update(app_sha=app_sha, catalog_commit=cat_commit,
               catalog_built_at=cat.get("built_at") if isinstance(cat.get("built_at"), str) else None)
    if not app_sha or not cat_commit:
        missing = [k for k, v in (("app_sha", app_sha), ("catalog.commit", cat_commit)) if not v]
        out.update(state="not_reported", word=f"Not reported: the health feed has no {' or '.join(missing)} yet.")
        return out

    repo_dir = site.get("repo") or ""
    try:
        unknown = [s for s in (cat_commit, app_sha) if not has_commit(repo_dir, s)]
        if unknown:
            raise GitUnavailable(f"commit {', '.join(_short(s) for s in unknown)} isn't in the dashboard's copy of "
                                 f"{repo_dir} yet (it fetches every 5 min)")
        files = changed_files(repo_dir, cat_commit, app_sha, site)
    except GitUnavailable as exc:
        out.update(state="unverified", word=f"Can't verify: {exc}.")
        out["findings"].append({"key": f"admin-unverified:{slug}", "level": WARNING, "host": slug,
                                "title": f"Can't check that the admin site's workflows match what's running: {exc}"})
        return out
    out["files"] = files
    if files:
        shown = ", ".join(files[:5]) + (f" (+{len(files) - 5} more)" if len(files) > 5 else "")
        out.update(state="behind", word=f"{BEHIND_TITLE}: its catalog was built from {_short(cat_commit)}, the app "
                                        f"runs {_short(app_sha)}, and {len(files)} workflow file(s) differ.")
        out["findings"].append({"key": f"admin-behind:{slug}", "level": ERROR, "host": slug,
                                "title": f"{BEHIND_TITLE} (catalog {_short(cat_commit)}, running {_short(app_sha)}: "
                                         f"{shown})"})
    else:
        out.update(state="ok", word=f"In step: the catalog ({_short(cat_commit)}) matches the running app "
                                    f"({_short(app_sha)}).")
    out["pending"] = _undeployed(slug, site, repo_dir, app_sha, now, out["findings"])
    return out


def _undeployed(slug: str, site: dict, repo_dir: str, app_sha: str, now: datetime, findings: list) -> dict | None:
    """Catalog-relevant commits on the latest scanned main that the running app lacks: {head, first, since, hours}."""
    scans = _scan_commits(slug)
    if not scans:
        return None
    head = scans[-1][0]
    if head == app_sha:
        return None
    try:
        if not has_commit(repo_dir, head):
            return None
        first = first_relevant_commit(repo_dir, app_sha, head, site)
        if not first:
            return None
        since = next((seen for commit, seen in scans if has_commit(repo_dir, commit)
                      and _contains(repo_dir, first, commit)), scans[-1][1])
    except GitUnavailable:
        return None
    hours = (now - since).total_seconds() / 3600
    pending = {"head": head, "first": first, "since": since.isoformat(), "hours": round(hours, 1)}
    if hours > site["undeployed_hours"]:
        findings.append({"key": f"admin-undeployed:{slug}", "level": WARNING, "host": slug,
                         "title": f"Workflow changes have been on main for {int(hours)}h but aren't deployed, so the "
                                  f"admin site doesn't show them (running {_short(app_sha)}, main {_short(head)})"})
    return pending


# ---------------------------------------------------------------------------------------------
# server timers with no diagram
# ---------------------------------------------------------------------------------------------

TIMER_KEY = "systemd.unit.discovery[timer]"


def box_jobs() -> dict[str, set[str]]:
    """unit -> the boxes annotations say it runs on ({"*"} = any box), over every active project's latest scan."""
    with get_cursor() as cur:
        cur.execute("SELECT DISTINCT ON (s.project_id) s.catalog FROM catalog_scans s JOIN projects p ON p.id = "
                    "s.project_id AND p.archived_at IS NULL ORDER BY s.project_id, s.created_at DESC")
        catalogs = [r["catalog"] for r in cur.fetchall()]
    out: dict[str, set[str]] = {}
    for cat in catalogs:
        for wf in (cat or {}).get("workflows") or []:
            for job in wf.get("box_jobs") or []:
                if isinstance(job, dict) and isinstance(job.get("unit"), str):
                    out.setdefault(job["unit"], set()).update(job.get("hosts") or ["*"])
    return out


def _units(raw: str) -> list[str] | None:
    """Timer names from the discovery JSON, leaving out ones that are loaded but neither running nor enabled."""
    try:
        rows = json.loads(raw)
    except ValueError:
        return None
    if isinstance(rows, dict):
        rows = rows.get("data") or []
    names = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict):
            continue
        name = r.get("{#UNIT.NAME}") or r.get("name")
        active = str(r.get("{#UNIT.ACTIVESTATE}") or "").lower()
        enabled = str(r.get("{#UNIT.UNITFILESTATE}") or "").lower()
        if isinstance(name, str) and name.endswith(".timer") and (active != "inactive" or enabled == "enabled"):
            names.append(name)
    return sorted(set(names))


def check_timers(cfg: dict) -> dict:
    """{checked, state, word, hosts: [{box, state, timers, uncovered}], findings}. checked=False = couldn't look."""
    from app import health, servers

    out = {"checked": False, "state": "off", "word": "", "hosts": [], "findings": []}
    client = health.client_from_config()
    boxes = [(s["name"], s["zabbix_host"]) for s in servers.load().get("servers") or [] if s.get("zabbix_host")]
    if client is None:
        out["word"] = "Not checked: the dashboard's Zabbix token isn't mounted."
        return out
    if not boxes:
        out["word"] = "Not checked: servers.yml (estate docs) isn't readable."
        return out
    try:
        found = client.call("host.get", {"output": ["hostid", "host"], "filter": {"host": [z for _, z in boxes]}})
        ids = {h["host"]: h["hostid"] for h in found}
        items = client.call("item.get", {"output": ["hostid", "lastvalue", "lastclock", "state", "error"],
                                         "hostids": list(ids.values()) or ["0"], "filter": {"key_": TIMER_KEY}})
    except Exception as exc:  # noqa: BLE001 — Zabbix down is "couldn't look", never "all clear"
        out.update(state="error", word=f"Not checked: Zabbix didn't answer ({str(exc)[:150]}).")
        return out
    by_host = {it["hostid"]: it for it in items}
    ignore = set(cfg.get("os_timers") or [])
    jobs = box_jobs()
    unseen = []
    uncovered: dict[str, list[str]] = {}   # unit -> the boxes it runs on uncovered: ONE warning per unit, not per box
    for box, zhost in sorted(boxes):
        row = {"box": box, "state": "ok", "timers": [], "uncovered": [], "error": ""}
        hostid = ids.get(zhost)
        it = by_host.get(hostid) if hostid else None
        if hostid is None:
            row["state"] = "invisible"
            unseen.append(box)
        elif it is None:
            row["state"] = "no_item"
            out["findings"].append({"key": f"timer-unchecked:{box}", "level": WARNING, "host": box,
                                    "title": f"Can't list the timers on {box}: it doesn't have the Zabbix item "
                                             f"{TIMER_KEY} (the server-health template)"})
        elif str(it.get("state")) == "1":
            row.update(state="unsupported", error=(it.get("error") or "")[:200])
            out["findings"].append({"key": f"timer-unchecked:{box}", "level": WARNING, "host": box,
                                    "title": f"Can't list the timers on {box}: Zabbix says "
                                             f"{row['error'] or 'the item is unsupported'}"})
        elif str(it.get("lastclock") or "0") == "0":
            row["state"] = "waiting"
        else:
            units = _units(it.get("lastvalue") or "")
            if units is None:
                row["state"] = "unreadable"
                out["findings"].append({"key": f"timer-unchecked:{box}", "level": WARNING, "host": box,
                                        "title": f"Can't list the timers on {box}: Zabbix's value isn't JSON"})
            else:
                row["timers"] = units
                row["uncovered"] = [u for u in units if u not in ignore
                                    and not (jobs.get(u, set()) & {box, "*"})]
                for u in row["uncovered"]:
                    uncovered.setdefault(u, []).append(box)
        out["hosts"].append(row)
    for u, on in sorted(uncovered.items()):
        out["findings"].append({"key": f"timer:{u}", "level": WARNING, "host": ", ".join(on),
                                "title": f"Automation on {', '.join(on)} with no diagram: {u}"})
    if unseen:
        out["findings"].append({"key": "timer-invisible", "level": WARNING, "host": ", ".join(unseen),
                                "title": f"Can't check server automation on {', '.join(unseen)}: the dashboard's "
                                         f"Zabbix login can't see {'it' if len(unseen) == 1 else 'them'}"})
    n = len(uncovered)
    out.update(checked=True, state="ok" if not n else "uncovered",
               word="Every timer on the servers has a diagram or is an OS timer." if not n else
               f"{n} kind(s) of timer on the servers have no diagram.")
    return out


# ---------------------------------------------------------------------------------------------
# sync: run the checks, track the findings
# ---------------------------------------------------------------------------------------------

_lock = threading.Lock()


def eventid(key: str) -> int:
    """A stable positive BIGINT for a finding's key."""
    return int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big") & (2 ** 63 - 1)


def sync(now: datetime | None = None, cfg: dict | None = None) -> dict:
    """One run. A concurrent caller is skipped. Never raises."""
    if not _lock.acquire(blocking=False):
        return {"ok": True, "skipped": True}
    try:
        return _sync_once(now or datetime.now(timezone.utc), cfg or load_config())
    except Exception as exc:  # noqa: BLE001 — a failed run is state to show, never a crash
        log.exception("drift sync")
        with get_cursor() as cur:
            _ensure_source(cur)
            cur.execute("UPDATE health_sources SET last_sync_at = %s, last_error = %s WHERE source = %s",
                        (now or datetime.now(timezone.utc), f"{exc.__class__.__name__}: {exc}"[:300], SOURCE))
        return {"ok": False, "error": exc.__class__.__name__}
    finally:
        _lock.release()


def _ensure_source(cur) -> dict:
    cur.execute("INSERT INTO health_sources (source) VALUES (%s) ON CONFLICT DO NOTHING", (SOURCE,))
    cur.execute("SELECT * FROM health_sources WHERE source = %s", (SOURCE,))
    return cur.fetchone()


def _sync_once(now: datetime, cfg: dict) -> dict:
    with get_cursor() as cur:
        prev = _ensure_source(cur).get("snapshot")
    prev = prev if isinstance(prev, dict) else {}
    prev_admin = {a.get("project"): a for a in prev.get("admin") or [] if isinstance(a, dict)}
    admin = [check_admin_site(slug, site, now) for slug, site in sorted(cfg["admin_sites"].items())]
    for a in admin:   # no feed this run: keep what it last showed (couldn't look is not fixed)
        if a["state"] == "off" and prev_admin.get(a["project"]):
            a["findings"] = prev_admin[a["project"]].get("findings") or []
    timers = check_timers(cfg)
    if not timers["checked"]:
        timers["findings"] = (prev.get("timers") or {}).get("findings") or []
    findings = [f for a in admin for f in a["findings"]] + timers["findings"]
    snapshot = {"ran_at": now.isoformat(), "admin": admin, "timers": timers}
    with get_cursor() as cur:
        inserted, raised, resolved = track(cur, SOURCE, findings, now)
        cur.execute("UPDATE health_sources SET snapshot = %s, last_sync_at = %s, last_ok_at = %s, last_error = NULL "
                    "WHERE source = %s", (Json(snapshot), now, now, SOURCE))
        if any(r["severity"] >= ERROR for r in inserted + raised):
            repo.mark_plan_stale(cur, None, None, "new drift error", actor="system")
    pushed = escalate(SOURCE, "Drift", inserted + raised, resolved)
    return {"ok": True, "findings": len(findings), "new": len(inserted), "resolved": len(resolved),
            "escalated": pushed}


def track(cur, source: str, findings: list[dict], now: datetime) -> tuple[list[dict], list[dict], list[dict]]:
    """Open set = this run's findings ({key, level, host, title}), in health_problems under `source` (eventid =
    eventid(key)). Returns (inserted or re-opened, raised to error while open, resolved). Shared with the pushed-report
    dead-man (app/health_reports.py, source 'reports')."""
    SOURCE = source  # noqa: N806 — the queries below read it
    cur.execute("SELECT * FROM health_problems WHERE source = %s", (SOURCE,))
    stored = {r["eventid"]: r for r in cur.fetchall()}
    inserted, raised, resolved = [], [], []
    open_ids = set()
    for f in findings:
        eid = eventid(f["key"])
        open_ids.add(eid)
        row = stored.get(eid)
        if row is None or row["resolved_at"] is not None:
            cur.execute(
                "INSERT INTO health_problems (source, eventid, host, name, severity, started_at, baseline) "
                "VALUES (%s, %s, %s, %s, %s, %s, false) ON CONFLICT (source, eventid) DO UPDATE SET host = "
                "EXCLUDED.host, name = EXCLUDED.name, severity = EXCLUDED.severity, started_at = EXCLUDED.started_at, "
                "resolved_at = NULL, escalated_at = NULL, baseline = false RETURNING *",
                (SOURCE, eid, f["host"][:200], f["title"][:500], f["level"], now))
            inserted.append(cur.fetchone())
            continue
        if row["name"] != f["title"][:500] or row["severity"] != f["level"] or row["host"] != f["host"][:200]:
            cur.execute("UPDATE health_problems SET host = %s, name = %s, severity = %s WHERE source = %s "
                        "AND eventid = %s RETURNING *", (f["host"][:200], f["title"][:500], f["level"], SOURCE, eid))
            changed = cur.fetchone()
            if row["severity"] < ERROR <= f["level"] and row["escalated_at"] is None:
                raised.append(changed)
    for eid, row in stored.items():
        if row["resolved_at"] is None and eid not in open_ids:
            cur.execute("UPDATE health_problems SET resolved_at = %s WHERE source = %s AND eventid = %s RETURNING *",
                        (now, SOURCE, eid))
            resolved.append(cur.fetchone())
    return inserted, raised, resolved


def _health_project() -> dict | None:
    try:
        return repo.get_project_by_slug(roles.slug("health"))
    except repo.NotFound:
        return None


def escalate(source: str, what: str, new: list[dict], resolved: list[dict]) -> int:
    """Errors (once, dedupe <source>:<eventid>) and their recoveries to the health project's errors outbox."""
    SOURCE = source  # noqa: N806
    project = _health_project()
    if project is None:
        return 0
    pushed = 0
    for row in new:
        if row["severity"] < ERROR:
            continue
        repo.push_outbox("errors", "health_error", f"{what} on {row['host']}: {row['name']} - see "
                                                   f"/{project['slug']}/health"[:400],
                         project_id=project["id"], dedupe_key=f"{source}:{row['eventid']}")
        with get_cursor() as cur:
            cur.execute("UPDATE health_problems SET escalated_at = now() WHERE source = %s AND eventid = %s",
                        (SOURCE, row["eventid"]))
        pushed += 1
    for row in resolved:
        if row["escalated_at"] is None:
            continue
        repo.push_outbox("errors", "health_error", f"{what} on {row['host']} RESOLVED: {row['name']}"[:400],
                         project_id=project["id"], dedupe_key=f"{source}:{row['eventid']}")
        pushed += 1
    return pushed


# ---------------------------------------------------------------------------------------------
# reads for the pages
# ---------------------------------------------------------------------------------------------

def summary() -> dict:
    """The Health page's drift section: the last run's detail + the open findings, errors first."""
    with get_cursor() as cur:
        cur.execute("SELECT * FROM health_sources WHERE source = %s", (SOURCE,))
        src = cur.fetchone() or {}
        cur.execute("SELECT * FROM health_problems WHERE source = %s AND resolved_at IS NULL "
                    "ORDER BY severity DESC, host, name", (SOURCE,))
        open_rows = cur.fetchall()
    snap = src.get("snapshot") if isinstance(src.get("snapshot"), dict) else {}
    return {"ran_at": src.get("last_sync_at"), "last_error": src.get("last_error"),
            "poll_seconds": config.DRIFT_POLL_SECONDS, "admin": snap.get("admin") or [],
            "timers": snap.get("timers") or {},
            "open": open_rows, "errors": [r for r in open_rows if r["severity"] >= ERROR],
            "warnings": [r for r in open_rows if r["severity"] < ERROR]}
