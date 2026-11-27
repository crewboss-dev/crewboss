"""Pinned TLS for a LAN (self-signed) Crewboss server (task #976, design D7).

A LAN install has no certificate authority behind its certificate, so the enrolment code carries a pin of the certificate's public key:
`host:port#<fp8>.<pin26>-<secret>`. `pin26` is the first 26 characters of lower-case unpadded base32 of sha256(SubjectPublicKeyInfo DER).

* At enrolment the runner handshakes with CERT_NONE, reads the peer certificate and compares its pin with the code's in constant time.
  Only then does it send a byte (the identity GET, the enrol POST). It stores the certificate PEM.
* From then on EVERY connection (enrol.py, link.py) uses an SSLContext whose only trust anchors are the stored PEMs, with the hostname checked.
* Claude Code (Node) gets NODE_EXTRA_CA_CERTS (adds to Node's store, so the Anthropic API still verifies); the Gemini MCP gets CB_SERVER_CA_FILE
  (used only for the dashboard connection, never SSL_CERT_FILE, which would break the Google call).
* A runner enrolled against a public (Let's Encrypt) address has no pin, so none of this applies: the system store is used, unchanged."""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import socket
import ssl
import struct
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization

CERT_FILE = "server-cert.pem"
ROTATE_LABEL = b"crewboss-runner-tls-rotate/v1\0"   # domain separation: a signature for any other purpose can never pass as a rotation
MAX_CERTS = 3        # the current one, the one being rotated in, and one spare; older ones are dropped
MAX_PEM = 8192
_PIN_RE = re.compile(r"[a-z2-7]{26}")
_FP8_RE = re.compile(r"[a-z2-7]{8}")


class PinError(Exception):
    pass


@dataclass(frozen=True)
class Code:
    hostport: str
    fp8: str
    pin: str | None     # None for the old format (a Let's Encrypt address)
    secret: str


def parse_code(code: str) -> Code:
    """`host:port#fp8-secret` (old) or `host:port#fp8.pin26-secret` (LAN). An old runner given the new format fails closed on its own
    `len(fp) != 8` check; this one says "update" only for shapes it cannot read at all."""
    code = (code or "").strip()
    bad = PinError("that is not a full enrolment code (host:port#fp8-secret or host:port#fp8.pin-secret); ask for a new one")
    if "#" not in code:
        raise bad
    hostport, _, rest = code.partition("#")
    fp, _, secret = rest.partition("-")
    fp8, dot, pin = fp.partition(".")
    if not hostport or ":" not in hostport or len(fp8) != 8 or not secret or (dot and not _PIN_RE.fullmatch(pin)):
        raise bad
    return Code(hostport, fp8, pin if dot else None, secret)


# ---- certificates ------------------------------------------------------------------------------------------------

def pem_to_der(pem: str) -> bytes:
    if not isinstance(pem, str) or len(pem) > MAX_PEM:
        raise PinError("not a certificate")
    try:
        return ssl.PEM_cert_to_DER_cert(pem.strip() + "\n")
    except (ValueError, TypeError):
        raise PinError("not a certificate") from None


def der_to_pem(der: bytes) -> str:
    return ssl.DER_cert_to_PEM_cert(der)


def spki_der(cert_der: bytes) -> bytes:
    try:
        return x509.load_der_x509_certificate(cert_der).public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    except Exception:  # noqa: BLE001
        raise PinError("not a certificate") from None


def spki_sha256(cert_der: bytes) -> bytes:
    return hashlib.sha256(spki_der(cert_der)).digest()


def pin26_of(cert_der: bytes) -> str:
    return base64.b32encode(spki_sha256(cert_der)).decode().lower().rstrip("=")[:26]


def pin_matches(cert_der: bytes, pin: str) -> bool:
    try:
        mine = pin26_of(cert_der)
    except PinError:
        return False
    return hmac.compare_digest(mine.encode(), pin.encode())


def hostname_in_cert(cert_der: bytes, host: str) -> bool:
    """Is `host` (a DNS name or an IP) among the certificate's subject alternative names?"""
    import ipaddress
    try:
        san = x509.load_der_x509_certificate(cert_der).extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except Exception:  # noqa: BLE001
        return False
    host = host.strip().lower().strip("[]")
    try:
        ip = ipaddress.ip_address(host)
        return ip in san.get_values_for_type(x509.IPAddress)
    except ValueError:
        return host in [n.lower() for n in san.get_values_for_type(x509.DNSName)]


# ---- the connections ---------------------------------------------------------------------------------------------

def split_hostport(hostport: str) -> tuple[str, int]:
    host, _, port = hostport.rpartition(":")
    return host.strip("[]"), int(port)


def fetch_peer_der(hostport: str, timeout: float = 10.0) -> bytes:
    """Handshake with CERT_NONE and return the peer certificate (DER). NOTHING is sent after the handshake: this is how the pin is
    checked BEFORE any request or secret leaves the PC."""
    host, port = split_hostport(hostport)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw, ctx.wrap_socket(raw, server_hostname=host) as tls:
            der = tls.getpeercert(binary_form=True)
    except (OSError, ssl.SSLError) as exc:
        raise PinError(f"could not reach the server: {exc}") from exc
    if not der:
        raise PinError("the server showed no certificate")
    return der


