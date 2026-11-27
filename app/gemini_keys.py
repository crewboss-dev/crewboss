"""A Google API key for a runner's Gemini tools (#861), sent from the runner's Machines card.

Crewboss is a pass-through here. The key travels once, from the posted form to the runner over its existing authenticated socket
(runner_ws.push_gemini_key), and the runner writes it to its own private key file. This module keeps ONLY the key's sha256 prefix (8 hex),
its last four characters and a set_at time per runner (device_tokens.gemini_key_*), to say "key ending ...abcd". Nothing here logs, stores or
returns the key; error text is fixed words; the activity rows name the runner and the last four characters, never the key.

Who may set it: a person on their OWN runner, or the account's owner on any runner (the route also asks the owner for a fresh step-up
when the runner is not their own). A runner that is offline is an error: nothing is queued."""
from __future__ import annotations

import hashlib
import re
from concurrent.futures import ThreadPoolExecutor

from app import repo, runner_ws
from app.db import get_cursor
from app.repo import BadRequest, Conflict, NotFound

KEY_RE = re.compile(r"[A-Za-z0-9_-]{20,200}")      # what a Google API key looks like; anything else is refused without echoing it
OFFLINE = "That runner is offline. Try again when it is connected: a key is never queued."


def fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]


def clean(key) -> str:
    """The pasted key, trimmed, or BadRequest with a fixed message (never the value)."""
    key = key.strip() if isinstance(key, str) else ""
    if not KEY_RE.fullmatch(key):
        raise BadRequest("That does not look like a Google API key (letters, digits, - and _, no spaces). Paste only the key.")
    return key


