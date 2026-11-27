"""The runner WebSocket, `WS /runner/ws` (task #627, change #50, phase P3). A native runner holds ONE outbound socket to the
dashboard: it is the runner's liveness, the channel for "a job is available" nudges, and the place a revoke lands within a
second. The wire protocol is docs/runner-protocol.md. The rules, from the Fable review (task comments 2258/2259):

* Refused BEFORE the upgrade (an HTTP 403, nothing accepted): a Host not on the allowlist (the MCP transport allowlist, this
  dashboard's own host and CREWBOSS_RUNNER_ALLOWED_HOSTS), ANY Origin header (a runner is native, a browser must never open this),
  and anything but a live DEVICE token (cbr_, or the old mdr_; job and session tokens are refused). The token is the Authorization header only,
  never the URL.
* Then a MUTUAL signed challenge. The server signs a transcript with the runner identity key (the runner checks it against the
  key it pinned at enrolment); the device signs a transcript with its own key. Each transcript carries a domain label, the
  runner's nonce, the server's FRESH nonce, the device id and the Host the runner dialled, so a signature can't be replayed
  (new nonces), relayed to another device's connection (device id) or to another host (Host), and the two directions can't be
  reflected into each other (labels). The same challenge repeats every CREWBOSS_RUNNER_WS_CHALLENGE_SECONDS; a wrong or missing
  answer closes the socket.
* A close is always a code: 4401 not authorised / revoked / failed challenge, 4408 timeout, 4400 protocol error, 4409
  replaced by a newer connection of the same device.
* Liveness: a verified socket proves the runner PROCESS is alive and updates the device's last_seen. It does NOT renew job
  leases: only POST /runner/jobs/<id>/heartbeat does, because a heartbeat names the job AND its fence and the broker checks both
  (a socket is per device and can't say which job a runner still holds). So fencing is untouched, and a runner whose socket is
  up but whose agent has hung still loses the job.
* claim, heartbeat, finish and release are messages on the VERIFIED socket (review comment 2280): REST took the device token alone, so
  a stolen token could claim jobs without the device key. REST now refuses them (routers/runner.py); the jobs.* calls are
  unchanged, so the one-SQL dispatch and the fencing mean what they did.
* Nudges carry ids only ({type, job_id, task_id}), never task text; the runner claims over this socket, which is the truth.
* A socket joins the registry, and evicts the older one of the device (4409), only at its FIRST GOOD PROOF: a connection that
  merely holds a stolen token never displaces the real runner. At most one unproven socket per device is parked; a newer
  one replaces the earlier unproven one only.
* One app process: the registry of live sockets is in this process's memory. More than one process would need a shared bus.
Every refusal is logged with ids only, never a token."""
from __future__ import annotations

import asyncio
import concurrent.futures
import base64
import binascii
import hashlib
import json
import re
import logging
import secrets
import struct
import threading
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from app import auth, config, jobs, local_auth, repo, runner_identity
from app.db import get_cursor

log = logging.getLogger("crewboss.runner-ws")

PROTOCOL = 1
CLOSE_PROTOCOL = 4400
CLOSE_AUTH = 4401
CLOSE_TIMEOUT = 4408
CLOSE_REPLACED = 4409
HELLO_TIMEOUT = 10.0
NONCE_BYTES = 32
MAX_FRAME = 4096
# One message type carries a file's bytes for the Owner to read (agent_file, #829). The file is sent in CHUNKS (the way ca_attach slices), each frame
# well under uvicorn's `--ws-max-size 8192` (which closes the socket on anything bigger, so a larger allowance here could never apply). Only a verified
# device may send a frame over MAX_FRAME, and only of this type (checked on the frame's own prefix before it is parsed); the server reassembles at most
# ONE file per connection (<= agent_pins.MAX_BYTES) and checks its sha256 on the last chunk.
MAX_BIG_FRAME = 7168
MAX_CHUNK_B64 = 6144
MAX_FILE_PARTS = 100
BIG_PREFIX = re.compile(r'\{\s*"type"\s*:\s*"agent_file"')
# Protocol identifiers (domain separation in the signed handshake), not names (the same lines are in runner/crewboss_runner/link.py).
# A runner built after the #589 rename says so in its hello ("labels": "crewboss") and gets the crewboss labels; a runner that
# says nothing still speaks the pre-rename labels, so a PC that was not updated keeps working. A device proof under either
# label is accepted: the server nonce is fresh for every challenge, so neither can be replayed.
SERVER_LABEL = b"crewboss-runner-ws/v1/server\0"
DEVICE_LABEL = b"crewboss-runner-ws/v1/device\0"
LEGACY_SERVER_LABEL = b"mallen-runner-ws/v1/server\0"   # TODO(#589 compat): drop with the pre-rename runners
LEGACY_DEVICE_LABEL = b"mallen-runner-ws/v1/device\0"
HELLO_LABELS = "crewboss"


# --- the allowlist ---------------------------------------------------------------------------------------------------

def _hostname(value: str) -> str:
    """The lower-case host part of a Host header value, or '' when it isn't one."""
    try:
        return (urlsplit("//" + value.strip()).hostname or "").lower()
    except ValueError:
        return ""


