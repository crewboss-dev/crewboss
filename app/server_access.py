"""Servers the dashboard can reach (task #367; owner 2026-10-14): "I add the IP address and it gives me the public key.
All I do is log into the server, put the public key in, and now Dashboard has access to that server… I just set up the
user myself, set up sudo, then pop in the public key."

The owner adds a server on the account's Servers page (name, address, port, login user; the account is the page's).
The page shows the one line to paste (the account CA, trusted for this server's principal only: app/ssh_ca.py) and
copy-paste setup commands per OS family. "Check access" then connects FROM THE DASHBOARD with a pass it signs itself
(actor 'dashboard-check') and a throw-away key, read-only: hostname, /etc/os-release, uname -r and `sudo -n true`.
The first good check PINS the host key (HostKeyAlias crewboss-srv-<id>); every later check — and every session's
connection through the instance's SSH helper — uses StrictHostKeyChecking=yes against it, and a changed key fails loudly.
Only a commissioned server (a good check, a pinned key) can be signed a pass for a session (POST /api/servers/{id}/
ssh-cert, the token's own account only).

Every value that reaches ssh's argv is checked here (and again by the table's CHECKs): plain host names or IP
addresses, POSIX user names, never a leading '-', no spaces or shell characters; ssh always gets `-l user -p port --
address`. ssh runs from an argument list (never a shell) with a timeout.

Direct reach is the norm. The SPECIAL CASE (task #580; a server that is only reachable by jumping through another one,
for updates and reboots; a special case, not the norm) is an optional
"Connect via" server (commissioned_servers.via_server_id): another commissioned server of the SAME account, never itself,
never one that has its own via (no chains), and commissioned. The target's address may then be a private/VPC IP such as
10.0.0.8. check() and every session connection hop through the via server with OpenSSH -J (ProxyJump): the via server's
own pinned host key, alias (crewboss-srv-<via>), login user and port, StrictHostKeyChecking=yes on BOTH hops (the
target's first check still pins with accept-new). The jump hop's options travel in a throw-away ssh config file (-J
passes only -F to the jump process), never in a shell. The one pass is signed with BOTH principals (srv-<target> and
srv-<via>, one hour, recorded in ssh_cert_issues): A VIA PASS ALSO OPENS THE VIA BOX FOR THAT HOUR. Direct servers are
unchanged. The paste line goes on the TARGET; the via server must already trust its own line. The via server's sshd must
allow TCP forwarding (the pass carries permit-port-forwarding)."""
from __future__ import annotations

import ipaddress
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app import config, repo, ssh_ca
from app.db import get_cursor
from app.repo import BadRequest, Conflict, Forbidden, NotFound

LOCAL = ZoneInfo(config.LOCAL_TZ)
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_LABEL = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
STATUSES = ("pending", "commissioned", "failed")
VIA_HOST_KEY_CHANGED = ("the jump server's host key changed — not connecting. Remove the jump server here and add it "
                        "again if it was rebuilt.")
HOST_KEY_CHANGED = ("host key changed — not connecting. The server presented a different host key from the one pinned "
                    "at its first check. If the server was rebuilt, remove it here and add it again.")
_COLUMNS = ("id, account_id, name, address, port, login_user, status, host_key, host_key_fingerprint, os_name, "
            "os_version, hostname, kernel, sudo_nopasswd, commissioned_at, last_check_at, last_check_error, created_at, "
            "created_by, via_server_id, last_probe_at, probe_ok, probe_misses, unreachable_since, probe_error, "
            "sessions_may_request_passes")


# --- the four states (#810) --------------------------------------------------------------------------------------
# The Machines card shows ONE of these for a server that receives deploys; it is derived here from the check and the probe, never stored.
#   needs_setup   new, or the last check failed (this includes a changed host key and a publickey refusal), or no answer for 60 minutes
#   ready         a good check, a pinned key, and the last probe (if any) answered
#   not_answering two probes in a row missed (about 10 minutes); setup stays folded: a reboot is not a rebuild
# ("Checking" is the moment a check is in flight: the page shows it while the request runs, nothing is stored.)
NEEDS_SETUP, READY, NOT_ANSWERING = "needs_setup", "ready", "not_answering"
STATES = (NEEDS_SETUP, READY, NOT_ANSWERING)
NOT_ANSWERING_MISSES = 2
NEEDS_SETUP_AFTER = timedelta(minutes=60)
# What a probe may ask for: the key types ssh itself knows, so a stored value can never smuggle an option into ssh-keyscan's argv.
_KEY_TYPE = re.compile(r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)|sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com)$")


