"""The front door (#976): how people reach this Crewboss, as the app sees it.

READ SIDE (the contract other slices code against). The sidecar (`app/frontdoor_agent.py`, slice C) is the only writer of
`<home>/frontdoor/front_door.json`; the app reads it through the instance folder mount:

    {"version": 1,
     "lan": {"hosts": ["192.0.2.20", "office-pc.local"], "https_port": 443, "cert_sha256": "3A:9F:...", "spki_pin26": "...", "not_after": "..."},
     "public": null | {"domain": "crew.example.com", "port": 443, "email": "...", "since": "..."}}

    state()               the parsed file (cached by mtime); a missing or unreadable file is the empty state
    lan_hosts()           the LAN names and addresses, as typed
    public_host()         the padlocked web address (hostname only), "" when there is none
    lan_url()             "https://<first lan host>[:port]" or ""
    origin_of(request)    "lan" | "public" | "local" | "other" from the Host header
    passkeys_available(request)   may this page offer passkeys? (a padlock or localhost, and auth_passkeys is not off)
    rp_id_for(request)    the WebAuthn RP ID for this request, or None when passkeys are not available here
    lan_fingerprint()     the LAN certificate's fingerprint (colon hex) or ""
    note_signin_ip(ip, ok)  D9: remember a sign-in attempt from a public internet address while there is no padlock

CLIENT SIDE: `client()` talks to the sidecar over the compose network with a bearer token (docs in the design, section 4).

This module is imported by app.config (to merge hosts and the public URL), so it must NOT import app.config at module level.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from app import instance

log = logging.getLogger("crewboss.frontdoor")

LAN_WARNING = ("LAN mode is for a local network. Don't expose it to the internet without upgrading to a signed "
               "certificate and passkey sign-in.")

_EMPTY = {"version": 1, "lan": None, "public": None}
_LOOPBACK = ("localhost", "127.0.0.1", "::1")
_HOST_OK = re.compile(r"^[a-z0-9.\-]{1,253}$")
_cache: dict = {"key": None, "data": _EMPTY}
_lock = threading.Lock()


# --- the state file ----------------------------------------------------------------------------------------------------

def state_dir() -> str:
    return os.environ.get("CREWBOSS_FRONTDOOR_STATE_DIR") or instance.path("frontdoor")


def state_file() -> str:
    return os.path.join(state_dir(), "front_door.json")


def _clean(raw) -> dict:
    """Keep only what we understand, with the right types. Garbage in the file never raises."""
    out = {"version": 1, "lan": None, "public": None}
    if not isinstance(raw, dict):
        return out
    lan = raw.get("lan")
    if isinstance(lan, dict):
        hosts = [h.strip().lower() for h in (lan.get("hosts") or []) if isinstance(h, str) and h.strip()]
        hosts = [h for h in hosts if _HOST_OK.match(h) or _is_ip(h)]
        if hosts:
            port = lan.get("https_port")
            out["lan"] = {"hosts": hosts, "https_port": port if isinstance(port, int) and 0 < port < 65536 else 443,
                          "cert_sha256": str(lan.get("cert_sha256") or ""), "spki_pin26": str(lan.get("spki_pin26") or ""),
                          "not_after": str(lan.get("not_after") or "")}
    pub = raw.get("public")
    if isinstance(pub, dict) and isinstance(pub.get("domain"), str) and _HOST_OK.match(pub["domain"].strip().lower()):
        port = pub.get("port")
        out["public"] = {"domain": pub["domain"].strip().lower(), "port": port if isinstance(port, int) and 0 < port < 65536 else 443,
                         "email": str(pub.get("email") or ""), "since": str(pub.get("since") or "")}
    return out


def state() -> dict:
    """The front door state, cached by the file's mtime and size."""
    path = state_file()
    try:
        st = os.stat(path)
        key = (path, st.st_mtime_ns, st.st_size)
    except OSError:
        key = (path, None, None)
    with _lock:
        if _cache["key"] == key:
            return _cache["data"]
        data = _EMPTY
        if key[1] is not None:
            try:
                with open(path, encoding="utf-8") as fh:
                    data = _clean(json.load(fh))
            except (OSError, ValueError):
                log.warning("front_door.json is unreadable; treating the front door as not set up")
        _cache.update(key=key, data=data)
        return data


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


def lan_hosts() -> list[str]:
    lan = state()["lan"]
    return list(lan["hosts"]) if lan else []


def lan_port() -> int:
    lan = state()["lan"]
    return lan["https_port"] if lan else 443


def public_host() -> str:
    pub = state()["public"]
    return pub["domain"] if pub else ""


def public_port() -> int:
    pub = state()["public"]
    return pub["port"] if pub else 443


