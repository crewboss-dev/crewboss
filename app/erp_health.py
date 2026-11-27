"""ERP health feed (task #196, change #15): System Health moves out of the ERP into the dashboard. The SENSORS stay in
the ERP (system_events, ops.record, sync_state); the dashboard reads one read-only JSON feed and shows + alerts on it.
Contract: docs/health-report-contract.md. The dashboard NEVER connects to the ERP's
database: everything comes over HTTP with a bearer token.

- PULL: a background loop next to the Zabbix one (config.ERP_HEALTH_POLL_SECONDS) calls sync(), which fetches
  GET <ERP_HEALTH_URL>/api/health, validates it (v1 only) and stores the raw snapshot in health_sources('erp').
- EVENTS: the snapshot's open `events` are tracked in health_problems (source 'erp') exactly like Zabbix problems: the
  first good fetch records what is already open as BASELINE (shown, never sent); after that a new error/critical event
  is pushed once to the health project's errors outbox (dedupe key erp-event:<id>) and its recovery follows on the
  same key when it leaves the open set.
- FRESHNESS: a failed fetch, or a snapshot older than ERP_HEALTH_STALE_MINUTES, shows as stale; no fresh snapshot for
  more than ERP_HEALTH_DOWN_MINUTES escalates ONCE (dedupe key erp-feed:down), and the recovery follows when it's back.
- RESOLVE: the Health page's button POSTs /api/health/events/{id}/resolve with a SEPARATE token (health:resolve).
  The ERP stays the only writer to its tables.

Tokens are read from their 0600 files at use time and never logged, stored or put in an error message."""
from __future__ import annotations

import json
import logging
import re
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Json

from app import config, repo, roles
from app.db import get_cursor

log = logging.getLogger("crewboss.erp_health")

SOURCE = "erp"
FEED_VERSION = 1
# system_events level -> the shared severity scale (health.SEVERITY / SEVERITY_CLASSES), so ERP events and Zabbix
# problems sort, colour and roll up the same way.
LEVELS = {"critical": 5, "error": 4, "warning": 2, "info": 1}
ESCALATE_MIN_LEVEL = 4  # error and critical go to the errors session; warning/info only show
DOWN_DEDUPE_KEY = "erp-feed:down"
MAX_BODY = 4 * 1024 * 1024
MAX_ITEMS = 10
KINDS = ("code_fault", "ops")
AI_CALLS_DEFAULT_LIMIT = 50
AI_CALLS_MAX_LIMIT = 200

# The failure lists the ERP feed returns (the ERP page that used to show them, /admin/ops, retired 2026-10-13, ERP #288), in its order. Keys the dashboard doesn't know still render (humanised).
FAILURE_LABELS = {
    "invoices_failed": "Failed invoices",
    "provision_errors": "Provisioning errors",
    "raw_events_errors": "Inbound events that failed to process",
    "billing_holding_unmatched": "Unmatched billing lines",
    "alert_devices_failed": "Failed alert devices",
    "escalations_failed": "Failed escalations",
    "snoozes_failed": "Failed snoozes",
}
# Each section's v1 JSON type. A section that is present but of another type (e.g. events as {count, items}) is
# INVALID: listed, shown as unreadable, never read as empty / all-clear, and the tracked open set is left alone.
SECTION_TYPES = {"events": list, "failures": dict, "heartbeats": list, "webhooks": list, "xero": dict, "ai_usage": dict,
                 "ai_models": dict}
SECTIONS = tuple(SECTION_TYPES)


class FeedError(Exception):
    """The feed couldn't be fetched or isn't a v1 snapshot. The message is safe to store and show (no token)."""


# ---------------------------------------------------------------------------------------------
# client
# ---------------------------------------------------------------------------------------------

def _read_token(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8") as fh:
            token = fh.read().strip()
    except OSError:
        return None
    return token or None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: urllib would replay the Authorization header to wherever it points."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        raise FeedError(f"HTTP {code}: the ERP answered with a redirect; ERP_HEALTH_URL must be the final address")


def _ssl_context() -> ssl.SSLContext:
    """Certificate + hostname verification on (the system CAs). The tests swap in one that trusts their own cert."""
    return ssl.create_default_context()


def _open(req: urllib.request.Request, timeout: float):
    return urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=_ssl_context())) \
        .open(req, timeout=timeout)


def config_problem() -> str | None:
    """Why the configured URL can't be used, or None. Only https: the bearer token never goes out in cleartext."""
    url = config.ERP_HEALTH_URL
    if not url:
        return None
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        return ("ERP_HEALTH_URL must be an https:// address; the dashboard won't send its token in cleartext, "
                "so nothing is fetched.")
    return None


