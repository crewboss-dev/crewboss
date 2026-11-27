"""A graceful stop for a runner that has no console to Ctrl+C (task #972).

`schtasks /End` is TerminateProcess: it orphans the agent and leaves the job's worktree and lease behind. So `crewboss-runner service
stop`, the Windows service's supervisor and `uninstall` first drop a stop-request file in the runner's (user-only) state folder; the
running runner watches for it and shuts down the way Ctrl+C does (kills its agents, releases the job, removes run.pid). Only if it
has not exited within STOP_WAIT seconds does the caller fall back to the hard stop."""
from __future__ import annotations

import asyncio
import json
import os
import time

from . import store

STOP_NAME = "stop.request"
STOP_WAIT = 30.0


def stop_file():
    return store.state_dir() / STOP_NAME


def root_stop_file():
    """The FIXED stop-request file in the ROOT state folder: the Windows service's supervisor (an admin process) writes here, never into a
    profile folder, so it never has to read the user-writable `active` file. Every runner's watch checks it too."""
    return store.root_state_dir() / STOP_NAME


LAST_REASON = ""   # why this process was asked to stop ("switch" = `use` changed the profile; the service's supervisor then restarts at once)


def _write(path, pid: int | None, reason: str) -> None:
    store.write_private(path, f"{pid or 'stop'}\n" + (f"{reason}\n" if reason else ""))


def request_stop(pid: int | None = None, reason: str = "") -> None:
    """Ask the runner with this pid (from run.pid) to stop. A request names its target, so a newer runner never consumes one meant for
    an older process that is still shutting down."""
    _write(stop_file(), pid, reason)


def request_stop_root(pid: int | None = None, reason: str = "") -> None:
    """The supervisor's request: in the root state folder, whichever profile the runner is running."""
    _write(root_stop_file(), pid, reason)


def _read(path) -> tuple[bool, int | None, str]:
    """(exists, target pid or None for an unscoped request, reason)"""
    try:
        lines = path.read_text(errors="replace").split("\n")
    except OSError:
        return False, None, ""
    first = lines[0].strip()
    return True, (int(first) if first.isdigit() and len(first) <= 10 else None), (lines[1].strip()[:20] if len(lines) > 1 else "")


def _target() -> int | None:
    return _read(stop_file())[1]


def _unlink(path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def clear() -> None:
    _unlink(stop_file())


async def watch(stop_cb, interval: float = 1.0) -> None:
    """Runs inside the runner: when a request file appears (this profile's, or the root one), remove it and call stop_cb once."""
    global LAST_REASON
    while True:
        for path in (stop_file(), root_stop_file()):
            ok, target, reason = _read(path)
            if ok and (target is None or target == os.getpid()):   # an unscoped request, or one for this process; another pid's is left alone
                _unlink(path)
                LAST_REASON = reason
                stop_cb()
                return
        await asyncio.sleep(interval)


def read_run_pid() -> int | None:
    """The running runner's pid from the fixed ROOT live.pid (any profile), else this profile's run.pid."""
    try:
        text = store.live_pid_file().read_text(errors="replace").strip()
    except OSError:
        return store.read_pid()
    return int(text) if text.isdigit() and len(text) <= 10 and int(text) > 0 else store.read_pid()


def job_running() -> dict | None:
    """The job the runner on this PC is running now, or None."""
    try:
        job = json.loads((store.state_dir() / "status.json").read_text()).get("job")
    except (OSError, ValueError):
        return None
    return job if job and store.run_alive() else None


def wait_exit(timeout: float = STOP_WAIT, poll: float = 0.5, alive=None, sleep=time.sleep, mono=time.monotonic) -> bool:
    alive = alive or store.run_alive
    end = mono() + timeout
    while alive():
        if mono() >= end:
            return False
        sleep(poll)
    return True


def graceful_stop(timeout: float = STOP_WAIT, reason: str = "", **kw) -> bool:
    """Ask the running runner to stop and wait for it to exit. True = gone (or was not running); False = still running."""
    alive = kw.get("alive") or store.run_alive
    if not alive():
        return True
    request_stop(store.read_pid(), reason) if reason else request_stop(store.read_pid())
    if wait_exit(timeout, **kw):
        return True
    clear()
    return False