def state(row: dict, now: datetime | None = None) -> str:
    """The server's state (see above). A row never checked, or whose check failed, needs setup however recently a probe answered."""
    if row["status"] != "commissioned" or not row["host_key"]:
        return NEEDS_SETUP
    now = now or datetime.now(timezone.utc)
    since = row.get("unreachable_since")
    if since and now - since >= NEEDS_SETUP_AFTER:
        return NEEDS_SETUP
    if (row.get("probe_misses") or 0) >= NOT_ANSWERING_MISSES:
        return NOT_ANSWERING
    return READY


def state_reason(row: dict, now: datetime | None = None) -> str:
    """Why a server is not Ready, in a sentence (the executor's refusal and the card's line)."""
    st = state(row, now)
    if st == READY:
        return ""
    if st == NOT_ANSWERING:
        return f"{row['name']} has not answered for {minutes_unreachable(row, now)} min; use Check now"
    if row["status"] == "failed":
        return f"{row['name']} needs setup: {row['last_check_error'] or 'its last check failed'}"
    if row["status"] != "commissioned" or not row["host_key"]:
        return f"{row['name']} needs setup: paste its line on the server and run Check access"
    return f"{row['name']} needs setup: no answer for {minutes_unreachable(row, now)} min; check access again"


def minutes_unreachable(row: dict, now: datetime | None = None) -> int:
    since = row.get("unreachable_since")
    return max(0, int(((now or datetime.now(timezone.utc)) - since).total_seconds() // 60)) if since else 0


# --- validation ---------------------------------------------------------------------------------------------------

def clean_name(name: str) -> str:
    name = (name or "").strip()
    if not NAME_RE.match(name) or name.isdigit():
        raise BadRequest("Name: letters, digits, '.', '_' or '-' (up to 63), starting with a letter or digit, and not "
                         "only digits — e.g. web02")
    return name


def clean_address(address: str) -> str:
    """A plain host name or an IPv4/IPv6 address, normalised; anything ssh could read as an option is refused."""
    address = (address or "").strip()
    try:
        return str(ipaddress.ip_address(address))
    except ValueError:
        pass
    host = address.lower().rstrip(".")
    if not host or len(host) > 253 or not all(_LABEL.match(label) for label in host.split(".")) \
            or host.replace(".", "").isdigit():
        raise BadRequest("Address: an IP address or a plain host name (e.g. 203.0.113.10 or web02.example.com), "
                         "no spaces, ports or other characters")
    return host


def clean_port(port) -> int:
    try:
        value = int(str(port).strip() or 22)
    except ValueError:
        raise BadRequest("Port: a number from 1 to 65535") from None
    if not 1 <= value <= 65535:
        raise BadRequest("Port: a number from 1 to 65535")
    return value


def clean_user(user: str) -> str:
    user = (user or "").strip()
    if not USER_RE.match(user):
        raise BadRequest("Login user: a Linux user name — lower-case letters, digits, '_' or '-', starting with a "
                         "letter or '_' (e.g. acme)")
    if user == "root":
        raise BadRequest("Login user: a named user with sudo, not root (the setup commands create it)")
    return user


# --- records ------------------------------------------------------------------------------------------------------

def _check_via(cur, account_id: int, vid: int, server_id: int | None) -> None:
    """The via rules, read on `cur`'s connection (set_via calls it under row locks)."""
    cur.execute(f"SELECT {_COLUMNS} FROM commissioned_servers WHERE id = %s", (vid,))
    via = cur.fetchone()
    if via is None or via["account_id"] != account_id:
        raise BadRequest("Connect via: pick one of this account's servers")
    if via["via_server_id"] is not None:
        raise BadRequest(f"Connect via: {via['name']} itself connects via another server; jump hosts can't chain")
    if via["status"] != "commissioned" or not via["host_key"]:
        raise BadRequest(f"Connect via: {via['name']} isn't commissioned yet — paste its line and run Check "
                         "access on it first")
    if server_id is not None:
        cur.execute("SELECT name FROM commissioned_servers WHERE via_server_id = %s ORDER BY id LIMIT 1",
                    (int(server_id),))
        other = cur.fetchone()
        if other:
            raise BadRequest(f"Connect via: {other['name']} connects through this server, so it can't have a "
                             "jump host of its own (no chains)")


def clean_via(account_id: int, via_id, server_id: int | None = None) -> int | None:
    """The id of the jump server, or None for a direct server (empty / None). BadRequest unless it is another
    COMMISSIONED server of the same account that has no via of its own (no chains) and — when `server_id` already
    exists — is not that server itself nor one that other servers already jump through (that would make a chain)."""
    if via_id is None or str(via_id).strip() == "":
        return None
    try:
        vid = int(str(via_id).strip())
    except ValueError:
        raise BadRequest("Connect via: pick one of this account's servers") from None
    if server_id is not None and vid == int(server_id):
        raise BadRequest("Connect via: a server can't connect via itself")
    with get_cursor(commit=False) as cur:
        _check_via(cur, account_id, vid, server_id)
    return vid


def set_via(account_id: int, server_id: int, via_id) -> dict:
    """Set or clear (empty) the server's jump host. Both rows are locked (FOR UPDATE, in id order, so two racing calls
    can't deadlock) in one transaction and the rules re-run under the lock, so X via Y and Y via X can't both land."""
    get(account_id, server_id)
    if via_id is None or str(via_id).strip() == "":
        with get_cursor() as cur:
            cur.execute("UPDATE commissioned_servers SET via_server_id = NULL WHERE id = %s AND account_id = %s",
                        (server_id, account_id))
        return get(account_id, server_id)
    try:
        vid = int(str(via_id).strip())
    except ValueError:
        raise BadRequest("Connect via: pick one of this account's servers") from None
    if vid == int(server_id):
        raise BadRequest("Connect via: a server can't connect via itself")
    with get_cursor() as cur:
        cur.execute("SELECT id FROM commissioned_servers WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
                    ([int(server_id), vid],))
        _check_via(cur, account_id, vid, int(server_id))
        cur.execute("UPDATE commissioned_servers SET via_server_id = %s WHERE id = %s AND account_id = %s",
                    (vid, server_id, account_id))
    return get(account_id, server_id)


def set_session_passes(account_id: int, server_id: int, on: bool, actor: str = "owner") -> dict:
    """Turn the per-server switch on or off (callers: the owner's page route; turning ON needs a fresh passkey tap there)."""
    row = get(account_id, server_id)
    with get_cursor() as cur:
        cur.execute("UPDATE commissioned_servers SET sessions_may_request_passes = %s WHERE id = %s AND account_id = %s",
                    (bool(on), server_id, account_id))
        repo.record_activity(cur, account_id, actor, "allowed" if on else "stopped allowing", "server", server_id,
                             f"{'Allowed' if on else 'Stopped allowing'} AI sessions to open a one-hour shell on {row['name']}")
    return get(account_id, server_id)


def get_via(row: dict) -> dict | None:
    """The jump server's row for `row`, or None for a direct server."""
    if row.get("via_server_id") is None:
        return None
    return get(row["account_id"], row["via_server_id"])


def add(account: dict, name: str, address: str, port, user: str, actor: str, via_server_id=None) -> dict:
    """Record a server for `account` (pending until a good check), optionally behind a jump host (`via_server_id`, see
    clean_via). Makes the account's CA if it has none yet, when the encryption key is there (else the page says so, and
    the line appears once it is)."""
    name, address, port, user = clean_name(name), clean_address(address), clean_port(port), clean_user(user)
    via = clean_via(account["id"], via_server_id)
    with get_cursor() as cur:
        cur.execute("SELECT 1 FROM commissioned_servers WHERE account_id = %s AND lower(name) = lower(%s)",
                    (account["id"], name))
        if cur.fetchone():
            raise Conflict(f"{account['name']} already has a server called {name}")
        cur.execute(f"INSERT INTO commissioned_servers (account_id, name, address, port, login_user, created_by, "
                    f"via_server_id) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING {_COLUMNS}",
                    (account["id"], name, address, port, user, actor, via))
        row = cur.fetchone()
    try:
        ssh_ca.ensure_ca(account)
    except ssh_ca.CaUnavailable:
        pass
    return row


def is_ready(row: dict) -> bool:
    """May code be deployed to this server? Its state is Ready (a good check, a pinned key, the last probe answered), and it is reached
    directly (a jump hop is access only: the executor refuses deploys through one). The pickers (a project's "Add a place to
    deploy") list only these, and the executor refuses anything else."""
    return state(row) == READY and not row.get("via_server_id")


def list_for(account_ids) -> list[dict]:
    with get_cursor(commit=False) as cur:
        cur.execute(f"SELECT {_COLUMNS} FROM commissioned_servers WHERE account_id = ANY(%s) ORDER BY lower(name), id",
                    (list(account_ids),))
        return cur.fetchall()


def get(account_id: int, server_id: int) -> dict:
    """The server, if it is this account's — another account's id is NotFound exactly like a missing one."""
    with get_cursor(commit=False) as cur:
        cur.execute(f"SELECT {_COLUMNS} FROM commissioned_servers WHERE id = %s AND account_id = %s",
                    (server_id, account_id))
        row = cur.fetchone()
    if not row:
        raise NotFound(f"server {server_id} not found")
    return row


def remove(account_id: int, server_id: int) -> dict:
    """Forget the server. The pasted line still trusts the account CA for srv-<id> until the owner deletes it from
    authorized_keys; no new pass is ever signed for a removed id (ids are never reused)."""
    row = get(account_id, server_id)
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT name FROM commissioned_servers WHERE via_server_id = %s ORDER BY id", (server_id,))
        users = [r["name"] for r in cur.fetchall()]
    if users:
        raise Conflict(f"{', '.join(users)} connect(s) via {row['name']}: remove those, or give them another way in, first")
    with get_cursor() as cur:
        cur.execute("DELETE FROM commissioned_servers WHERE id = %s AND account_id = %s", (server_id, account_id))
    return row


LEGACY_ALIAS_PREFIX = "mallen-dash-srv-"   # TODO(#589 compat): the alias before the rename; host keys pinned then still carry it


def alias(server_id: int) -> str:
    """The name the pinned host key is filed under, so it never depends on (or collides by) the address."""
    return f"crewboss-srv-{int(server_id)}"


def known_hosts(row: dict) -> str:
    """The pinned host key lines, filed under the CURRENT alias. A key pinned before the rename (task #589) is stored under
    the old alias ('mallen-dash-srv-<id>'); only the first field of each line (the alias) is rewritten, never the key."""
    a = alias(row["id"])
    return "\n".join(" ".join([a, *ln.split(None, 1)[1:]]) for ln in (row["host_key"] or "").splitlines() if ln.strip())


def public_view(row: dict, via_name: str | None = None) -> dict:
    """What a session's GET /api/servers shows: no host key line, no CA material. `via` is the jump server's name."""
    return {"via_server_id": row.get("via_server_id"), "via": via_name, "id": row["id"], "name": row["name"], "address": row["address"], "port": row["port"],
            "user": row["login_user"], "status": row["status"], "hostname": row["hostname"],
            "os": " ".join(x for x in (row["os_name"], row["os_version"]) if x) or None,
            "kernel": row["kernel"], "sudo_nopasswd": row["sudo_nopasswd"],
            "host_key_fingerprint": row["host_key_fingerprint"],
            "commissioned_at": row["commissioned_at"].isoformat() if row["commissioned_at"] else None,
            "last_check_at": row["last_check_at"].isoformat() if row["last_check_at"] else None,
            "last_check_error": row["last_check_error"]}


# --- a session's pass ---------------------------------------------------------------------------------------------

def issue_pass(account: dict, server_id: int, public_key: str, actor: str) -> dict:
    """POST /api/servers/{id}/ssh-cert: a one-hour pass for one of `account`'s COMMISSIONED servers. NotFound for
    another account's or a missing id; Conflict while it isn't commissioned (no pinned host key to connect safely)."""
    row = get(account["id"], server_id)
    if not row["sessions_may_request_passes"]:   # F3: a session token needs the per-server switch (off by default; owner, step-up)
        raise Forbidden(f"server {server_id} doesn't let AI sessions open a shell: the owner can allow it in that server's "
                        "Access and setup on the Machines page")
    if row["status"] == "failed":
        raise Conflict(f"server {server_id}'s last check failed ({row['last_check_error'] or 'no reason recorded'}); "
                       "no passes until a Check access on the Servers page succeeds")
    if row["status"] != "commissioned" or not row["host_key"]:
        raise Conflict(f"server {server_id} isn't commissioned yet: the owner pastes its line and runs Check access "
                       "on the Servers page first")
    via = get_via(row)
    if via is not None and (via["status"] != "commissioned" or not via["host_key"]):
        raise Conflict(f"server {server_id} connects via {via['name']}, which isn't commissioned (its last check "
                       "failed or it has no pinned host key); fix that on the Servers page first")
    if via is not None and not via["sessions_may_request_passes"]:   # the pass also opens the jump server for that hour
        raise Forbidden(f"server {server_id} connects via {via['name']}, which doesn't let AI sessions open a shell: "
                        "the owner can allow it there on the Machines page")
    # A via pass has BOTH principals, so it also opens the via box for that hour (module docstring).
    signed = ssh_ca.sign(account, row["id"], public_key, actor, via["id"] if via else None)
    out = {"cert": signed["cert"], "principal": signed["principal"], "valid_before": signed["valid_before"],
           "known_hosts_line": known_hosts(row), "host_key_alias": alias(row["id"]), "address": row["address"],
           "port": row["port"], "user": row["login_user"], "id": row["id"], "name": row["name"]}
    if via is not None:
        out["via"] = {"id": via["id"], "name": via["name"], "address": via["address"], "port": via["port"],
                      "user": via["login_user"], "host_key_alias": alias(via["id"]),
                      "known_hosts_line": known_hosts(via)}
    return out


# --- check access -------------------------------------------------------------------------------------------------

CHECK_SCRIPT = ("echo @@host; hostname; echo @@os; cat /etc/os-release 2>/dev/null; echo @@kernel; uname -r; "
                "echo @@sudo; if sudo -n true >/dev/null 2>&1; then echo yes; else echo no; fi; echo @@end")


def _run(argv: list[str], timeout: int) -> subprocess.CompletedProcess:
    """The one place ssh is started (the tests replace it). An argument list, never a shell."""
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)


def jump_config(via: dict, key: str, cert: str, known_hosts: str) -> str:
    """The throw-away ssh config for the jump hop (`-J` hands the jump process only -F, so its options live here): the
    via server's own address, port, login user, pinned host key under its own alias, the same pass, and
    StrictHostKeyChecking=yes. Every value is validated; the only Host block is the via alias."""
    for path in (key, cert, known_hosts):
        if not path or any(c in path for c in " \t\r\n\"'#"):
            raise BadRequest("unexpected character in a key path")
    a = alias(via["id"])
    return "\n".join([
        f"Host {a}", f"  HostName {clean_address(via['address'])}", f"  Port {clean_port(via['port'])}",
        f"  User {clean_user(via['login_user'])}", f"  IdentityFile {key}", f"  CertificateFile {cert}",
        "  IdentitiesOnly yes", "  IdentityAgent none", "  BatchMode yes", "  PasswordAuthentication no",
        "  KbdInteractiveAuthentication no", f"  ConnectTimeout {config.SSH_CONNECT_TIMEOUT}",
        f"  UserKnownHostsFile {known_hosts}", "  GlobalKnownHostsFile /dev/null", f"  HostKeyAlias {a}",
        "  UpdateHostKeys no", "  CheckHostIP no", "  StrictHostKeyChecking yes", "  ForwardAgent no",
        "  ForwardX11 no", "  LogLevel ERROR", ""])


def ssh_argv(row: dict, key: str, cert: str, known_hosts: str, pinned: bool, command: str,
             via: dict | None = None, jump_cfg: str | None = None) -> list[str]:
    """ssh's argument list. With `via` (the jump server's row) and `jump_cfg` (the file jump_config() wrote) it adds
    `-F <that file> -J <via alias>` in place of `-F /dev/null`; the target keeps its own strict/pinned options."""
    if (via is None) != (jump_cfg is None):
        raise BadRequest("a jump host needs its config file")
    return ["ssh", "-F", jump_cfg or "/dev/null", *(["-J", alias(via["id"])] if via else []),
            "-i", key, "-o", f"CertificateFile={cert}", "-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
            "-o", "BatchMode=yes", "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
            "-o", f"ConnectTimeout={config.SSH_CONNECT_TIMEOUT}",
            "-o", f"UserKnownHostsFile={known_hosts}", "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", f"HostKeyAlias={alias(row['id'])}", "-o", "UpdateHostKeys=no", "-o", "CheckHostIP=no",
            "-o", f"StrictHostKeyChecking={'yes' if pinned else 'accept-new'}",
            "-o", "ForwardAgent=no", "-o", "ForwardX11=no", "-o", "ClearAllForwardings=yes", "-o", "LogLevel=ERROR",
            "-l", clean_user(row["login_user"]), "-p", str(clean_port(row["port"])),
            "--", clean_address(row["address"]), command]


def _parse(out: str) -> dict:
    sections, cur = {}, None
    for line in out.splitlines():
        if line.startswith("@@") and line[2:] in ("host", "os", "kernel", "sudo", "end"):
            cur = line[2:]
            sections[cur] = []
        elif cur:
            sections[cur].append(line)
    osr = {}
    for line in sections.get("os", []):
        k, sep, v = line.partition("=")
        if sep:
            osr[k.strip()] = v.strip().strip('"').strip("'")
    first = lambda key: (sections.get(key) or [""])[0].strip()[:120] or None  # noqa: E731
    return {"complete": "end" in sections, "hostname": first("host"), "kernel": first("kernel"),
            "os_name": (osr.get("NAME") or "")[:120] or None, "os_version": (osr.get("VERSION_ID") or "")[:60] or None,
            "sudo_nopasswd": first("sudo") == "yes"}


def _failure(row: dict, res: subprocess.CompletedProcess, pinned: bool, via: dict | None = None) -> str:
    err = (res.stderr or "").strip()
    last = err.splitlines()[-1][:200] if err else ""
    if via and alias(via["id"]) in err and ("Host key verification failed" in err or "IDENTIFICATION HAS CHANGED" in err
                                            or "host key" in err.lower()):
        return VIA_HOST_KEY_CHANGED
    if pinned and ("Host key verification failed" in err or "IDENTIFICATION HAS CHANGED" in err):
        return HOST_KEY_CHANGED
    if "Permission denied" in err:
        if via:
            return (f"a server refused the dashboard's pass (the jump server {via['name']} or {row['name']}). Is each "
                    "one's own line in its login user's authorized_keys, mode 600?")
        return (f"the server refused the dashboard's pass. Is the line in ~{row['login_user']}/.ssh/authorized_keys, "
                f"owned by {row['login_user']}, mode 600 (and ~/.ssh 700)?")
    if any(s in err for s in ("timed out", "No route to host", "Connection refused", "Could not resolve",
                              "Name or service not known", "Network is unreachable")):
        return (f"can't reach {row['address']}:{row['port']} " + (f"through {via['name']} " if via else "from the dashboard ")
                + f"({last})")
    return f"ssh failed (exit {res.returncode}){': ' + last if last else ''}"


def _record(server_id: int, ok: bool, error: str | None = None, found: dict | None = None,
            host_key: str | None = None, host_key_fingerprint: str | None = None) -> None:
    with get_cursor() as cur:
        if ok:
            cur.execute("UPDATE commissioned_servers SET status = 'commissioned', last_check_at = now(), "
                        "last_check_error = NULL, commissioned_at = COALESCE(commissioned_at, now()), "
                        "probe_misses = 0, unreachable_since = NULL, probe_error = '', "
                        "hostname = %s, os_name = %s, os_version = %s, kernel = %s, sudo_nopasswd = %s, "
                        "host_key = COALESCE(host_key, %s), host_key_fingerprint = COALESCE(host_key_fingerprint, %s) "
                        "WHERE id = %s",
                        (found["hostname"], found["os_name"], found["os_version"], found["kernel"],
                         found["sudo_nopasswd"], host_key, host_key_fingerprint, server_id))
        else:
            cur.execute("UPDATE commissioned_servers SET status = 'failed', last_check_at = now(), "
                        "last_check_error = %s WHERE id = %s", (error[:500], server_id))


def _key_fingerprint(known_hosts: str) -> str:
    line = known_hosts.splitlines()[0].split()
    kind = line[1].removeprefix("ssh-").removeprefix("ecdsa-sha2-").upper()
    return f"{ssh_ca.fingerprint(' '.join(line[1:3]))} ({kind})"


def check(account: dict, server_id: int) -> dict:
    """Check access to one of `account`'s servers, read-only, and record the result. Returns the updated row."""
    row = get(account["id"], server_id)
    pinned = bool(row["host_key"])
    via = get_via(row)
    if via is not None and (via["status"] != "commissioned" or not via["host_key"]):
        _record(row["id"], False, f"the jump server {via['name']} isn't commissioned: check access to it first")
        return get(account["id"], server_id)
    try:
        with ssh_ca.workdir() as d:
            key, kh = os.path.join(d, "key"), os.path.join(d, "known_hosts")
            ssh_ca.keygen(["-q", "-t", "ed25519", "-N", "", "-C", "dashboard-check", "-f", key])
            signed = ssh_ca.sign(account, row["id"], Path(key + ".pub").read_text(encoding="utf-8"),
                                 "dashboard-check", via["id"] if via else None)
            ssh_ca.write_private(key + "-cert.pub", signed["cert"] + "\n")
            ssh_ca.write_private(kh, (known_hosts(row) + "\n") if pinned else "")
            jcfg = None
            if via:
                via_kh, jcfg = os.path.join(d, "known_hosts_via"), os.path.join(d, "ssh_config_via")
                ssh_ca.write_private(via_kh, known_hosts(via) + "\n")
                ssh_ca.write_private(jcfg, jump_config(via, key, key + "-cert.pub", via_kh))
            argv = ssh_argv(row, key, key + "-cert.pub", kh, pinned, CHECK_SCRIPT, via, jcfg)
            try:
                res = _run(argv, config.SSH_CHECK_TIMEOUT)
            except subprocess.TimeoutExpired:
                _record(row["id"], False, f"no answer from {row['address']}:{row['port']} within "
                                          f"{config.SSH_CHECK_TIMEOUT} s")
                return get(account["id"], server_id)
            found = _parse(res.stdout or "")
            if res.returncode != 0 or not found["complete"]:
                _record(row["id"], False, _failure(row, res, pinned, via) if res.returncode != 0
                        else "the server answered, but not with the expected check output")
                return get(account["id"], server_id)
            host_key = None
            if not pinned:
                lines = [ln.strip() for ln in Path(kh).read_text(encoding="utf-8").splitlines()
                         if ln.strip().startswith(alias(row["id"]) + " ")]
                if not lines:
                    _record(row["id"], False, "connected, but no host key was recorded to pin; check again")
                    return get(account["id"], server_id)
                host_key = "\n".join(lines)
            _record(row["id"], True, found=found, host_key=host_key,
                    host_key_fingerprint=_key_fingerprint(host_key) if host_key else None)
    except ssh_ca.CaUnavailable as exc:
        _record(row["id"], False, str(exc))
    except (RuntimeError, OSError, BadRequest) as exc:
        _record(row["id"], False, f"the check couldn't start: {str(exc)[:200]}")
    return get(account["id"], server_id)


# --- the probe (#810) ---------------------------------------------------------------------------------------------

def _keyscan(argv: list[str], timeout: int) -> subprocess.CompletedProcess:
    """The one place ssh-keyscan is started (the tests replace it). An argument list, never a shell."""
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)