def public_url() -> str:
    """"https://domain[:port]" of the padlocked address from front_door.json, or ""."""
    host = public_host()
    if not host:
        return ""
    port = public_port()
    return f"https://{host}" + ("" if port == 443 else f":{port}")


def _fmt_host(host: str, port: int) -> str:
    h = f"[{host}]" if ":" in host else host
    return h if port == 443 else f"{h}:{port}"


def lan_url() -> str:
    hosts = lan_hosts()
    return "https://" + _fmt_host(hosts[0], lan_port()) if hosts else ""


def lan_fingerprint() -> str:
    lan = state()["lan"]
    return lan["cert_sha256"] if lan else ""


def lan_not_after() -> str:
    lan = state()["lan"]
    return lan["not_after"] if lan else ""


def origin_hosts() -> list[str]:
    """host[:port] strings (as a browser sends them in Origin) for the LAN hosts and the public domain. config.ORIGIN_HOSTS and the
    MCP allowed hosts add these to the instance's own list."""
    out = [_fmt_host(h, lan_port()) for h in lan_hosts()]
    if public_host():
        out.append(_fmt_host(public_host(), public_port()))
    return out


def bare_hosts() -> list[str]:
    """The LAN hosts and the public domain with no port (the MCP allowed-hosts list adds its own port patterns)."""
    return [*lan_hosts(), *([public_host()] if public_host() else [])]


# --- which door did this request come through ----------------------------------------------------------------------------

def _host_of(request) -> str:
    """The hostname (lower case, no port) of the request's Host header, or "" when it is missing or not a plausible host."""
    try:
        raw = (request.headers.get("host") or "").strip().lower()
    except AttributeError:
        return ""
    if not raw or len(raw) > 300 or any(c in raw for c in " /\\@?#\t\r\n,"):
        return ""
    if raw.startswith("["):                        # [::1]:8931
        host, close, rest = raw[1:].partition("]")
        if not close:
            return ""
        if rest and not (rest.startswith(":") and rest[1:].isdigit()):
            return ""
        return host if _is_ip(host) else ""
    host, sep, port = raw.partition(":")
    if sep and not port.isdigit():
        return ""
    host = host.rstrip(".")
    return host if (_HOST_OK.match(host) or _is_ip(host)) else ""


def _is_loopback(host: str) -> bool:
    if host in _LOOPBACK or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _configured_public_hosts() -> set[str]:
    """The padlocked names: the sidecar's public domain, plus the hostname of an https PUBLIC_URL that is a real name (an install that
    predates the front door card keeps working)."""
    from app import config
    hosts = {public_host()} - {""}
    parts = urlsplit(config.PUBLIC_URL)
    h = (parts.hostname or "").lower()
    if parts.scheme == "https" and h and not _is_loopback(h) and not _is_ip(h) and h not in lan_hosts():
        hosts.add(h)
    return hosts


def origin_of(request) -> str:
    """"public" (a padlocked address), "lan" (a LAN host from front_door.json), "local" (loopback) or "other" (anything else, including a
    missing or odd Host header)."""
    host = _host_of(request)
    if not host:
        return "other"
    if host in _configured_public_hosts():
        return "public"
    if host in lan_hosts():
        return "lan"
    if _is_loopback(host):
        return "local"
    return "other"


def passkeys_enabled_setting() -> bool:
    from app import config
    return config.AUTH_PASSKEYS != "off"


def passkeys_available(request) -> bool:
    """Passkeys are an optional upgrade: only on a padlocked address or on localhost (a loopback IP is no WebAuthn RP ID, so only the name
    localhost), and only when auth_passkeys is not `off`. Never on the LAN's self-signed address."""
    if not passkeys_enabled_setting():
        return False
    origin = origin_of(request)
    if origin == "public":
        return True
    if origin == "local":
        host = _host_of(request)
        return host == "localhost" or host.endswith(".localhost")
    return False


def rp_id_for(request) -> str | None:
    """The WebAuthn RP ID of this request's own origin, or None when passkeys are not available on it."""
    return _host_of(request) if passkeys_available(request) else None


# --- D9: someone reached the sign-in page from the internet while there is no padlock ------------------------------------------

SEEN_KEY = "lan_public_seen"
_SEEN_MIN_GAP = 300          # seconds between writes of the same address, so a scanner cannot make us write all day


def _has_padlock() -> bool:
    return bool(_configured_public_hosts())


def _in_own_v6_subnet(addr) -> bool:
    if addr.version != 6:
        return False
    for h in lan_hosts():
        try:
            mine = ipaddress.ip_address(h)
        except ValueError:
            continue
        if mine.version == 6 and addr in ipaddress.ip_network(f"{mine}/64", strict=False):
            return True
    return False