def _devices(account_id: int, user_id: int | None, device_id: int, everywhere: bool) -> list[dict]:
    """The runners to act on: this one, or (everywhere) every active runner of the same person. A person only ever reaches their own;
    for the owner (user_id None) "everywhere" means every runner of the same person as the one on the card."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT id, name, user_id FROM device_tokens WHERE id = %s AND account_id = %s AND revoked_at IS NULL AND expires_at > now() "
                    "AND (%s::int IS NULL OR user_id = %s)", (device_id, account_id, user_id, user_id))
        first = cur.fetchone()
        if not first:
            raise NotFound(f"device {device_id} not found")
        if not everywhere:
            return [dict(first)]
        cur.execute("SELECT id, name, user_id FROM device_tokens WHERE account_id = %s AND user_id = %s AND revoked_at IS NULL "
                    "AND expires_at > now() ORDER BY id", (account_id, first["user_id"]))
        return [dict(r) for r in cur.fetchall()]


def _record(device: dict, account_id: int, by: str, fp, last4, what: str, state: str | None = None) -> None:
    """Keep what the runner confirmed (fp, last4) or, when it did not, a state: 'unconfirmed' (no answer) or 'unknown' (a different fingerprint
    came back). A state always clears the fingerprint and last four: the old record is no longer known to be true."""
    with get_cursor() as cur:
        cur.execute("UPDATE device_tokens SET gemini_key_fp = %s, gemini_key_last4 = %s, gemini_key_state = %s, "
                    "gemini_key_set_at = CASE WHEN %s::text IS NULL THEN NULL ELSE now() END WHERE id = %s", (fp, last4, state, fp, device["id"]))
        repo.record_activity(cur, account_id, by[:100], "set" if fp else "removed", "device", device["id"], what)


OLDER = "did not answer (it may be on an older runner version: update it, then paste again)"


def _push(device: dict, action: str, key: str = "") -> dict:
    """One runner. Returns {"device", "ok", "error", "fingerprint", "timeout"}; the error is a fixed sentence."""
    try:
        res = runner_ws.push_gemini_key(device["id"], action, key)
    except runner_ws.RunnerOffline:
        return {"device": device, "ok": False, "error": OFFLINE, "fingerprint": "", "timeout": False}
    except runner_ws.KeyPushFailed:
        return {"device": device, "ok": False, "error": OLDER, "fingerprint": "", "timeout": True}
    if not res.get("ok"):
        return {"device": device, "ok": False, "error": "The runner could not write the key file" + (f" ({res['error']})" if res.get("error") else "") + ".",
                "fingerprint": "", "timeout": False}
    return {"device": device, "ok": True, "error": "", "fingerprint": res.get("fingerprint", ""), "timeout": False}


def _one_set(d: dict, account_id: int, by: str, key: str, fp: str, last4: str) -> dict:
    r = _push(d, "set", key)
    if r["ok"] and r["fingerprint"] != fp:
        r = {**r, "ok": False, "error": "The runner stored something different from what was sent, so the key was cleared. Paste it again."}
        _push(d, "clear")
        _record(d, account_id, by, None, None, f"Google API key on runner {d['id']} could not be confirmed (cleared)", "unknown")
    elif r["ok"]:
        _record(d, account_id, by, fp, last4, f"Google API key sent to runner {d['id']} (key ending {last4})")
    elif r["timeout"]:
        _record(d, account_id, by, None, None, f"Google API key on runner {d['id']} is unconfirmed (no answer)", "unconfirmed")
    return r


def _one_clear(d: dict, account_id: int, by: str) -> dict:
    r = _push(d, "clear")
    if r["ok"]:
        _record(d, account_id, by, None, None, f"Google API key removed from runner {d['id']}")
    elif r["timeout"]:
        _record(d, account_id, by, None, None, f"Google API key on runner {d['id']} is unconfirmed (no answer)", "unconfirmed")
    return r


def _each(devices: list[dict], fn) -> list[dict]:
    """Every runner at once, with ONE overall wait (the slowest runner's, not the sum). Results keep the devices' order."""
    if len(devices) == 1:
        return [fn(devices[0])]
    with ThreadPoolExecutor(max_workers=min(len(devices), 16)) as pool:
        return list(pool.map(fn, devices))


def set_key(account_id: int, device_id: int, key, by: str, user_id: int | None = None, everywhere: bool = False) -> list[dict]:
    """Send the key to a runner (or all the same person's runners, concurrently). Per-runner results; a fingerprint the runner returns that is not
    the one computed here counts as a failure. Raises BadRequest (bad key), NotFound, and Conflict when the only runner asked is offline."""
    key = clean(key)
    fp, last4 = fingerprint(key), key[-4:]
    devices = _devices(account_id, user_id, device_id, everywhere)
    results = _each(devices, lambda d: _one_set(d, account_id, by, key, fp, last4))
    if len(results) == 1 and results[0]["error"] == OFFLINE:
        raise Conflict(OFFLINE)
    return results


def remove_key(account_id: int, device_id: int, by: str, user_id: int | None = None, everywhere: bool = False) -> list[dict]:
    devices = _devices(account_id, user_id, device_id, everywhere)
    results = _each(devices, lambda d: _one_clear(d, account_id, by))
    if len(results) == 1 and results[0]["error"] == OFFLINE:
        raise Conflict(OFFLINE)
    return results


def summary(results: list[dict], removed: bool = False) -> str:
    """Fixed-text flash for a result list: counts and fixed sentences only (no runner names: a flash travels in a cookie; never a key)."""
    ok = sum(1 for r in results if r["ok"])
    errors = list(dict.fromkeys(r["error"] for r in results if not r["ok"]))
    text = ""
    if ok:
        text = ("Key removed from " if removed else "Key sent to ") + (f"{ok} runners. " if ok != 1 else "the runner. ")
    if errors:
        text += (f"{len(results) - ok} runner(s) failed: " if ok else "") + " ".join(errors)
    return text.strip()[:300]


def runner_user(account_id: int, device_id: int) -> int | None:
    """The person a runner belongs to (None when there is no such runner in the account)."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT user_id FROM device_tokens WHERE id = %s AND account_id = %s", (device_id, account_id))
        row = cur.fetchone()
    return row["user_id"] if row else None
