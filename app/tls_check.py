"""Public-certificate check (task #707).

An https public_url on a real host name must be served with a publicly trusted, name-matching certificate that is not
about to expire. The check opens a TLS connection to it FROM INSIDE the app, with normal verification (the system trust
store, the host name), and reads the certificate's end date. It never sends anything but the handshake.

    check(url)          -> Result (never raises)
    startup(loop)       -> `enforce`: waits (up to TLS_CHECK_GRACE_SECONDS) for a good certificate and RAISES if none;
                           `warn`: starts the background loop and returns at once.
    banner()            -> the problem text for the page banner, or None

Defaults to `warn`: a transient failure can never stop an existing install. Only an instance that says `tls_check: enforce`
(the installer writes it for new installs) refuses to start. A name that is localhost / a loopback address is exempt.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import socket
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

from app import config, front_door

log = logging.getLogger("crewboss.tls")


@dataclass
class Result:
    ok: bool
    problem: str = ""
    days_left: int | None = None
    host: str = ""
    checked_at: float = 0.0


_last: Result | None = None


def target(url: str) -> tuple[str, int] | None:
    """(host, port) when `url` needs the check: https and not a loopback / localhost host. None = exempt."""
    parts = urlsplit(url or "")
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host:
        return None
    if host == "localhost" or host.endswith(".localhost"):
        return None
    try:
        if ipaddress.ip_address(host).is_loopback:
            return None
    except ValueError:
        pass
    return host, parts.port or 443


def _connect_to(host: str, port: int) -> tuple[str, int]:
    """Where to dial: the override (tls_check_connect, 'host:port') or the public name itself."""
    override = config.TLS_CHECK_CONNECT
    if override:
        h, _, p = override.rpartition(":")
        if h and p.isdigit():
            return h, int(p)
    return host, port


LAN_RENEW_DAYS = 60       # the LAN certificate is renewed 60 days before it ends (design D2); warn from there


def lan_check(host: str, port: int, timeout: float | None = None, now: float | None = None) -> Result:
    """The LAN (self-signed) certificate: no trust store, so compare the served certificate's fingerprint with the one front_door.json
    says the front door made, and warn when it is within LAN_RENEW_DAYS of its end. Never sends anything but the handshake."""
    timeout = config.TLS_CHECK_TIMEOUT_SECONDS if timeout is None else timeout
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        ipaddress.ip_address(host)
        sni = None
    except ValueError:
        sni = host
    try:
        with socket.create_connection(_connect_to(host, port), timeout=timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=sni) as tls:
                der = tls.getpeercert(binary_form=True)
    except ssl.SSLError as exc:
        return Result(False, f"the secure connection to {host} failed: {exc.reason or exc}", None, host, time.time())
    except OSError as exc:
        return Result(False, f"could not connect to {host}:{port} to check its certificate ({exc.__class__.__name__})", None, host, time.time())
    if not der:
        return Result(False, f"{host} gave no certificate", None, host, time.time())
    got = hashlib.sha256(der).hexdigest().upper()
    want = front_door.lan_fingerprint().replace(":", "").upper()
    if want and got != want:
        return Result(False, f"the certificate {host} is serving is not the one the front door made (the fingerprint changed)", None, host, time.time())
    try:
        from cryptography import x509
        end = x509.load_der_x509_certificate(der).not_valid_after_utc.timestamp()
    except Exception:  # noqa: BLE001
        return Result(False, f"the certificate for {host} has no readable end date", None, host, time.time())
    days = int((end - (time.time() if now is None else now)) // 86400)
    if days < 0:
        return Result(False, f"the certificate for {host} expired {-days} day(s) ago", days, host, time.time())
    if days <= LAN_RENEW_DAYS:
        return Result(False, f"the certificate for {host} ends in {days} day(s) and has not been renewed yet", days, host, time.time())
    return Result(True, "", days, host, time.time())


def check(url: str, timeout: float | None = None, now: float | None = None) -> Result:
    """One verified handshake. A failure is a Result with `ok=False` and a short plain-English problem (never a secret)."""
    tgt = target(url)
    if tgt is None:
        return Result(True, host="", checked_at=time.time())
    host, port = tgt
    if host in front_door.lan_hosts():
        return lan_check(host, port, timeout, now)
    timeout = config.TLS_CHECK_TIMEOUT_SECONDS if timeout is None else timeout
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection(_connect_to(host, port), timeout=timeout) as raw:
            with ctx.wrap_socket(raw, server_hostname=host) as tls:
                cert = tls.getpeercert()
    except ssl.SSLCertVerificationError as exc:
        reason = getattr(exc, "verify_message", "") or str(exc)
        return Result(False, f"the certificate for {host} is not trusted: {reason}", None, host, time.time())
    except ssl.SSLError as exc:
        return Result(False, f"TLS to {host} failed: {exc.reason or exc}", None, host, time.time())
    except OSError as exc:
        return Result(False, f"could not connect to {host}:{port} to check its certificate ({exc.__class__.__name__})",
                      None, host, time.time())
    try:
        end = ssl.cert_time_to_seconds(cert["notAfter"])
    except (KeyError, ValueError):
        return Result(False, f"the certificate for {host} has no readable end date", None, host, time.time())
    days = int((end - (time.time() if now is None else now)) // 86400)
    if days < 0:
        return Result(False, f"the certificate for {host} expired {-days} day(s) ago", days, host, time.time())
    if days < config.TLS_MIN_DAYS:
        return Result(False, f"the certificate for {host} expires in {days} day(s) (renewal is not happening)", days, host, time.time())
    return Result(True, "", days, host, time.time())


def run_once() -> Result:
    """Check public_url, remember the result, log loudly on a problem."""
    global _last
    res = check(config.PUBLIC_URL)
    _last = res
    if not res.ok:
        log.error("PUBLIC CERTIFICATE PROBLEM: %s. People will see browser warnings and passkeys may fail. "
                  "(tls_check: %s)", res.problem, config.TLS_CHECK)
    elif res.host:
        log.info("public certificate for %s is valid, %s day(s) left", res.host, res.days_left)
    return res


def banner() -> str | None:
    """Text for the page banner, or None (fine, exempt, switched off, or not checked yet)."""
    if config.TLS_CHECK == "off" or _last is None or _last.ok:
        return None
    return _last.problem


def last() -> Result | None:
    return _last


def enabled() -> bool:
    return config.TLS_CHECK != "off" and target(config.PUBLIC_URL) is not None


def wait_for_good_certificate(grace: float | None = None, sleep=time.sleep) -> Result:
    """`enforce` start-up: retry until the certificate is good or the grace window ends (a fresh install's certificate is
    issued a few seconds after the proxy first starts). A verification failure that is not "nothing there yet" is final
    only after the window too, because the proxy may still be swapping in the real certificate."""
    grace = config.TLS_CHECK_GRACE_SECONDS if grace is None else grace
    deadline = time.monotonic() + grace
    while True:
        res = run_once()
        if res.ok or time.monotonic() >= deadline:
            return res
        sleep(5)


async def startup() -> "asyncio.Task | None":
    """Called from the app's lifespan. Returns the periodic task (to cancel at shutdown) or None."""
    if not enabled():
        return None
    if config.TLS_CHECK == "enforce":
        res = await asyncio.to_thread(wait_for_good_certificate)
        if not res.ok:
            raise RuntimeError(f"refusing to start (tls_check: enforce): {res.problem}. Fix the certificate, or set "
                               f"tls_check: warn in instance.yaml to start anyway.")
    else:
        asyncio.get_running_loop().run_in_executor(None, run_once)      # background: never delays start-up
    if config.TLS_CHECK_INTERVAL_SECONDS <= 0:
        return None
    return asyncio.create_task(_loop())


async def _loop() -> None:
    while True:
        await asyncio.sleep(config.TLS_CHECK_INTERVAL_SECONDS)
        try:
            await asyncio.to_thread(run_once)
        except Exception:      # a check must never take the app down
            log.exception("certificate check failed to run")
