"""Subscription mode: is Claude Code signed in (preflight), and did the interactive session actually start (watchdog)? (#627)

A signed-out Claude Code under our pty sits at a login prompt nobody can see, and the job would show "running" for ever
because the runner keeps heartbeating. Two guards:

1. `signed_in()` runs `claude auth status` (Claude Code 2.1.x): NO TTY, short timeout, the agent environment with
   ANTHROPIC_API_KEY stripped, stdout/stderr thrown away. Only the EXIT CODE is used (0 signed in, 1 not signed in; checked
   against a Claude Code with an empty config dir). The command's output names the account, so it is never read, parsed,
   logged or stored (security rule k98): the result is one boolean.
2. `scan()` looks at the end of the pty log (agent.log) for login text, failure text, or a sign that Remote Control registered.

Pattern sources (strings in the Claude Code 2.1.314 binary): LOGIN = "Select login method:" (the first-run / signed-out
screen), "Please run /login", "Not logged in", "You must be logged in to use Remote Control.", "Remote Control requires a
claude.ai subscription"; FAILURE = "Remote Control cannot start (...)", "Remote Control could not attach". TRUST = the folder
trust dialog ("Quick safety check ... Yes, I trust this folder"), which would also sit invisibly. REGISTERED: the registration
text of the interactive --remote-control flag could not be observed on this box (the folder-trust dialog blocks an unattended
probe), so these are best guesses and the rule is OFF unless CREWBOSS_RUNNER_REGISTER_CHECK=1, until a real run confirms them."""
from __future__ import annotations

import os
import re
import subprocess
import time

from . import settings, spawn

SIGNED_OUT_MESSAGE = "Claude Code is not signed in: run `claude` on this PC and log in"
AUTH_TIMEOUT = 20.0

# Matched against the log with escape sequences removed, whitespace collapsed and case folded. Keep every pattern here.
LOGIN_PATTERNS = ("select login method", "please run /login", "not logged in", "you must be logged in to use remote control",
                  "remote control requires a claude.ai subscription", "paste code here if prompted", "browser didn't open")
FAILURE_PATTERNS = ("remote control cannot start", "remote control could not attach", "remote control is disabled by your",
                    "remote control is only available with claude.ai subscriptions")
TRUST_PATTERNS = ("itrustthisfolder",)   # matched against the text with ALL whitespace removed (the TUI moves the cursor between words)
TRUST_KEYS = (b"\x1b[B", b"\r")   # Down (to "Yes, I trust this folder", the second option), then Enter: SEPARATE writes, because
TRUST_KEY_GAP = 0.5                # the TUI handles a batched pair against stale state and picks "No, exit" (seen on 2.1.321)
TRUST_SETTLE = float(settings.get("TRUST_SETTLE", "5"))   # seconds after answering before the prompt must be gone
# A tool permission prompt (a tool that is not in --allowedTools): the dialog "Do you want to proceed?" with numbered choices
# "1. Yes / 2. Yes, and don't ask again / 3. No" (see permission_prompt_tool). NEVER answered by the runner: a job that sits at one is given back.
PERMISSION_GRACE = float(settings.get("PERMISSION_GRACE", "90"))   # seconds the prompt must sit unchanged (a person on the phone may answer it)
REGISTERED_PATTERNS = ("remote control active", "remote control is connected", "remote control connected", "claude.ai/code/")

START_WINDOW = float(settings.get("START_WINDOW", "120"))   # seconds the session gets to register
POLL = 1.0
TAIL_BYTES = 256 * 1024

_ANSI = re.compile(r"\x1b\[[0-9;?<>=]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")


def register_check_enabled() -> bool:
    return settings.get("REGISTER_CHECK", "0") == "1"


def signed_in(claude: str | None = None, timeout: float = AUTH_TIMEOUT) -> bool | None:
    """True / False from the exit code of `claude auth status`; None when it could not be decided (timeout, cannot run,
    a code other than 0/1): the caller must not block on None."""
    claude = claude or spawn.claude_binary()
    try:
        proc = subprocess.Popen([claude, "auth", "status"], env=spawn.agent_env("subscription"), stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **spawn.new_group_kwargs())
    except OSError:
        return None
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            spawn.kill_tree(proc, 1.0)
        except OSError:
            pass
        proc.wait()
        return None
    return True if rc == 0 else False if rc == 1 else None


