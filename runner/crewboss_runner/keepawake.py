"""Keep the PC from going to SLEEP while a job runs (task #972), and only then.

Held while at least one job is active (starting, running, or finishing/pushing), released the moment none is. An idle, connected
runner never keeps the machine awake, and the DISPLAY is never kept on (system sleep only). Concurrent jobs are reference-counted.
It stops IDLE sleep only: closing a laptop lid or pressing the power button still sleeps the PC.

  windows  SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED) from a thread that stays alive (the state is per thread);
           cleared with ES_CONTINUOUS alone. No ES_DISPLAY_REQUIRED, no ES_AWAYMODE_REQUIRED.
  macos    `caffeinate -i -w <runner pid>` while held (it also ends if the runner dies)
  linux    `systemd-inhibit --what=sleep ... sleep infinity` as a child (killed on release, and by the kernel if the runner dies)
  wsl      nothing: an inhibitor inside WSL does not stop WINDOWS sleeping. Set the power plan on Windows instead.

Turn it off with CREWBOSS_RUNNER_KEEP_AWAKE_DURING_JOBS=0 (setting `keep_awake_during_jobs`, default on)."""
from __future__ import annotations

import atexit
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading

from . import settings

log = logging.getLogger("crewboss-runner")

SETTING = "KEEP_AWAKE_DURING_JOBS"   # CREWBOSS_RUNNER_KEEP_AWAKE_DURING_JOBS, i.e. keep_awake_during_jobs
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
INHIBIT_CMD = ["systemd-inhibit", "--what=sleep", "--who=crewboss-runner", "--why=agent job running", "--mode=block", "sleep", "infinity"]


def enabled() -> bool:
    return (settings.get(SETTING, "1") or "1").strip().lower() not in ("0", "false", "no", "off")


def platform_kind() -> str:
    from . import service
    return service.detect()


def availability(plat: str | None = None) -> tuple[bool, str]:
    """(available on this OS, how)"""
    plat = plat or platform_kind()
    if plat == "windows":
        return True, "SetThreadExecutionState (system sleep only)"
    if plat == "macos":
        return (True, "caffeinate -i") if shutil.which("caffeinate") else (False, "caffeinate not found")
    if plat == "wsl":
        return False, "WSL2: an inhibitor inside WSL does not stop Windows sleeping; set the Windows power plan to never sleep while plugged in"
    return (True, "systemd-inhibit --what=sleep") if shutil.which("systemd-inhibit") else (False, "systemd-inhibit not found")


class WindowsBackend:
    """A thread that stays alive while held: SetThreadExecutionState applies to the calling thread."""

    def __init__(self, set_state=None):
        self._set_state = set_state
        self._release = threading.Event()
        self._thread: threading.Thread | None = None

    def _fn(self):
        if self._set_state is None:
            import ctypes
            self._set_state = ctypes.windll.kernel32.SetThreadExecutionState   # type: ignore[attr-defined]
        return self._set_state

    def start(self) -> None:
        fn = self._fn()
        ready = threading.Event()
        self._release.clear()

        def hold():
            fn(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
            ready.set()
            self._release.wait()
            fn(ES_CONTINUOUS)

        self._thread = threading.Thread(target=hold, name="keep-awake", daemon=True)
        self._thread.start()
        ready.wait(5)

    def stop(self) -> None:
        self._release.set()
        if self._thread:
            self._thread.join(5)
            self._thread = None


class ProcBackend:
    def __init__(self, argv: list[str], die_with_parent: bool = False):
        self.argv, self.die_with_parent, self.proc = argv, die_with_parent, None

    def start(self) -> None:
        kw = {}
        if self.die_with_parent and sys.platform.startswith("linux"):
            import ctypes
            libc = ctypes.CDLL(None)

            def pdeath():   # the kernel ends the inhibitor if the runner is killed without cleaning up
                libc.prctl(1, signal.SIGTERM)   # PR_SET_PDEATHSIG
            kw["preexec_fn"] = pdeath
        self.proc = subprocess.Popen(self.argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)

    def stop(self) -> None:
        p, self.proc = self.proc, None
        if p is None:
            return
        p.terminate()
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait(5)


def make_backend(plat: str | None = None):
    plat = plat or platform_kind()
    if plat == "windows":
        return WindowsBackend()
    if plat == "macos" and shutil.which("caffeinate"):
        return ProcBackend(["caffeinate", "-i", "-w", str(os.getpid())])
    if plat == "linux" and shutil.which("systemd-inhibit"):
        return ProcBackend(INHIBIT_CMD, die_with_parent=True)
    return None


class KeepAwake:
    def __init__(self, backend_factory=make_backend, is_enabled=enabled):
        self._factory, self._enabled = backend_factory, is_enabled
        self._lock = threading.Lock()
        self._count = 0
        self._backend = None
        self._warned = False

    @property
    def count(self) -> int:
        return self._count

    @property
    def held(self) -> bool:
        return self._backend is not None

    def acquire(self) -> None:
        with self._lock:
            self._count += 1
            if self._count == 1 and self._enabled():
                self._start()

    def _start(self) -> None:
        try:
            backend = self._factory()
            if backend is None:
                raise RuntimeError("keep-awake is not available on this system")
            backend.start()
            self._backend = backend
        except Exception as exc:  # noqa: BLE001  never let power management cost a job
            if not self._warned:
                self._warned = True
                log.warning("keep-awake unavailable (%s); the PC may sleep during a job", exc)

    def release(self) -> None:
        with self._lock:
            self._count = max(0, self._count - 1)
            if self._count == 0:
                self._stop()

    def release_all(self) -> None:
        """Runner shutdown: let go whatever the count says."""
        with self._lock:
            self._count = 0
            self._stop()

    def _stop(self) -> None:
        backend, self._backend = self._backend, None
        if backend is not None:
            try:
                backend.stop()
            except Exception:  # noqa: BLE001
                log.exception("keep-awake release failed")

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


GLOBAL = KeepAwake()
atexit.register(GLOBAL.release_all)
