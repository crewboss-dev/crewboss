"""The live runner conversation, runner side (#89): turn what Claude Code does in a job into small frames and send them to Crewboss so the
person can WATCH it in the task window. Watch only: nothing here ever sends text to the agent.

Where the frames come from
  * Claude Code's own session transcript, a JSONL file it writes for the session: <config dir>/projects/<cwd slug>/<session id>.jsonl (the runner
    names the session with `--session-id`, so the file is known exactly). Its format is not ours: only `type` user/assistant and `message.content`
    blocks (text, tool_use, tool_result) are read; anything else is ignored, and a line that does not parse is skipped. If the file never
    appears, or lines arrive but none can be read, ONE status frame says "live view unavailable for this run" and the job carries on.
  * The local Gemini MCP server's log (gemini_mcp.py writes the question it was sent and Gemini's reply to a JSONL file beside the job), shown
    as a second speaker.
  * In a subscription (pty) session: a permission prompt on screen, and the claude.ai/code session link, found by a probe the worker gives us.

Frame (JSON, as sent):  {seq, speaker: claude|gemini|runner, kind, text, meta}
  kind  assistant_text | tool_call | tool_result | permission_request | status | note
  Claude's tool_call  text "Read app/x.py" | "Edit app/x.py (+12 -3)" | "$ <command, 200 chars>" | "<Tool> <short args>"; meta {tool, path?}
  tool_result         text = the first 500 characters of the output; meta {tool, status: ok|error, is_error}
  status              meta {state: working|idle|unavailable|session}, optional meta.session_url (https://claude.ai/code/...)
  gemini              speaker "gemini": kind tool_call = what Claude sent Gemini, assistant_text = Gemini's reply, note = an error
Every text goes through redact.redact, then is cut to MAX_TEXT; a job sends MAX_TOTAL characters at most, then one "truncated" note.
Sent as `job_activity {job_id, fence, seq (the first frame's), frames[]}` over the verified socket about every 0.5 s; the server answers
`job_activity_result` and a stale fence (409) ends the stream."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path

from .redact import redact

log = logging.getLogger("crewboss-runner.activity")

MAX_TEXT = 2000            # characters of one frame's text (an assistant message, a command line, a Gemini reply)
RESULT_CHARS = 500         # a tool result's first characters
COMMAND_CHARS = 200
MAX_TOTAL = 2 * 1024 * 1024   # characters sent for one job, then one "truncated" note
MAX_MESSAGE = 6800         # bytes of one socket message (the server's socket takes 8192; the batch is packed under this, as serialised)
FLUSH_SECONDS = 0.5
IDLE_SECONDS = 20.0        # no new transcript entry for this long while the agent runs: status idle
FIND_SECONDS = 45.0        # the transcript file must appear within this long, else "unavailable"
MAX_BACKLOG = 400          # frames held while the socket is down; older ones are dropped
UNREADABLE_LINES = 25      # this many lines with none readable = a format we do not know
UNAVAILABLE = "live view unavailable for this run"
SESSION_FIND = re.compile(r"https://claude\.ai/code/session_[A-Za-z0-9_-]{8,64}")
SESSION_URL = re.compile(r"https://claude\.ai/code/session_[A-Za-z0-9_-]{8,64}")   # fullmatch: the only link that is ever sent on


# --- where Claude Code keeps the transcript -----------------------------------------------------------------------------------------

def config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))


def cwd_slug(cwd: str) -> str:
    """Claude Code names a project's folder after its working directory with every non-alphanumeric character as '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def transcript_path(cwd: str, session_id: str, home: Path | None = None) -> Path | None:
    """The session's JSONL, or None if it is not there (yet). The expected place first, then any project folder that holds that session id
    (the working directory may be written through a symlink)."""
    home = home or config_dir()
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", session_id or ""):
        return None
    direct = home / "projects" / cwd_slug(cwd) / f"{session_id}.jsonl"
    if direct.is_file():
        return direct
    try:
        for p in (home / "projects").glob(f"*/{session_id}.jsonl"):
            return p
    except OSError:
        pass
    return None