def probe_argv(row: dict) -> list[str]:
    """`ssh-keyscan -T 5 -t <pinned key type> -p <port> -- <address>`: no login, no pass signed, so ssh_cert_issues keeps meaning
    "someone was let in" and auth.log sees only a pre-auth disconnect. Every value is validated like ssh_argv's."""
    kind = (row.get("host_key") or "").split()[1:2]
    if not kind or not _KEY_TYPE.match(kind[0]):
        raise BadRequest("no usable pinned host key to probe")
    return ["ssh-keyscan", "-T", str(int(config.SERVER_PROBE_TIMEOUT)), "-t", kind[0], "-p", str(clean_port(row["port"])),
            "--", clean_address(row["address"])]


def probe(row: dict) -> dict:
    """Probe one server: {ok, key_changed, error}. ok = it answered with the PINNED key. A different key under the same type is
    key_changed (immediate Needs setup); no answer is a miss. Never raises for a network problem."""
    try:
        argv = probe_argv(row)
        res = _keyscan(argv, int(config.SERVER_PROBE_TIMEOUT) + 3)
    except BadRequest as exc:
        return {"ok": False, "key_changed": False, "error": str(exc)[:200]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "key_changed": False, "error": f"no answer from {row['address']}:{row['port']}"}
    except OSError as exc:
        return {"ok": False, "key_changed": False, "error": f"ssh-keyscan could not start: {str(exc)[:150]}"}
    pinned = (row["host_key"].split() + ["", "", ""])[1:3]
    seen = [ln.split()[1:3] for ln in (res.stdout or "").splitlines() if ln.strip() and not ln.startswith("#") and len(ln.split()) >= 3]
    if any(got == pinned for got in seen):
        return {"ok": True, "key_changed": False, "error": ""}
    if any(got[0] == pinned[0] for got in seen):
        return {"ok": False, "key_changed": True, "error": HOST_KEY_CHANGED}
    return {"ok": False, "key_changed": False, "error": f"no host key from {row['address']}:{row['port']}"}