def log_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def log_text(path: str, start: int = 0, fold: bool = True) -> str:
    """The end of the pty log (from byte `start` on) as plain lower-case text on one line (fold=False keeps the case, to read a tool
    name). Used only for matching; never stored or sent."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(start, fh.tell() - TAIL_BYTES))
            raw = fh.read()
    except OSError:
        return ""
    text = re.sub(r"\s+", " ", _ANSI.sub(" ", raw.decode("utf-8", "replace")))
    return text.lower() if fold else text


def scan(text: str, session_name: str = "") -> str:
    """-> 'login' | 'failure' | 'trust' | 'registered' | '' (nothing decisive yet)."""
    if any(p in text for p in LOGIN_PATTERNS):
        return "login"
    if any(p in text for p in FAILURE_PATTERNS):
        return "failure"
    compact = re.sub(r"\s+", "", text)
    if any(p in compact for p in TRUST_PATTERNS):
        return "trust"
    if any(p in text for p in REGISTERED_PATTERNS) or (session_name and session_name.lower() in text):
        return "registered"
    return ""


# The real dialog: a header (a box top or "Tool use" / "Bash command" / "Edit file" ...), the tool call, "Do you want to proceed?",
# then the numbered choices "1. Yes", optionally "2. Yes, and don't ask again ...", and the last one "No". Agent PROSE that merely
# contains the question is not a dialog: the question must be followed, to the end of the screen, by the numbered choices.
_PERM_QUESTION = re.compile(r"do you want to (?:proceed|make this edit|create|run|allow)[^\n?]{0,80}\?", re.I)
# Dialogs of other shapes that also wait for a person with no tool permission question: a dynamic workflow, leaving plan mode, and the
# AskUserQuestion picker. Each is named by its own words, so agent prose without the numbered choices is not one.
_OTHER_DIALOGS = (("Workflow", re.compile(r"run a dynamic workflow\s*\?", re.I)), ("ExitPlanMode", re.compile(r"exit plan mode\s*\?", re.I)),
                  ("EnterPlanMode", re.compile(r"enter plan mode\s*\?", re.I)))
_PICKER_HINT = re.compile(r"enter to select.{0,40}(?:esc to cancel|navigate)|\bchat about this\b|\btype something\b", re.I | re.S)
_PERM_CHOICES = re.compile(r"^\W{0,4}1\.\s*yes\b.*?\b\d\.\s*no\b[^\n]{0,80}\W*$", re.I | re.S)
_PERM_HEADER = re.compile(r"[\u256d\u250c\u2554]|\b(?:tool use|bash command|edit file|create file|read file|web fetch|mcp tool)\b", re.I)   # log_text flattens lines


_HEADER_TOOL = {"bash command": "Bash", "edit file": "Edit", "create file": "Write", "read file": "Read", "web fetch": "WebFetch"}


def _other_dialog(text: str) -> str | None:
    """The tool of a Workflow / plan-mode / AskUserQuestion dialog at the END of the screen, else None: its question, then numbered choices
    and little else; or the question picker's footer ("Enter to select ... Esc to cancel") as the last thing drawn."""
    tail_ok = lambda rest: len(re.sub(r"\s+", "", rest)) <= 600 and re.search(r"(?m)^\W{0,4}1\.\s*\S", rest.strip()) is not None
    for tool, rx in _OTHER_DIALOGS:
        ms = list(rx.finditer(text))
        if ms and tail_ok(text[ms[-1].end():]):
            return tool
    tail = text[-900:]
    hit = list(_PICKER_HINT.finditer(tail))
    if hit and len(re.sub(r"\s+", "", tail[hit[-1].end():])) <= 120 and re.search(r"(?m)^\W{0,4}1\.\s*\S", tail):
        return "AskUserQuestion"
    return None


def permission_prompt_tool(text: str) -> str | None:
    """None when the END of the screen is not Claude Code's tool permission dialog; else the tool it asks about ('an unlisted tool' when
    the name cannot be read). `text` is log_text(..., fold=False). The dialog is recognised by its layout (question, then numbered
    choices "1. Yes ... N. No" at the end of the screen); the tool name is read only from INSIDE the dialog (between its header and the
    question): `Bash(cmd)`, `Skill(name)`, `server - tool (MCP)` or an mcp__server__tool id. Agent prose never counts."""
    other = _other_dialog(text)
    if other:
        return other
    qs = list(_PERM_QUESTION.finditer(text))
    if not qs:
        return None
    q = qs[-1]
    rest = text[q.end():]
    if len(re.sub(r"\s+", "", rest)) > 600 or not _PERM_CHOICES.search(rest.strip()):   # drawn over later, or no choices: not pending
        return None
    before = text[max(0, q.start() - 1200):q.start()]
    headers = list(_PERM_HEADER.finditer(before))
    if not headers:
        return "an unlisted tool"
    head = before[headers[-1].start():]
    by_header = _HEADER_TOOL.get(headers[-1].group(0).lower())
    found = re.findall(r"mcp__[\w-]+__[\w-]+", head)
    if found:
        return found[-1]
    mcp = re.findall(r"([\w-]+) - ([\w-]+) \(MCP\)", head)
    if mcp:
        return f"mcp__{mcp[-1][0]}__{mcp[-1][1]}"
    calls = re.findall(r"\b([A-Z][A-Za-z]{2,30})\(", head)
    if calls:
        return calls[-1]
    return by_header or "an unlisted tool"      # a real "Bash command" dialog has no Bash( in it: the header names the tool