def context_for(pems: list[str]) -> ssl.SSLContext:
    """Trust ONLY these certificates (cadata), and check the hostname. A self-signed leaf is its own anchor."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_verify_locations(cadata="\n".join(p.strip() for p in pems) + "\n")
    partial = getattr(ssl, "VERIFY_X509_PARTIAL_CHAIN", 0)
    if partial:
        ctx.verify_flags |= partial   # harmless for a self-signed leaf (spike S0); it matters only for a CA:TRUE fallback chain
    return ctx


def trust_by_pin(cfg: dict, fetch=None) -> bool:
    """Trust the server's KEY, not one exact certificate (a same-key renewal then just works). Handshake with CERT_NONE, send NOTHING, then
    require (i) sha256(SPKI) equals the stored pin (constant time) and (ii) the certificate's subject alternative names contain the host
    this runner dials. A certificate already stored is trusted as before. A new one that passes is added to cfg["tls_certs"] (capped) and
    True is returned so the caller persists it and refreshes the certificate file agents trust. Raises PinError, having sent nothing."""
    if not is_pinned(cfg):
        return False
    hostport = cfg["server"]
    der = (fetch or fetch_peer_der)(hostport)
    pem = der_to_pem(der)
    if pem in certs_of(cfg):
        return False
    if not pin_matches(der, cfg.get("tls_pin", "")):
        raise PinError("the certificate this server showed has a different key from the one pinned at enrolment; nothing was sent")
    if not hostname_in_cert(der, split_hostport(hostport)[0]):
        raise PinError("the certificate this server showed does not name the address it was reached at; nothing was sent")
    cfg["tls_certs"] = (certs_of(cfg) + [pem])[-MAX_CERTS:]
    return True


def certs_of(cfg: dict | None) -> list[str]:
    raw = (cfg or {}).get("tls_certs")
    return [p for p in raw if isinstance(p, str)] if isinstance(raw, list) else []


def context_from_cfg(cfg: dict | None):
    """The SSLContext for a pinned profile, or None (use the system store) when the profile has no pin."""
    pems = certs_of(cfg)
    if not (cfg or {}).get("tls_pin") or not pems:
        return None
    return context_for(pems)


def is_pinned(cfg: dict | None) -> bool:
    return bool((cfg or {}).get("tls_pin") and certs_of(cfg))


# ---- the certificate file (for Node and the Gemini MCP) ----------------------------------------------------------

def cert_file(state: Path) -> Path:
    return Path(state) / CERT_FILE


def write_cert_file(state: Path, pems: list[str]) -> Path:
    from . import store
    path = cert_file(state)
    store.write_private(path, "\n".join(p.strip() for p in pems) + "\n")
    return path


def sync_cert_file(state: Path, cfg: dict | None) -> None:
    """Make the file match the config (startup, and after a rotation). Removed when the profile is not pinned."""
    if is_pinned(cfg):
        write_cert_file(state, certs_of(cfg))
    else:
        try:
            cert_file(state).unlink()
        except OSError:
            pass


def pinned_ca_file() -> str | None:
    """The active profile's certificate file when it is pinned and the file exists, else None. Never raises (it runs while building an
    agent's environment)."""
    try:
        from . import store
        cfg = store.Config().load()
        if not is_pinned(cfg):
            return None
        path = cert_file(store.state_dir())
        return str(path) if path.is_file() else None
    except Exception:  # noqa: BLE001
        return None


def node_ca_file(user_value: str | None) -> str | None:
    """What NODE_EXTRA_CA_CERTS should be for an agent: the pinned certificate file, or, when the user already set the variable, a
    COMBINED file (their file's content plus ours) so their own extra CAs keep working. Never raises; None = leave the variable alone."""
    ours = pinned_ca_file()
    if not ours:
        return None
    if not user_value or Path(user_value) == Path(ours):
        return ours
    try:
        theirs = Path(user_value).read_text(encoding="utf-8", errors="replace")
        mine = Path(ours).read_text(encoding="utf-8")
        combined = Path(ours).with_name("server-cert-combined.pem")
        from . import store
        store.write_private(combined, theirs.rstrip("\n") + "\n" + mine)
        return str(combined)
    except Exception:  # noqa: BLE001 - an unreadable user file: use ours alone rather than none (a LAN server must still verify)
        return ours


# ---- rotation ----------------------------------------------------------------------------------------------------

def rotate_message(new_der: bytes, old_spki_sha256: bytes, host: str) -> bytes:
    """What the server signs with its identity key (see app/runner_ws.py)."""
    h = host.strip().lower().encode("utf-8")
    return (ROTATE_LABEL + spki_sha256(new_der) + hashlib.sha256(new_der).digest() + old_spki_sha256
            + struct.pack(">H", len(h)) + h)
