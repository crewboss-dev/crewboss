"""The live runner conversation (#89): what a job's agent says and does, stored per job and shown read-only in the task window.

A runner reads Claude Code's own session transcript (and its local Gemini tool's log), turns each entry into a small frame, scrubs it with its
redact and sends batches over its verified socket as `job_activity` (app/runner_ws.py). This module is the server's half:

* `accept(device_id, job_id, fence, frames)`: ONLY the device that holds the job at exactly this fence (jobs._running, the same test as a
  heartbeat). A stale fence is jobs.StaleFence (409). Every frame is cleaned again here (the runner is not trusted to have done it): a known
  speaker and kind, text scrubbed with deploys.redact and cut to MAX_TEXT, meta cut to a few short scalar fields. A job is capped at MAX_ROWS rows
  and MAX_BYTES of text; past a cap one "truncated" note is stored and the rest is dropped (the heartbeat still works).
* `history(job_id, after, limit)`: rows after a seq, for the window when it opens or reconnects.
* `purge()`: rows older than RETENTION_DAYS (called by the job sweeper, at most hourly).

Everything stored is UNTRUSTED text (the agent read a repository and a task a client may have written): it is only ever rendered escaped."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time

from app import deploys, jobs
from app.db import get_cursor

log = logging.getLogger("crewboss.job-activity")

SPEAKERS = ("claude", "gemini", "runner")
KINDS = ("assistant_text", "tool_call", "tool_result", "permission_request", "status", "note")
MAX_TEXT = 2048            # characters of one frame's text (the runner cuts earlier; the socket takes 8 KiB a message)
MAX_FRAMES = 40            # frames in one message
MAX_ROWS = 4000            # rows kept per job (the 'truncated' note included)
MAX_BYTES = 2 * 1024 * 1024  # characters of text kept per job
MAX_META = 600             # bytes of one frame's meta, serialised
RETENTION_DAYS = 30
PURGE_EVERY = 3600.0
META_KEYS = {"tool": 80, "path": 300, "status": 20, "state": 20, "is_error": 0, "added": 0, "removed": 0, "session_url": 300, "model": 60, "truncated": 0}
SESSION_URL = re.compile(r"https://claude\.ai/code/session_[A-Za-z0-9_-]{8,64}")   # fullmatch: the only link a frame may carry
# KEY=VALUE where the key names a secret: the value goes (the runner does the same first; this is the second look). PIN and PSK only as a whole word
# of the key (ADMIN_PIN, wifi-psk), the rest anywhere in it (PGPASSWORD, API_KEY, github_token).
_ASSIGN = re.compile(r"(?<![A-Za-z0-9_.-])([A-Za-z_][A-Za-z0-9_.-]{0,80})([ \t]*=[ \t]*)(\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s\"']+)")
_SECRET_KEY = re.compile(r"(?i)pass|pwd|secret|token|key|auth|cred|(?:^|[_.-])(?:pin|psk)(?:$|[_.-])")
_last_purge = 0.0
_waiters: dict[int, set] = {}
_wlock = threading.Lock()


def scrub_assignments(text: str) -> str:
    """KEY=VALUE with a secret-looking key -> KEY=[removed] (an .env line, PGPASSWORD=..., ADMIN_PIN=...)."""
    return _ASSIGN.sub(lambda m: f"{m.group(1)}{m.group(2)}[removed]" if _SECRET_KEY.search(m.group(1)) else m.group(0), text)


def clean_text(text: str) -> str:
    return scrub_assignments(deploys.redact(text.replace("\x00", "")))


def wake(job_id: int) -> None:
    """Frames were stored: wake the streams watching this job (any thread)."""
    with _wlock:
        subs = list(_waiters.get(job_id, ()))
    for loop, ev in subs:
        try:
            loop.call_soon_threadsafe(ev.set)
        except RuntimeError:       # that loop is gone
            pass


class Watch:
    """An asyncio.Event a stream waits on; `with Watch(job_id) as w: await w.wait(2.0)`."""

    def __init__(self, job_id: int):
        self.job_id, self.ev = job_id, asyncio.Event()

    def __enter__(self):
        with _wlock:
            _waiters.setdefault(self.job_id, set()).add((asyncio.get_running_loop(), self.ev))
        return self

    def __exit__(self, *exc):
        with _wlock:
            subs = _waiters.get(self.job_id, set())
            subs = {x for x in subs if x[1] is not self.ev}
            if subs:
                _waiters[self.job_id] = subs
            else:
                _waiters.pop(self.job_id, None)

    async def wait(self, timeout: float) -> bool:
        """True if woken, False on the timeout."""
        try:
            await asyncio.wait_for(self.ev.wait(), timeout)
            woken = True
        except asyncio.TimeoutError:
            woken = False
        self.ev.clear()
        return woken


def _meta(raw) -> dict:
    out: dict = {}
    if not isinstance(raw, dict):
        return out
    url = raw.get("session_url")
    if isinstance(url, str) and SESSION_URL.fullmatch(url):   # never redacted (a session id is a long opaque run, which redact would remove) and never anything else
        out["session_url"] = url
    for key, cap in META_KEYS.items():
        if key == "session_url":
            continue
        v = raw.get(key)
        if isinstance(v, bool) or (isinstance(v, int) and abs(v) < 10 ** 9):
            out[key] = v
        elif isinstance(v, str) and cap:
            out[key] = deploys.redact(v)[:cap]
    return out if len(json.dumps(out)) <= MAX_META else {}


def clean_frame(raw) -> dict | None:
    """One frame as the runner sent it -> the row to store, or None if it is not a frame at all."""
    if not isinstance(raw, dict):
        return None
    speaker, kind, rseq = raw.get("speaker", "claude"), raw.get("kind"), raw.get("seq")
    if speaker not in SPEAKERS or kind not in KINDS or not isinstance(rseq, int) or isinstance(rseq, bool) or rseq < 0:
        return None
    text = raw.get("text")
    text = clean_text(text) if isinstance(text, str) else ""
    return {"speaker": speaker, "kind": kind, "rseq": rseq, "text": text[:MAX_TEXT], "meta": _meta(raw.get("meta"))}


def accept(device_id: int, job_id: int, fence: int, frames) -> dict:
    """Store a batch. -> {accepted, dropped}. jobs.StaleFence when this device does not hold the job at this fence."""
    if not isinstance(frames, list):
        raise jobs.BadRequest("frames must be a list")
    rows = [f for f in (clean_frame(x) for x in frames[:MAX_FRAMES]) if f]
    with get_cursor() as cur:
        jobs._running(cur, job_id, fence, device_id)          # locks the job row: batches of one job are serialised
        cur.execute("SELECT COALESCE(max(seq), 0) AS seq, COALESCE(max(rseq) FILTER (WHERE fence = %s), -1) AS rseq, count(*) AS n, "
                    "COALESCE(sum(length(text)), 0) AS bytes, COALESCE(bool_or(meta ? 'truncated'), false) AS cut "
                    "FROM job_activity WHERE job_id = %s", (fence, job_id))
        st = cur.fetchone()
        seq, last_r, n, size, cut = st["seq"], st["rseq"], st["n"], st["bytes"], st["cut"]
        accepted = dropped = 0
        for f in rows:
            if f["rseq"] <= last_r:                      # a batch the runner re-sent: already stored
                continue
            last_r = f["rseq"]
            if n >= MAX_ROWS - 1 or size + len(f["text"]) > MAX_BYTES:
                if not cut:
                    seq += 1
                    cur.execute("INSERT INTO job_activity (job_id, seq, fence, rseq, speaker, kind, text, meta) VALUES (%s, %s, %s, %s, 'runner', 'note', %s, %s)",
                                (job_id, seq, fence, f["rseq"], "… truncated: the live view of this job reached its size limit", json.dumps({"truncated": True})))
                    cut, n = True, n + 1
                dropped += 1
                continue
            seq += 1
            cur.execute("INSERT INTO job_activity (job_id, seq, fence, rseq, speaker, kind, text, meta) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        (job_id, seq, fence, f["rseq"], f["speaker"], f["kind"], f["text"], json.dumps(f["meta"])))
            n, size, accepted = n + 1, size + len(f["text"]), accepted + 1
    if accepted:
        wake(job_id)
    return {"accepted": accepted, "dropped": dropped}


def _row(r: dict) -> dict:
    return {"seq": r["seq"], "at": r["at"].isoformat(), "speaker": r["speaker"], "kind": r["kind"], "text": r["text"], "meta": r["meta"] or {}}


def history(job_id: int, after: int = 0, limit: int = 500) -> list[dict]:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT seq, at, speaker, kind, text, meta FROM job_activity WHERE job_id = %s AND seq > %s ORDER BY seq LIMIT %s",
                    (job_id, max(int(after), 0), max(1, min(int(limit), 1000))))
        return [_row(r) for r in cur.fetchall()]


def session_url(job_id: int) -> str:
    """The latest claude.ai/code link a status frame of this job carried ('' if none): one indexed look, not a scan of the history."""
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT meta->>'session_url' AS u FROM job_activity WHERE job_id = %s AND kind = 'status' AND meta ? 'session_url' ORDER BY seq DESC LIMIT 1", (job_id,))
        row = cur.fetchone()
    return row["u"] if row and row["u"] and SESSION_URL.fullmatch(row["u"]) else ""


def for_viewer(frames: list[dict], can_see_results: bool) -> list[dict]:
    """People who may only VIEW the project read Claude's messages and the tool-call rows; a tool's output (file contents, command output) is for
    those who can run jobs or edit the project: its text is blanked and the row says so."""
    if can_see_results:
        return frames
    return [{**f, "text": "", "meta": {**f["meta"], "restricted": True}} if f["kind"] == "tool_result" else f for f in frames]


def purge(force: bool = False) -> int:
    """Rows past RETENTION_DAYS. Throttled to once an hour unless forced. -> rows deleted (0 when throttled)."""
    global _last_purge
    now = time.monotonic()
    if not force and now - _last_purge < PURGE_EVERY:
        return 0
    _last_purge = now
    with get_cursor() as cur:
        cur.execute("DELETE FROM job_activity WHERE at < now() - make_interval(days => %s)", (RETENTION_DAYS,))
        return cur.rowcount
