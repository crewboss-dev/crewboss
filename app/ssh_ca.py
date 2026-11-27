"""Per-account SSH certificate authority (task #367; owner 2026-10-14). Each account gets ONE ed25519 CA, made on first
need. A server the owner adds trusts that CA for exactly one principal, srv-<id> (the line pasted into the login user's
authorized_keys: cert-authority,principals="srv-<id>" …), so every pass signed here opens one server for one hour, and
a session of account A can never be signed a pass for account B's servers (their CAs differ; app/server_access.py and
the API check the account before signing).

The CA's PRIVATE key never leaves this module: it is stored encrypted (Fernet; the key is a 0600 file mounted into the
container, config.SSH_CA_KEY_FILE, read at use time), decrypted only into a 0700 temp dir (tmpfs when there is one) for
one `ssh-keygen -s` call, and the dir is removed straight after. It is never returned, rendered, or logged; the
ssh-keygen error text that may surface carries no key material.

Passes: `-n srv-<id>`, valid from SSH_CERT_BACKDATE_MINUTES ago (clock skew) for SSH_CERT_MINUTES, a unique serial,
key id "<account>:<actor>:srv-<id>:<serial>" (what sshd logs), and `-O clear` then only permit-pty and
permit-port-forwarding: no agent forwarding, no X11, no user rc. Every pass is recorded in ssh_cert_issues.

The deploy executor (task #627, P5; app/deploys.py) signs a different pass: `minutes` sets its validity, and `pty=False`,
`port_forwarding=False`, `force_command=...` leave a certificate that can run ONE command and nothing else.

Jump host (#580): for a server with a "Connect via" server the ONE pass carries BOTH principals (srv-<target> and
srv-<via>), because the jump hop needs a cert valid on the via box too. So a via pass also opens the via server for that
same hour. Direct servers' passes are unchanged (one principal). The pass is recorded once, principal "srv-T,srv-V"."""
from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import struct
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import config
from app.db import get_cursor
from app.repo import BadRequest

ED25519 = "ssh-ed25519"
_KEY_ID_UNSAFE = re.compile(r"[^A-Za-z0-9._@:/+-]")


class CaUnavailable(Exception):
    """The CA can't be used right now (no or wrong encryption key file). The message names no secret."""


# --- helpers ------------------------------------------------------------------------------------------------------

def _fernet():
    from cryptography.fernet import Fernet

    try:
        raw = Path(config.SSH_CA_KEY_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        raw = ""
    if not raw:
        raise CaUnavailable("the dashboard's SSH CA key file isn't set up (CREWBOSS_SSH_CA_KEY_FILE), so it can't sign passes")
    try:
        return Fernet(raw.encode())
    except (ValueError, TypeError):
        raise CaUnavailable("the dashboard's SSH CA key file doesn't hold a valid key") from None


@contextmanager
def workdir():
    """A 0700 directory for one operation's key material, on tmpfs when there is one; always removed."""
    base = "/dev/shm" if os.path.isdir("/dev/shm") and os.access("/dev/shm", os.W_OK) else None
    path = tempfile.mkdtemp(prefix="mdd-ssh-", dir=base)
    os.chmod(path, 0o700)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def write_private(path: str, data: bytes | str) -> None:
    """Create a 0600 file from the first byte (never a moment where it is readable by anyone else)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data.encode() if isinstance(data, str) else data)


def keygen(args: list[str]) -> None:
    """ssh-keygen with an argument list (never a shell). Its stderr never holds key material."""
    res = subprocess.run(["ssh-keygen", *args], capture_output=True, text=True, timeout=30)
    if res.returncode != 0:
        raise RuntimeError(f"ssh-keygen failed: {(res.stderr or '').strip()[:200]}")


def _acting_user() -> int | None:
    from app import access
    return access.current_user_id()


def fingerprint(key_line: str) -> str:
    """'SHA256:…' of an OpenSSH public key line (as ssh-keygen -l prints it)."""
    blob = base64.b64decode(key_line.split()[1])
    return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")


def parse_ed25519(line: str) -> str:
    """'ssh-ed25519 <blob>' from a session's public key line (any comment dropped), or BadRequest. Only ed25519, and
    the blob must decode to a real ed25519 key, so nothing else reaches ssh-keygen."""
    line = line.strip() if isinstance(line, str) else ""
    if not line or len(line) > 400 or any(c in line for c in "\r\n\0"):
        raise BadRequest("public_key must be one 'ssh-ed25519 …' line")
    parts = line.split()
    if not 2 <= len(parts) <= 3 or parts[0] != ED25519:
        raise BadRequest("public_key must be an ssh-ed25519 key ('ssh-ed25519 AAAA… [comment]')")
    try:
        blob = base64.b64decode(parts[1], validate=True)
    except (ValueError, TypeError):
        raise BadRequest("public_key isn't valid base64") from None
    head = struct.pack(">I", len(ED25519)) + ED25519.encode() + struct.pack(">I", 32)
    if len(blob) != len(head) + 32 or not blob.startswith(head):
        raise BadRequest("public_key isn't an ed25519 key")
    return f"{ED25519} {parts[1]}"


def principal(server_id: int) -> str:
    return f"srv-{int(server_id)}"


# --- the CA -------------------------------------------------------------------------------------------------------

def get_ca(account_id: int) -> dict | None:
    """{account_id, public_key, fingerprint, created_at} or None. Never the private key."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT account_id, public_key, fingerprint, created_at FROM ssh_cas WHERE account_id = %s",
                    (account_id,))
        return cur.fetchone()