def record_probe(server_id: int, result: dict) -> None:
    """Store a probe. An answer resets the run of misses; a miss adds one and starts the 'unreachable since' clock; a changed key
    fails the server at once (status 'failed' with the host-key message), which is Needs setup."""
    with get_cursor() as cur:
        if result["ok"]:
            cur.execute("UPDATE commissioned_servers SET last_probe_at = now(), probe_ok = true, probe_misses = 0, "
                        "unreachable_since = NULL, probe_error = '' WHERE id = %s", (server_id,))
        else:
            cur.execute("UPDATE commissioned_servers SET last_probe_at = now(), probe_ok = false, probe_misses = probe_misses + 1, "
                        "unreachable_since = COALESCE(unreachable_since, now()), probe_error = %s WHERE id = %s",
                        (result["error"][:300], server_id))
            if result["key_changed"]:
                cur.execute("UPDATE commissioned_servers SET status = 'failed', last_check_at = now(), last_check_error = %s "
                            "WHERE id = %s", (HOST_KEY_CHANGED, server_id))


def probe_one(row: dict) -> dict:
    """Probe + record one server; the recovery rule: a server that was Needs setup only because it went quiet (it is still
    'commissioned') and answers again gets ONE automatic full check, so its facts and sudo are re-proved, never silently trusted."""
    before = state(row)
    result = probe(row)
    record_probe(row["id"], result)
    if result["ok"] and before == NEEDS_SETUP and row["status"] == "commissioned":
        from app import repo
        check(repo.get_project(row["account_id"]), row["id"])
    return result


