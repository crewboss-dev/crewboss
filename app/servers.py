"""Servers per account (task #300; owner, 2026-10-13): how each account's servers are backed up and
updated, shown — never entered (k80). Read-only from two places:

- the estate knowledge repo, mounted read-only at config.ESTATE_DIR: `servers.yml` (servers → account, projects,
  stacks, backup schedule/paths/retention, update settings) and the policy docs `infra/backups/strategy.md` and
  `infra/updates.md` (reboot windows, read from its table until servers.yml carries `updates.reboot_window`);
- Zabbix, over the existing read-only token: last backup success time, reboot required, pending updates, uptime and
  open problems per host. Cached briefly so a page load never hammers the API.

summary(account_slug) answers the owner's four questions in order: what's wrong, what needs doing, what's happened,
how it works.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from app import config
from app import health as health_mod

log = logging.getLogger("crewboss.servers")
LOCAL = ZoneInfo(config.LOCAL_TZ)
# The Zabbix item keys read per server. The three "health" items come from this install's own agent template, so their keys are
# instance.yaml `server_health_keys` (reboot, sec_updates, updates); without them those readings are simply absent.
KEYS = {
    "backup": "vfs.file.time[/opt/backup/last-success,modify]",
    "uptime": "system.uptime",
    **{k: str(config.SERVER_HEALTH_KEYS.get(k) or "") for k in ("reboot", "sec_updates", "updates")},   # "" matches no item
}
LIVE_TTL = 60
DAILY_MAX_AGE = timedelta(hours=26)
WEEKLY_MAX_AGE = timedelta(days=8)
DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_cache: dict = {"at": 0.0, "key": None, "data": None}
_lock = threading.Lock()


# --- the repo side ------------------------------------------------------------------------------------------------

def _estate() -> Path:
    return Path(config.ESTATE_DIR)


def load() -> dict:
    """servers.yml as a dict ({} when the repo isn't mounted). Each server gets the defaults merged in."""
    path = _estate() / "servers.yml"
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        log.warning("servers.yml unreadable: %s", exc)
        return {}
    defaults = doc.get("defaults") or {}
    out = []
    for s in doc.get("servers") or []:
        if not isinstance(s, dict) or not s.get("name"):
            continue
        merged = {**s, "account": s.get("account") or defaults.get("account")}
        merged["backup"] = {**(defaults.get("backup") or {}), **(s.get("backup") or {})}
        merged["updates"] = {**(defaults.get("updates") or {}), **(s.get("updates") or {})}
        out.append(merged)
    return {"verified": str(doc.get("verified") or ""), "verified_by": doc.get("verified_by") or "",
            "defaults": defaults, "servers": out}


def doc_text(rel: str) -> str:
    try:
        return (_estate() / rel).read_text(encoding="utf-8")
    except OSError:
        return ""


_WINDOW_ROW = re.compile(r"^\|\s*([a-z0-9-]+)[^|]*\|\s*(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+(\d{1,2}:\d{2})\s*\|"
                         r"\s*(\d{1,2}:\d{2})?", re.M)


def reboot_windows() -> dict[str, dict]:
    """Reboot window per server from infra/updates.md §3 (a proposal until approved). servers.yml's own
    updates.reboot_window wins when present (see summary)."""
    text = doc_text("infra/updates.md")
    status = "proposed" if "PROPOSAL" in text[:300] else "approved"
    return {m.group(1): {"day": m.group(2), "time": m.group(3), "backup_first": m.group(4) or "", "status": status}
            for m in _WINDOW_ROW.finditer(text)}


def _backup_time(server: dict) -> tuple[str, bool, str]:
    """(HH:MM, weekly?, day) from the backup schedule's on_calendar, e.g. '*-*-* 03:30:00' or 'Sun *-*-* 03:00:00'."""
    cal = str(((server.get("backup") or {}).get("schedule") or {}).get("on_calendar") or "")
    m = re.search(r"(\d{1,2}):(\d{2})", cal)
    hhmm = f"{int(m.group(1)):02d}:{m.group(2)}" if m else ""
    day = next((d for d in DAYS if cal.startswith(d)), "")
    return hhmm, bool(day), day


# --- the live side (Zabbix) ---------------------------------------------------------------------------------------

def _fetch_live(hosts: list[str]) -> dict:
    client = health_mod.client_from_config()
    if client is None or not hosts:
        return {"ok": False, "error": "Zabbix isn't configured", "hosts": {}}
    try:
        found = client.call("host.get", {"output": ["hostid", "host", "name"], "filter": {"host": hosts}})
        ids = {h["hostid"]: h["host"] for h in found}
        out = {h: {"items": {}, "problems": []} for h in ids.values()}
        if ids:
            items = client.call("item.get", {"output": ["hostid", "key_", "lastvalue", "lastclock"],
                                             "hostids": list(ids), "filter": {"key_": list(KEYS.values())}})
            for it in items:
                out[ids[it["hostid"]]]["items"][it["key_"]] = it
            for hid, host in ids.items():
                probs = client.call("problem.get", {"output": ["eventid", "name", "severity", "clock"],
                                                    "hostids": [hid], "recent": False})
                out[host]["problems"] = sorted(probs, key=lambda p: -int(p.get("severity") or 0))
        return {"ok": True, "error": "", "hosts": out}
    except Exception as exc:  # noqa: BLE001  (a page never fails over Zabbix)
        log.warning("servers: Zabbix read failed: %s", exc)
        return {"ok": False, "error": str(exc)[:200], "hosts": {}}


def live(hosts: list[str]) -> dict:
    key = tuple(sorted(hosts))
    with _lock:
        if _cache["data"] is not None and _cache["key"] == key and time.monotonic() - _cache["at"] < LIVE_TTL:
            return _cache["data"]
    data = _fetch_live(list(key))
    with _lock:
        _cache.update(at=time.monotonic(), key=key, data=data)
    return data


def _num(item: dict | None) -> float | None:
    try:
        return float(item["lastvalue"]) if item and str(item.get("lastclock")) not in ("", "0") else None
    except (TypeError, ValueError, KeyError):
        return None


# --- the page model -----------------------------------------------------------------------------------------------

def _fmt(dt: datetime | None) -> str:
    return dt.astimezone(LOCAL).strftime("%a %d %b %H:%M") if dt else "unknown"


def _ago(delta: timedelta) -> str:
    h = delta.total_seconds() / 3600
    return f"{int(h * 60)} min" if h < 1 else f"{h:.0f} h" if h < 48 else f"{h / 24:.0f} days"


def summary(account_slug: str, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    est = load()
    servers = [s for s in est.get("servers", []) if s.get("account") == account_slug]
    if not est:
        return {"configured": False, "servers": [], "wrong": [], "todo": [], "happened": [], "verified": ""}
    if not servers:
        # #366: an account with no recorded servers sees an empty state, never another account's servers or docs.
        return {"configured": True, "empty": True, "servers": [], "wrong": [], "todo": [], "happened": [],
                "verified": "", "verified_by": "", "strategy_md": "", "updates_md": "", "updates_status": "",
                "live_ok": True}
    # The policy docs describe the estate owner's own setup (servers.yml defaults.account), so only that account
    # sees them; another account with servers here gets its server records but not the owner's documents (#366).
    owns_docs = account_slug == (est.get("defaults") or {}).get("account")
    # The reboot windows come from the owner's infra/updates.md: only the owner's account reads them (task #368);
    # another account's servers use their own servers.yml updates.reboot_window, else none.
    windows = reboot_windows() if owns_docs else {}
    lv = live([s["zabbix_host"] for s in servers if s.get("zabbix_host")])
    wrong, todo, happened, cards, unseen = [], [], [], [], []
    for s in servers:
        name = s["name"]
        host = lv["hosts"].get(s.get("zabbix_host") or "")
        items = (host or {}).get("items", {})
        hhmm, weekly, day = _backup_time(s)
        win = s["updates"].get("reboot_window") or windows.get(name)
        card = {"name": name, "role": s.get("role", ""), "projects": s.get("projects") or [],
                "stacks": s.get("stacks") or [], "backup": s["backup"], "updates": s["updates"],
                "backup_time": hhmm, "backup_weekly": weekly, "backup_day": day, "window": win,
                "zabbix_host": s.get("zabbix_host") or "", "live": host is not None}
        # backup age
        ts = _num(items.get(KEYS["backup"]))
        last_backup = datetime.fromtimestamp(ts, timezone.utc) if ts else None
        card["last_backup"] = _fmt(last_backup)
        if last_backup:
            age = now - last_backup
            limit = WEEKLY_MAX_AGE if weekly else DAILY_MAX_AGE
            card["backup_ok"] = age <= limit
            if age > limit:
                wrong.append({"server": name, "what": f"Backup is {_ago(age)} old (allowed {_ago(limit)})"})
            happened.append({"server": name, "at": last_backup, "when": _fmt(last_backup), "what": "Backup finished"})
        else:
            card["backup_ok"] = None
        # reboot + updates
        reboot = _num(items.get(KEYS["reboot"]))
        card["reboot_required"] = bool(reboot) if reboot is not None else s["updates"].get("reboot_required")
        if card["reboot_required"]:
            when = f"in its window, {win['day']} {win['time']}" if win else "(no window set)"
            todo.append({"server": name, "what": f"Reboot to finish installing updates {when}",
                         "how": "a session reboots it with your OK, after a fresh backup"})
        sec = _num(items.get(KEYS["sec_updates"]))
        card["security_updates"] = int(sec) if sec is not None else None
        card["updates_total"] = int(_num(items.get(KEYS["updates"])) or 0) if items.get(KEYS["updates"]) else None
        up = _num(items.get(KEYS["uptime"]))
        if up:
            booted = now - timedelta(seconds=up)
            card["last_reboot"] = _fmt(booted)
            happened.append({"server": name, "at": booted, "when": _fmt(booted), "what": "Rebooted"})
        else:
            card["last_reboot"] = s["updates"].get("last_reboot") or "unknown"
        # problems
        card["problems"] = [{"name": p["name"], "severity": int(p.get("severity") or 0),
                             "severity_label": health_mod.SEVERITY.get(int(p.get("severity") or 0), ""),
                             "since": _fmt(datetime.fromtimestamp(int(p["clock"]), timezone.utc))}
                            for p in (host or {}).get("problems", [])]
        for p in card["problems"]:
            if p["severity"] >= 2:
                wrong.append({"server": name, "what": p["name"], "severity": p["severity_label"]})
        if s.get("zabbix_host") and lv["ok"] and host is None:
            unseen.append(name)
        drift = str(s["backup"].get("conf_drift_vs_repo") or "none")
        if drift.lower() != "none":
            todo.append({"server": name, "what": f"Backup config drift: {drift}", "how": "the owner fixes it (#271)"})
        cards.append(card)
    # The owner's own estate wording (its Zabbix host group, its backup strategy doc) only on the owner's account
    # (task #368); another account gets the same to-do in neutral words.
    if unseen:
        todo.append({"server": ", ".join(unseen), "what": "No live status: the dashboard's read-only Zabbix login can't see "
                     + ("this server" if len(unseen) == 1 else "these servers"),
                     "how": ("add them to the watched host group in Zabbix (with your OK)" if owns_docs
                             else "give the dashboard's read-only Zabbix login access to them (with your OK)")})
    if servers and not any(s["backup"].get("drills") for s in servers):
        todo.append({"server": "all", "what": "No restore drill has ever been run",
                     "how": ("procedure written in infra/backups/strategy.md §7; runs with your OK (#299)" if owns_docs
                             else "a restore drill runs with your OK")})
    if not lv["ok"] and servers:
        wrong.append({"server": "all", "what": f"Live status unavailable: {lv['error']}"})
    happened.sort(key=lambda h: h["at"], reverse=True)
    return {"configured": True, "servers": cards, "wrong": wrong, "todo": todo, "happened": happened[:30],
            "verified": est.get("verified", ""), "verified_by": est.get("verified_by", ""),
            "strategy_md": doc_text("infra/backups/strategy.md") if owns_docs else "",
            "updates_md": doc_text("infra/updates.md") if owns_docs else "",
            "updates_status": ("proposed" if "PROPOSAL" in doc_text("infra/updates.md")[:300] else "approved")
                              if owns_docs else "",
            "live_ok": lv["ok"], "owns_docs": owns_docs}


def pct(hhmm: str) -> float:
    """Position of a time on a 24-hour track, 0..100."""
    try:
        h, m = (int(x) for x in hhmm.split(":"))
        return round((h * 60 + m) / 1440 * 100, 2)
    except (ValueError, AttributeError):
        return 0.0
