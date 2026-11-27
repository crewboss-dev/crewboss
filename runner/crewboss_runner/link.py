"""The runner's one outbound socket to `WS /runner/ws` (docs/runner-protocol.md).

Handshake: hello -> the server's challenge (its proof is checked against the key PINNED at enrolment, never the key in the
frame) -> our signed proof -> ready. The server re-challenges every ~20 s; each is verified and answered the same way.
After ready the socket carries `job_available` nudges in and claim / heartbeat / finish requests out (rpc())."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import secrets as pysecrets
import struct
import time
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from websockets.exceptions import ConnectionClosed
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from . import tlspin

log = logging.getLogger("crewboss-runner.link")

# Protocol identifiers, not names (see app/runner_ws.py). The hello says "labels": "crewboss"; a server that has not been updated
# ignores that and answers under the pre-rename labels, which this runner still accepts (and then signs under too).
SERVER_LABEL = b"crewboss-runner-ws/v1/server\0"
DEVICE_LABEL = b"crewboss-runner-ws/v1/device\0"
LEGACY_SERVER_LABEL = b"mallen-runner-ws/v1/server\0"   # TODO(#589 compat): drop once every server speaks the crewboss labels
LEGACY_DEVICE_LABEL = b"mallen-runner-ws/v1/device\0"
CLOSE_AUTH, CLOSE_REPLACED = 4401, 4409
BACKOFF_MIN, BACKOFF_MAX, BACKOFF_REPLACED = 1.0, 60.0, 300.0


def transcript(label: bytes, client_nonce: bytes, server_nonce: bytes, device_id: int, host: str) -> bytes:
    h = host.strip().lower().encode("utf-8")
    return label + client_nonce + server_nonce + struct.pack(">Q", device_id) + struct.pack(">H", len(h)) + h


def host_header(hostport: str, scheme: str) -> str:
    """The Host the WebSocket library writes: default ports (443 for wss, 80 for ws) are omitted. This is what is signed."""
    host, _, port = hostport.rpartition(":")
    default = "443" if scheme == "https" else "80"
    return host if port == default else hostport


def ws_url(hostport: str, scheme: str) -> str:
    return ("wss://" if scheme == "https" else "ws://") + host_header(hostport, scheme) + "/runner/ws"


class Revoked(Exception):
    """Closed with 4401: the device was revoked / expired, or our proof was refused. Stop; re-enrol."""


class Replaced(Exception):
    """Closed with 4409: another socket of this device proved the key. Long backoff."""


class PinFailure(Exception):
    """The server's proof didn't verify against the pinned key. We do not answer."""


class LinkDown(Exception):
    """A request couldn't be sent or answered because the socket is not (or no longer) up."""


def next_delay(current: float | None, code: int | None) -> float:
    """Reconnect delay after a failure: 4409 waits minutes; anything else doubles from 1 s up to 60 s (jittered)."""
    if code == CLOSE_REPLACED:
        return BACKOFF_REPLACED
    base = BACKOFF_MIN if current is None else min(max(current, BACKOFF_MIN) * 2, BACKOFF_MAX)
    return base


