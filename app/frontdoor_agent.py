"""The front-door sidecar (#976, design D4/D5/D6): the ONLY thing that writes the bundled Caddy's config.

Runs in its own container (same image as the app, command `python -m app.frontdoor_agent serve`): read-only root, no
capabilities, the install user's uid, the folder <home>/frontdoor read-write and Caddy's admin socket volume. The app talks to it
over the compose network with a bearer token and can only ask for typed actions (a domain, an optional email, fixed names). The
sidecar renders the Caddyfile from its OWN template; it never accepts config text, so a compromised app cannot serve files,
proxy elsewhere or switch the secure connection off.

HTTP API (JSON, `Authorization: Bearer <frontdoor-token>`; one action at a time, a second one gets 409):
  GET  /status                 -> {managed, lan, public, busy, last_result, rotation: {next_fp, next_not_after, acks}}
  POST /check {domain}         -> {check_id}; GET /check/<id> -> {done, domain, dns, port80, port443}   (30-90 s, poll)
  POST /switch {domain, email?, check_id, allow_no_443} -> {ok, problem?}   (needs a passed check under 15 min old)
  POST /remove-public          -> {ok}
  POST /lan-cert/prepare       -> {ok, next_cert_pem, sig_material}   sig_material = {new_spki_sha256, new_cert_sha256,
                                  old_spki_sha256 (hex), host}; the APP signs it with the identity key (this process never holds it)
  POST /lan-cert/switch        -> {ok}
CLI (the installer runs it in a throwaway container): init | serve | renew-if-due | reload.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from app import lan_tls

DOMAIN_RE = re.compile(r"^(?=.{4,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")   # same as ./crewboss valid_domain
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}$")
FRONT_DOOR_HEADERS = ("X-Crewboss-Front-Door", "X-MDD-Front-Door")
MARKER = "# Written by crewboss frontdoor"          # first line of a Caddyfile this sidecar owns
LE_STAGING = "https://acme-staging-v02.api.letsencrypt.org/directory"
CHECK_TTL = 15 * 60
LOG_WAIT = 90.0
HEALTH_WAIT = 120.0


class Refused(Exception):
    """A request the sidecar will not do; the message is plain words for the card."""


def env_dir(name: str, default: str) -> Path:
    return Path(os.environ.get(name) or default)


# --- the Caddyfile template ---------------------------------------------------------------------------------------------

def _acme_issuer(directory: str | None, disable: str | None, trusted_root: str | None) -> list[str]:
    if not (directory or disable or trusted_root):
        return []
    body = []
    if directory:
        body.append(f"\t\t\tdir {directory}")
    if trusted_root:
        body.append(f"\t\t\ttrusted_roots {trusted_root}")
    if disable == "tlsalpn":
        body.append("\t\t\tdisable_tlsalpn_challenge")
    elif disable == "http":
        body.append("\t\t\tdisable_http_challenge")
    return ["\ttls {", "\t\tissuer acme {", *body, "\t\t}", "\t}"]


def _strips() -> list[str]:
    return [f"\trequest_header -{h}" for h in FRONT_DOOR_HEADERS]


def render_caddyfile(state: dict, dry: dict | None = None, acme: dict | None = None) -> str:
    """The whole Caddyfile for `state` (front_door.json). `dry` = {"domain", "disable": "tlsalpn"|"http", "dir"} adds a
    Let's Encrypt staging block for a check. `acme` = {"dir", "root"} points the production block at another ACME server
    (the Pebble end-to-end test only). Everything interpolated is validated here again: no newline, brace or space gets in."""
    acme = acme or {}
    lan, public = state.get("lan"), state.get("public")
    hosts = list(lan["hosts"]) if lan else []
    for h in hosts:
        if not lan_tls.valid_lan_host(h):
            raise Refused(f"{h!r} is not a valid network address")
    glob = ["\tadmin unix//run/caddy-admin/caddy.sock|0666"]
    if hosts:
        glob.append(f"\tdefault_sni {next((h for h in hosts if _is_ip(h)), hosts[0])}")
    glob += ["\tlog default {", "\t\toutput file /frontdoor/logs/caddy.log {", "\t\t\tmode 0644", "\t\t}", "\t\tformat json", "\t}"]
    if public and public.get("email"):
        if not EMAIL_RE.match(public["email"]):
            raise Refused("That email address does not look right.")
        glob.append(f"\temail {public['email']}")
    out = [MARKER + " (the Front door card rewrites this file; edits are lost)", "{", *glob, "}", ""]
    if hosts:
        port = int(lan.get("https_port", 443))
        target = "{host}" if port == 443 else f"{{host}}:{port}"
        out += [", ".join(f"https://{h}" for h in hosts) + " {",
                "\ttls /frontdoor/lan.crt /frontdoor/lan.key", *_strips(), "\tencode zstd gzip", "\treverse_proxy app:8000", "}", "",
                # NEVER an HSTS header here: it would make the certificate warning impossible to click through.
                ", ".join(f"http://{h}" for h in hosts) + " {",
                "\thandle /trust* {", "\t\treverse_proxy app:8000", "\t}",
                "\thandle {", f"\t\tredir https://{target}{{uri}} 308", "\t}", "}", ""]
    if public:
        d = public["domain"]
        if not DOMAIN_RE.match(d):
            raise Refused("That web address does not look right.")
        out += [f"{d} {{", *_acme_issuer(acme.get("dir"), None, acme.get("root")),
                '\theader Strict-Transport-Security "max-age=31536000"', *_strips(), "\tencode zstd gzip", "\treverse_proxy app:8000", "}", ""]
    if dry:
        d = dry["domain"]
        if not DOMAIN_RE.match(d) or dry.get("disable") not in ("tlsalpn", "http"):
            raise Refused("That web address does not look right.")
        out += [f"{d} {{", *_acme_issuer(dry.get("dir") or LE_STAGING, dry["disable"], acme.get("root")), '\trespond "crewboss check" 200', "}", ""]
    return "\n".join(out)


def _is_ip(h: str) -> bool:
    try:
        ipaddress.ip_address(h)
        return True
    except ValueError:
        return False


# --- Caddy's admin API over the unix socket -----------------------------------------------------------------------------

import http.client


class _Unix(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float = 60):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


def caddy_load(text: str, sock: str | None = None) -> tuple[bool, str]:
    """POST the Caddyfile to /load. Caddy checks it whole and keeps the old config when it refuses (spike S0). must-revalidate
    forces a reload even when the text is unchanged (a swapped certificate file is picked up)."""
    sock = sock or os.environ.get("CREWBOSS_CADDY_SOCK", "/run/caddy-admin/caddy.sock")
    try:
        c = _Unix(sock)
        c.request("POST", "/load", body=text.encode(), headers={"Content-Type": "text/caddyfile", "Cache-Control": "must-revalidate"})
        r = c.getresponse()
        body = r.read().decode("utf-8", "replace")
        if r.status == 200:
            return True, ""
        try:
            body = json.loads(body).get("error", body)
        except ValueError:
            pass
        return False, body[:400]
    except OSError as exc:
        return False, f"cannot reach the proxy: {exc}"


# --- reading Caddy's JSON log -------------------------------------------------------------------------------------------

_TYPES = ("rateLimited", "dns", "connection", "unauthorized", "caa", "serverInternal")


def explain(problem: str, detail: str, domain: str, port: int) -> str:
    """One plain sentence for an ACME problem type (the words 'ACME' and 'challenge' never reach the card)."""
    t = next((x for x in _TYPES if x.lower() in (problem or "").lower()), "")
    if t == "rateLimited":
        return "Let's Encrypt has asked us to wait before trying again. Try again in an hour."
    if t == "dns":
        return f"The internet cannot find {domain}. Check that the address exists and points at your internet connection."
    if t == "caa":
        return f"The owner of {domain} does not allow Let's Encrypt to give it a padlock (a CAA record in its settings)."
    if t == "connection":
        return (f"Let's Encrypt could not connect to {domain} on port {port}. Check that your router forwards port {port} to this "
                "computer and that no firewall blocks it.")
    if t == "unauthorized":
        return (f"Let's Encrypt reached an address for {domain}, but it was not this computer. Check that the address points at "
                f"your internet connection and that port {port} is forwarded to this computer.")
    return f"The check for {domain} did not work ({(detail or problem or 'unknown reason')[:160]})."


def watch_log(path: Path, offset: int, domain: str, challenge: str, port: int, timeout: float = LOG_WAIT,
              sleep=time.sleep, clock=time.monotonic, stale_after: float | None = None) -> tuple[bool | None, str]:
    """Wait for Caddy to say it got a certificate for `domain` (True) or why it could not (False, plain sentence). With
    `stale_after`, None means "Caddy never started on this name": it already holds a certificate from an earlier check."""
    start = clock()
    deadline = start + timeout
    seen = False
    pos, buf = offset, b""
    while clock() < deadline:
        try:
            with open(path, "rb") as fh:
                fh.seek(pos)
                data = fh.read()
                pos += len(data)
        except OSError:
            data = b""
        buf += data
        *lines, buf = buf.split(b"\n")
        for raw in lines:
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            if rec.get("identifier") != domain:
                continue
            seen = True
            msg = rec.get("msg", "")
            if msg == "certificate obtained successfully":
                return True, ""
            if msg == "challenge failed":
                p = rec.get("problem") or {}
                return False, explain(p.get("type", ""), p.get("detail", ""), domain, port)
            if msg == "could not get certificate from issuer":
                return False, explain(str(rec.get("error", "")), str(rec.get("error", "")), domain, port)
        if stale_after is not None and not seen and clock() - start >= stale_after:
            return None, "stale"
        sleep(1)
    return False, f"The check took too long. Check that {domain} points at your internet connection and that port {port} is forwarded to this computer."


# --- state on disk ------------------------------------------------------------------------------------------------------

def write_atomic(path: Path, data: bytes | str, mode: int = 0o644) -> None:
    tmp = path.with_name(path.name + ".tmp")
    b = data.encode() if isinstance(data, str) else data
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb") as fh:
        fh.write(b)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def lan_block(cert_pem: bytes, hosts: list[str], https_port: int, http_port: int = 80) -> dict:
    c = lan_tls.load_cert(cert_pem)
    return {"hosts": hosts, "https_port": https_port, "http_port": http_port, "cert_sha256": lan_tls.fingerprint(c),
            "spki_pin26": lan_tls.pin26(c), "not_after": lan_tls.not_after(c).strftime("%Y-%m-%dT%H:%M:%SZ")}


def private_only(addrs: list[str]) -> bool:
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%")[0])
        if ip.is_global:
            return False
    return True


def resolve(domain: str) -> list[str]:
    try:
        return sorted({i[4][0] for i in socket.getaddrinfo(domain, None) if i[0] in (socket.AF_INET, socket.AF_INET6)})
    except OSError:
        return []


def trusted_serve_check(host: str, domain: str, port: int = 443, cafile: str | None = None, timeout: float = 10) -> bool:
    ctx = ssl.create_default_context(cafile=cafile)
    try:
        with socket.create_connection((host, port), timeout=timeout) as s, ctx.wrap_socket(s, server_hostname=domain):
            return True
    except (OSError, ssl.SSLError):
        return False


# --- the agent ----------------------------------------------------------------------------------------------------------

class Agent:
    def __init__(self, base: Path, token: str, loader=caddy_load, resolver=resolve, now=time.time, sleep=time.sleep,
                 health=None, tls_probe=trusted_serve_check, acme: dict | None = None, staging: str | None = None,
                 log_wait: float = LOG_WAIT, health_wait: float = HEALTH_WAIT, background=True):
        self.base, self.token, self.load, self.resolve = Path(base), token, loader, resolver
        self.now, self.sleep, self.health, self.tls_probe = now, sleep, health or self._health, tls_probe
        self.acme, self.staging = acme or {}, staging or LE_STAGING
        self.log_wait, self.health_wait, self.background = log_wait, health_wait, background
        self.lock = threading.Lock()
        self.checks: dict[str, dict] = {}
        self.last_result: dict | None = None

    # -- files
    @property
    def state_file(self) -> Path: return self.base / "front_door.json"
    @property
    def caddyfile(self) -> Path: return self.base / "Caddyfile"
    @property
    def log_file(self) -> Path: return self.base / "logs" / "caddy.log"

    def state(self) -> dict:
        try:
            return json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return {"version": 1, "lan": None, "public": None}

    def managed(self) -> bool:
        try:
            return self.caddyfile.read_text().startswith(MARKER) and self.state_file.is_file()
        except OSError:
            return False

    def _log_size(self) -> int:
        try:
            return self.log_file.stat().st_size
        except OSError:
            return 0

    def _health(self) -> bool:
        try:
            with urllib.request.urlopen(os.environ.get("CREWBOSS_APP_HEALTH", "http://app:8000/healthz"), timeout=3) as r:
                return r.status == 200
        except (urllib.error.URLError, OSError):
            return False

    def _render(self, state: dict, dry: dict | None = None) -> str:
        return render_caddyfile(state, dry, self.acme)

    def _restore_active(self) -> None:
        """Re-load what is on disk: also stops Caddy retrying certificate requests in the background."""
        try:
            self.load(self.caddyfile.read_text())
        except OSError:
            pass

    # -- status
    def status(self) -> dict:
        st = self.state()
        nxt = self.base / "lan-next.crt"
        rot = {"next_fp": None, "next_not_after": None, "acks": None}
        if nxt.is_file():
            c = lan_tls.load_cert(nxt.read_bytes())
            rot.update(next_fp=lan_tls.fingerprint(c), next_not_after=lan_tls.not_after(c).strftime("%Y-%m-%dT%H:%M:%SZ"))
        return {"managed": self.managed(), "lan": st.get("lan"), "public": st.get("public"), "busy": self.lock.locked(),
                "last_result": self.last_result, "rotation": rot}

    def _need_managed(self) -> None:
        if not self.managed():
            raise Refused("This install's proxy settings were edited by hand, so they can't be changed here.")

    # -- check
    def start_check(self, domain: str) -> dict:
        domain = (domain or "").strip().lower()
        if not DOMAIN_RE.match(domain):
            raise Refused("That web address does not look right (use a name like crew.example.com).")
        self._need_managed()
        if not self.lock.acquire(blocking=False):
            raise Refused("busy")
        cid = secrets.token_hex(8)
        self.checks[cid] = {"done": False, "domain": domain, "at": self.now(), "dns": None, "port80": None, "port443": None}
        if self.background:
            threading.Thread(target=self._run_check, args=(cid,), daemon=True).start()
        else:
            self._run_check(cid)
        return {"check_id": cid}

    def _run_check(self, cid: str) -> None:
        rec = self.checks[cid]
        domain = rec["domain"]
        try:
            addrs = self.resolve(domain)
            if not addrs:
                rec["dns"] = {"addrs": [], "ok": False, "problem": f"{domain} is not set up yet. Create it at your domain provider and point it at your internet connection."}
                return
            if private_only(addrs) and not self.acme:          # self.acme is set only by the Pebble test env: a test domain is private
                rec["dns"] = {"addrs": addrs, "ok": False, "problem": f"{domain} points inside your own network ({addrs[0]}). Point it at your internet connection's address instead."}
                return
            rec["dns"] = {"addrs": addrs, "ok": True, "problem": ""}
            for key, disable, port, name in (("port80", "tlsalpn", 80, "http"), ("port443", "http", 443, "tlsalpn")):
                ok, why = self._dry_run(domain, disable, port, cid)
                rec[key] = {"ok": ok, "problem": why}
                if key == "port80" and not ok:
                    rec["port443"] = {"ok": False, "problem": "Not checked: fix port 80 first."}
                    break
        except Exception as exc:                       # never leave a check hanging
            rec["dns"] = rec["dns"] or {"addrs": [], "ok": False, "problem": f"The check could not run: {exc}"}
        finally:
            self._restore_active()
            rec["done"] = True
            self.lock.release()

    def _dry_run(self, domain: str, disable: str, port: int, cid: str) -> tuple[bool, str]:
        """Let's Encrypt STAGING with only one kind of validation: a real outside client proves the port. Caddy files a certificate
        by issuer URL (host:port and path only, found in the Pebble test), so the port-443 check uses the directory URL with an
        explicit ':443' to keep the port-80 check's certificate from being reused. A name checked before still holds both
        certificates: that case (Caddy never starts) is answered from check_history.json (S0 finding)."""
        url = self.staging if port == 80 else _explicit_443(self.staging)
        off = self._log_size()
        ok, err = self.load(self._render(self.state(), {"domain": domain, "disable": disable, "dir": url}))
        if not ok:
            return False, f"The proxy refused the check: {err}"
        ok, why = watch_log(self.log_file, off, domain, "http-01" if disable == "tlsalpn" else "tls-alpn-01", port, self.log_wait,
                            sleep=self.sleep, stale_after=6)
        hist = self._history()
        if ok is None:        # Caddy already holds a staging certificate for this name: believe the earlier pass, if there was one
            return (True, "") if (hist.get(domain, {}).get(str(port)) or self.acme) else (False, "This address was checked before and could not be checked again. Try again in a day.")
        if ok:
            hist.setdefault(domain, {})[str(port)] = _iso(self.now())
            write_atomic(self.base / "check_history.json", json.dumps(hist))
        return ok, why

    def _history(self) -> dict:
        try:
            return json.loads((self.base / "check_history.json").read_text())
        except (OSError, ValueError):
            return {}

    def get_check(self, cid: str) -> dict:
        rec = self.checks.get(cid)
        if not rec:
            raise KeyError(cid)
        return {k: v for k, v in rec.items() if k != "at"} | {"check_id": cid}

    # -- switch / remove
    def switch(self, domain: str, email: str | None, check_id: str, allow_no_443: bool) -> dict:
        domain = (domain or "").strip().lower()
        if not DOMAIN_RE.match(domain):
            raise Refused("That web address does not look right.")
        if email and not EMAIL_RE.match(email):
            raise Refused("That email address does not look right.")
        self._need_managed()
        rec = self.checks.get(check_id or "")
        if not rec or rec["domain"] != domain or not rec["done"] or self.now() - rec["at"] > CHECK_TTL:
            raise Refused("Run the check again first (it must be for this address and less than 15 minutes old).")
        if not (rec["dns"] or {}).get("ok") or not (rec["port80"] or {}).get("ok") or not (allow_no_443 or (rec["port443"] or {}).get("ok")):
            raise Refused("The check did not pass, so nothing was changed.")
        if not self.lock.acquire(blocking=False):
            raise Refused("busy")
        try:
            old = self.state()
            new = json.loads(json.dumps(old))
            new["public"] = {"domain": domain, "port": 443, "email": email or "", "since": _iso(self.now())}
            off = self._log_size()
            ok, err = self.load(self._render(new))
            if not ok:
                self._restore_active()
                return self._done({"ok": False, "problem": f"The proxy refused the change ({err}). Nothing was changed."})
            ok, why = watch_log(self.log_file, off, domain, "", 443, self.log_wait, sleep=self.sleep, stale_after=6)
            if ok is None:          # Caddy already holds a certificate for the name: serving it is the proof (probe below)
                ok = True
            if ok and not self.tls_probe("caddy", domain, 443, self.acme.get("root_file")):
                ok, why = False, f"The padlock for {domain} was issued but is not being served."
            if not ok:
                self._restore_active()
                return self._done({"ok": False, "problem": f"{why} Nothing was changed."})
            self._commit(new)
            res = self._done({"ok": True})
            self._watch_app()
            return res
        finally:
            self.lock.release()

    def remove_public(self) -> dict:
        self._need_managed()
        if not self.lock.acquire(blocking=False):
            raise Refused("busy")
        try:
            old = self.state()
            if not old.get("public"):
                return {"ok": True}
            if not old.get("lan"):
                raise Refused("This install has no network address to go back to, so the web address can't be removed.")
            new = dict(old, public=None)
            ok, err = self.load(self._render(new))
            if not ok:
                self._restore_active()
                return self._done({"ok": False, "problem": f"The proxy refused the change ({err}). Nothing was changed."})
            self._commit(new)
            res = self._done({"ok": True})
            self._watch_app()
            return res
        finally:
            self.lock.release()

    def _commit(self, new: dict) -> None:
        """Caddyfile first, front_door.json LAST; each keeps a .prev."""
        for f in (self.caddyfile, self.state_file):
            if f.is_file():
                write_atomic(f.with_name(f.name + ".prev"), f.read_bytes())
        write_atomic(self.caddyfile, self._render(new))
        write_atomic(self.state_file, json.dumps(new, indent=1))

    def _done(self, res: dict) -> dict:
        self.last_result = res | {"at": _iso(self.now())}
        return res

    def _watch_app(self) -> None:
        """The app restarts itself after answering; if it does not come back healthy in 120 s, undo the change."""
        def run():
            self.sleep(5)
            deadline = self.now() + self.health_wait
            while self.now() < deadline:
                if self.health():
                    return
                self.sleep(2)
            with (self.lock if self.background else contextlib.nullcontext()):
                for f in (self.caddyfile, self.state_file):
                    p = f.with_name(f.name + ".prev")
                    if p.is_file():
                        write_atomic(f, p.read_bytes())
                self._restore_active()
                self._done({"ok": False, "problem": "The app did not come back, so the previous settings were restored."})
        if self.background:
            threading.Thread(target=run, daemon=True).start()
        else:
            run()

    # -- LAN certificate rotation
    def lan_prepare(self) -> dict:
        self._need_managed()
        st = self.state()
        lan = st.get("lan")
        if not lan:
            raise Refused("This install has no network address certificate.")
        if not self.lock.acquire(blocking=False):
            raise Refused("busy")
        try:
            old = lan_tls.load_cert((self.base / "lan.crt").read_bytes())
            new_pem = lan_tls.renew((self.base / "lan.crt").read_bytes(), (self.base / "lan.key").read_bytes(), lan["hosts"])
            nc = lan_tls.load_cert(new_pem)
            write_atomic(self.base / "lan-next.crt", new_pem)
            write_atomic(self.base / "lan-next.key", (self.base / "lan.key").read_bytes(), 0o600)
            return {"ok": True, "next_cert_pem": new_pem.decode(),
                    "sig_material": {"new_spki_sha256": lan_tls.spki_sha256(nc).hex(), "new_cert_sha256": hashlib.sha256(lan_tls.der(nc)).hexdigest(),
                                     "old_spki_sha256": lan_tls.spki_sha256(old).hex(), "host": lan["hosts"][0]}}
        finally:
            self.lock.release()

    def lan_switch(self) -> dict:
        self._need_managed()
        if not (self.base / "lan-next.crt").is_file():
            raise Refused("There is no new certificate waiting.")
        if not self.lock.acquire(blocking=False):
            raise Refused("busy")
        try:
            st = self.state()
            prev_crt, prev_key = (self.base / "lan.crt").read_bytes(), (self.base / "lan.key").read_bytes()
            write_atomic(self.base / "lan.crt", (self.base / "lan-next.crt").read_bytes())
            write_atomic(self.base / "lan.key", (self.base / "lan-next.key").read_bytes(), 0o600)
            ok, err = self.load(self._render(st))
            if not ok:
                write_atomic(self.base / "lan.crt", prev_crt)
                write_atomic(self.base / "lan.key", prev_key, 0o600)
                self._restore_active()
                return self._done({"ok": False, "problem": f"The proxy refused the new certificate ({err}). Nothing was changed."})
            st["lan"] = lan_block((self.base / "lan.crt").read_bytes(), st["lan"]["hosts"], st["lan"].get("https_port", 443), st["lan"].get("http_port", 80))
            write_atomic(self.state_file, json.dumps(st, indent=1))
            for f in ("lan-next.crt", "lan-next.key"):
                (self.base / f).unlink(missing_ok=True)
            return self._done({"ok": True})
        finally:
            self.lock.release()

    def renew_if_due(self) -> str:
        """Daily: prepare a new certificate 60 days before expiry. It NEVER switches by itself: the app announces the new certificate to the
        runners and switches on the owner's press, or by itself near the end only once every runner is ready (app/front_door.auto_renew)."""
        if not self.managed() or not (self.base / "lan.crt").is_file():
            return "nothing"
        left = lan_tls.days_left(lan_tls.load_cert((self.base / "lan.crt").read_bytes()))
        if left > lan_tls.RENEW_DAYS:
            return "not due"
        if not (self.base / "lan-next.crt").is_file():
            self.lan_prepare()
            return "prepared"
        return "waiting"


