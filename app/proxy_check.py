"""Notice a reverse proxy the app does not trust (R1).

trusted_proxies defaults to loopback only. An install whose proxy reaches the app from another address (a docker gateway, a mesh
peer) then sees every client as that one address, so every client shares one rate-limit bucket. The first request that arrives from
an untrusted, non-loopback peer carrying X-Forwarded-For is the tell: log it once per process and keep the address for the page
banner (templating.proxy_banner). Nothing here changes who is believed."""
import ipaddress
import logging

from app import local_auth

log = logging.getLogger("crewboss.proxy_check")

_peer: str | None = None


def observe(peer: str, forwarded: str) -> None:
    """Called per request with the socket peer and the X-Forwarded-For header. Cheap: returns at once after the first hit."""
    global _peer
    if _peer is not None or not peer or not (forwarded or "").strip():
        return
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        return
    if addr.is_loopback or local_auth._trusted_peer(peer):
        return
    _peer = peer[:100]
    log.warning("PROXY NOT TRUSTED: requests arrive from %s carrying X-Forwarded-For, but that address is not in trusted_proxies, so "
                "every client shares one rate-limit bucket. If that address is YOUR reverse proxy, add it to trusted_proxies in instance.yaml (or run ./crewboss up). "
                "If it is not, investigate: a client may be reaching the app directly.", _peer)


def banner() -> str | None:
    if _peer is None:
        return None
    return (f"requests reach Crewboss from {_peer}, which is not in trusted_proxies, so sign-in rate limits treat everyone as one "
            f"client. Add that address to trusted_proxies in instance.yaml and restart, but only if it is YOUR reverse proxy. "
            f"If it is not, investigate: a client may be reaching the app directly")


def reset() -> None:     # tests
    global _peer
    _peer = None