def allowed_hosts() -> set[str]:
    from app import mcp_server   # its list: the instance's own host names plus 127.0.0.1/localhost
    hosts = {h.lower() for h in mcp_server._ALLOWED_HOSTS}
    hosts.add((urlsplit(config.RUNNER_BASE_URL).hostname or "").lower())
    hosts.update(h.strip().lower() for h in config.RUNNER_ALLOWED_HOSTS.split(",") if h.strip())
    hosts.discard("")
    return hosts


def host_allowed(host_header: str) -> bool:
    return bool(host_header) and _hostname(host_header) in allowed_hosts()


# --- the transcripts -------------------------------------------------------------------------------------------------

def transcript(label: bytes, client_nonce: bytes, server_nonce: bytes, device_id: int, host: str) -> bytes:
    """What is signed: label, both nonces, the device id (8 bytes, big-endian) and the Host the runner dialled
    (lower-case Host header value, length-prefixed so no field can run into another)."""
    h = host.strip().lower().encode("utf-8")
    return label + client_nonce + server_nonce + struct.pack(">Q", device_id) + struct.pack(">H", len(h)) + h


def server_proof(client_nonce: bytes, server_nonce: bytes, device_id: int, host: str, legacy: bool = False) -> bytes:
    return runner_identity.sign(transcript(LEGACY_SERVER_LABEL if legacy else SERVER_LABEL,
                                           client_nonce, server_nonce, device_id, host))


def _b64d(value, length: int | None = None) -> bytes | None:
    try:
        raw = base64.b64decode(value, validate=True) if isinstance(value, str) else None
    except (binascii.Error, ValueError):
        return None
    return raw if raw is not None and (length is None or len(raw) == length) else None


# --- the registry ----------------------------------------------------------------------------------------------------

class Conn:
    """One live, accepted socket. Everything the rest of the app does to it goes through post(), which is safe from any
    thread: a revoke runs in a worker thread, the socket in the event loop."""

    def __init__(self, device_id: int, loop: asyncio.AbstractEventLoop):
        self.device_id, self.loop = device_id, loop
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.verified = False
        self.attention: tuple[str, ...] = ()   # fixed problem kinds the runner reported (ATTENTION_TEXT keys); in memory only
        self.capabilities: dict = {}           # what the runner can do beyond claude + git (#861: {"gemini": {"key": bool}}); in memory only
        self.findings: tuple[dict, ...] = ()   # agent-setup scan findings (path, rule, severity, hint), sanitised; in memory only
        self.assembly: dict | None = None      # the one agent_file being received in chunks: {key, next, buf}; in memory only, at most MAX_BYTES
        self.rotations: dict[str, str] = {}    # tls_rotate frames in flight (#976): id -> the new certificate's pin26; the ack must name the same pin
        self.key_waiters: dict[str, concurrent.futures.Future] = {}   # gemini_key requests in flight (#861): id -> the answer; never holds the key

    def post(self, item: tuple) -> None:
        try:
            self.loop.call_soon_threadsafe(self.inbox.put_nowait, item)
        except RuntimeError:   # the loop is gone: the socket with it
            pass


_conns: dict[int, Conn] = {}      # VERIFIED sockets: the device proved its key. One per device.
_pending: dict[int, Conn] = {}    # accepted but not yet proven (a token alone): at most one per device
_lock = threading.Lock()


def _add_pending(conn: Conn) -> Conn | None:
    """Park an unproven socket. A second pending one of the same device replaces the earlier PENDING one; the verified
    socket is never touched here (a stolen token alone must not knock the real runner offline). Returns the replaced one."""
    with _lock:
        old = _pending.get(conn.device_id)
        _pending[conn.device_id] = conn
    return old


def _promote(conn: Conn) -> Conn | None:
    """The newcomer's first good proof: only now does it become THE socket and evict the older verified one (4409)."""
    with _lock:
        if _pending.get(conn.device_id) is conn:
            del _pending[conn.device_id]
        old = _conns.get(conn.device_id)
        _conns[conn.device_id] = conn
    return old if old is not conn else None


def _unregister(conn: Conn) -> None:
    with _lock:
        if _conns.get(conn.device_id) is conn:
            del _conns[conn.device_id]
        if _pending.get(conn.device_id) is conn:
            del _pending[conn.device_id]


# What the Runners page says for each problem kind a runner may report. The runner sends KINDS only; the words are ours, so a
# semi-trusted PC cannot put free text on the page.
ATTENTION_TEXT = {"not_signed_in": "Claude Code is not signed in: run `claude` on that PC and log in",
                  "claude_missing": "Claude Code is not installed on that PC",
                  "git_missing": "git is not installed on that PC",
                  "elevated": "Runs as administrator: jobs refused. Start the runner without admin rights (crewboss-runner service install on that PC)",
                  "agent_setup_blocked": "Known-risky agent setup on that PC (see the findings below); run `crewboss-runner doctor` there",
                  "repo_setup_blocked": "A project's repository has known-risky agent setup (see the findings below); fix it in that repository"}
# Agent-setup scan (#646): the rule ids the runner may report, and the cap on what is kept. Never file contents: path, rule, severity, hint.
FINDING_RULES = frozenset({"perm.allow_all", "hook.remote_exec", "hook.outside_repo", "hook.present", "mcp.unexpected_host",
                           "mcp.remote_exec", "plugin.enabled", "claudemd.injection", "secret.private_key", "secret.aws_key",
                           "secret.gcp_key", "secret.anthropic_key", "secret.github_token", "secret.env_file", "config.unreadable",
                           "hook.http", "settings.env", "settings.exec", "perm.broad_shell",
                           "frontmatter.hooks", "frontmatter.mcp_servers", "frontmatter.permission_mode", "frontmatter.tools",
                           "config.symlink", "config.unreviewed", "frontmatter.unreadable",
                           "settings.hooks", "settings.plugins", "settings.allow"})