def _explicit_443(url: str) -> str:
    m = re.match(r"^(https://[^/:]+)(/.*)$", url)
    return f"{m.group(1)}:443{m.group(2)}" if m else url


def _iso(t: float) -> str:
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- HTTP ---------------------------------------------------------------------------------------------------------------

def make_handler(agent: Agent):
    class H(BaseHTTPRequestHandler):
        server_version = "frontdoor"

        def log_message(self, *a):
            pass

        def _send(self, code: int, body: dict):
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authed(self) -> bool:
            got = self.headers.get("Authorization", "")
            if got.startswith("Bearer ") and hmac.compare_digest(got[7:].encode(), agent.token.encode()):
                return True
            self._send(401, {"ok": False, "problem": "unauthorised"})
            return False

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 4096:
                raise Refused("request too large")
            try:
                data = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                raise Refused("bad request")
            if not isinstance(data, dict):
                raise Refused("bad request")
            return data

        def _run(self, fn):
            try:
                self._send(200, fn())
            except Refused as exc:
                self._send(409 if str(exc) == "busy" else 400, {"ok": False, "problem": "Another change is running. Try again in a minute." if str(exc) == "busy" else str(exc)})
            except KeyError:
                self._send(404, {"ok": False, "problem": "unknown check"})
            except Exception:
                self._send(500, {"ok": False, "problem": "The front door service had an error."})

        def do_GET(self):
            if not self._authed():
                return
            if self.path == "/status":
                return self._run(agent.status)
            m = re.fullmatch(r"/check/([0-9a-f]{16})", self.path)
            if m:
                return self._run(lambda: agent.get_check(m.group(1)))
            self._send(404, {"ok": False, "problem": "not found"})

        def do_POST(self):
            if not self._authed():
                return
            p = self.path
            routes = {
                "/check": lambda b: agent.start_check(str(b.get("domain", ""))),
                "/switch": lambda b: agent.switch(str(b.get("domain", "")), (str(b["email"]) if b.get("email") else None),
                                                  str(b.get("check_id", "")), bool(b.get("allow_no_443"))),
                "/remove-public": lambda b: agent.remove_public(),
                "/lan-cert/prepare": lambda b: agent.lan_prepare(),
                "/lan-cert/switch": lambda b: agent.lan_switch(),
            }
            if p not in routes:
                return self._send(404, {"ok": False, "problem": "not found"})
            self._run(lambda: routes[p](self._body()))
    return H