class ErpHealthClient:
    """The three calls the contract defines. Tokens are re-read from their files on every call."""

    def __init__(self, base_url: str, read_token_file: str, resolve_token_file: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        if self.base_url.endswith("/api/health"):  # tolerate the feed's own URL being configured
            self.base_url = self.base_url[: -len("/api/health")]
        self.read_token_file, self.resolve_token_file, self.timeout = read_token_file, resolve_token_file, timeout

    def has_read_token(self) -> bool:
        return _read_token(self.read_token_file) is not None

    def has_resolve_token(self) -> bool:
        return _read_token(self.resolve_token_file) is not None

    def _call(self, method: str, path: str, token: str, body: dict | None = None) -> dict:
        url = self.base_url + path
        if urllib.parse.urlsplit(url).scheme != "https":
            raise FeedError("ERP_HEALTH_URL must be an https:// address")
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with _open(req, self.timeout) as resp:
                raw = resp.read(MAX_BODY + 1)
        except urllib.error.HTTPError as exc:
            raise FeedError(f"HTTP {exc.code}{_error_detail(exc)}") from None
        except urllib.error.URLError as exc:
            raise FeedError(f"can't reach the ERP: {exc.reason}") from None
        except (TimeoutError, OSError) as exc:
            raise FeedError(f"can't reach the ERP: {exc.__class__.__name__}") from None
        if len(raw) > MAX_BODY:
            raise FeedError("the ERP's answer is too large")
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            raise FeedError("the ERP's answer isn't JSON") from None

    def health(self) -> dict:
        token = _read_token(self.read_token_file)
        if not token:
            raise FeedError("read token not configured")
        return self._call("GET", "/api/health", token)

    def ai_calls(self, before: int | None = None, limit: int = AI_CALLS_DEFAULT_LIMIT) -> dict:
        token = _read_token(self.read_token_file)
        if not token:
            raise FeedError("read token not configured")
        q = {"limit": str(limit)}
        if before:
            q["before"] = str(before)
        return self._call("GET", "/api/health/ai-calls?" + urllib.parse.urlencode(q), token)

    def resolve(self, event_id: int, resolved_by: str) -> dict:
        token = _read_token(self.resolve_token_file)
        if not token:
            raise FeedError("resolve token not configured")
        return self._call("POST", f"/api/health/events/{int(event_id)}/resolve", token,
                          {"resolved_by": resolved_by})


def _error_detail(exc: urllib.error.HTTPError) -> str:
    """A short reason from an error body ({"detail": ...} or {"error": ...}), never headers."""
    try:
        body = json.loads(exc.read(4096) or b"{}")
    except Exception:  # noqa: BLE001
        return ""
    if isinstance(body, dict):
        msg = body.get("detail") or body.get("error") or body.get("message")
        if isinstance(msg, str) and msg.strip():
            return ": " + " ".join(msg.split())[:200]
    return ""


def _origin(url: str) -> tuple[str, str, int] | None:
    parts = urllib.parse.urlsplit(url or "")
    if not parts.scheme or not parts.hostname:
        return None
    try:
        port = parts.port or {"https": 443, "http": 80}.get(parts.scheme, 0)
    except ValueError:
        return None
    return parts.scheme, parts.hostname.lower(), port


def same_origin(url: str) -> bool:
    """Is `url` on exactly the ERP's own origin (scheme, host AND port of ERP_HEALTH_URL)? The read token goes only there."""
    mine = _origin(config.ERP_HEALTH_URL)
    return mine is not None and _origin(url) == mine


def fetch_json(url: str, timeout: float = 10.0) -> dict:
    """GET one JSON object from `url` with the ERP health READ token, over the same verified, https-only, never-redirected
    client as the feed (#828: the slim release endpoint the drift check reads). The bearer token is sent ONLY to the ERP's own
    origin (same_origin): any other address is refused before a request is made. Raises FeedError; the message carries no token."""
    if not same_origin(url):
        raise FeedError("not the ERP's own address: the token is never sent anywhere else")
    token = _read_token(config.ERP_HEALTH_READ_TOKEN_FILE)
    if not token:
        raise FeedError("read token not configured")
    client = ErpHealthClient(url, config.ERP_HEALTH_READ_TOKEN_FILE, config.ERP_HEALTH_RESOLVE_TOKEN_FILE, timeout)
    body = client._call("GET", "", token)
    if not isinstance(body, dict):
        raise FeedError("the answer isn't a JSON object")
    return body


def pushed() -> bool:
    """Does the ERP push its report (task #404, CREWBOSS_HOME/health_feeds.yaml mode: push)? Then nothing is fetched: the
    receiver ingests each report, and the dead-man (app/health_reports.py) replaces the feed-down check."""
    from app import health_reports

    return health_reports.mode(roles.slug("health")) == "push"


def client_from_config() -> ErpHealthClient | None:
    """None unless an https URL is set AND the read token file holds a token (the resolve token is optional), and the
    ERP isn't pushing instead."""
    if pushed() or not config.ERP_HEALTH_URL or config_problem():
        return None
    client = ErpHealthClient(config.ERP_HEALTH_URL, config.ERP_HEALTH_READ_TOKEN_FILE,
                             config.ERP_HEALTH_RESOLVE_TOKEN_FILE)
    return client if client.has_read_token() else None


def is_configured() -> bool:
    """Is the ERP's health reaching the dashboard: fetched (pull) or pushed? is_pulling() is only the former."""
    return pushed() or client_from_config() is not None


def is_pulling() -> bool:
    return client_from_config() is not None


# ---------------------------------------------------------------------------------------------
# parsing: raw v1 JSON -> what the page shows. The RAW body is stored; this runs on every read, so a parser fix
# applies to the stored snapshot without a refetch.
# ---------------------------------------------------------------------------------------------

def _dt(v) -> datetime | None:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        try:
            return datetime.fromtimestamp(v, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(v, str) and v.strip():
        s = v.strip()
        if s.endswith(("Z", "z")):
            s = s[:-1] + "+00:00"
        try:
            d = datetime.fromisoformat(s)
        except ValueError:
            return None
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return None


def _int(v) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        return int(v.strip())
    return None


def _float(v) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


def _text(v, limit: int = 300) -> str:
    if v is None:
        return ""
    if isinstance(v, (dict, list)):
        v = json.dumps(v, ensure_ascii=False, separators=(", ", ": "), default=str)
    return " ".join(str(v).split())[:limit]


def _first(d: dict, *keys):
    for k in keys:
        if d.get(k) not in (None, ""):
            return d[k]
    return None


def humanise(key: str) -> str:
    s = str(key).replace("_", " ").replace("-", " ").strip()
    return (s[:1].upper() + s[1:]) if s else "?"


def fmt(v) -> str:
    """Display a scalar: thousands separators on counts, sensible decimals on money/rates, yes/no on booleans."""
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, int):
        return f"{v:,}"
    if isinstance(v, float):
        if v.is_integer() and abs(v) >= 1:
            return f"{int(v):,}"
        return f"{v:,.2f}" if abs(v) >= 1 else (f"{v:.4f}".rstrip("0").rstrip(".") or "0")
    return _text(v, 120)


def age_label(minutes: float | None) -> str:
    if minutes is None:
        return "never"
    m = int(max(minutes, 0))
    if m < 60:
        return f"{m}m"
    if m < 60 * 48:
        return f"{m // 60}h {m % 60}m"
    return f"{m // 1440}d"


# SKIPPED WHILE THE DASHBOARD'S OWN ARE LIVE (#562, decided 2026-10-27): the ERP's key rotation reminders (its key
# clock, #563: ops.record("keyclock", ..., dedup_key="key_rotation:<NAME>")). The Dashboard raises its own from the key
# store's status (app/platform_keys.py: due within 30 days, 7, overdue, each pushed once), so while that feed is FRESH
# (platform_keys.fresh: a good read of the ERP's store within an hour) the ERP's copy is dropped here, where every event
# is read (pull, pushed reports and the page): it never reaches health_problems or the push, and one already stored
# resolves on the next snapshot. When the Dashboard's reads are stale the ERP's come through as before: the ERP is the
# backup. A refused key (key_refused:<NAME>, app/keyhealth.py) always comes through.
SKIPPED_DEDUP_PREFIXES = ("key_rotation:",)
SKIPPED_SOURCES = ("keyclock",)     # the key clock records nothing else; covers a feed without dedup_key


def skipped(e: dict) -> bool:
    """An ERP event the Dashboard deliberately doesn't import (above)."""
    key = e.get("dedup_key")
    return (isinstance(key, str) and key.startswith(SKIPPED_DEDUP_PREFIXES)) or e.get("source") in SKIPPED_SOURCES


def _keys_reminders_live() -> bool:
    """Are the Dashboard's own key reminders live for the ERP's store (the health role)? Unknown = no."""
    from app import platform_keys
    try:
        return platform_keys.fresh(roles.slug("health"))
    except Exception:  # noqa: BLE001 — can't tell: let the ERP's through (the backup)
        log.warning("couldn't tell whether the platform key reminders are live", exc_info=True)
        return False


def _kind(v) -> str:
    """The event's kind (#828): "code_fault" or "ops". A missing or unknown value is ops, never a code fault."""
    return v.strip().lower() if isinstance(v, str) and v.strip().lower() in KINDS else "ops"


_LOGIC_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _event(e, skip_key_rotation: bool = False) -> dict | None:
    if not isinstance(e, dict) or (skip_key_rotation and skipped(e)):
        return None
    eid = _int(e.get("id"))
    if eid is None:
        return None
    sev = str(_first(e, "severity", "level") or "").strip().lower()
    return {
        "id": eid,
        "severity": sev or "unknown",
        "level": LEVELS.get(sev, 0),
        "source": _text(e.get("source"), 100),
        "title": _text(_first(e, "title", "message"), 300) or "(no title)",
        "detail": _text(e.get("detail"), 600),
        "first_seen": _dt(e.get("first_seen")),
        "last_seen": _dt(e.get("last_seen")),
        "count": _int(e.get("count")) or 1,
        "kind": _kind(e.get("kind")),
        "workflow": _text(e.get("workflow"), 80) if _LOGIC_ID.match(_text(e.get("workflow"), 80) or "-") else "",   # #715: which mapped workflow step failed
        "step": _text(e.get("step"), 60) if _LOGIC_ID.match(_text(e.get("step"), 60) or "-") else "",
        "tagged": isinstance(e.get("kind"), str) and e["kind"].strip().lower() in KINDS,   # the ERP said which kind it is
    }


def _failure_item(it) -> dict:
    if not isinstance(it, dict):
        return {"label": _text(it, 200), "detail": "", "at": None}
    label = _first(it, "label", "title", "name", "number", "reference", "message", "id")
    detail = _first(it, "detail", "error", "reason", "message")
    if detail is label:
        detail = None
    return {"id": _int(it.get("id")), "label": _text(label, 200) or "(unnamed)", "detail": _text(detail, 300),
            "at": _dt(_first(it, "at", "created_at", "updated_at", "last_seen"))}


def _failures(raw) -> list[dict]:
    if not isinstance(raw, dict):
        return []
    order = list(FAILURE_LABELS) + sorted(k for k in raw if k not in FAILURE_LABELS)
    out = []
    for key in order:
        if key not in raw:
            continue
        val = raw[key]
        if isinstance(val, dict):
            items = val.get("items") if isinstance(val.get("items"), list) else []
            count = _int(val.get("count"))
            count = len(items) if count is None else count
        else:
            items, count = [], _int(val) or 0
        out.append({"key": key, "label": FAILURE_LABELS.get(key) or humanise(key), "count": count,
                    "entries": [_failure_item(i) for i in items[:MAX_ITEMS]]})
    return out


def _heartbeats(raw, generated_at: datetime | None) -> list[dict]:
    rows = []
    if not isinstance(raw, list):
        return []
    for r in raw:
        if not isinstance(r, dict):
            continue
        last = _dt(_first(r, "last_run", "last_run_at", "updated_at"))
        age = _float(r.get("age_minutes"))
        if age is None and last and generated_at:
            age = (generated_at - last).total_seconds() / 60
        rows.append({"name": _text(_first(r, "name", "entity", "worker"), 120) or "(unnamed)", "last_run": last,
                     "age_minutes": age, "age": age_label(age),
                     "red": age is not None and age > config.ERP_HEARTBEAT_RED_MINUTES})
    rows.sort(key=lambda r: (not r["red"], r["age_minutes"] is not None, r["name"]))
    return rows


def _webhooks(raw) -> list[dict]:
    if not isinstance(raw, list):
        return []
    rows = []
    for r in raw:
        if not isinstance(r, dict):
            continue
        rows.append({"source": _text(r.get("source"), 100) or "(unknown)",
                     "count": _int(_first(r, "count", "count_24h", "total")) or 0,
                     "errors": _int(_first(r, "errors", "error_count", "errors_24h")) or 0,
                     "last_at": _dt(_first(r, "last_at", "last", "last_seen"))})
    rows.sort(key=lambda r: (-r["errors"], -r["count"], r["source"]))
    return rows


_XERO_KNOWN = ("breaker", "breaker_state", "state", "pushes_today", "push_volume_today", "pushes")


def _xero(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    breaker = _first(raw, "breaker", "breaker_state", "state")
    extra = {}
    if isinstance(breaker, dict):
        extra.update({k: v for k, v in breaker.items() if k != "state" and not isinstance(v, (dict, list))})
        breaker = breaker.get("state")
    state = _text(breaker, 40).lower() or "unknown"
    tone = {"closed": "ok", "ok": "ok", "open": "bad", "tripped": "bad",
            "half-open": "warn", "half_open": "warn"}.get(state, "neutral")
    extra.update({k: v for k, v in raw.items() if k not in _XERO_KNOWN and not isinstance(v, (dict, list))})
    return {"breaker": state, "tone": tone,
            "pushes_today": _int(_first(raw, "pushes_today", "push_volume_today", "pushes")),
            "extra": [(humanise(k), fmt(v) if not isinstance(v, str) else _text(v, 120))
                      for k, v in extra.items()]}


def _ai_usage(raw) -> dict | None:
    """ai_usage.summary(cur) has no pinned shape in the contract, so it renders generically: top-level scalars,
    top-level dicts of scalars as one table (rows = the key, e.g. today / month), and lists of dicts as tables."""
    if not isinstance(raw, dict):
        return None
    scalars, periods, columns, tables = [], [], [], []
    for k, v in raw.items():
        if isinstance(v, dict) and all(not isinstance(x, (dict, list)) for x in v.values()):
            periods.append((humanise(k), v))
            columns += [c for c in v if c not in columns]
        elif isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            cols = []
            for row in v[:20]:
                cols += [c for c in row if c not in cols and not isinstance(row[c], (dict, list))]
            tables.append({"label": humanise(k), "columns": [humanise(c) for c in cols],
                           "rows": [[fmt(row.get(c)) for c in cols] for row in v[:20]]})
        elif not isinstance(v, (dict, list)):
            scalars.append((humanise(k), fmt(v)))
    return {"scalars": scalars, "columns": [humanise(c) for c in columns],
            "periods": [(label, [fmt(d.get(c)) for c in columns]) for label, d in periods], "tables": tables}


# ai_models: which model each AI workflow is set to and actually used. Read-only here: the switch lives on the ERP's
# admin page, so each row only links there. Links are kept only when they point at the ERP's /admin over https.
AI_MODELS_ADMIN_PREFIXES = tuple(p for p in ((config.ERP_URL + "admin/") if config.ERP_URL.startswith("https://") else "",) if p)
AI_MODELS_RECENT_DAYS = 7  # an off-model call older than this reads as history, not live drift
AI_MODELS_MAX_ROWS = 200


def _admin_link(v) -> str | None:
    s = _text(v, 300)
    return s if s.startswith(AI_MODELS_ADMIN_PREFIXES) and not any(c in s for c in "\"'<> ") else None


def _ai_model_use(u, today) -> dict | None:
    if not isinstance(u, dict):
        return None
    last = _dt(u.get("last_used"))
    return {"model": _text(u.get("model"), 80) or "?", "calls": _int(u.get("calls")) or 0,
            "input_tokens": _int(u.get("input_tokens")), "output_tokens": _int(u.get("output_tokens")),
            "aud": _float(u.get("aud")), "usd": _float(u.get("usd")),
            "last_used": last.date().isoformat() if last else "",
            "recent": bool(last and (today - last.date()).days <= AI_MODELS_RECENT_DAYS)}


def _ai_models(raw, generated_at=None) -> dict | None:
    """The ai_models section: workflows grouped in feed (registry) order. Drift is split into LIVE (an off-model was
    used in the last AI_MODELS_RECENT_DAYS days) and HISTORY (only older calls), so pre-alignment calls don't read
    as a current problem while they age out of the 30-day window."""
    if not isinstance(raw, dict):
        return None
    today = (generated_at or datetime.now(timezone.utc)).date()
    groups: dict[str, list] = {}
    totals = {"workflows": 0, "client_facing": 0, "drift_live": 0, "drift_history": 0, "aud": 0.0, "usd": 0.0,
              "calls": 0}
    wfs = raw.get("workflows") if isinstance(raw.get("workflows"), list) else []
    for w in wfs[:AI_MODELS_MAX_ROWS]:
        if not isinstance(w, dict):
            continue
        cfg = w.get("configured") if isinstance(w.get("configured"), dict) else {}
        configured = _text(cfg.get("model"), 80)
        used = [x for x in (_ai_model_use(u, today) for u in (w.get("used_30d") or [])
                            if isinstance(w.get("used_30d"), list)) if x]
        used.sort(key=lambda x: (-x["calls"], x["model"]))
        drift = w.get("drift") if isinstance(w.get("drift"), dict) else {}
        state = "ok"
        if drift.get("flag") is True:
            off = [u for u in used if u["model"] != configured]
            state = "live" if (not off or any(u["recent"] for u in off)) else "history"
        aud = sum(u["aud"] for u in used if u["aud"] is not None)
        usd = sum(u["usd"] for u in used if u["usd"] is not None)
        calls = sum(u["calls"] for u in used)
        row = {"key": _text(w.get("key"), 80), "name": _text(w.get("name"), 120) or humanise(w.get("key") or "?"),
               "client_facing": w.get("client_facing") is True, "host": _text(w.get("host"), 20),
               "switchable": w.get("switchable") is True,
               "tier": _text(cfg.get("tier"), 20), "model": configured or "-",
               "source": _text(cfg.get("source"), 20), "pin": _text(cfg.get("pin"), 60),
               "used": used, "calls": calls, "aud": aud if any(u["aud"] is not None for u in used) else None,
               "usd": usd if any(u["usd"] is not None for u in used) else None,
               "drift": state, "drift_reason": _text(drift.get("reason"), 200),
               "admin_url": _admin_link(w.get("admin_url"))}
        groups.setdefault(_text(w.get("group"), 60) or "Other", []).append(row)
        totals["workflows"] += 1
        totals["client_facing"] += row["client_facing"]
        totals["calls"] += calls
        totals["aud"] += aud
        totals["usd"] += usd
        if state != "ok":
            totals["drift_" + state] += 1
    unattributed = []
    for u in (raw.get("unattributed_30d") or [])[:20] if isinstance(raw.get("unattributed_30d"), list) else []:
        if isinstance(u, dict):
            unattributed.append({"job": _text(u.get("job"), 80) or "?", "calls": _int(u.get("calls")) or 0,
                                 "aud": _float(u.get("aud"))})
    fx = raw.get("fx") if isinstance(raw.get("fx"), dict) else None
    # No AUD rate in the feed (fx null) means every aud is null: show US$ rather than a misleading A$0.00.
    currency = "A$" if any(w["aud"] is not None for _, ws in groups.items() for w in ws) else "US$"
    return {"currency": currency, "window_days": _int(raw.get("window_days")) or 30, "since": _text(raw.get("since"), 10),
            "admin_url": _admin_link(raw.get("admin_url")),
            "fx": {"rate": _float(fx.get("aud_per_usd")), "as_of": _text(fx.get("as_of"), 10)} if fx else None,
            "groups": list(groups.items()), "totals": totals, "unattributed": unattributed,
            "recent_days": AI_MODELS_RECENT_DAYS}


def parse_snapshot(body) -> dict:
    """Validate + normalise a v1 feed. Raises FeedError for anything that isn't a v1 object. Every section is
    optional: a missing one is listed in `missing` and shown as 'not in this feed', never as all-clear."""
    if not isinstance(body, dict):
        raise FeedError("the feed isn't a JSON object")
    v = body.get("v")
    if isinstance(v, bool) or v != FEED_VERSION:
        raise FeedError(f"unsupported feed version {v!r} (this dashboard reads v{FEED_VERSION})")
    generated_at = _dt(body.get("generated_at"))
    missing = [k for k in SECTIONS if body.get(k) is None]
    invalid = [k for k, t in SECTION_TYPES.items() if body.get(k) is not None and not isinstance(body[k], t)]
    sec = {k: (None if k in invalid else body.get(k)) for k in SECTIONS}
    raw_events = sec["events"] or []
    skip = any(isinstance(x, dict) and skipped(x) for x in raw_events) and _keys_reminders_live()
    events = [e for e in (_event(x, skip) for x in raw_events) if e]
    hidden = []
    if config.ERP_HEALTH_CODE_FAULTS_ONLY:   # #828: ops events are neither tracked, escalated nor shown
        hidden = [e["id"] for e in events if e["kind"] != "code_fault"]
        events = [e for e in events if e["kind"] == "code_fault"]
    events.sort(key=lambda e: (-e["level"], -(e["last_seen"] or datetime.min.replace(tzinfo=timezone.utc))
                               .timestamp()))
    return {
        "v": FEED_VERSION,
        "generated_at": generated_at,
        "events_present": isinstance(sec["events"], list),
        # The ERP capped the list: new ids still count, but an id missing from it is NOT a recovery.
        "events_truncated": body.get("events_truncated") is True,
        "events": events,
        "events_hidden": hidden,   # ids left out by the code-fault filter: closing one is quiet
        "failures": _failures(sec["failures"]),
        "heartbeats": _heartbeats(sec["heartbeats"], generated_at),
        "webhooks": _webhooks(sec["webhooks"]),
        "xero": _xero(sec["xero"]),
        "ai_usage": _ai_usage(sec["ai_usage"]),
        "ai_models": _ai_models(sec["ai_models"], generated_at),
        "missing": missing,
        "invalid": invalid,
        "unreadable": missing + invalid,
    }


def parse_ai_calls(body, limit: int) -> dict:
    if not isinstance(body, dict):
        raise FeedError("the AI call log isn't a JSON object")
    v = body.get("v", FEED_VERSION)
    if isinstance(v, bool) or v != FEED_VERSION:
        raise FeedError(f"unsupported AI call log version {v!r} (this dashboard reads v{FEED_VERSION})")
    raw = body.get("calls") if isinstance(body.get("calls"), list) else []
    calls = []
    for c in raw:
        if not isinstance(c, dict):
            continue
        ok = c.get("ok")
        calls.append({
            "id": _int(c.get("id")),
            "at": _dt(_first(c, "at", "created_at", "ts")),
            "feature": _text(_first(c, "feature", "purpose", "caller", "source"), 120) or "(unlabelled)",
            "model": _text(c.get("model"), 80),
            "input_tokens": _int(c.get("input_tokens")),
            "output_tokens": _int(c.get("output_tokens")),
            "cost": _float(_first(c, "cost_usd", "cost")),
            "ok": ok is not False and not c.get("error"),
            "error": _text(c.get("error"), 200),
        })
    next_before = _int(body.get("next_before"))
    if next_before is None and "next_before" not in body and len(calls) >= limit:
        ids = [c["id"] for c in calls if c["id"] is not None]
        next_before = min(ids) if ids else None
    return {"calls": calls, "next_before": next_before}


# ---------------------------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------------------------

_lock = threading.Lock()


def sync(client: ErpHealthClient | None = None, now: datetime | None = None) -> dict:
    """One fetch. A concurrent caller is simply skipped (the loop comes round again). Never raises."""
    client = client or client_from_config()
    if client is None:
        return {"ok": False, "error": "not configured"}
    if not _lock.acquire(blocking=False):
        return {"ok": True, "skipped": True}
    try:
        return _sync_once(client, now or datetime.now(timezone.utc))
    finally:
        _lock.release()


def _ensure_source(cur) -> dict:
    cur.execute("INSERT INTO health_sources (source) VALUES (%s) ON CONFLICT DO NOTHING", (SOURCE,))
    cur.execute("SELECT * FROM health_sources WHERE source = %s", (SOURCE,))
    return cur.fetchone()


def _sync_once(client: ErpHealthClient, now: datetime) -> dict:
    try:
        body = client.health()
        parse_snapshot(body)
    except Exception as exc:  # noqa: BLE001 — a failed fetch is state to show, never a crash
        msg = (str(exc) if isinstance(exc, FeedError) else exc.__class__.__name__)[:300] or "fetch failed"
        log.warning("erp health fetch failed: %s", msg)
        with get_cursor() as cur:
            _ensure_source(cur)
            cur.execute("UPDATE health_sources SET last_sync_at = %s, last_error = %s, "
                        "failing_since = COALESCE(failing_since, %s) WHERE source = %s", (now, msg, now, SOURCE))
        down = _check_down(now)
        return {"ok": False, "error": msg, "down_escalated": down}
    result = ingest(body, now)
    result["down_escalated"] = _check_down(now)
    return result


def ingest(body: dict, now: datetime) -> dict:
    """Store one good v1 snapshot and track its events — the same whether it was fetched (pull) or pushed to
    /api/health/report (task #404, app/health_reports.py). Raises FeedError on a body that isn't v1."""
    snap = parse_snapshot(body)
    inserted, upgraded, resolved = [], [], []
    with get_cursor() as cur:
        src = _ensure_source(cur)
        first = src["bookmark"] is None
        cur.execute("UPDATE health_sources SET snapshot = %s, generated_at = %s, last_sync_at = %s, last_ok_at = %s, "
                    "last_error = NULL, failing_since = NULL WHERE source = %s",
                    (Json(body), snap["generated_at"] or now, now, now, SOURCE))
        if snap["events_present"]:  # no events section = no knowledge of the open set: leave it as it was
            inserted, upgraded, resolved = _track_events(cur, snap["events"], first, now,
                                                         truncated=snap["events_truncated"])
            if snap["events_hidden"]:   # filtered out and never sent: it just leaves quietly. One that WAS sent (the flag
                hidden = set(snap["events_hidden"])   # went on, or the ERP re-tagged it ops) still gets its RESOLVED push.
                resolved = [r for r in resolved if not (r["eventid"] in hidden and r["escalated_at"] is None)]
            bookmark = max([int(src["bookmark"] or 0)] + [e["id"] for e in snap["events"]])
            cur.execute("UPDATE health_sources SET bookmark = %s WHERE source = %s", (bookmark, SOURCE))
        if upgraded or any(not r["baseline"] and r["severity"] >= ESCALATE_MIN_LEVEL for r in inserted):
            repo.mark_plan_stale(cur, None, None, "new ERP error event", actor="system")
    escalated = _escalate(inserted, resolved, upgraded)
    return {"ok": True, "first": first and snap["events_present"], "new": len(inserted), "resolved": len(resolved),
            "upgraded": len(upgraded), "escalated": escalated, "open": len(snap["events"])}


def _track_events(cur, events: list[dict], first: bool, now: datetime,
                  truncated: bool = False) -> tuple[list[dict], list[dict], list[dict]]:
    """Record the open set. Returns (inserted, upgraded, resolved). `upgraded` = an open row whose severity just rose
    to error/critical and was never sent (ops.record rewrites the level on a dedup hit, so severity follows every
    snapshot). With `truncated` (the ERP capped the list) nothing is read as recovered."""
    cur.execute("SELECT eventid, severity, resolved_at, escalated_at, kind FROM health_problems WHERE source = %s",
                (SOURCE,))
    stored = {r["eventid"]: r for r in cur.fetchall()}
    open_ids = {e["id"] for e in events}
    inserted, upgraded, resolved = [], [], []
    for e in events:
        row = stored.get(e["id"])
        if row is None:
            cur.execute(
                "INSERT INTO health_problems (source, eventid, host, name, severity, started_at, baseline, kind) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (source, eventid) DO NOTHING RETURNING *",
                (SOURCE, e["id"], e["source"][:200], e["title"][:500], e["level"], e["first_seen"] or now, first,
                 e["kind"]))
            new = cur.fetchone()
            if new:
                inserted.append(new)
            continue
        if row["severity"] == e["level"] and row["resolved_at"] is None:
            if row.get("kind") != e["kind"]:
                cur.execute("UPDATE health_problems SET kind = %s WHERE source = %s AND eventid = %s",
                            (e["kind"], SOURCE, e["id"]))
            continue
        # Severity moved and/or the event is open again (a transient gap in the feed): follow the ERP. Re-opening is
        # quiet; only a rise across error, never sent before, escalates (once: escalated_at is then set).
        cur.execute("UPDATE health_problems SET severity = %s, kind = %s, resolved_at = NULL WHERE source = %s "
                    "AND eventid = %s RETURNING *", (e["level"], e["kind"], SOURCE, e["id"]))
        changed = cur.fetchone()
        if (changed and row["severity"] < ESCALATE_MIN_LEVEL <= e["level"] and row["escalated_at"] is None):
            upgraded.append(changed)
    if not truncated:
        for eid, row in stored.items():
            if row["resolved_at"] is None and eid not in open_ids:
                cur.execute("UPDATE health_problems SET resolved_at = %s WHERE source = %s AND eventid = %s "
                            "AND resolved_at IS NULL RETURNING *", (now, SOURCE, eid))
                gone = cur.fetchone()
                if gone:
                    resolved.append(gone)
    return inserted, upgraded, resolved


def _health_project() -> dict | None:
    try:
        return repo.get_project_by_slug(roles.slug("health"))
    except repo.NotFound:
        return None


def _level_word(level: int) -> str:
    return next((w for w, n in LEVELS.items() if n == level), "unknown")


def _escalate(inserted: list[dict], resolved: list[dict], upgraded: list[dict] | None = None) -> int:
    project = _health_project()
    if project is None:
        return 0
    pushed = 0
    for row in upgraded or []:  # rose to error/critical while open: new to the errors session, even if baseline
        repo.push_outbox("errors", "health_error",
                         (f"ERP event {row['eventid']} from {row['host'] or 'unknown source'} rose to "
                          f"{_level_word(row['severity'])}: {row['name']} - see /{project['slug']}/health")[:400],
                         project_id=project["id"], dedupe_key=f"erp-event:{row['eventid']}")
        with get_cursor() as cur:
            cur.execute("UPDATE health_problems SET escalated_at = now() WHERE source = %s AND eventid = %s",
                        (SOURCE, row["eventid"]))
        pushed += 1
    for row in inserted:
        if row["baseline"] or row["resolved_at"] is not None or row["severity"] < ESCALATE_MIN_LEVEL:
            continue
        repo.push_outbox("errors", "health_error",
                         (f"ERP {_level_word(row['severity'])} from {row['host'] or 'unknown source'}: {row['name']} "
                          f"(ERP event {row['eventid']}) - see /{project['slug']}/health")[:400],
                         project_id=project["id"], dedupe_key=f"erp-event:{row['eventid']}")
        with get_cursor() as cur:
            cur.execute("UPDATE health_problems SET escalated_at = now() WHERE source = %s AND eventid = %s",
                        (SOURCE, row["eventid"]))
        pushed += 1
    for row in resolved:
        if row["escalated_at"] is None:
            continue
        repo.push_outbox("errors", "health_error",
                         (f"ERP event {row['eventid']} from {row['host'] or 'unknown source'} RESOLVED: "
                          f"{row['name']}")[:400],
                         project_id=project["id"], dedupe_key=f"erp-event:{row['eventid']}")
        pushed += 1
    return pushed


def _fresh_ref(src: dict) -> datetime | None:
    """When the newest snapshot we hold was made (its generated_at), else when we started failing."""
    return src.get("generated_at") or src.get("last_ok_at") or src.get("failing_since")


def _check_down(now: datetime) -> bool:
    """Feed down (no fresh snapshot for > DOWN minutes) -> one push; back to fresh -> the recovery, same key.
    Returns True when something was pushed."""
    with get_cursor() as cur:
        src = _ensure_source(cur)
    ref = _fresh_ref(src)
    age = now - ref if ref else None
    is_down = age is not None and age > timedelta(minutes=config.ERP_HEALTH_DOWN_MINUTES)
    is_fresh = (not src.get("last_error") and age is not None
                and age <= timedelta(minutes=config.ERP_HEALTH_STALE_MINUTES))
    project = _health_project()
    if is_down and src.get("down_escalated_at") is None:
        if project is None:
            return False
        mins = int(age.total_seconds() // 60)
        why = f": {src['last_error']}" if src.get("last_error") else " (the snapshot it serves is old)"
        repo.push_outbox("errors", "health_error",
                         (f"ERP health feed DOWN for {mins} min{why} - see /{project['slug']}/health")[:400],
                         project_id=project["id"], dedupe_key=DOWN_DEDUPE_KEY)
        with get_cursor() as cur:
            cur.execute("UPDATE health_sources SET down_escalated_at = %s WHERE source = %s", (now, SOURCE))
        return True
    if is_fresh and src.get("down_escalated_at") is not None:
        with get_cursor() as cur:
            cur.execute("UPDATE health_sources SET down_escalated_at = NULL WHERE source = %s", (SOURCE,))
        if project is None:
            return False
        repo.push_outbox("errors", "health_error", "ERP health feed is back up.", project_id=project["id"],
                         dedupe_key=DOWN_DEDUPE_KEY)
        return True
    return False


# ---------------------------------------------------------------------------------------------
# resolve (the Health page's button)
# ---------------------------------------------------------------------------------------------

RESOLVED_BY = "owner (Crewboss)"


NOT_OPEN = "Not an open ERP event: it isn't in the current snapshot, or the snapshot is stale. Refresh and retry."


def resolve_event(event_id: int, client: ErpHealthClient | None = None, now: datetime | None = None) -> dict:
    """POST the ERP's resolve with the health:resolve token. {"ok": True} or {"ok": False, "error": <shown>,
    "status"?: 404}. Only an id that is OPEN in the current FRESH snapshot is ever sent: anything else is refused here
    (status 404) without a request to the ERP. On success the event leaves the stored snapshot at once; the next fetch
    confirms it (and sends the recovery if the event had been escalated)."""
    if not config.ERP_HEALTH_URL or config_problem():
        return {"ok": False, "error": "ERP feed not connected."}
    s = summary(now)
    if s["state"] != "ok" or event_id not in {e["id"] for e in s["events"]}:
        return {"ok": False, "status": 404, "error": NOT_OPEN}
    client = client or ErpHealthClient(config.ERP_HEALTH_URL, config.ERP_HEALTH_READ_TOKEN_FILE,
                                       config.ERP_HEALTH_RESOLVE_TOKEN_FILE)
    if not client.has_resolve_token():
        return {"ok": False, "error": "Can't resolve from here: the ERP resolve token isn't configured "
                                      "(erp-health-resolve-token)."}
    try:
        client.resolve(event_id, RESOLVED_BY)
    except FeedError as exc:
        msg = str(exc)
        if msg.startswith("HTTP 404"):
            return {"ok": False, "error": "The ERP doesn't have that open event (already resolved?)."}
        return {"ok": False, "error": f"The ERP refused: {msg}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"Resolve failed: {exc.__class__.__name__}"}
    _prune_event(event_id)
    return {"ok": True}


def _prune_event(event_id: int) -> None:
    with get_cursor() as cur:
        cur.execute("SELECT snapshot FROM health_sources WHERE source = %s FOR UPDATE", (SOURCE,))
        row = cur.fetchone()
        snap = row and row["snapshot"]
        if isinstance(snap, dict) and isinstance(snap.get("events"), list):
            snap["events"] = [e for e in snap["events"] if not (isinstance(e, dict) and _int(e.get("id")) == event_id)]
            cur.execute("UPDATE health_sources SET snapshot = %s WHERE source = %s", (Json(snap), SOURCE))


# ---------------------------------------------------------------------------------------------
# reads for the pages
# ---------------------------------------------------------------------------------------------

STATE_WORDS = {"off": "ERP feed not connected", "waiting": "Waiting for the first fetch", "ok": "Connected",
               "stale": "Stale", "down": "Down"}


def summary(now: datetime | None = None) -> dict:
    """Everything the ERP section needs. `state`: off | waiting | ok | stale | down."""
    now = now or datetime.now(timezone.utc)
    configured = is_configured()
    problem = None if pushed() else config_problem()
    with get_cursor() as cur:
        cur.execute("SELECT * FROM health_sources WHERE source = %s", (SOURCE,))
        src = cur.fetchone() or {}
        cur.execute("SELECT eventid, baseline, escalated_at FROM health_problems WHERE source = %s", (SOURCE,))
        tracked = {r["eventid"]: r for r in cur.fetchall()}
    snap, parse_error = None, None
    if src.get("snapshot") is not None:
        try:
            snap = parse_snapshot(src["snapshot"])
        except FeedError as exc:
            parse_error = str(exc)
    ref = _fresh_ref(src)
    age = (now - ref) if ref else None
    if not configured:
        state = "off"
    elif not src.get("last_sync_at"):
        state = "waiting"
    elif age is not None and age > timedelta(minutes=config.ERP_HEALTH_DOWN_MINUTES):
        state = "down"
    elif src.get("last_error") or age is None or age > timedelta(minutes=config.ERP_HEALTH_STALE_MINUTES):
        state = "stale"
    elif snap is None or not snap["events_present"]:
        state = "stale"  # fresh, but no readable events list: never shown (or rolled up) as all clear
    else:
        state = "ok"
    events = []
    for e in (snap or {}).get("events", []):
        t = tracked.get(e["id"]) or {}
        events.append({**e, "baseline": bool(t.get("baseline")), "escalated_at": t.get("escalated_at")})
    alerting = [e for e in events if e["level"] >= ESCALATE_MIN_LEVEL]
    return {
        "state": state,
        "word": STATE_WORDS[state],
        "pushed": pushed(),
        "configured": configured,
        "config_problem": problem,
        "events_readable": bool(snap and snap["events_present"]),
        "resolve_ready": bool(_read_token(config.ERP_HEALTH_RESOLVE_TOKEN_FILE)),
        "source": src,
        "snapshot": snap,
        "parse_error": parse_error,
        "events": events,
        # The roll-ups (home slot, "Failing now") count error/critical only, as plan.py and the ERP's own badge do:
        # info/warning rows (e.g. a permanent "pbx_push:ok") still list on the Health page but never turn it red.
        "alerting": alerting,
        "worst": max((e["level"] for e in alerting), default=None),
        "age_minutes": int(age.total_seconds() // 60) if age is not None else None,
        "stale_minutes": config.ERP_HEALTH_STALE_MINUTES,
        "down_minutes": config.ERP_HEALTH_DOWN_MINUTES,
        "heartbeat_red": config.ERP_HEARTBEAT_RED_MINUTES,
        "poll_seconds": config.ERP_HEALTH_POLL_SECONDS,
    }


def slot(s: dict | None = None) -> dict:
    s = s or summary()
    n = len(s["alerting"])
    what = f"{n} open error{'s' if n != 1 else ''}"
    if s["state"] == "off":
        label = "https required" if s.get("config_problem") else "not connected"
    elif s["state"] == "waiting":
        label = "first fetch pending"
    elif s["state"] == "stale" and not s.get("source", {}).get("last_error") and not s.get("events_readable"):
        label = "events unreadable"
    elif s["state"] in ("stale", "down"):
        label = f"feed {s['state']}" + (f" · {what}" if n else "")
    else:
        label = what if n else "all clear"
    return {"state": s["state"], "open": n, "worst": s["worst"], "label": label}


# The home slot's colour logic speaks the Zabbix states (off, waiting, ok, stale, error): ERP 'down' is an error.
_RANK = {"off": 0, "waiting": 1, "ok": 2, "stale": 3, "error": 4}


def combined_slot(zabbix: dict, erp: dict) -> dict:
    """The worst of Zabbix and the ERP feed, in health.slot()'s shape. A source that isn't connected doesn't count
    (an unconnected ERP leaves the Zabbix slot exactly as it was)."""
    if erp["state"] == "off":
        return zabbix
    erp_state = "error" if erp["state"] == "down" else erp["state"]
    states = [erp_state] + ([zabbix["state"]] if zabbix["state"] != "off" else [])
    worsts = [w for w in (zabbix.get("worst"), erp.get("worst")) if w is not None]
    label = f"ERP {erp['label']}" if zabbix["state"] == "off" else f"Zabbix {zabbix['label']} · ERP {erp['label']}"
    return {"state": max(states, key=_RANK.__getitem__), "open": zabbix["open"] + erp["open"],
            "worst": max(worsts) if worsts else None, "label": label, "project": zabbix["project"]}


def ai_calls_page(before: int | None, limit: int, client: ErpHealthClient | None = None) -> dict:
    """A live page of the ERP's AI call log (not stored). {configured, calls, next_before, error, limit, before}."""
    limit = max(1, min(int(limit or AI_CALLS_DEFAULT_LIMIT), AI_CALLS_MAX_LIMIT))
    before = before if before and before > 0 else None
    out = {"configured": True, "calls": [], "next_before": None, "error": None, "limit": limit, "before": before}
    client = client or client_from_config()
    if client is None:
        out["configured"] = False
        return out
    try:
        out.update(parse_ai_calls(client.ai_calls(before, limit), limit))
    except FeedError as exc:
        out["error"] = str(exc)
    except Exception as exc:  # noqa: BLE001
        out["error"] = exc.__class__.__name__
    return out


def home_slot() -> dict:
    """The global home / overview Health slot: the worst of Zabbix and the ERP feed, plus open drift (#402),
    stopped-reporting (#404) and platform key (#562: overdue, or the key store can't be read) errors."""
    from app import health

    out = combined_slot(health.slot(), slot())
    with get_cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM health_problems WHERE source IN ('drift', 'reports', 'keys') "
                    "AND resolved_at IS NULL AND severity >= 4")
        errors = cur.fetchone()["n"]
    if errors:
        out = dict(out, open=out["open"] + errors, worst=max(out.get("worst") or 0, 4),
                   label=f"{out['label']} · {errors} drift/reporting/key error{'s' if errors != 1 else ''}")
    return out
