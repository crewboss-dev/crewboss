"""Running with no console (task #972): the hidden logon task starts `crewboss-runnerw.exe` (a GUI-subsystem launcher), and the
Windows service has no console either. Under those, sys.stdout / sys.stderr are None, so a bare print() would raise. `setup()` then
sends logging AND anything printed to a rotating file, %LOCALAPPDATA%\\crewboss-runner\\runner.log on Windows (state/runner.log
elsewhere), and gives the process a hidden console of its own (Windows) so the child processes it starts (git, claude, adb) show no
window yet still get a graceful CTRL_BREAK before any hard kill; if that fails, each child is started with CREATE_NO_WINDOW.

The status page link is per start (a fresh one-use token), so the running runner also writes it to a user-only file (`ui-url.txt` in
the state folder); `crewboss-runner status` prints it from there."""
from __future__ import annotations

import logging
import logging.handlers
import os
import subprocess
import sys
from pathlib import Path

from . import settings, store

LOG_NAME = "runner.log"
LOG_BYTES = 1_000_000
LOG_BACKUPS = 3
URL_NAME = "ui-url.txt"
CREATE_NO_WINDOW = 0x08000000
_NEW_CONSOLE_OR_DETACHED = 0x00000010 | 0x00000008


def log_dir() -> Path:
    """%LOCALAPPDATA%\\crewboss-runner on Windows (the parent of the state folder); the state folder elsewhere."""
    state = store.state_dir()
    if os.name == "nt" and not settings.get("STATE_DIR"):
        if state.parent.name == store.PROFILES:   # %LOCALAPPDATA%\\crewboss-runner\\logs\\<profile>: one log per profile
            return state.parent.parent.parent / "logs" / state.name
        return state.parent
    return state


def log_file() -> Path:
    return log_dir() / LOG_NAME


def url_file() -> Path:
    return store.state_dir() / URL_NAME


def headless_started() -> bool:
    """True once setup() has put the log file in place of the console."""
    return any(getattr(h, "_crewboss", False) for h in logging.getLogger().handlers)


def headless() -> bool:
    return sys.stdout is None or sys.stderr is None


class _LogStream:
    """A write-only text stream that logs each line, standing in for a missing sys.stdout / sys.stderr."""
    encoding = "utf-8"
    errors = "strict"

    def __init__(self, level: int):
        self.level, self._buf, self._log = level, "", logging.getLogger("crewboss-runner.console")

    def write(self, text) -> int:
        self._buf += str(text)
        *lines, self._buf = self._buf.split("\n")
        for ln in lines:
            if ln.strip():
                self._log.log(self.level, ln.rstrip())
        return len(text)

    def flush(self) -> None:
        if self._buf.strip():
            self._log.log(self.level, self._buf.rstrip())
        self._buf = ""

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True


def hide_child_windows() -> None:
    """Windows only: every child process starts with CREATE_NO_WINDOW unless the caller asked for its own console."""
    if os.name != "nt" or getattr(subprocess.Popen, "_crewboss_hidden", False):
        return
    orig = subprocess.Popen.__init__

    def init(self, *a, **kw):
        flags = kw.get("creationflags", 0) or 0
        if not flags & _NEW_CONSOLE_OR_DETACHED:
            kw["creationflags"] = flags | CREATE_NO_WINDOW
        orig(self, *a, **kw)

    subprocess.Popen.__init__ = init
    subprocess.Popen._crewboss_hidden = True


def own_console() -> bool:
    """Windows, no console: give this process a HIDDEN console of its own. Children then share it, so CTRL_BREAK to a child's process
    group (the graceful stop in spawn.kill_tree) works, and no window ever shows. False if it could not (or not Windows)."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        k32 = ctypes.windll.kernel32   # type: ignore[attr-defined]
        if not k32.GetConsoleWindow() and not k32.AllocConsole():
            return k32.GetLastError() == 5   # access denied: already on a windowless console (CREATE_NO_WINDOW): children share it
        hwnd = k32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)   # SW_HIDE
        return True
    except Exception:  # noqa: BLE001
        return False


def setup(force: bool = False, name: str = LOG_NAME, console: bool = True, directory: Path | None = None) -> Path | None:
    """If there is no console (or force), log to the rotating file and replace the missing streams. Returns the log path, else None."""
    path = (directory or log_dir()) / name
    if headless_started():
        return path
    if not (force or headless()):
        return None
    store.ensure_private_dir(path.parent)
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=LOG_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8")
    handler._crewboss = True
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    if sys.stdout is None:
        sys.stdout = _LogStream(logging.INFO)
    if sys.stderr is None:
        sys.stderr = _LogStream(logging.ERROR)
    sys.__stdout__ = sys.__stdout__ or sys.stdout
    sys.__stderr__ = sys.__stderr__ or sys.stderr
    if not (console and own_console()):   # with our own hidden console the children inherit it; without, each gets a hidden one of its own
        hide_child_windows()
    logging.getLogger("crewboss-runner").info("headless start, logging to %s", path)
    return path


def write_url(url: str) -> None:
    store.write_private(url_file(), url + "\n")


def clear_url() -> None:
    try:
        url_file().unlink()
    except OSError:
        pass


def read_url() -> str:
    """The status page link of the running runner, or '' (no runner, or it exited)."""
    if not store.run_alive():
        return ""
    try:
        return url_file().read_text().strip()
    except OSError:
        return ""