MAX_FINDINGS = 8


def clean_findings(raw) -> tuple[dict, ...]:
    """Keep only well-formed findings of a known rule: short plain strings, severity block|warn. Anything else is dropped."""
    out = []
    for f in raw[:MAX_FINDINGS] if isinstance(raw, list) else []:
        if (isinstance(f, dict) and f.get("rule") in FINDING_RULES and f.get("severity") in ("block", "warn")
                and isinstance(f.get("path"), str) and isinstance(f.get("hint"), str)):
            out.append({"rule": f["rule"], "severity": f["severity"], "path": f["path"][:140], "hint": f["hint"][:100]})
    return tuple(out)


def findings(device_id: int) -> list[dict]:
    """The agent-setup findings of a connected runner (empty when none or offline)."""
    with _lock:
        c = _conns.get(device_id)
        return list(c.findings) if c and c.verified else []


def clean_capabilities(raw) -> dict:
    """Only the capability facts we know, as plain booleans: {"gemini": {"key": bool}} (a usable API key file on the runner). Anything else is dropped."""
    out: dict = {}
    g = raw.get("gemini") if isinstance(raw, dict) else None
    if isinstance(g, dict):
        out["gemini"] = {"key": g.get("key") is True}
    return out


def capabilities(device_id: int) -> dict:
    """The capabilities a connected runner reported (empty when none or offline)."""
    with _lock:
        c = _conns.get(device_id)
        return dict(c.capabilities) if c and c.verified else {}


class RunnerOffline(Exception):
    """The runner has no verified socket right now (nothing is queued for later)."""


class KeyPushFailed(Exception):
    """The runner did not answer a gemini_key request in time, or the socket closed first. The message is fixed text (never the key)."""


GEMINI_KEY_WAIT = 10.0   # seconds the page waits for the runner's reply


def push_gemini_key(device_id: int, action: str, key: str = "", timeout: float | None = None) -> dict:
    """Send a Google API key to (action "set") or remove it from (action "clear") a runner, ONE WAY over its existing authenticated socket, and
    wait for its reply {ok, fingerprint, error}. Blocking: call from a worker thread. Raises RunnerOffline when it is not connected (nothing is
    queued: a key is never held here) and KeyPushFailed on no answer. The key lives only in this call's arguments, the socket frame and the
    runner's file: it is not stored, not logged and not in any exception text."""
    if action not in ("set", "clear"):
        raise ValueError("action")
    with _lock:
        c = _conns.get(device_id)
    if not (c and c.verified):
        raise RunnerOffline()
    rid = secrets.token_hex(8)
    fut: concurrent.futures.Future = concurrent.futures.Future()
    c.key_waiters[rid] = fut
    try:
        c.post(("gemini_key", rid, action, key if action == "set" else ""))
        try:
            return fut.result(timeout=GEMINI_KEY_WAIT if timeout is None else timeout)
        except concurrent.futures.TimeoutError:
            raise KeyPushFailed("the runner did not answer in time") from None
    finally:
        c.key_waiters.pop(rid, None)


# --- TLS pin rotation (#976 D7) --------------------------------------------------------------------------------------

TLS_ROTATE_LABEL = b"crewboss-runner-tls-rotate/v1\0"   # a protocol identifier: the runner (runner/crewboss_runner/tlspin.py) signs nothing, it checks this


def _pem_der(pem: str) -> bytes:
    import ssl
    if not isinstance(pem, str) or len(pem) > 8192:
        raise ValueError("not a certificate")
    return ssl.PEM_cert_to_DER_cert(pem.strip() + "\n")


def spki_sha256(cert_der: bytes) -> bytes:
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    return hashlib.sha256(x509.load_der_x509_certificate(cert_der).public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).digest()


def pin26(cert_der: bytes) -> str:
    return base64.b32encode(spki_sha256(cert_der)).decode().lower().rstrip("=")[:26]


def tls_rotate_message(new_der: bytes, old_spki_sha256: bytes, host: str) -> bytes:
    """Signed with the IDENTITY key (never the TLS key): label || sha256(new SPKI) || sha256(new cert DER) || sha256(old SPKI) || len(host) (2) || host.
    The host is the one THIS runner dialled, so a frame cannot be replayed to a runner on another address."""
    h = host.strip().lower().encode("utf-8")
    return (TLS_ROTATE_LABEL + spki_sha256(new_der) + hashlib.sha256(new_der).digest() + old_spki_sha256
            + struct.pack(">H", len(h)) + h)


def rotate_tls(cert_pem: str, old_spki_sha256: bytes) -> int:
    """Announce the new LAN certificate to every VERIFIED runner socket. `old_spki_sha256` is the sha256 of the SPKI of the certificate being
    served now. Returns how many sockets were sent it; the others learn nothing (a runner offline the whole time must be enrolled again).
    Thread-safe."""
    der = _pem_der(cert_pem)
    if len(old_spki_sha256) != 32:
        raise ValueError("old_spki_sha256")
    pin = pin26(der)
    with _lock:
        conns = [c for c in _conns.values() if c.verified]
    for c in conns:
        rid = secrets.token_hex(8)
        c.rotations[rid] = pin
        c.post(("tls_rotate", rid, cert_pem, old_spki_sha256))
    return len(conns)