class Link:
    def __init__(self, cfg: dict, device_key: Ed25519PrivateKey, device_token: str, connect=None, sleep=asyncio.sleep,
                 ssl=None):
        self.cfg, self.key, self.token = cfg, device_key, device_token
        self.device_id = int(cfg["device_id"])
        self.pinned = Ed25519PublicKey.from_public_bytes(base64.b64decode(cfg["server_public_key"]))
        self.host = host_header(cfg["server"], cfg.get("scheme", "https"))
        self.url = ws_url(cfg["server"], cfg.get("scheme", "https"))
        self._connect, self._sleep, self._ssl = connect, sleep, ssl
        self.state = "connecting"      # connecting | ready | backoff | revoked | stopped
        self.nudges = asyncio.Event()  # set by job_available
        self.ca_nudges = asyncio.Event()   # set by ca_available: a CA chat turn is waiting (task #702)
        self.env_nudges = asyncio.Event()  # set by env_available: an environment check is waiting (#810)
        self.ca_stopped: set[int] = set()  # turn ids the server told us to stop (ca_stop)
        self.ready = asyncio.Event()
        self.sessions = 0              # how many times the socket has become ready (a reconnect = a new server-side session)
        self._ws = None
        self._proven = False           # per connection: set only once the server's proof verified against the pinned key
        self._pending: dict[str, asyncio.Future] = {}
        self._next_id = 0
        self.stopping = False
        self.last_error = ""
        self.on_change = lambda: None
        self.on_tls = lambda: None          # called after cfg["tls_certs"] / ["tls_pin"] changed: persist it (cli.run_async)
        self.on_gemini_key = lambda: None   # called after the key file was written or removed (the worker re-checks its capability)

    # --- requests ------------------------------------------------------------------------------------------------
    async def rpc(self, mtype: str, timeout: float = 30.0, **fields) -> dict:
        ws = self._ws
        if ws is None or not self.ready.is_set():
            raise LinkDown("socket not ready")
        self._next_id += 1
        rid = f"r{self._next_id}"
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await ws.send(json.dumps({"type": mtype, "id": rid, **fields}, separators=(",", ":")))
            return await asyncio.wait_for(fut, timeout)
        except (asyncio.TimeoutError, OSError) as exc:
            raise LinkDown(f"no answer to {mtype}") from exc
        except Exception as exc:  # noqa: BLE001 — a closed socket
            if isinstance(exc, LinkDown):
                raise
            raise LinkDown(str(exc)) from exc
        finally:
            self._pending.pop(rid, None)

    async def notify(self, mtype: str, **fields) -> bool:
        """Send a frame that has no reply (e.g. `attention`). False when the socket is not ready."""
        ws = self._ws
        if ws is None or not self.ready.is_set():
            return False
        try:
            await ws.send(json.dumps({"type": mtype, **fields}, separators=(",", ":")))
        except Exception:  # noqa: BLE001 — a closed socket; the next session re-sends
            return False
        return True

    def _fail_pending(self) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(LinkDown("socket closed"))
        self._pending.clear()

    def _set(self, state: str, err: str = "") -> None:
        self.state, self.last_error = state, err
        self.on_change()

    # --- the loop ------------------------------------------------------------------------------------------------
    async def run(self) -> None:
        delay: float | None = None
        while not self.stopping:
            fixed = None
            was_ready = False
            try:
                await self._session()
            except Revoked:
                self._set("revoked", "revoked, expired or refused (4401): re-enrol this runner")
                self.ready.clear()
                return
            except Replaced:
                fixed = BACKOFF_REPLACED
                self._set("backoff", "replaced by another socket of this device (4409)")
            except PinFailure:
                fixed = BACKOFF_REPLACED
                self._set("backoff", "the server's proof did not match the pinned key")
            except Exception as exc:  # noqa: BLE001 — refused upgrade, network, protocol: back off
                self._set("backoff", f"{type(exc).__name__}: {exc}"[:200])
            finally:
                was_ready = self.ready.is_set()
                self.ready.clear()
                self._ws = None
                self._fail_pending()
            if self.stopping:
                break
            if was_ready:
                delay = None   # it worked for a while: start the backoff over
            delay = fixed if fixed else next_delay(delay, None)
            if self.state != "backoff":
                self._set("backoff")
            await self._sleep(delay * random.uniform(0.8, 1.2))
        if self.state != "revoked":
            self._set("stopped", self.last_error)

    async def _session(self) -> None:
        connect = self._connect
        if connect is None:
            from websockets.asyncio.client import connect
        self._set("connecting")
        kw = {"additional_headers": {"Authorization": f"Bearer {self.token}"}, "max_size": 1 << 20,
              "open_timeout": 15, "ping_interval": 20, "ping_timeout": 20}   # NO origin: a runner never sends one
        if self._ssl is None and self.url.startswith("wss://") and tlspin.is_pinned(self.cfg):
            # Trust the pinned KEY: a same-key renewal is accepted here (key and host name checked, nothing sent), then the normal
            # certificate-verifying context below is built from the stored certificates.
            if await asyncio.to_thread(tlspin.trust_by_pin, self.cfg):
                self.on_tls()
        ctx = self._ssl if self._ssl is not None else (tlspin.context_from_cfg(self.cfg) if self.url.startswith("wss://") else None)
        if ctx is not None:   # a pinned (LAN) profile trusts ONLY its stored certificates; no pin = the system store, as before
            kw["ssl"] = ctx
        async with connect(self.url, **kw) as ws:
            self._ws = ws
            self._proven = False
            client_nonce = pysecrets.token_bytes(32)
            await ws.send(json.dumps({"type": "hello", "labels": "crewboss",
                                       "client_nonce": base64.b64encode(client_nonce).decode()}))
            try:
                async for raw in ws:
                    await self._handle(ws, raw, client_nonce)
            except ConnectionClosed:
                pass   # judged by the close code below
            code = getattr(ws, "close_code", None)
            if code == CLOSE_AUTH:
                raise Revoked
            if code == CLOSE_REPLACED:
                raise Replaced
            if code not in (None, 1000, 1001):
                raise ConnectionError(f"closed with {code}")
            if code is None:
                raise ConnectionError("connection lost")

    async def _handle(self, ws, raw, client_nonce: bytes) -> None:
        if not isinstance(raw, str):
            raise ConnectionError("binary frame")
        try:
            msg = json.loads(raw)
        except ValueError:
            raise ConnectionError("bad frame") from None
        mtype = msg.get("type") if isinstance(msg, dict) else None
        if mtype != "challenge" and not self._proven:
            # Nothing but a challenge is accepted from a peer that has not proven the pinned key. Log the type's length only.
            log.warning("frame before the server proved the pinned key: closing (no frame contents logged)")
            await ws.close(code=1008, reason="server not proven")
            raise PinFailure
        if mtype == "challenge":
            await self._answer(ws, msg, client_nonce)
        elif mtype == "ready":
            self.sessions += 1
            self._drop_old_certs(ws)
            self.ready.set()
            self._set("ready")
        elif mtype == "job_available":
            self.nudges.set()
        elif mtype == "ca_available":
            self.ca_nudges.set()
        elif mtype == "env_available":
            self.env_nudges.set()
        elif mtype == "gemini_key":
            await self._gemini_key(ws, msg)
        elif mtype == "tls_rotate":   # only reachable once the server proved the pinned identity key (the gate above)
            await self._tls_rotate(ws, msg)
        elif mtype == "ca_stop":
            tid = msg.get("turn_id")
            if isinstance(tid, int) and not isinstance(tid, bool):
                self.ca_stopped.add(tid)
        elif isinstance(mtype, str) and mtype.endswith("_result"):
            fut = self._pending.get(msg.get("id"))
            if fut and not fut.done():
                fut.set_result(msg)
        elif mtype == "pong":
            pass
        else:
            log.warning("ignoring unknown frame type %r", str(mtype)[:30])

    @staticmethod
    def _peer_der(ws) -> bytes | None:
        """The certificate this socket's TLS session verified, when the transport can say (a fake test socket cannot)."""
        try:
            return ws.transport.get_extra_info("ssl_object").getpeercert(binary_form=True)
        except Exception:  # noqa: BLE001
            return None

    def _current_spki(self, ws) -> bytes | None:
        """sha256(SPKI) of the certificate this connection is on: the peer's, else the stored one the pin names."""
        der = self._peer_der(ws)
        if der:
            try:
                return tlspin.spki_sha256(der)
            except tlspin.PinError:
                return None
        for pem in tlspin.certs_of(self.cfg):
            try:
                d = tlspin.pem_to_der(pem)
                if tlspin.pin_matches(d, self.cfg.get("tls_pin", "")):
                    return tlspin.spki_sha256(d)
            except tlspin.PinError:
                continue
        return None

    async def _tls_rotate(self, ws, msg: dict) -> None:
        """The server announces a new certificate, signed by its IDENTITY key (not the TLS key: a stolen TLS key cannot push a pin). Verified
        against the pinned identity key under a label of its own; on success both certificates are kept as anchors and the new pin is acked."""
        rid = msg.get("id")
        reply: dict = {"type": "tls_rotate_result", "id": rid if isinstance(rid, str) else "", "ok": False}
        try:
            if not tlspin.is_pinned(self.cfg):
                raise ValueError("this runner is not pinned")
            pem = msg.get("cert_pem")
            der = tlspin.pem_to_der(pem)
            sig = base64.b64decode(msg.get("sig"), validate=True)
            old = self._current_spki(ws)
            if old is None:
                raise ValueError("no current certificate")
            self.pinned.verify(sig, tlspin.rotate_message(der, old, self.host))   # raises InvalidSignature
            if not tlspin.hostname_in_cert(der, urlsplit("//" + self.host).hostname or ""):
                raise ValueError("the new certificate does not name this server")
            pem = tlspin.der_to_pem(der)   # normalised
            certs = [c for c in tlspin.certs_of(self.cfg) if c != pem] + [pem]
            self.cfg["tls_certs"] = certs[-tlspin.MAX_CERTS:]
            self.on_tls()
            reply["ok"], reply["fp"] = True, tlspin.pin26_of(der)
        except Exception as exc:  # noqa: BLE001 - nothing changed; a fixed word, never the exception text
            log.warning("tls_rotate refused (%s)", type(exc).__name__)
            reply["error"] = "refused"
        await ws.send(json.dumps(reply, separators=(",", ":")))

    def _drop_old_certs(self, ws) -> None:
        """Once a session is ready on the NEWEST stored certificate, the older ones are no longer trusted."""
        certs = tlspin.certs_of(self.cfg)
        der = self._peer_der(ws)
        if len(certs) < 2 or not der:
            return
        newest = certs[-1]
        try:
            if tlspin.der_to_pem(der) != newest and tlspin.pem_to_der(newest) != der:
                return
            self.cfg["tls_certs"], self.cfg["tls_pin"] = [newest], tlspin.pin26_of(der)
        except tlspin.PinError:
            return
        self.on_tls()

    async def _gemini_key(self, ws, msg: dict) -> None:
        """The dashboard sends a Google API key (action "set") or asks to remove it ("clear"): write or delete the runner's private key file and
        answer {ok, fingerprint | error}. The frame is the only place the key passes through here: it is never logged and never in a reply."""
        from . import gemini
        rid, action = msg.get("id"), msg.get("action")
        reply: dict = {"type": "gemini_key_result", "id": rid if isinstance(rid, str) else "", "ok": False}
        try:
            if action == "set":
                reply["fingerprint"] = await asyncio.to_thread(gemini.store_key, msg.get("key"))
            elif action == "clear":
                await asyncio.to_thread(gemini.remove_key)
            else:
                raise gemini.GeminiError("unknown action")
            reply["ok"] = True
        except gemini.GeminiError as exc:
            reply["error"] = str(exc)[:80]
        except Exception:  # noqa: BLE001 - fixed text only: an exception's text could hold a path
            reply["error"] = "could not update the key file"
        msg.pop("key", None)
        await ws.send(json.dumps(reply, separators=(",", ":")))
        if reply["ok"]:
            self.on_gemini_key()

    async def _answer(self, ws, msg: dict, client_nonce: bytes) -> None:
        try:
            server_nonce = base64.b64decode(msg["server_nonce"], validate=True)
            proof = base64.b64decode(msg["server_proof"], validate=True)
            ok = len(server_nonce) == 32 and msg.get("device_id") == self.device_id
            device_label = DEVICE_LABEL
            if ok:
                try:
                    self.pinned.verify(proof, transcript(SERVER_LABEL, client_nonce, server_nonce, self.device_id, self.host))
                except InvalidSignature:   # a server that predates the rename: its proof is under the old label, and so is our answer
                    self.pinned.verify(proof, transcript(LEGACY_SERVER_LABEL, client_nonce, server_nonce, self.device_id, self.host))
                    device_label = LEGACY_DEVICE_LABEL
        except (InvalidSignature, KeyError, ValueError, TypeError):
            ok = False
        if not ok:   # do not answer an unproven server
            await ws.close(code=1008, reason="server proof failed")
            raise PinFailure
        self._proven = True
        sig = self.key.sign(transcript(device_label, client_nonce, server_nonce, self.device_id, self.host))
        await ws.send(json.dumps({"type": "proof", "signature": base64.b64encode(sig).decode()}))

    async def stop(self) -> None:
        self.stopping = True
        ws = self._ws
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
