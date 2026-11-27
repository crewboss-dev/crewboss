"""The self-signed certificate for LAN mode (#976, design D2). Used by app/frontdoor_agent.py (the sidecar) and its init CLI.

ECDSA P-256, self-signed, basicConstraints CA:FALSE (critical), keyUsage digitalSignature (critical), extendedKeyUsage
serverAuth, SAN = the LAN addresses and names, 825 days (Apple's ceiling). The KEY is reused on renewal, so the runner pin
(the SHA-256 of the SubjectPublicKeyInfo, see pin26) survives a renewal; only the certificate's fingerprint changes.
Spike S0 (Temp_Files/976-s0-findings.md) proved Python ssl and Node accept exactly this shape as their own trust anchor.
"""
from __future__ import annotations

import base64
import datetime
import hashlib
import ipaddress
import re

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

VALID_DAYS = 825
RENEW_DAYS = 60          # a new certificate is prepared this long before expiry
AUTO_SWITCH_DAYS = 14    # the APP switches it in by itself this long before expiry, and only if every runner is ready (never the sidecar)
_NAME = re.compile(r"^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")


def valid_lan_host(host: str) -> bool:
    """An IPv4 address or a plain host name (office-pc, office-pc.local). Nothing else: this text ends up in a Caddyfile. IPv6 is refused in
    v1 (and with it zone IDs like `fe80::1%eth0`, which could carry newlines or braces into the Caddyfile)."""
    if not isinstance(host, str) or host != host.strip():
        return False
    try:
        return ipaddress.ip_address(host).version == 4
    except ValueError:
        return bool(_NAME.match(host))


def _san(host: str) -> x509.GeneralName:
    try:
        return x509.IPAddress(ipaddress.ip_address(host))
    except ValueError:
        return x509.DNSName(host)


def new_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def key_pem(key) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


def load_key(pem: bytes):
    return serialization.load_pem_private_key(pem, password=None)


def load_cert(pem: bytes | str) -> x509.Certificate:
    return x509.load_pem_x509_certificate(pem.encode() if isinstance(pem, str) else pem)


def generate(hosts: list[str], key=None, name: str = "Crewboss", now: datetime.datetime | None = None,
             days: int = VALID_DAYS) -> tuple[bytes, bytes]:
    """(certificate PEM, key PEM). Pass `key` to renew with the same key."""
    if not hosts or not all(valid_lan_host(h) for h in hosts):
        raise ValueError("hosts must be a non-empty list of addresses or names")
    key = key or new_key()
    now = now or datetime.datetime.now(datetime.timezone.utc)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, (name or "Crewboss")[:60] or "Crewboss")])
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + datetime.timedelta(days=days))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
            .add_extension(x509.KeyUsage(True, False, False, False, False, False, False, False, False), True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False)
            .add_extension(x509.SubjectAlternativeName([_san(h) for h in hosts]), False)
            .sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM), key_pem(key)


def der(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.DER)


def spki_der(cert: x509.Certificate) -> bytes:
    return cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def fingerprint(cert: x509.Certificate) -> str:
    """What people compare: SHA-256 of the certificate DER, colon hex (what browser certificate viewers show)."""
    h = hashlib.sha256(der(cert)).hexdigest().upper()
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2))


def spki_sha256(cert: x509.Certificate) -> bytes:
    return hashlib.sha256(spki_der(cert)).digest()


def pin26(cert: x509.Certificate) -> str:
    """The runner pin (D7): first 26 chars of lower-case unpadded base32 of sha256(SPKI DER), 130 bits."""
    return base64.b32encode(spki_sha256(cert)).decode().rstrip("=").lower()[:26]


def not_after(cert: x509.Certificate) -> datetime.datetime:
    return cert.not_valid_after_utc


def days_left(cert: x509.Certificate, now: datetime.datetime | None = None) -> float:
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return (not_after(cert) - now).total_seconds() / 86400


def hosts_of(cert: x509.Certificate) -> list[str]:
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    return [str(i) for i in san.get_values_for_type(x509.IPAddress)] + san.get_values_for_type(x509.DNSName)


def renew(old_pem: bytes, key_pem_bytes: bytes, hosts: list[str] | None = None, name: str = "Crewboss",
          now: datetime.datetime | None = None) -> bytes:
    """A fresh certificate on the SAME key (so the runner pin is unchanged). Hosts default to the old certificate's."""
    old = load_cert(old_pem)
    return generate(hosts or hosts_of(old), key=load_key(key_pem_bytes), name=name, now=now)[0]