def rotate_tls_from_prepare(prepared: dict) -> int:
    """Broadcast the answer of the sidecar's POST /lan-cert/prepare, `{ok, next_cert_pem, sig_material: {new_spki_sha256, new_cert_sha256,
    old_spki_sha256 (hex), host}}`. The hashes are CHECKED against the certificate (a sidecar that disagrees with its own PEM sends nothing).
    The sidecar reuses the key, so new and old SPKI are normally equal and the pin does not change. The signed host is per connection (the
    one each runner dialled), not sig_material["host"]."""
    pem, mat = prepared.get("next_cert_pem"), prepared.get("sig_material")
    if not prepared.get("ok", True) or not isinstance(mat, dict):
        raise ValueError("prepare failed")
    der = _pem_der(pem)
    new_spki, new_cert, old_spki = (bytes.fromhex(str(mat.get(k, ""))) for k in ("new_spki_sha256", "new_cert_sha256", "old_spki_sha256"))
    if new_spki != spki_sha256(der) or new_cert != hashlib.sha256(der).digest():
        raise ValueError("sig_material does not match the certificate")
    return rotate_tls(pem, old_spki)


def _store_tls_ack(device_id: int, fp: str) -> None:
    with get_cursor() as cur:
        cur.execute("UPDATE device_tokens SET tls_fp = %s, tls_fp_at = now() WHERE id = %s", (fp, device_id))


TLS_RECENT_DAYS = 30


def tls_ready(pin: str) -> dict:
    """{ready, total, pending}: of the live computers that PIN the certificate (they have acknowledged one before) and connected in the last
    30 days, how many acknowledged the certificate with this pin (for the Front door card and the automatic renewal). `pending` names the
    ones that have not. A never-pinned or long-silent computer is not counted: it cannot be waited for."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT id, name, tls_fp = %s AS ok FROM device_tokens WHERE revoked_at IS NULL AND expires_at > now() "
                    "AND tls_fp IS NOT NULL AND last_seen > now() - make_interval(days => %s) ORDER BY id", (pin, TLS_RECENT_DAYS))
        rows = cur.fetchall()
    return {"ready": sum(1 for r in rows if r["ok"]), "total": len(rows),
            "pending": [r["name"] or f"device {r['id']}" for r in rows if not r["ok"]]}


def attention(device_id: int) -> list[str]:
    """The reported problems of a connected runner, as page text (empty when none or offline)."""
    with _lock:
        c = _conns.get(device_id)
        kinds = c.attention if c and c.verified else ()
    return [ATTENTION_TEXT[k] for k in kinds]


def any_connected() -> bool:
    return bool(_conns)


def connected(device_id: int) -> bool:
    with _lock:
        c = _conns.get(device_id)
        return bool(c and c.verified)


def close_device(device_id: int, code: int = CLOSE_AUTH, reason: str = "") -> int:
    """Close every live socket of a device (a revoke). Safe from any thread. Returns how many were told."""
    with _lock:   # the verified socket AND any pending one
        conns = [c for c in (_conns.get(device_id), _pending.get(device_id)) if c]
    for c in conns:
        c.post(("close", code, reason))
    if conns:
        log.warning("runner ws: closing %d socket(s) of device %s (%s)", len(conns), device_id, reason or code)
    return len(conns)


def close_user(user_id: int, reason: str = "user disabled") -> int:
    """Close every live socket of a user's devices (the user was disabled)."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT id FROM device_tokens WHERE user_id = %s", (user_id,))
        ids = [r["id"] for r in cur.fetchall()]
    return sum(close_device(i, CLOSE_AUTH, reason) for i in ids)


def nudge_ca(device_id: int) -> int:
    """Tell a connected runner a CA chat turn is waiting for it (no ids: it claims whatever is its own). Returns 1 if told."""
    with _lock:
        c = _conns.get(device_id)
    if c and c.verified:
        c.post(("ca_nudge",))
        return 1
    return 0


def nudge_env(device_id: int) -> int:
    """Tell a connected runner an environment check is waiting for it (#810; no ids: it claims whatever is its own). Returns 1 if told."""
    with _lock:
        c = _conns.get(device_id)
    if c and c.verified:
        c.post(("env_nudge",))
        return 1
    return 0


def nudge_job(job_id: int) -> int:
    """Offer a claimable job to the live sockets of the devices that could claim it. Ids only. Returns how many sockets."""
    n = 0
    for device_id in jobs.claimable_devices(job_id):
        with _lock:
            c = _conns.get(device_id)
        if c and c.verified:
            c.post(("nudge", job_id))
            n += 1
    return n


# --- the socket ------------------------------------------------------------------------------------------------------

def client_ip(conn) -> str:
    """The caller's address for logs and rate limits (a Request or a WebSocket). X-Forwarded-For is believed only from a trusted
    proxy (local_auth.client_ip, config.TRUSTED_PROXIES); from anyone else it is ignored."""
    return local_auth.client_ip(conn)


async def _refuse(ws: WebSocket, why: str, device_id: int | None = None) -> None:
    """Refuse before the upgrade (HTTP 403). Logged with ids only; the caller never learns why."""
    ip = client_ip(ws) or "?"
    log.warning("runner ws: refused %s from %s%s", why, ip, f" (device {device_id})" if device_id else "")
    await ws.close(code=1008)