async def watch_permission(agent, poll: float | None = None, grace: float | None = None) -> str:
    """Watch a RUNNING subscription session for a tool permission prompt nobody answers (a tool outside --allowedTools: the job would
    hang to its time limit). -> the tool's name once the prompt has sat unchanged for `grace` seconds, or '' when the session ended.
    Never types anything: the runner does not approve a tool (the person at the PC or on claude.ai/code may, within the grace)."""
    import asyncio
    poll = POLL if poll is None else poll
    grace = PERMISSION_GRACE if grace is None else grace
    seen_size, since = -1, time.monotonic()
    while agent.proc.poll() is None:
        size = log_size(agent.log_path)
        tool = None
        if size == seen_size:
            tool = permission_prompt_tool(await asyncio.to_thread(log_text, agent.log_path, 0, False))
            if tool and time.monotonic() - since >= grace:
                return tool
        else:
            seen_size, since = size, time.monotonic()
        await asyncio.sleep(poll)
    return ""


_TRUST_RE = re.compile(r"\s*".join(TRUST_PATTERNS[0]))
TRUST_GONE_CHARS = 40   # this much new text after the dialog's last words means a screen was drawn over it


def after_trust_dialog(text: str) -> str | None:
    """None when the dialog's last words are the end of the screen (still showing), else the text drawn after them."""
    last = None
    for last in _TRUST_RE.finditer(text):
        pass
    if last is None:
        return text
    rest = text[last.end():]
    return rest if len(re.sub(r"\s+", "", rest)) >= TRUST_GONE_CHARS else None


def trust_prompt_is_for(text: str, worktree: str) -> bool:
    """True when the trust dialog on screen names exactly this worktree path (whitespace ignored). The runner answers the
    dialog only for the worktree it created itself, never for any other folder."""
    wt = re.sub(r"\s+", "", str(worktree)).lower().rstrip("/")
    return bool(wt) and wt in re.sub(r"\s+", "", text)


async def watch(agent, session_name: str, window: float | None = None, poll: float | None = None,
                worktree: str | None = None, settle: float | None = None) -> str:
    """Watch a freshly spawned subscription session. -> '' (registered, all well) or 'login' | 'failure' | 'trust' |
    'exited' | 'timeout'. With the register check switched off, only login/failure text and an exit within the window count.

    Folder trust: Claude Code asks "Do you trust this folder?" for every new git worktree and waits for ever. When `worktree`
    (the job's own worktree, created by the runner) is given and the dialog names exactly that path, the runner answers it ONCE
    ("Yes, I trust this folder": Down, Enter) through the pty it owns. It then looks only at output after the answer: if the
    dialog is still there after `settle` seconds, or it names any other path (never answered), the verdict is 'trust'
    (the job is given back), never healthy."""
    import asyncio
    settle = TRUST_SETTLE if settle is None else settle
    answered_at = None   # monotonic time of the one answer
    answered_size = 0    # log size at that moment
    window = START_WINDOW if window is None else window
    poll = POLL if poll is None else poll
    check_register = register_check_enabled()
    deadline = time.monotonic() + window
    while True:
        full = await asyncio.to_thread(log_text, agent.log_path)
        if answered_at is None:
            verdict = scan(full, session_name)
            if (verdict == "trust" and worktree and hasattr(agent, "send") and trust_prompt_is_for(full, worktree)
                    and agent.proc.poll() is None):
                answered_size = log_size(agent.log_path)
                answered_at = time.monotonic()
                for key in TRUST_KEYS:
                    await asyncio.to_thread(agent.send, key)
                    await asyncio.sleep(TRUST_KEY_GAP)
                await asyncio.sleep(poll)
                continue
        else:
            post = await asyncio.to_thread(log_text, agent.log_path, answered_size)
            rest = after_trust_dialog(post)   # the accepted dialog leaves its last frame in the log: look at what came after it
            verdict = "trust" if rest is None else scan(rest, session_name)
            if verdict == "trust":
                if agent.proc.poll() is not None or time.monotonic() - answered_at >= settle:
                    return "trust"   # still showing after the answer: never healthy
                await asyncio.sleep(poll)
                continue
            if time.monotonic() - answered_at < settle and verdict == "" and agent.proc.poll() is None:
                await asyncio.sleep(poll)   # give the answer time to take; a still-visible dialog would reappear
                continue
        if verdict in ("login", "failure"):
            return verdict
        if verdict == "registered":
            return ""
        exited = agent.proc.poll() is not None
        if exited:
            return "trust" if verdict == "trust" else "exited"
        if time.monotonic() >= deadline:
            if verdict == "trust":
                return "trust"
            return "timeout" if check_register else ""
        await asyncio.sleep(poll)