def probe_all() -> dict:
    """One sweep: every direct, commissioned server. A jump-host server is skipped (a keyscan can't cross the hop without a login):
    its card shows its jump host's state."""
    with get_cursor(commit=False) as cur:
        cur.execute(f"SELECT {_COLUMNS} FROM commissioned_servers WHERE status = 'commissioned' AND host_key IS NOT NULL "
                    "AND via_server_id IS NULL ORDER BY id")
        rows = cur.fetchall()
    out = {"probed": 0, "ok": 0}
    for row in rows:
        out["probed"] += 1
        out["ok"] += 1 if probe_one(row)["ok"] else 0
    return out


def mark_needs_setup(server_id: int, why: str) -> None:
    """Move a server to Needs setup (a deploy was refused with 'Permission denied (publickey)': the paste line is gone)."""
    _record(server_id, False, why)


# --- the page model -----------------------------------------------------------------------------------------------

def setup_commands(user: str, line: str) -> list[dict]:
    """Copy-paste setup per OS family: create the user if it isn't there (no password, key-only, not 'locked'; an
    existing user is left as it is), passwordless sudo in /etc/sudoers.d/<user> checked with visudo (removed again if
    it doesn't pass), ~/.ssh 700 and authorized_keys 600 owned by the user, and the line appended. `user` is already validated; `line` holds no single quote."""
    user = clean_user(user)
    if "'" in line:
        raise BadRequest("unexpected character in the paste line")
    home = f'H=$(getent passwd {user} | cut -d: -f6)'
    sudoers = f"/etc/sudoers.d/{user}"
    debian = "\n".join([
        f"id {user} >/dev/null 2>&1 || {{ sudo useradd -m -s /bin/bash {user} && sudo usermod -p '*' {user}; }}",
        f"echo '{user} ALL=(ALL) NOPASSWD:ALL' | sudo tee {sudoers} >/dev/null",
        f"sudo chmod 440 {sudoers} && sudo visudo -cf {sudoers} || sudo rm -f {sudoers}",
        home,
        f'sudo install -d -m 700 -o {user} -g "$(id -gn {user})" "$H/.ssh"',
        f"echo '{line}' | sudo tee -a \"$H/.ssh/authorized_keys\" >/dev/null",
        f'sudo chown {user}:"$(id -gn {user})" "$H/.ssh/authorized_keys" && sudo chmod 600 "$H/.ssh/authorized_keys"',
    ])
    rhel = debian + '\nsudo restorecon -R "$H/.ssh" 2>/dev/null || true'
    alpine = "\n".join([
        "apk add --no-cache sudo",
        f"id {user} >/dev/null 2>&1 || {{ adduser -D -s /bin/sh {user} && echo '{user}:*' | chpasswd -e; }}",
        f"echo '{user} ALL=(ALL) NOPASSWD:ALL' > {sudoers}",
        f"chmod 440 {sudoers} && visudo -cf {sudoers} || rm -f {sudoers}",
        home,
        f'install -d -m 700 -o {user} -g "$(id -gn {user})" "$H/.ssh"',
        f"echo '{line}' >> \"$H/.ssh/authorized_keys\"",
        f'chown {user}:"$(id -gn {user})" "$H/.ssh/authorized_keys" && chmod 600 "$H/.ssh/authorized_keys"',
    ])
    return [{"key": "debian", "label": "Debian / Ubuntu", "how": "as a user with sudo", "script": debian},
            {"key": "rhel", "label": "RHEL / Rocky / Alma / Fedora", "how": "as a user with sudo", "script": rhel},
            {"key": "alpine", "label": "Alpine", "how": "as root", "script": alpine}]