def _device_live(device_id: int) -> dict | None:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT d.id, d.pubkey FROM device_tokens d JOIN users u ON u.id = d.user_id "
                    "WHERE d.id = %s AND d.revoked_at IS NULL AND d.expires_at > now() AND u.disabled_at IS NULL",
                    (device_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def _touch(device_id: int) -> None:
    with get_cursor() as cur:
        cur.execute("UPDATE device_tokens SET last_seen = now() WHERE id = %s", (device_id,))


def _task_of(job_id: int) -> int | None:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT task_id FROM jobs WHERE id = %s", (job_id,))
        row = cur.fetchone()
        return row["task_id"] if row else None


def _verify(pubkey_b64: str, message: bytes, signature: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(pubkey_b64)).verify(signature, message)
    except (InvalidSignature, binascii.Error, ValueError, TypeError):
        return False
    return True


JOB_MESSAGES = ("claim", "heartbeat", "finish", "release", "skills", "agent_file")
CA_MESSAGES = ("ca_claim", "ca_event", "ca_finish", "ca_attach")   # the CA's chat turns (task #702, app/ca.py); verified socket only
ENV_MESSAGES = ("env_claim", "env_finish")   # environment checks (#810, app/environment.py); verified socket only
MAX_ATTENTION = 8


def _rid(msg: dict):
    """The request id the runner chose, echoed in the reply (a short string or int; anything else is dropped)."""
    rid = msg.get("id")
    return rid if isinstance(rid, (int, str)) and not isinstance(rid, bool) and len(str(rid)) <= 64 else None


def _job_message(device_id: int, msg: dict) -> dict:
    """claim / heartbeat / finish, ONLY over the verified socket (the device proved its key; REST took a device token
    alone and so no longer does any of this). Same jobs.* calls as before: one-SQL claim, fence checked on every write.
    The reply is `<type>_result` with the request's id; a stale fence is {"ok": false, "status": 409}."""
    mtype, rid = msg["type"], _rid(msg)
    out: dict = {"type": mtype + "_result", "id": rid}
    try:
        if mtype == "claim":
            out["job"] = jobs.claim(device_id)   # None = nothing for this device (the old REST 204)
            out["ok"] = True
        else:
            job_id, fence = msg.get("job_id"), msg.get("fence")
            if not all(isinstance(v, int) and not isinstance(v, bool) for v in (job_id, fence)):
                return {**out, "ok": False, "status": 400, "detail": "job_id and fence must be integers"}
            if mtype == "heartbeat":
                out.update(jobs.heartbeat(device_id, job_id, fence))
            elif mtype == "release":
                out.update(jobs.release(device_id, job_id, fence, str(msg.get("reason") or "")))
            elif mtype == "agent_file":
                out.update(jobs.agent_file_reported(device_id, job_id, fence, msg.get("path"), msg.get("sha256"), msg.get("b64"), msg.get("source")))
            elif mtype == "skills":
                w, k = msg.get("written"), msg.get("skipped")
                out.update(jobs.skills_delivered(device_id, job_id, fence, w if isinstance(w, list) else [], k if isinstance(k, list) else []))
            else:
                out.update(jobs.finish(device_id, job_id, fence, bool(msg.get("ok")), str(msg.get("summary") or ""),
                                       str(msg.get("outcome") or "")))
            out["ok"] = True
    except jobs.StaleFence as exc:
        out.update(ok=False, status=409, detail=str(exc))
    except repo.BadRequest as exc:   # e.g. an agent file that fails its checks: a reason the runner can show, the socket stays open
        out.update(ok=False, status=400, detail=str(exc)[:200])
    except Exception:  # noqa: BLE001 — a failing write must not kill the socket
        log.exception("runner ws: device %s %s", device_id, mtype)
        out.update(ok=False, status=500, detail="server error")
    return out


def assemble_file(conn: Conn, msg: dict) -> tuple[dict | None, dict | None]:
    """One `agent_file` frame of a VERIFIED socket. -> (the whole message to process, None) once a file is complete (or has no bytes to assemble),
    (None, the reply to send) otherwise: `ok` for a chunk that is not the last, or a 400 with a plain reason for anything wrong (the assembly is
    dropped and the socket kept: the runner reads the reason and gives the job back cleanly). Chunk frames: `part` 0..parts-1 in order, `parts`,
    `b64` (<= MAX_CHUNK_B64 characters), plus the file's `path`, `sha256`, `source`, `job_id` and `fence` (identical on every chunk). A frame with
    `b64` and no `part` is a one-chunk file. Memory: one assembly per connection, never more than agent_pins.MAX_BYTES of bytes."""
    from app import agent_pins
    rid = _rid(msg)

    def fail(why: str):
        conn.assembly = None
        return None, {"type": "agent_file_result", "id": rid, "ok": False, "status": 400, "detail": why}

    b64 = msg.get("b64")
    if b64 in (None, ""):                      # named only (too big to review, not text, or a link): nothing to assemble
        conn.assembly = None
        return msg, None
    part, parts = msg.get("part", 0), msg.get("parts", 1)
    if not all(isinstance(v, int) and not isinstance(v, bool) for v in (part, parts)) or not 0 < parts <= MAX_FILE_PARTS or not 0 <= part < parts:
        return fail("bad chunk numbers")
    if not isinstance(b64, str) or len(b64) > MAX_CHUNK_B64:
        return fail("chunk too large")
    try:
        chunk = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        return fail("bad chunk bytes")
    key = tuple(msg.get(k) for k in ("job_id", "fence", "path", "sha256", "source")) + (parts,)
    if part == 0:
        conn.assembly = {"key": key, "next": 0, "buf": bytearray()}
    asm = conn.assembly
    if asm is None or asm["key"] != key or asm["next"] != part:
        return fail("chunk out of order or from another file")
    if len(asm["buf"]) + len(chunk) > agent_pins.MAX_BYTES:
        return fail("file too large to review")
    asm["buf"] += chunk
    asm["next"] += 1
    if part < parts - 1:
        return None, {"type": "agent_file_result", "id": rid, "ok": True, "part": part}
    conn.assembly = None
    data = bytes(asm["buf"])
    if hashlib.sha256(data).hexdigest() != msg.get("sha256"):
        return None, {"type": "agent_file_result", "id": rid, "ok": False, "status": 400, "detail": "sha256 does not match the bytes received"}
    return {**msg, "b64": base64.b64encode(data).decode("ascii")}, None


def _ca_message(device_id: int, msg: dict) -> dict | None:
    """ca_claim / ca_event / ca_finish over the verified socket. ca_claim and ca_finish are answered (`<type>_result`, the
    request's id); a ca_event is not, except that a turn the runner no longer holds is answered with `ca_stop` (its signal to
    stop Claude). Same fence rules as a job: a stale fence changes nothing."""
    from app import ca
    mtype, rid = msg["type"], _rid(msg)
    out: dict = {"type": mtype + "_result", "id": rid}
    turn_id, fence = msg.get("turn_id"), msg.get("fence")
    try:
        if mtype == "ca_claim":
            out["turn"] = ca.claim_turn(device_id)
            out["ok"] = True
            return out
        if not all(isinstance(v, int) and not isinstance(v, bool) for v in (turn_id, fence)):
            return {**out, "ok": False, "status": 400, "detail": "turn_id and fence must be integers"}
        if mtype == "ca_attach":   # one slice of a file attached to the message this turn answers (task #727)
            from app import ca_files
            aid, part = msg.get("attachment_id"), msg.get("part")
            if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in (aid, part)):
                return {**out, "ok": False, "status": 400, "detail": "attachment_id and part must be integers"}
            out.update(ca_files.chunk_for_turn(device_id, turn_id, fence, aid, part))
            out["ok"] = True
            return out
        if mtype == "ca_event":
            ca.turn_event(device_id, turn_id, fence, str(msg.get("kind") or ""), str(msg.get("text") or ""), str(msg.get("name") or ""), msg.get("seq"))
            return None
        out.update(ca.finish_turn(device_id, turn_id, fence, bool(msg.get("ok")), str(msg.get("session_id") or ""),
                                  msg.get("tokens_in"), msg.get("tokens_out"), msg.get("tools"), str(msg.get("error") or "")))
        out["ok"] = True
    except ca.NotFound as exc:   # ca_attach for a file/turn that is not there
        out.update(ok=False, status=404, detail=str(exc))
    except ca.TurnGone as exc:
        if mtype == "ca_event":
            return {"type": "ca_stop", "turn_id": turn_id}
        out.update(ok=False, status=409, detail=str(exc))
    except Exception:  # noqa: BLE001 — a failing write must not kill the socket
        log.exception("runner ws: device %s %s", device_id, mtype)
        out.update(ok=False, status=500, detail="server error")
    return out


