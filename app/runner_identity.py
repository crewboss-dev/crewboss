"""The server's long-lived runner identity key (task #627, change #50): an ed25519 key whose public half a runner is
told (the enrolment code carries its fingerprint, so the runner knows it is talking to THIS dashboard before it sends
anything) and whose private half signs what the server says to runners in later phases.

A 0600 file bind-mounted read-only into the secrets dir (config.RUNNER_IDENTITY_KEY_FILE, docker-compose.yml), made ONCE
on the host before the first deploy with `python -m app.runner_identity keygen [path]` (like `python -m app.push keygen`).
The app only reads it; it never makes one. The private key never leaves
this module: only the public key and its fingerprint are returned.

fp8: the first 8 characters of the lower-case, unpadded BASE32 of the SHA-256 of the RAW 32-byte public key (40 bits,
easy to read aloud and to type; base32 has no '-' or '#', so it sits safely in the enrolment code `host:port#<fp8>-<random>`)."""
from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app import config


class IdentityUnavailable(Exception):
    """The identity key can't be used right now. The message names no secret."""


DEFAULT_HOST_KEY = os.path.join(os.environ.get("CREWBOSS_SECRETS_DIR") or os.path.expanduser("~/.config/crewboss"), "runner-identity-key")


_cache: tuple[str, Ed25519PrivateKey] | None = None


def _load() -> Ed25519PrivateKey:
    """The key, cached per path (the file is read-only at runtime; a deploy restarts the app)."""
    global _cache
    path = config.RUNNER_IDENTITY_KEY_FILE
    if _cache is None or _cache[0] != path:
        _cache = (path, _read())
    return _cache[1]


def _read() -> Ed25519PrivateKey:
    """READ the key, never make it: a key made at runtime would live in the container's writable layer and change on
    every recreate, invalidating every code and every runner's pinned fingerprint. Missing, empty, a directory (docker
    makes one for a missing bind source) or unreadable all read as IdentityUnavailable (a 503 to callers)."""
    path = Path(config.RUNNER_IDENTITY_KEY_FILE)
    hint = "run `python -m app.runner_identity keygen` on the host before deploying"
    try:
        pem = path.read_bytes()
    except FileNotFoundError:
        raise IdentityUnavailable(f"the runner identity key is missing ({hint})") from None
    except IsADirectoryError:
        raise IdentityUnavailable(f"the runner identity key path is a directory ({hint})") from None
    except OSError:
        raise IdentityUnavailable("the runner identity key file can't be read (CREWBOSS_RUNNER_IDENTITY_KEY_FILE)") from None
    if not pem.strip():
        raise IdentityUnavailable(f"the runner identity key file is empty ({hint})")
    try:
        key = serialization.load_pem_private_key(pem, password=None)
    except (ValueError, TypeError):
        raise IdentityUnavailable("the runner identity key file doesn't hold a valid key") from None
    if not isinstance(key, Ed25519PrivateKey):
        raise IdentityUnavailable("the runner identity key file doesn't hold an ed25519 key")
    return key


def keygen(path: str = DEFAULT_HOST_KEY) -> str:
    """Write a new ed25519 identity key (PKCS#8 PEM, 0600 from the first byte) at `path`; returns its fp8. NEVER
    overwrites: an existing file or directory is an error (a new key would orphan every runner's pinned fingerprint)."""
    if os.path.isdir(path):
        raise SystemExit(f"{path} is a directory (docker made it because the key was missing at `compose up`): "
                         "remove it (sudo rmdir) and run this again")
    if os.path.exists(path):
        raise SystemExit(f"{path} already exists; refusing to replace the runner identity key")
    os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(pem)
    return fingerprint8(key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))


def public_key_raw() -> bytes:
    return _load().public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def public_key() -> str:
    """The identity public key: base64 of the raw 32 bytes (the same encoding a runner's own public key is sent in)."""
    return base64.b64encode(public_key_raw()).decode("ascii")


def sign(message: bytes) -> bytes:
    """An ed25519 signature of `message` by the identity key (what the server says to a runner is signed with this)."""
    return _load().sign(bytes(message))


def fp8() -> str:
    return fingerprint8(public_key_raw())


def fingerprint8(raw_public_key: bytes) -> str:
    return base64.b32encode(hashlib.sha256(raw_public_key).digest()).decode("ascii").rstrip("=").lower()[:8]


if __name__ == "__main__":
    import sys
    if len(sys.argv) not in (2, 3) or sys.argv[1] != "keygen":
        print(f"usage: python -m app.runner_identity keygen [path]   (default {DEFAULT_HOST_KEY})", file=sys.stderr)
        sys.exit(2)
    target = sys.argv[2] if len(sys.argv) == 3 else DEFAULT_HOST_KEY
    print(f"created: {target}\nfp8: {keygen(target)}")
