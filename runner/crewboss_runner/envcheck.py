"""Environment check on this PC (#810 step 6): "is this machine ready to build that project?" A lightweight job, no Claude agent.

The dashboard says WHAT a project needs as rows of (kind, name, args). THIS file decides HOW each kind is checked: the kinds are a fixed
catalogue implemented here as built-in probes. The runner NEVER executes a command string it was sent, never uses a name as anything but a
lookup key or a plain argument, and an unknown kind is answered "unsupported" (a runner older than the dashboard says so instead of guessing).

  tool           `name` is one of the KNOWN tools below, is on PATH, and its version (asked with that tool's own known flag) is at least
                 args.min_version. Any other name is "unsupported": the runner does not start arbitrary programs from PATH.
  env            the variable `name` is set (and, when it holds an absolute path, that path exists)
  android_sdk_pkg  `sdkmanager --list_installed` lists the package `name` (e.g. platforms;android-34)
  file           the path `name` exists (~ is expanded)
  keystore       the file `name` exists AND one of its certificates has the SHA-256 fingerprint args.fingerprint (via `keytool -list -v`).
                 The key itself is never read by this program or sent anywhere; a fingerprint is not secret.

Every reply is compact (one short status per row) because the server caps an incoming frame at 4096 bytes.

Loop (beside the job worker and the assistant's, on the same verified socket): `env_claim` -> run the probes in a thread -> `env_finish`."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .link import Link, LinkDown

log = logging.getLogger("crewboss-runner.env")

IDLE_POLL_SECONDS = 30.0
PROBE_TIMEOUT = 15.0
SDK_TIMEOUT = 90.0
MAX_ROWS = 40
MAX_VALUE = 40

# Names are validated before they reach a lookup or an argv. A name is a KEY, never a command: no separators, no spaces, no shell characters.
TOOL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
PKG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9;._-]{0,79}$")
PATH_RE = re.compile(r"^[~/A-Za-z0-9._:\\()+][~/A-Za-z0-9._:\\ ()+-]{0,239}$")
# A path that starts with two slashes of either kind (//host/share, \\host\share, \\?\, \\.\) is a NETWORK or device path: on Windows merely
# touching it makes the PC authenticate to that host (an NTLM leak), so it is never looked at.
NETWORK_PATH = re.compile(r"^[\\/]{2}")
# On Windows only a ~-relative path or an X:\ absolute one is accepted.
WINDOWS_PATH = re.compile(r"^(~([\\/]|$)|[A-Za-z]:[\\/])")
VERSION_RE = re.compile(r"^\d+(\.\d+){0,3}$")
FINGERPRINT_RE = re.compile(r"^([0-9A-F]{2}:){31}[0-9A-F]{2}$")
FOUND_VERSION = re.compile(r"(\d+(?:\.\d+){0,3})")

# THE TOOLS a requirement may name, each with the flag that makes it print its version. Nothing else on PATH is ever started. A tool some OSes
# call something else (python3 / python / py) is tried under those fixed names only.
TOOLS: dict[str, tuple[str, ...]] = {
    "java": ("-version",), "javac": ("-version",), "node": ("--version",), "npm": ("--version",),
    "python3": ("--version",), "python": ("--version",), "py": ("--version",), "pip3": ("--version",),
    "dotnet": ("--version",), "git": ("--version",), "gradle": ("--version",), "adb": ("version",),
    "kotlin": ("-version",), "flutter": ("--version",), "go": ("version",), "cargo": ("--version",), "rustc": ("--version",),
    "swift": ("--version",), "xcodebuild": ("-version",), "wix": ("--version",), "msbuild": ("-version",),
}
ALIASES = {"python3": ("python3", "python", "py"), "python": ("python", "python3", "py"), "pip3": ("pip3",)}


def os_name() -> str:
    """linux | windows | macos. WSL2 is linux: it is a Linux build environment."""
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def parse_version(text: str) -> tuple[int, ...] | None:
    """The first dotted number in a tool's output as a tuple of ints, or None. Java's old `1.8.0_292` is major 8."""
    m = FOUND_VERSION.search(text or "")
    if not m:
        return None
    parts = tuple(int(x) for x in m.group(1).split("."))
    if len(parts) >= 2 and parts[0] == 1 and "java" in (text or "").lower()[:200]:
        return parts[1:]
    return parts


def at_least(found: tuple[int, ...], wanted: str) -> bool:
    want = tuple(int(x) for x in wanted.split("."))
    width = max(len(found), len(want))
    return (found + (0,) * (width - len(found))) >= (want + (0,) * (width - len(want)))


def _run(argv: list[str], timeout: float, stdin: str | None = None) -> subprocess.CompletedProcess:
    """The one place a probe starts a program (tests replace it). An argument list, never a shell; no inherited stdin."""
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, input=stdin if stdin is not None else None,
                          stdin=None if stdin is not None else subprocess.DEVNULL)


def _status(s: str, v: str = "") -> dict:
    return {"s": s, "v": str(v)[:MAX_VALUE]}


# --- the probes ------------------------------------------------------------------------------------------------------

def _which(name: str) -> str | None:
    """Look a known tool up on PATH, and ONLY on PATH: on Windows shutil.which also tries the current folder, so a program an attacker dropped
    next to where the runner was started would win; a hit inside the current folder is dropped."""
    found = shutil.which(name, path=os.environ.get("PATH", ""))
    if found and os_name() == "windows":
        try:
            if Path(found).resolve().parent == Path.cwd().resolve():
                return None
        except OSError:
            return None
    return found


def probe_tool(name: str, args: dict) -> dict:
    if not TOOL_RE.match(name):
        return _status("error", "bad name")
    flags = TOOLS.get(name)
    if flags is None:
        return _status("unsupported")
    wanted = str(args.get("min_version") or "")
    if wanted and not VERSION_RE.match(wanted):
        return _status("error", "bad version")
    candidates = ALIASES.get(name, (name,))
    path, used = next(((p, a) for p, a in ((_which(a), a) for a in candidates) if p), (None, name))
    if not path:
        return _status("missing")
    if not wanted:
        return _status("ok")
    try:
        res = _run([path, *TOOLS[used]], PROBE_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        return _status("old", "version unknown")
    found = parse_version((res.stdout or "") + "\n" + (res.stderr or ""))
    if not found:
        return _status("old", "version unknown")
    shown = ".".join(str(x) for x in found)
    return _status("ok" if at_least(found, wanted) else "old", shown)


def probe_env(name: str, args: dict) -> dict:
    if not ENV_RE.match(name):
        return _status("error", "bad name")
    value = os.environ.get(name, "")
    if not value:
        return _status("missing")
    if os.path.isabs(value) and not os.path.exists(value):
        return _status("missing", "folder not found")
    return _status("ok")


def _sdkmanager() -> str | None:
    roots = [os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT")]
    exe = "sdkmanager.bat" if os_name() == "windows" else "sdkmanager"
    for root in filter(None, roots):
        for sub in ("cmdline-tools/latest/bin", "cmdline-tools/bin", "tools/bin"):
            cand = Path(root) / sub / exe
            if cand.is_file():
                return str(cand)
    return shutil.which("sdkmanager")


def installed_packages(text: str) -> set[str]:
    """The package ids in `sdkmanager --list_installed` output (a table: `  id | version | description | location`)."""
    out = set()
    for line in (text or "").splitlines():
        cell = line.split("|", 1)[0].strip()
        if PKG_RE.match(cell) and ";" in cell:
            out.add(cell)
    return out


_sdk_cache: dict | None = None      # set by run_check: `sdkmanager --list_installed` is slow, so it runs ONCE per check, not once per row


def _installed() -> tuple[set[str] | None, str]:
    """(installed package ids, or None + why not). Cached for the length of one check."""
    if _sdk_cache is not None and "r" in _sdk_cache:
        return _sdk_cache["r"]
    tool = _sdkmanager()
    if not tool:
        out = (None, "no sdkmanager")
    else:
        try:
            out = (installed_packages(_run([tool, "--list_installed"], SDK_TIMEOUT).stdout or ""), "")
        except (subprocess.TimeoutExpired, OSError):
            out = (None, "sdkmanager failed")
    if _sdk_cache is not None:
        _sdk_cache["r"] = out
    return out


def probe_android_sdk_pkg(name: str, args: dict) -> dict:
    if not PKG_RE.match(name):
        return _status("error", "bad name")
    have, why = _installed()
    if have is None:
        return _status("missing" if why == "no sdkmanager" else "error", why)
    return _status("ok" if name in have else "missing")


def _path(name: str) -> str | None:
    """A path to look at, or None. Never a network or device path (two leading slashes of either kind), and on Windows only ~-relative or X:\\."""
    if not PATH_RE.match(name) or NETWORK_PATH.match(name):
        return None
    if os_name() == "windows" and (not WINDOWS_PATH.match(name) or ":" in name[2:]):
        return None
    return os.path.expanduser(name)


def probe_file(name: str, args: dict) -> dict:
    path = _path(name)
    if path is None:
        return _status("error", "bad path")
    return _status("ok" if os.path.exists(path) else "missing")


def _keytool() -> str | None:
    exe = "keytool.exe" if os_name() == "windows" else "keytool"
    home = os.environ.get("JAVA_HOME")
    if home and (Path(home) / "bin" / exe).is_file():
        return str(Path(home) / "bin" / exe)
    return shutil.which("keytool")


def fingerprints(text: str) -> set[str]:
    return {m.group(1).upper() for m in re.finditer(r"SHA-?256:\s*((?:[0-9A-Fa-f]{2}:){31}[0-9A-Fa-f]{2})", text or "")}


def probe_keystore(name: str, args: dict) -> dict:
    path = _path(name)
    if path is None:
        return _status("error", "bad path")
    expected = str(args.get("fingerprint") or "").upper()
    if not FINGERPRINT_RE.match(expected):
        return _status("error", "no fingerprint")
    if not os.path.isfile(path):
        return _status("missing")
    tool = _keytool()
    if not tool:
        return _status("missing", "no keytool")
    try:
        # an empty line answers the password prompt: a JKS keystore lists its certificates (the fingerprint is public) without one
        res = _run([tool, "-list", "-v", "-keystore", path], PROBE_TIMEOUT, stdin="\n")
    except (subprocess.TimeoutExpired, OSError):
        return _status("error", "keytool failed")
    found = fingerprints((res.stdout or "") + "\n" + (res.stderr or ""))
    if not found:
        return _status("error", "unreadable")
    return _status("ok" if expected in found else "mismatch")


PROBES = {"tool": probe_tool, "env": probe_env, "android_sdk_pkg": probe_android_sdk_pkg, "file": probe_file, "keystore": probe_keystore}


def run_probe(row: dict) -> dict:
    """One requirement -> {"i" (echoed), "s", "v"}. `s`: ok | missing | old | mismatch | error | unsupported | na. A row for another OS is `na`."""
    i = row.get("i")
    kind, name = row.get("kind"), row.get("name")
    out = {"i": i}
    if not isinstance(kind, str) or not isinstance(name, str):
        return {**out, **_status("error", "malformed")}
    only = row.get("os") or "any"
    if only not in ("any", os_name()):
        return {**out, **_status("na")}
    probe = PROBES.get(kind)
    if probe is None:
        return {**out, **_status("unsupported")}
    args = row.get("args") if isinstance(row.get("args"), dict) else {}
    try:
        return {**out, **probe(name, args)}
    except Exception as exc:  # noqa: BLE001 - one broken probe must not end the check
        log.exception("probe %s failed", kind)
        return {**out, **_status("error", type(exc).__name__)}


def run_check(rows: list[dict]) -> list[dict]:
    """Every row's answer, keyed by the row's own `i` (the dashboard's requirement id), so an answer can never land on another row."""
    global _sdk_cache
    _sdk_cache = {}
    try:
        return [run_probe(r) for r in (rows or [])[:MAX_ROWS]]
    finally:
        _sdk_cache = None


# --- the loop --------------------------------------------------------------------------------------------------------

class EnvWorker:
    def __init__(self, link: Link, idle_poll: float = IDLE_POLL_SECONDS):
        self.link, self.idle_poll = link, idle_poll
        self.stopping = False
        self.results: list[int] = []        # check ids finished, for tests

    async def loop(self) -> None:
        while not self.stopping:
            if not self.link.ready.is_set():
                await asyncio.sleep(0.2)
                continue
            try:
                reply = await self.link.rpc("env_claim")
            except LinkDown:
                continue
            check = reply.get("check") if reply.get("ok") else None
            if check:
                await self.run_check(check)
                continue
            self.link.env_nudges.clear()
            try:
                await asyncio.wait_for(self.link.env_nudges.wait(), self.idle_poll)
            except asyncio.TimeoutError:
                pass

    async def run_check(self, check: dict) -> None:
        results = await asyncio.to_thread(run_check, check.get("rows") or [])
        try:
            await self.link.rpc("env_finish", check_id=check["check_id"], results=results, os=os_name())
        except LinkDown:
            return                       # the server re-offers a check it never got an answer to
        self.results.append(check["check_id"])