def _env_message(device_id: int, msg: dict) -> dict:
    """env_claim / env_finish over the verified socket. env_claim -> `env_claim_result` with {check_id, project_id, rows:[{i, kind, name,
    os, args}]} or check null; env_finish -> `env_finish_result`. The runner is sent (kind, name, args) only, never a command; its answer is one
    short status per row and the server alone decides compliance."""
    from app import environment
    mtype, rid = msg["type"], _rid(msg)
    out: dict = {"type": mtype + "_result", "id": rid}
    try:
        if mtype == "env_claim":
            out["check"] = environment.claim(device_id)
            out["ok"] = True
        else:
            check_id = msg.get("check_id")
            if not isinstance(check_id, int) or isinstance(check_id, bool):
                return {**out, "ok": False, "status": 400, "detail": "check_id must be an integer"}
            out.update(environment.finish(device_id, check_id, msg.get("results"), str(msg.get("os") or "")))
            out["ok"] = True
    except environment.NotFound as exc:
        out.update(ok=False, status=404, detail=str(exc))
    except Exception:  # noqa: BLE001 - a failing write must not kill the socket
        log.exception("runner ws: device %s %s", device_id, mtype)
        out.update(ok=False, status=500, detail="server error")
    return out


class _Gone(Exception):
    """The peer vanished mid-send."""


async def _send(ws: WebSocket, **msg) -> None:
    try:
        await ws.send_text(json.dumps(msg, separators=(",", ":")))
    except (WebSocketDisconnect, RuntimeError, OSError):
        raise _Gone from None


async def _reader(ws: WebSocket, conn: Conn) -> None:
    """Feed the runner's frames into the connection's inbox; ends on a disconnect."""
    try:
        while True:
            conn.post(("rx", await ws.receive_text()))
    except WebSocketDisconnect:
        conn.post(("gone",))
    except Exception:  # noqa: BLE001 — a binary frame or a broken socket
        conn.post(("bad",))