# --- one transcript entry -> frames ---------------------------------------------------------------------------------------------------

def cut(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


_ASSIGN = re.compile(r"(?<![A-Za-z0-9_.-])([A-Za-z_][A-Za-z0-9_.-]{0,80})([ \t]*=[ \t]*)(\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s\"']+)")
_SECRET_KEY = re.compile(r"(?i)pass|pwd|secret|token|key|auth|cred|(?:^|[_.-])(?:pin|psk)(?:$|[_.-])")


def scrub_assignments(text: str) -> str:
    """KEY=VALUE where the key names a secret (PASS PWD PIN PSK SECRET TOKEN KEY AUTH CRED, any case) -> KEY=[removed]: an .env line, PGPASSWORD=...,
    ADMIN_PIN=.... PIN and PSK count only as a whole word of the key (so `shipping=` is left alone). The server does the same again."""
    return _ASSIGN.sub(lambda m: f"{m.group(1)}{m.group(2)}[removed]" if _SECRET_KEY.search(m.group(1)) else m.group(0), text)


def clean(text, limit: int = MAX_TEXT) -> str:
    return cut(scrub_assignments(redact(str(text or "").replace("\x00", ""))), limit)


# Files whose CONTENT is never shown, whatever redact would have caught: a result that read one is replaced by HIDDEN.
HIDDEN = "(hidden: sensitive file)"
_SENSITIVE_NAME = (r"\.env(?:[.\w-]*)|[^/\s'\"]*\.(?:pem|key|p12|pfx|kdbx)|id_[A-Za-z0-9_]+|\.netrc|\.pgpass|\.npmrc|\.git-credentials"
                   r"|[^/\s'\"]*credentials[^/\s'\"]*|[^/\s'\"]*secret[^/\s'\"]*|\.docker/config\.json")
SENSITIVE_PATH = re.compile(r"(?i)(?:^|/)(?:" + _SENSITIVE_NAME + r")$")
_SENSITIVE_IN_TEXT = re.compile(r"(?im)(?:^|[\s/'\"=:])(?:" + _SENSITIVE_NAME + r")(?=$|[\s:'\"])")
_READERS = re.compile(r"(?i)\b(?:cat|head|tail|less|more|grep|egrep|rg|ag|sed|awk|strings|xxd|od|bat|type|source|cp|base64|tr|cut|sort|nl|jq|yq|openssl|gpg)\b|(?:^|[;&|\s])\.\s")
_ENV_DUMP = re.compile(r"(?i)^\s*(?:sudo\s+)?(?:env|printenv|set|export\s+-p|declare\s+-x|declare\s+-p|compgen\s+-e)(?:\s|$|;|\|)")


def sensitive_path(path) -> bool:
    return isinstance(path, str) and bool(SENSITIVE_PATH.search(path.replace("\\", "/")))


def sensitive_command(cmd) -> bool:
    """A Bash command that prints the environment, or reads one of the sensitive files."""
    if not isinstance(cmd, str):
        return False
    for part in re.split(r"&&|\|\||;|\n", cmd):
        if _ENV_DUMP.match(part) or (_READERS.search(part) and _SENSITIVE_IN_TEXT.search(" " + part.replace("\\", "/"))):
            return True
    return bool(_ENV_DUMP.match(cmd))


def clean_path(path: str) -> str:
    """A path scrubbed part by part: redact treats a long unbroken run of letters (and '/') as a secret, which a deep path is not."""
    return "/".join(redact(part) for part in str(path or "").split("/"))[:300]


def _lines(s) -> int:
    return len(s.splitlines()) if isinstance(s, str) and s else 0


def describe_tool(name: str, args) -> tuple[str, dict]:
    """(the one-line text, meta) for a tool call: 'Read app/x.py', 'Edit app/x.py (+12 -3)', '$ pytest tests/x.py'."""
    args = args if isinstance(args, dict) else {}
    meta = {"tool": str(name)[:80]}
    path = args.get("file_path") or args.get("notebook_path") or args.get("path")
    if name in ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit") and isinstance(path, str):
        meta["path"] = clean_path(path)
        text = f"{name} {meta['path']}"
        if name == "Edit":
            added, removed = _lines(args.get("new_string")), _lines(args.get("old_string"))
            text += f" (+{added} −{removed})"
            meta.update(added=added, removed=removed)
        elif name == "Write":
            added = _lines(args.get("content"))
            text += f" (+{added})"
            meta["added"] = added
        return text, meta
    if name == "Bash" and isinstance(args.get("command"), str):
        return "$ " + clean(re.sub(r"\s*\n\s*", " ↵ ", args["command"].strip()), COMMAND_CHARS), meta
    for key in ("pattern", "query", "url", "prompt", "description", "command", "skill"):
        if isinstance(args.get(key), str) and args[key]:
            return f"{name} " + clean(args[key], 160), meta
    return str(name)[:80], meta


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str))
    return ""