def _when(dt: datetime | None) -> str:
    return dt.astimezone(LOCAL).strftime("%a %d %b %Y %H:%M") if dt else ""


def _ago(dt: datetime | None) -> str:
    """"2 min ago" for the card's summary line; empty when never."""
    if not dt:
        return ""
    mins = max(0, int((datetime.now(timezone.utc) - dt).total_seconds() // 60))
    return "just now" if mins < 1 else f"{mins} min ago" if mins < 120 else f"{mins // 60} h ago"


def page_model(account: dict) -> dict:
    """The account's servers for its Servers page: each with its paste line and setup commands. Never CA private
    material; the CA is made here only if adding happened before the key file was there."""
    rows = list_for([account["id"]])
    ca, ca_error = None, ""
    if rows:
        try:
            ca = ssh_ca.ensure_ca(account)
        except ssh_ca.CaUnavailable as exc:
            ca_error = str(exc)
    cards = []
    by_id = {r["id"]: r for r in rows}
    for r in rows:
        line = ssh_ca.paste_line(ca, account, r["id"]) if ca else ""
        via = by_id.get(r["via_server_id"]) if r["via_server_id"] else None
        eff = state(via) if via else state(r)         # a jump-host server shows its host's state (a keyscan can't cross the hop)
        cards.append(dict(r, via_name=via["name"] if via else "", state=eff, state_reason=state_reason(via or r),
                          probe_when=_ago(r["last_probe_at"]), unreachable_min=minutes_unreachable(via or r), line=line, setup=setup_commands(r["login_user"], line) if line else [],
                          os=" ".join(x for x in (r["os_name"], r["os_version"]) if x),
                          commissioned_when=_when(r["commissioned_at"]), checked_when=_when(r["last_check_at"])))
    # a via must be commissioned and not itself a via; a server others jump through can't be given one
    used_as_via = {r["via_server_id"] for r in rows if r["via_server_id"]}
    for c in cards:
        c["via_options"] = [o for o in rows if o["id"] != c["id"] and o["status"] == "commissioned" and o["host_key"]
                            and not o["via_server_id"] and c["id"] not in used_as_via]
    return {"via_options": [o for o in rows if o["status"] == "commissioned" and o["host_key"]
                            and not o["via_server_id"]],
            "servers": cards, "ca_error": ca_error, "ca_fingerprint": ca["fingerprint"] if ca else ""}