async def serve(ws: WebSocket) -> None:
    headers = ws.headers
    if "origin" in headers:   # any Origin at all: a browser is the one thing that must never get here
        return await _refuse(ws, "an Origin header (a browser)")
    host = headers.get("host", "")
    if not host_allowed(host):
        return await _refuse(ws, "a Host not on the allowlist")
    authz = headers.get("authorization", "")
    token = authz.split(" ", 1)[1].strip() if authz.lower().startswith("bearer ") else ""
    if not token.startswith(auth.DEVICE_PREFIXES):
        return await _refuse(ws, "no device token (job and session tokens can't open a runner socket)")
    row = await asyncio.to_thread(auth.resolve_token, token)
    if not row or row["stream"] != "device":
        return await _refuse(ws, "an invalid, revoked or expired device token")
    device_id = row["device_id"]
    try:
        runner_identity.fp8()   # no identity key, no socket
    except runner_identity.IdentityUnavailable:
        return await _refuse(ws, "no runner identity key", device_id)

    await ws.accept()
    conn = Conn(device_id, asyncio.get_running_loop())
    replaced = _add_pending(conn)   # not in the registry proper until it proves the device key
    if replaced:
        replaced.post(("close", CLOSE_REPLACED, "replaced by a newer unproven connection"))
    reader = asyncio.create_task(_reader(ws, conn))
    code, reason = CLOSE_PROTOCOL, "protocol error"
    try:
        code, reason = await _session(ws, conn, device_id, host)
    except _Gone:
        code, reason = 1000, "gone"
    except Exception:  # noqa: BLE001
        log.exception("runner ws: device %s", device_id)
        code, reason = 1011, "server error"
    finally:
        conn.verified = False
        _unregister(conn)
        reader.cancel()
        try:
            await ws.close(code=code, reason=reason[:100])
        except Exception:  # noqa: BLE001 — already closed
            pass
    log.info("runner ws: device %s closed (%s %s)", device_id, code, reason)