class Translator:
    """Reads transcript entries (dicts) and returns frames without a seq (the streamer numbers them). Remembers which tool a result belongs to."""

    def __init__(self):
        self.tools: dict[str, str] = {}
        self.hide: set[str] = set()          # tool_use ids whose result is never shown (a sensitive file, the environment)
        self.readable = 0       # entries that were messages we understood
        self.lines = 0          # lines seen

    @staticmethod
    def sensitive_call(name, args) -> bool:
        args = args if isinstance(args, dict) else {}
        if name in ("Read", "Grep", "Glob", "NotebookEdit", "Edit", "Write"):
            return any(sensitive_path(args.get(k)) for k in ("file_path", "path", "notebook_path", "glob", "pattern"))
        if name == "Bash":
            return sensitive_command(args.get("command"))
        return False

    def entry(self, e) -> list[dict]:
        self.lines += 1
        if not isinstance(e, dict):
            return []
        kind = e.get("type")
        msg = e.get("message")
        if kind not in ("assistant", "user") or not isinstance(msg, dict):
            return []
        content = msg.get("content")
        out: list[dict] = []
        if isinstance(content, str):
            self.readable += 1
            return out                      # a person's or the job's own prompt: not shown (the brief is on the task)
        if not isinstance(content, list):
            return out
        self.readable += 1
        for b in content:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if kind == "assistant" and t == "text" and isinstance(b.get("text"), str) and b["text"].strip():
                out.append({"speaker": "claude", "kind": "assistant_text", "text": clean(b["text"].strip()), "meta": {}})
            elif kind == "assistant" and t == "tool_use":
                text, meta = describe_tool(b.get("name") or "Tool", b.get("input"))
                if isinstance(b.get("id"), str):
                    self.tools[b["id"]] = meta["tool"]
                    if self.sensitive_call(b.get("name"), b.get("input")):
                        self.hide.add(b["id"])
                out.append({"speaker": "claude", "kind": "tool_call", "text": text, "meta": meta})
            elif kind == "user" and t == "tool_result":
                err = bool(b.get("is_error"))
                tool = self.tools.get(b.get("tool_use_id"), "")
                raw = _result_text(b.get("content"))
                if b.get("tool_use_id") in self.hide or (tool in ("Grep", "Glob") and _SENSITIVE_IN_TEXT.search(raw)):
                    out.append({"speaker": "claude", "kind": "tool_result", "text": HIDDEN, "meta": {"tool": tool, "status": "hidden", "is_error": False}})
                    continue
                out.append({"speaker": "claude", "kind": "tool_result", "text": clean(raw, RESULT_CHARS),
                            "meta": {"tool": tool, "status": "error" if err else "ok", "is_error": err}})
        return out


def gemini_frames(rec) -> list[dict]:
    """One line of gemini_mcp's log -> frames. {"k": "question"|"reply"|"error", "text": ...}; both sides are scrubbed again here."""
    if not isinstance(rec, dict) or not isinstance(rec.get("text"), str):
        return []
    k = rec.get("k")
    kind = {"question": "tool_call", "reply": "assistant_text", "error": "note"}.get(k)
    if not kind:
        return []
    return [{"speaker": "gemini", "kind": kind, "text": clean(rec["text"]), "meta": {}}]