def is_internet_address(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_global and not _in_own_v6_subnet(addr)


_last_noted: dict[str, float] = {}


def note_signin_ip(ip: str, ok: bool) -> bool:
    """Record (and log) a sign-in attempt or success from a public internet address, but only on a LAN install with no padlock. It warns; it
    never blocks (a LAN client on IPv6 can look public). Returns True when it was recorded. Never raises."""
    try:
        if not lan_hosts() or _has_padlock() or not is_internet_address(ip):
            return False
        now = time.time()
        if now - _last_noted.get(ip, 0) < _SEEN_MIN_GAP and not ok:
            return False
        _last_noted[ip] = now
        if len(_last_noted) > 500:
            _last_noted.clear()
        log.warning("sign-in %s from a public internet address (%s) while this install has no padlock. %s",
                    "succeeded" if ok else "attempted", ip, LAN_WARNING)
        from app.db import get_cursor
        value = json.dumps({"ip": ip, "at": int(now), "ok": bool(ok)})
        with get_cursor() as cur:
            cur.execute("INSERT INTO instance_settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE "
                        "SET value = EXCLUDED.value, updated_at = now()", (SEEN_KEY, value))
        return True
    except Exception:       # a guard must never break sign-in
        log.exception("could not record the public sign-in address")
        return False


def public_seen() -> dict | None:
    """The last recorded {ip, at, ok}, or None (nothing recorded, dismissed, or there is now a padlock)."""
    if _has_padlock() or not lan_hosts():
        return None
    try:
        from app.db import get_cursor
        with get_cursor(commit=False) as cur:
            cur.execute("SELECT value FROM instance_settings WHERE key = %s", (SEEN_KEY,))
            row = cur.fetchone()
        if not row:
            return None
        data = json.loads(row["value"])
        return data if isinstance(data, dict) and data.get("ip") else None
    except Exception:
        return None


def dismiss_public_seen() -> None:
    from app.db import get_cursor
    with get_cursor() as cur:
        cur.execute("DELETE FROM instance_settings WHERE key = %s", (SEEN_KEY,))
    _last_noted.clear()


# --- the sidecar client ------------------------------------------------------------------------------------------------------

class FrontDoorError(Exception):
    """A plain-words problem talking to the front door service. str(exc) is safe to show a person."""


def sidecar_url() -> str:
    return (os.environ.get("CREWBOSS_FRONTDOOR_URL") or "http://frontdoor:8100").rstrip("/")


def token_file() -> str:
    return os.environ.get("CREWBOSS_FRONTDOOR_TOKEN_FILE") or instance.path("secrets/frontdoor-token")


def available() -> bool:
    """Is there a front door service to talk to (its token exists)?"""
    return os.path.isfile(token_file())


LONG_TIMEOUT = 130.0      # /switch and /remove-public wait for Caddy and the certificate; the sidecar answers within about 90 s


class Client:
    """The sidecar API (design section 4). Every call is one HTTP request with the bearer token; answers are JSON dicts. A refusal or an
    unreachable service raises FrontDoorError with a plain sentence. `opener` is for tests."""

    def __init__(self, base: str | None = None, token: str | None = None, timeout: float = 20.0, opener=None):
        self.base = (base or sidecar_url()).rstrip("/")
        self._token = token
        self.timeout = timeout
        self._open = opener or urllib.request.urlopen

    def _tok(self) -> str:
        if self._token is not None:
            return self._token
        try:
            with open(token_file(), encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            raise FrontDoorError("The front door service is not set up on this install.") from None

    def _call(self, method: str, path: str, body: dict | None = None, timeout: float | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Authorization": "Bearer " + self._tok(), "Content-Type": "application/json"})
        try:
            with self._open(req, timeout=timeout or self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 409:
                raise FrontDoorError("Another change is already running. Wait a minute and try again.") from None
            detail = ""
            try:
                detail = str(json.loads(exc.read() or b"{}").get("problem") or "")
            except (ValueError, AttributeError):
                pass
            raise FrontDoorError(detail or "The front door service refused that.") from None
        except (urllib.error.URLError, OSError, TimeoutError):
            raise FrontDoorError("Crewboss could not reach its front door service. Is it running?") from None
        try:
            out = json.loads(raw or b"{}")
        except ValueError:
            raise FrontDoorError("The front door service gave an answer Crewboss could not read.") from None
        if not isinstance(out, dict):
            raise FrontDoorError("The front door service gave an answer Crewboss could not read.")
        return out

    def status(self) -> dict:
        return self._call("GET", "/status")

    def check(self, domain: str) -> dict:
        """Starts the check; {check_id, ...}. Poll check_result(check_id)."""
        return self._call("POST", "/check", {"domain": domain}, timeout=LONG_TIMEOUT)

    def check_result(self, check_id: str) -> dict:
        return self._call("GET", "/check/" + urllib.request.quote(str(check_id), safe=""))

    def switch(self, domain: str, email: str = "", check_id: str = "", allow_no_443: bool = False) -> dict:
        body = {"domain": domain, "check_id": check_id, "allow_no_443": bool(allow_no_443)}
        if email:
            body["email"] = email
        return self._call("POST", "/switch", body, timeout=LONG_TIMEOUT)      # synchronous: up to about 90 s

    def remove_public(self) -> dict:
        return self._call("POST", "/remove-public", {}, timeout=LONG_TIMEOUT)

    def rotate_prepare(self) -> dict:
        return self._call("POST", "/lan-cert/prepare", {})

    def rotate_switch(self) -> dict:
        return self._call("POST", "/lan-cert/switch", {})


def client() -> Client:
    return Client()


# --- input checks the card shares with the sidecar's own (same expression as the installer's valid_domain) -----------------------------

DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
EMAIL_RE = re.compile(r"^[^\s@<>{}\"']{1,64}@[a-z0-9.\-]{1,200}$", re.I)


def valid_domain(name: str) -> bool:
    name = (name or "").strip().lower()
    return bool(DOMAIN_RE.match(name)) and name != "localhost" and not name.endswith(".localhost") and not _is_ip(name)


def valid_email(value: str) -> bool:
    return bool(EMAIL_RE.match((value or "").strip()))


def restart_app_soon(delay: float = 2.0) -> None:
    """After a successful switch the app exits by itself (SIGTERM to its own process, `delay` seconds after it has answered) and docker's
    `restart: unless-stopped` brings it back with the new config. The sidecar watches /healthz and rolls back if it does not return."""
    import signal

    def go():
        log.warning("front door changed: restarting the app to pick up the new address")
        os.kill(os.getpid(), signal.SIGTERM)
    t = threading.Timer(delay, go)
    t.daemon = True
    t.start()


# --- LAN certificate renewal, app side ----------------------------------------------------------------------------------------

AUTO_SWITCH_DAYS = 14


def lan_days_left(now: float | None = None) -> float | None:
    """Days until the LAN certificate in front_door.json ends, or None when there is none."""
    na = (state().get("lan") or {}).get("not_after") or ""
    try:
        end = time.mktime(time.strptime(na[:10], "%Y-%m-%d"))
    except ValueError:
        return None
    return (end - (time.time() if now is None else now)) / 86400


def _audit(verb: str, summary: str) -> None:
    try:
        from app import access
        from app.db import get_cursor
        with get_cursor() as cur:
            access.audit(cur, verb, summary)
    except Exception:  # noqa: BLE001 — an audit hiccup must not hide the result
        log.exception("front door audit failed")


def auto_renew(now: float | None = None) -> str:
    """Daily, from the app. The sidecar only PREPARES the new certificate (60 days out). Here, inside AUTO_SWITCH_DAYS of the end, the app
    announces it to every connected runner and switches. The key is reused and runners trust the pinned KEY, so a runner that has not
    acknowledged yet (offline, asleep) still connects after the switch; those are listed in the audit line. Every switch and every failure
    is logged and audited, as the Front door card does. Returns what it did."""
    left = lan_days_left(now)
    if left is None or left > AUTO_SWITCH_DAYS or not available():
        return "not due"
    from app import runner_ws
    c = client()
    try:
        prepared = c.rotate_prepare()
        runner_ws.rotate_tls_from_prepare(prepared)
        pin = runner_ws.pin26(runner_ws._pem_der(prepared["next_cert_pem"]))
        r = runner_ws.tls_ready(pin)
        out = c.rotate_switch()
    except Exception as exc:  # noqa: BLE001 — audited, and tried again tomorrow
        log.warning("automatic LAN certificate renewal failed (%.0f days left): %s", left, exc)
        _audit("front_door.lan_cert_auto_failed", f"Automatic network certificate renewal failed, {left:.0f} days left: {exc}")
        return "failed"
    if not out.get("ok"):
        problem = str(out.get("problem") or "refused")
        log.warning("automatic LAN certificate switch refused (%.0f days left): %s", left, problem)
        _audit("front_door.lan_cert_auto_failed", f"Automatic network certificate switch refused, {left:.0f} days left: {problem}")
        return "switch failed"
    pending = r.get("pending") or []
    note = (f"; {r['ready']} of {r['total']} computers had acknowledged it, not yet: {', '.join(pending)[:250]}" if r["ready"] != r["total"]
            else f"; all {r['total']} computers had acknowledged it")
    log.warning("LAN certificate renewed automatically, %.0f days left%s", left, note)
    _audit("front_door.lan_cert_auto", f"Network certificate renewed automatically, {left:.0f} days left{note}")
    return "switched"
