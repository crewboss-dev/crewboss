"""Enrolment: `crewboss-runner enrol <host:port#fp8-secret>`.

The code names the server's identity key by fingerprint. The runner fetches the server's public key, computes the fingerprint
ITSELF (it never trusts the fp8 the server states) and aborts unless it equals the code's, BEFORE sending the secret or its
public key. It then generates the device ed25519 key, enrols, and stores the key + token in the secret store."""
from __future__ import annotations

import base64
import hashlib
import json
import socket
import urllib.error
import urllib.request
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from . import settings, store, tlspin


class EnrolError(Exception):
    pass


def fp8_of(raw_pubkey: bytes) -> str:
    """Same rule as the server (app/runner_identity.py): first 8 chars of lower-case unpadded base32 of sha256(raw key)."""
    return base64.b32encode(hashlib.sha256(raw_pubkey).digest()).decode().lower().rstrip("=")[:8]


def parse_code(code: str) -> tuple[str, str, str]:
    """-> (host:port, fp8, secret). The whole code is required: without the fingerprint nothing can be pinned."""
    c = parse_full(code)
    return c.hostport, c.fp8, c.secret


def parse_full(code: str) -> tlspin.Code:
    """Both formats: `host:port#fp8-secret` and, for a LAN server, `host:port#fp8.pin26-secret` (the pin is None in the old one)."""
    try:
        return tlspin.parse_code(code)
    except tlspin.PinError as exc:
        raise EnrolError(str(exc)) from None


def base_url(hostport: str, scheme: str = "https") -> str:
    return f"{scheme}://{hostport}"


def _http(method: str, url: str, body: dict | None = None, timeout: float = 15.0, context=None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:   # TLS verified: system store, or only the pinned cert
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"{}")
        except ValueError:
            return exc.code, {}
    except (urllib.error.URLError, socket.timeout, OSError) as exc:
        raise EnrolError(f"could not reach the server: {getattr(exc, 'reason', exc)}") from exc


def enrol(code: str, name: str, platform: str, secrets: store.Secrets, config: store.Config,
          scheme: str = "https", http=_http, fetch_cert=tlspin.fetch_peer_der, state: Path | None = None) -> dict:
    parsed = parse_full(code)
    hostport, want_fp, pin = parsed.hostport, parsed.fp8, parsed.pin
    base = base_url(hostport, scheme)
    pem = ""
    if pin:   # a LAN server: its certificate is self-signed, so the code carries the pin. Check it BEFORE a single byte is sent.
        if scheme != "https":
            raise EnrolError("a code with a certificate pin needs https")
        try:
            der = fetch_cert(hostport)
        except tlspin.PinError as exc:
            raise EnrolError(str(exc)) from None
        if not tlspin.pin_matches(der, pin):
            raise EnrolError("ABORTED: the certificate this server showed is not the one in the code. This is not the dashboard the code "
                             "was made for (or someone is in the way); nothing was sent.")
        pem = tlspin.der_to_pem(der)
        if http is _http:
            ctx = tlspin.context_for([pem])
            http = lambda m, u, b=None: _http(m, u, b, context=ctx)   # noqa: E731 - every later request trusts only this certificate
    status, ident = http("GET", base + "/runner/identity")
    if status != 200 or not isinstance(ident.get("public_key"), str):
        raise EnrolError(f"the server did not give its identity (HTTP {status})")
    try:
        server_pub = base64.b64decode(ident["public_key"], validate=True)
    except ValueError:
        raise EnrolError("the server's identity key is malformed") from None
    got = fp8_of(server_pub) if len(server_pub) == 32 else ""
    if got != want_fp:
        raise EnrolError(f"ABORTED: the server's identity key fingerprint ({got or '?'}) does not match the code's "
                         f"({want_fp}). This is not the dashboard the code was made for; nothing was sent.")
    key = Ed25519PrivateKey.generate()
    pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    status, res = http("POST", base + "/runner/enrol", {
        "code": code.strip(), "pubkey": base64.b64encode(pub).decode(), "name": name, "platform": platform})
    if status != 200 or "device_token" not in res:
        raise EnrolError(f"the server refused the enrolment (HTTP {status}): {res.get('detail', 'no reason given')}")
    secrets.set("device_key", base64.b64encode(key.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())).decode())
    secrets.set("device_token", res["device_token"])
    cfg = {"server": hostport, "scheme": scheme, "fp8": want_fp, "server_public_key": ident["public_key"],
           "device_id": res["device_id"], "mode": settings.normalise_mode(res.get("mode")), "name": name, "expires_at": res.get("expires_at", "")}
    if pin:
        cfg["tls_pin"], cfg["tls_certs"] = pin, [pem]
        if state is not None:
            tlspin.write_cert_file(state, [pem])
    config.save(cfg)
    return cfg