def frame_size(frame: dict) -> int:
    return len(json.dumps(frame, separators=(",", ":")).encode("utf-8"))


def fit(frame: dict, room: int = MAX_MESSAGE - 400) -> dict:
    """The frame, with its text shortened until the serialised frame fits one message (a non-ASCII text is 6 bytes a character on the wire)."""
    while frame_size(frame) > room and len(frame["text"]) > 40:
        frame = {**frame, "text": frame["text"][:max(40, len(frame["text"]) // 2)].rstrip() + "…"}
    return frame


def pack(frames: list[dict], budget: int = MAX_MESSAGE - 400) -> tuple[list[dict], list[dict]]:
    """(the leading frames that fit one message, the rest)."""
    out, used = [], 0
    for i, f in enumerate(frames):
        n = frame_size(f) + 1
        if out and used + n > budget:
            return out, frames[i:]
        out.append(f)
        used += n
    return out, []


# --- the streamer ---------------------------------------------------------------------------------------------------------------------

class Tail:
    """Reads the new COMPLETE lines of a growing file (a half-written last line waits for its end)."""

    def __init__(self, path: Path | None = None):
        self.path, self.pos, self.buf = path, 0, b""

    def read(self) -> list[str]:
        if not self.path:
            return []
        try:
            size = self.path.stat().st_size
            if size < self.pos:             # truncated or replaced: start again
                self.pos, self.buf = 0, b""
            with open(self.path, "rb") as fh:
                fh.seek(self.pos)
                data = fh.read(1 << 20)
        except OSError:
            return []
        self.pos += len(data)
        self.buf += data
        *done, self.buf = self.buf.split(b"\n")
        return [x.decode("utf-8", "replace") for x in done if x.strip()]


class Streamer:
    """One per running job. `run()` until cancelled; `drain()` once the agent has ended and before the job is finished (the fence is still ours)."""

    def __init__(self, link, job: dict, cwd: str, session_id: str, gemini_file: Path | None = None, home: Path | None = None,
                 screen=None, clock=time.monotonic):
        """`screen()` -> (tool asked about or '', session url or '') from the pty log (a subscription session), or None."""
        self.link, self.job_id, self.fence = link, job["job_id"], job["fence"]
        self.cwd, self.session_id, self.home = cwd, session_id, home
        self.tr, self.main, self.gem = Translator(), Tail(), Tail(gemini_file)
        self.screen, self.clock = screen, clock
        self.seq, self.queue, self.sent_chars, self.cut = 0, [], 0, False
        self.state, self.started, self.last_entry = "", clock(), clock()
        self.unavailable = self.stopped = False
        self.asked, self.url = "", ""
        self.lock = threading.Lock()     # poll() runs in a thread; drain() must not read the same tail at the same time

    # frames
    def add(self, speaker: str, kind: str, text: str, meta: dict | None = None) -> None:
        if self.cut or self.stopped:
            return
        if self.sent_chars + len(text) > MAX_TOTAL:
            self.cut = True
            frame = {"speaker": "runner", "kind": "note", "text": "… truncated: the live view of this job reached its size limit", "meta": {"truncated": True}}
        else:
            self.sent_chars += len(text)
            frame = {"speaker": speaker, "kind": kind, "text": text, "meta": meta or {}}
        self.queue.append({**fit(frame), "seq": self.seq})
        self.seq += 1
        if len(self.queue) > MAX_BACKLOG:
            del self.queue[:len(self.queue) - MAX_BACKLOG]

    def status(self, state: str, text: str = "", **meta) -> None:
        if state != self.state or meta.get("session_url"):
            self.state = state
            self.add("runner", "status", text, {"state": state if state != "session" else "working", **meta})

    def take(self, frames: list[dict]) -> None:
        for f in frames:
            if f["kind"] == "tool_result":
                self.asked = ""
            if f["speaker"] == "claude":
                self.last_entry = self.clock()
                self.status("working")
            self.add(f["speaker"], f["kind"], f["text"], f["meta"])

    # one look
    def poll(self) -> None:
        with self.lock:
            self._poll()

    def _poll(self) -> None:
        if self.unavailable:
            return
        if not self.main.path:
            self.main.path = transcript_path(self.cwd, self.session_id, self.home)
        for line in self.main.read():
            try:
                entry = json.loads(line)
            except ValueError:
                self.tr.lines += 1
                continue
            self.take(self.tr.entry(entry))
        for line in self.gem.read():
            try:
                self.take(gemini_frames(json.loads(line)))
            except ValueError:
                pass
        now = self.clock()
        if (not self.main.path and now - self.started > FIND_SECONDS) or (self.tr.lines >= UNREADABLE_LINES and not self.tr.readable):
            self.unavailable = True
            self.status("unavailable", UNAVAILABLE)
            return
        if self.state == "working" and now - self.last_entry > IDLE_SECONDS:
            self.status("idle")

    def look_at_screen(self) -> None:
        if not self.screen:
            return
        try:
            tool, url = self.screen()
        except Exception:  # noqa: BLE001 - a probe must never stop the stream
            return
        if url and url != self.url and SESSION_URL.fullmatch(url):   # strict: never redacted, never anything but a claude.ai/code session link
            self.url = url
            self.add("runner", "status", "", {"state": "working", "session_url": url[:300]})
        if tool and tool != self.asked:
            self.asked = tool
            self.add("claude", "permission_request", f"Waiting for permission to use {clean(tool, 80)}", {"tool": clean(tool, 80)})
        elif not tool:
            self.asked = ""

    # sending
    async def flush(self) -> bool:
        """Send what is queued. False once the server said the fence is stale (the job is no longer ours): the stream ends."""
        while self.queue and not self.stopped:
            batch, _rest = pack(list(self.queue))
            try:
                reply = await self.link.rpc("job_activity", timeout=10.0, job_id=self.job_id, fence=self.fence, seq=batch[0]["seq"], frames=batch)
            except Exception:  # noqa: BLE001 - the socket is down: keep the frames, try again next tick
                return True
            if reply.get("status") == 409:
                self.stopped, self.queue = True, []
                return False
            del self.queue[:len(batch)]    # sent (or refused for a reason a retry cannot fix: never loop on it). Frames poll() added meanwhile stay.
        return True

    async def run(self) -> None:
        try:
            n = 0
            while not self.stopped:
                await asyncio.to_thread(self.poll)
                n += 1
                if self.screen and n % 4 == 0:
                    await asyncio.to_thread(self.look_at_screen)
                if not await self.flush():
                    return
                await asyncio.sleep(FLUSH_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the live view is a convenience: it never ends or fails the job
            log.exception("live view stopped for job %s", self.job_id)

    async def drain(self) -> None:
        """The agent has ended: one last read and send (a short wait, never an error)."""
        try:
            await asyncio.to_thread(self.poll)
            self.status("idle")
            await asyncio.wait_for(self.flush(), timeout=15.0)
        except Exception:  # noqa: BLE001
            log.exception("live view: final flush failed for job %s", self.job_id)

    def forget(self) -> int:
        """API mode only (the worker calls it after drain): the transcript of this session and every other file Claude Code keeps for its id are
        deleted, so a job's output does not stay on the PC. -> how many paths were removed. A subscription session is the person's own and is kept."""
        home = self.home or config_dir()
        found: list[Path] = []
        for pattern in (f"projects/*/{self.session_id}.jsonl", f"projects/*/{self.session_id}", f"file-history/{self.session_id}",
                        f"session-env/{self.session_id}", f"todos/{self.session_id}*", f"shell-snapshots/*{self.session_id}*", f"debug/{self.session_id}*"):
            try:
                found += list(home.glob(pattern))
            except OSError:
                pass
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", self.session_id or ""):
            return 0
        n = 0
        for p in found:
            try:
                shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()
                n += 1
            except OSError:
                pass
        return n