def read_token(path: str | None = None) -> str:
    f = Path(path or os.environ.get("CREWBOSS_FRONTDOOR_TOKEN_FILE", "/run/secrets/frontdoor-token"))
    tok = f.read_text().strip()
    if len(tok) < 32:
        raise SystemExit("frontdoor: the token file is empty or too short")
    return tok


def agent_from_env() -> Agent:
    base = env_dir("CREWBOSS_FRONTDOOR_PATH", "/frontdoor")
    acme = {}
    if os.environ.get("CREWBOSS_ACME_DIR"):
        acme["dir"] = os.environ["CREWBOSS_ACME_DIR"]
    if os.environ.get("CREWBOSS_ACME_TEST_ROOT"):       # a path inside /frontdoor (Caddy's view); the sidecar sees the same path
        acme["root"] = os.environ["CREWBOSS_ACME_TEST_ROOT"]
        acme["root_file"] = os.environ["CREWBOSS_ACME_TEST_ROOT"]
    return Agent(base, read_token(), acme=acme, staging=os.environ.get("CREWBOSS_ACME_STAGING_DIR") or LE_STAGING)


def serve(port: int = 8100) -> None:
    agent = agent_from_env()

    def daily():
        while True:
            try:
                agent.renew_if_due()
            except Exception as exc:
                print(f"frontdoor: renew-if-due failed: {exc}", file=sys.stderr, flush=True)
            time.sleep(24 * 3600)
    threading.Thread(target=daily, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", port), make_handler(agent)).serve_forever()


# --- init (the installer's door) ----------------------------------------------------------------------------------------

def init(out: Path, lan_hosts: list[str], name: str = "Crewboss", https_port: int = 443, http_port: int = 80,
         reissue: bool = False, from_domain: str | None = None, email: str | None = None, acme: dict | None = None) -> dict:
    """Make <out> (the instance's frontdoor/ folder): the LAN key + certificate (the key is KEPT on --reissue), front_door.json
    and the Caddyfile. --from-domain makes a public-only door from an existing install's domain (no LAN certificate)."""
    out = Path(out)
    (out / "logs").mkdir(parents=True, exist_ok=True)
    os.chmod(out / "logs", 0o755)
    state = {"version": 1, "lan": None, "public": None}
    if reissue and not from_domain:                      # a new LAN address must not drop the web address
        try:
            state["public"] = (json.loads((out / "front_door.json").read_text()) or {}).get("public")
        except (OSError, ValueError):
            pass
    if from_domain:
        if not DOMAIN_RE.match(from_domain):
            raise SystemExit(f"{from_domain!r} is not a valid domain")
        if email and not EMAIL_RE.match(email):
            raise SystemExit("that email address does not look right")
        state["public"] = {"domain": from_domain, "port": 443, "email": email or "", "since": _iso(time.time())}
    if lan_hosts:
        if not all(lan_tls.valid_lan_host(h) for h in lan_hosts):
            raise SystemExit("a LAN address or name is not valid")
        key_f, crt_f = out / "lan.key", out / "lan.crt"
        key = lan_tls.load_key(key_f.read_bytes()) if (reissue and key_f.is_file()) else None
        crt, kpem = lan_tls.generate(lan_hosts, key=key, name=name)
        write_atomic(key_f, kpem, 0o600)
        write_atomic(crt_f, crt, 0o644)
        state["lan"] = lan_block(crt, lan_hosts, https_port, http_port)
    write_atomic(out / "front_door.json", json.dumps(state, indent=1))
    write_atomic(out / "Caddyfile", render_caddyfile(state, None, acme))
    return state


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="frontdoor_agent")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve")
    s.add_argument("--port", type=int, default=8100)
    i = sub.add_parser("init")
    i.add_argument("--out", required=True)
    i.add_argument("--lan-host", action="append", default=[])
    i.add_argument("--name", default="Crewboss")
    i.add_argument("--https-port", type=int, default=443)
    i.add_argument("--http-port", type=int, default=80)
    i.add_argument("--reissue", action="store_true")
    i.add_argument("--from-domain")
    i.add_argument("--email")
    sub.add_parser("renew-if-due")
    a = p.parse_args(argv)
    if a.cmd == "serve":
        serve(a.port)
    elif a.cmd == "init":
        st = init(Path(a.out), a.lan_host, a.name, a.https_port, a.http_port, a.reissue, a.from_domain, a.email)
        print(json.dumps(st))
    else:
        print(agent_from_env().renew_if_due())
    return 0


if __name__ == "__main__":
    sys.exit(main())