async def _session(ws: WebSocket, conn: Conn, device_id: int, host: str) -> tuple[int, str]:
    """The conversation. Returns the (code, reason) to close with."""
    loop = asyncio.get_running_loop()
    challenge_every = max(config.RUNNER_WS_CHALLENGE_SECONDS, 0.05)
    proof_timeout = max(config.RUNNER_WS_PROOF_TIMEOUT_SECONDS, 0.05)
    recheck_every = max(config.RUNNER_WS_RECHECK_SECONDS, 0.05)
    client_nonce: bytes | None = None
    legacy_labels = True   # until the hello says this runner knows the crewboss labels
    pending: tuple[bytes, float] | None = None   # (server nonce, deadline) of the challenge in flight
    first = True
    hello_deadline = loop.time() + HELLO_TIMEOUT
    next_challenge = float("inf")
    next_recheck = loop.time() + recheck_every

    def refused(why: str) -> None:
        log.warning("runner ws: device %s refused: %s", device_id, why)

    while True:
        now = loop.time()
        due = hello_deadline if client_nonce is None else pending[1] if pending is not None else next_challenge
        due = min(due, next_recheck)
        try:
            item = await asyncio.wait_for(conn.inbox.get(), timeout=max(due - now, 0))
        except asyncio.TimeoutError:
            item = None
        now = loop.time()

        if item is not None:
            kind = item[0]
            if kind == "close":
                return item[1], item[2]
            if kind == "gone":
                return 1000, "gone"
            if kind == "bad":
                return CLOSE_PROTOCOL, "bad frame"
            if kind == "nudge":
                if conn.verified:
                    await _send(ws, type="job_available", job_id=item[1],
                                task_id=await asyncio.to_thread(_task_of, item[1]))
                continue
            if kind == "env_nudge":
                if conn.verified:
                    await _send(ws, type="env_available")
                continue
            if kind == "gemini_key":   # (rid, action, key): the one frame that carries a key, sent once and not kept
                if conn.verified:
                    frame = {"type": "gemini_key", "id": item[1], "action": item[2]}
                    if item[2] == "set":
                        frame["key"] = item[3]
                    await _send(ws, **frame)
                else:
                    waiter = conn.key_waiters.get(item[1])
                    if waiter and not waiter.done():
                        waiter.set_exception(KeyPushFailed("the runner is not connected"))
                continue
            if kind == "tls_rotate":   # (rid, cert_pem, old_spki_sha256): signed per connection, with the host this runner dialled
                if conn.verified:
                    try:
                        sig = await asyncio.to_thread(runner_identity.sign, tls_rotate_message(_pem_der(item[2]), item[3], host))
                    except Exception:  # noqa: BLE001 - a bad certificate: nothing is sent
                        log.warning("runner ws: tls_rotate not sent to device %s", device_id)
                        continue
                    await _send(ws, type="tls_rotate", id=item[1], cert_pem=item[2], sig=base64.b64encode(sig).decode())
                continue
            if kind == "ca_nudge":
                if conn.verified:
                    await _send(ws, type="ca_available")
                continue
            data = item[1]   # kind == "rx": a frame from the runner
            try:
                big = len(data) <= MAX_BIG_FRAME and conn.verified and bool(BIG_PREFIX.match(data))
                msg = json.loads(data) if len(data) <= MAX_FRAME or big else None
                if big and isinstance(msg, dict) and msg.get("type") != "agent_file":
                    msg = None
            except ValueError:
                msg = None
            if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
                refused("malformed frame")
                return CLOSE_PROTOCOL, "malformed frame"
            mtype = msg["type"]
            if mtype == "hello" and client_nonce is None:
                client_nonce = _b64d(msg.get("client_nonce"), NONCE_BYTES)
                if client_nonce is None:
                    refused("bad client nonce")
                    return CLOSE_PROTOCOL, "bad client nonce"
                legacy_labels = msg.get("labels") != HELLO_LABELS
                next_challenge = now   # challenge at once
            elif mtype == "proof" and pending is not None and client_nonce is not None:
                nonce, _deadline = pending
                pending = None
                sig = _b64d(msg.get("signature"))
                dev = await asyncio.to_thread(_device_live, device_id)
                ok = bool(dev and sig and any(_verify(
                    dev["pubkey"], transcript(label, client_nonce, nonce, device_id, host), sig)
                    for label in (DEVICE_LABEL, LEGACY_DEVICE_LABEL)))
                if not ok:
                    refused("failed the signed challenge" if dev else "device no longer live")
                    return CLOSE_AUTH, "challenge failed"
                await asyncio.to_thread(_touch, device_id)
                next_challenge = now + challenge_every
                if first:
                    first = False
                    conn.verified = True
                    older = _promote(conn)
                    if older:
                        older.post(("close", CLOSE_REPLACED, "replaced"))
                    await _send(ws, type="ready", v=PROTOCOL, challenge_seconds=int(challenge_every))
                    for j in await asyncio.to_thread(jobs.claimable_jobs, device_id):   # queued before it connected
                        await _send(ws, type="job_available", job_id=j["job_id"], task_id=j["task_id"])
                    from app import ca, environment
                    if await asyncio.to_thread(ca.queued_for_device, device_id):   # a chat turn queued before it connected
                        await _send(ws, type="ca_available")
                    if await asyncio.to_thread(environment.queued_for_device, device_id):   # an environment check queued while it was off
                        await _send(ws, type="env_available")
            elif mtype == "attention" and conn.verified:   # no reply; only known kinds are kept, in memory
                kinds = msg.get("kinds")
                conn.attention = tuple(k for k in dict.fromkeys(k for k in (kinds[:MAX_ATTENTION] if isinstance(kinds, list) else [])
                                                                 if isinstance(k, str)) if k in ATTENTION_TEXT)
                conn.findings = clean_findings(msg.get("findings"))
                conn.capabilities = clean_capabilities(msg.get("capabilities"))
            elif mtype == "gemini_key_result" and conn.verified:   # the reply to a gemini_key request: ok, a fingerprint or a fixed error word
                waiter = conn.key_waiters.get(msg.get("id")) if isinstance(msg.get("id"), str) else None
                if waiter and not waiter.done():
                    fp = msg.get("fingerprint")
                    waiter.set_result({"ok": msg.get("ok") is True,
                                       "fingerprint": fp if isinstance(fp, str) and re.fullmatch(r"[0-9a-f]{8}", fp) else "",
                                       "error": str(msg.get("error") or "")[:120] if msg.get("ok") is not True else ""})
            elif mtype == "tls_rotate_result" and conn.verified:   # the runner verified the new certificate and trusts it too
                want = conn.rotations.pop(msg.get("id"), None) if isinstance(msg.get("id"), str) else None
                if want and msg.get("ok") is True and msg.get("fp") == want:
                    await asyncio.to_thread(_store_tls_ack, device_id, want)
            elif mtype == "ping":
                await _send(ws, type="pong")
            elif mtype == "agent_file" and conn.verified:   # chunked: reassembled here (event loop; one file per connection)
                whole, reply = assemble_file(conn, msg)
                await _send(ws, **(reply if reply is not None else await asyncio.to_thread(_job_message, device_id, whole)))
            elif mtype in JOB_MESSAGES and conn.verified:
                await _send(ws, **await asyncio.to_thread(_job_message, device_id, msg))
            elif mtype in ENV_MESSAGES and conn.verified:
                await _send(ws, **await asyncio.to_thread(_env_message, device_id, msg))
            elif mtype in CA_MESSAGES and conn.verified:
                reply = await asyncio.to_thread(_ca_message, device_id, msg)
                if reply is not None:
                    await _send(ws, **reply)
            else:
                refused(f"unexpected {mtype[:20]!r} frame")
                return CLOSE_PROTOCOL, "unexpected frame"

        if client_nonce is None:
            if now >= hello_deadline:
                refused("no hello")
                return CLOSE_TIMEOUT, "no hello"
        elif pending is not None:
            if now >= pending[1]:
                refused("challenge timed out")
                return CLOSE_TIMEOUT, "challenge timed out"
        elif now >= next_challenge:
            nonce = secrets.token_bytes(NONCE_BYTES)
            pending = (nonce, now + proof_timeout)
            proof = await asyncio.to_thread(server_proof, client_nonce, nonce, device_id, host, legacy_labels)
            await _send(ws, type="challenge", server_nonce=base64.b64encode(nonce).decode(),
                        server_proof=base64.b64encode(proof).decode(),
                        server_public_key=runner_identity.public_key(), fp8=runner_identity.fp8(), device_id=device_id)
        if now >= next_recheck:
            next_recheck = now + recheck_every
            if not await asyncio.to_thread(_device_live, device_id):   # the backstop: expiry, a disabled user, a missed signal
                refused("device no longer live")
                return CLOSE_AUTH, "no longer authorised"