def ensure_ca(account: dict) -> dict:
    """The account's CA (public side), made on first need. CaUnavailable without the encryption key file."""
    row = get_ca(account["id"])
    if row:
        return row
    box = _fernet()
    with workdir() as d:
        path = os.path.join(d, "ca")
        keygen(["-q", "-t", "ed25519", "-N", "", "-C", f"crewboss:{account['slug']}", "-f", path])
        private = Path(path).read_bytes()
        public = Path(path + ".pub").read_text(encoding="utf-8").strip()
    with get_cursor() as cur:
        # Two first requests at once: the first insert wins, the other's key is simply dropped.
        cur.execute("INSERT INTO ssh_cas (account_id, public_key, private_key_enc, fingerprint) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (account_id) DO NOTHING",
                    (account["id"], public, box.encrypt(private).decode(), fingerprint(public)))
    del private
    return get_ca(account["id"])


def paste_line(ca: dict, account: dict, server_id: int) -> str:
    """The one line the owner pastes into the login user's authorized_keys: trust this account's CA for this server's
    principal only."""
    kind, blob = ca["public_key"].split()[:2]
    return f'cert-authority,principals="{principal(server_id)}" {kind} {blob} crewboss:{account["slug"]}'


def sign(account: dict, server_id: int, public_key: str, actor: str, via_id: int | None = None,
         minutes: int | None = None, force_command: str | None = None, pty: bool = True,
         port_forwarding: bool = True) -> dict:
    """Sign `public_key` (ed25519) as a one-hour pass for srv-<server_id> under the account's CA and record it.
    The caller has already checked that the server (and `via_id`, when set) is this account's. With `via_id` the pass has
    both principals (see the module docstring). Returns {cert, principal, valid_before, serial,
    key_id} — never anything of the CA's private side."""
    key = parse_ed25519(public_key)
    if force_command is not None and (not force_command.strip() or any(c in force_command for c in "\r\n\0")):
        raise BadRequest("force_command must be one line")
    ensure_ca(account)
    box = _fernet()
    prin = principal(server_id) + (f",{principal(via_id)}" if via_id is not None else "")
    now = datetime.now(timezone.utc).replace(microsecond=0)
    valid_after = now - timedelta(minutes=config.SSH_CERT_BACKDATE_MINUTES)
    valid_before = now + timedelta(minutes=minutes if minutes else config.SSH_CERT_MINUTES)
    stamp = "%Y%m%d%H%M%SZ"
    with get_cursor() as cur:
        cur.execute("SELECT private_key_enc FROM ssh_cas WHERE account_id = %s", (account["id"],))
        enc = cur.fetchone()["private_key_enc"]
        cur.execute("SELECT nextval('ssh_cert_serial_seq') AS n")
        serial = int(cur.fetchone()["n"])
        key_id = _KEY_ID_UNSAFE.sub("_", f"{account['slug']}:{actor}:{prin.replace(',', '+')}:{serial}")[:200]
        from cryptography.fernet import InvalidToken
        try:
            ca_private = box.decrypt(enc.encode())
        except InvalidToken:
            raise CaUnavailable("the dashboard's SSH CA key file doesn't open this account's CA") from None
        with workdir() as d:
            ca_path, pub_path = os.path.join(d, "ca"), os.path.join(d, "key.pub")
            write_private(ca_path, ca_private)
            del ca_private
            write_private(pub_path, key + "\n")
            keygen(["-q", "-s", ca_path, "-I", key_id, "-n", prin,
                    "-V", f"{valid_after.strftime(stamp)}:{valid_before.strftime(stamp)}", "-z", str(serial),
                    "-O", "clear", *(["-O", "permit-pty"] if pty else []),
                    *(["-O", "permit-port-forwarding"] if port_forwarding else []),
                    *(["-O", f"force-command={force_command}"] if force_command else []), pub_path])
            cert = Path(os.path.join(d, "key-cert.pub")).read_text(encoding="utf-8").strip()
        cur.execute("INSERT INTO ssh_cert_issues (server_id, account_id, actor, key_id, serial, principal, "
                    "key_fingerprint, valid_before, via_server_id, user_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (server_id, account["id"], actor[:200], key_id, serial, prin, fingerprint(key), valid_before,
                     via_id, _acting_user()))   # the real person behind the pass (#713)
    return {"cert": cert, "principal": prin, "valid_before": valid_before.isoformat(), "serial": serial,
            "key_id": key_id}
