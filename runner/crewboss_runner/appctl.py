"""The ONE place the tray and the control panel (#974) touch the rest of the runner: a thin adapter over the public functions of
service / doctor / keepawake / store (and stopctl once task #972 lands it). Nothing else in tray.py, panel.py or ui.py imports those
modules for an action, so when #972 changes a signature this is the only file to re-check.

Every function returns plain data (never raises for an expected failure) so a web page or a menu can show it."""
from __future__ import annotations

import contextlib
import hashlib
import importlib
import inspect
import io
import os
import platform as _platform
import signal
import subprocess
import threading
import time
import types
from pathlib import Path

from . import doctor, enrol as enrol_mod, keepawake, profiles, runlog, service, settings, store

_lock = threading.Lock()   # service.* prints and shares stdout: one action at a time
_elev_lock = threading.Lock()   # the check-and-set of the elevation state is atomic
_elev: dict = {"state": "idle", "message": "", "action": ""}
STILL_WAITING = 259   # STILL_ACTIVE: run_elevated gave up waiting (the prompt or the elevated window is still open)   # the last elevated (UAC) action, polled by the page
STOP_WAIT = 30.0


def platform() -> str:
    return service.detect()


def stopctl():
    """#972's graceful-stop module, or None on a build that does not have it yet."""
    try:
        return importlib.import_module("crewboss_runner.stopctl")
    except ImportError:
        return None


def _call(fn, *a, **kw) -> tuple[int, str]:
    """Run a CLI-style function that prints; return (exit code, what it printed)."""
    buf = io.StringIO()
    with _lock, contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            rc = fn(*a, **kw)
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else 1
        except OSError as exc:   # schtasks / systemctl / launchctl not found
            return 1, f"could not run the service manager ({exc.__class__.__name__}: {exc})"
    return int(rc or 0), buf.getvalue().strip()


# --- is it running, what is it doing ----------------------------------------------------------------------------------
def is_running() -> bool:
    return store.run_alive()


def enrolled() -> dict | None:
    return store.Config().load()


def job_running() -> dict | None:
    sc = stopctl()
    if sc is not None:
        return sc.job_running()
    import json
    try:
        job = json.loads((store.state_dir() / "status.json").read_text()).get("job")
    except (OSError, ValueError):
        return None
    return job if job and store.run_alive() else None


# --- autostart ---------------------------------------------------------------------------------------------------------
def autostart_kind() -> str:
    """'off' | 'logon' (the hidden logon task / systemd user unit / LaunchAgent) | 'service' (the Windows service)."""
    plat = platform()
    if plat == "windows":
        try:
            kind = service.windows_kind()
        except OSError:
            return "off"
        return {"service": "service", "task": "logon"}.get(kind, "off")
    rc, _ = _call(service.status, plat)
    return "logon" if rc == 0 else "off"


def autostart_options(plat: str | None = None) -> list[dict]:
    plat = plat or platform()
    logon = {"windows": "At logon, hidden (a Scheduled Task, no window, no admin)", "macos": "At login (a LaunchAgent)",
             "linux": "At login (a systemd user unit)", "wsl": "At login (a Windows Scheduled Task for WSL)"}[plat]
    opts = [{"key": "off", "label": "Off: start it by hand", "available": True},
            {"key": "logon", "label": logon, "available": True}]
    if plat == "windows":
        opts.append({"key": "service", "label": "Windows Service (starts at boot, asks for administrator rights once)", "available": True})
    return opts


def start() -> tuple[bool, str]:
    """Start the runner, whichever way it is set up: autostart (task / unit / agent / service) or, with none, `crewboss-runnerw run` detached."""
    if is_running():
        return True, "already running"
    plat = platform()
    kind = autostart_kind()
    if plat == "windows" and kind == "service" and not service.is_admin():
        return _elevated("start", ["service", "start"])
    if kind != "off":
        rc, out = _call(service.control, plat, "start")
        if rc == 0:
            return True, "starting"
        if plat != "windows":   # the unit may be installed but disabled: fall through to a direct start
            return _detached_run()
        return False, out or "could not start it"
    return _detached_run()


def _detached_run() -> tuple[bool, str]:
    exe = service.runner_path()
    exe = service.runnerw_path(exe) or exe
    argv = [exe, "run"]
    kw: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    else:
        kw["start_new_session"] = True
    try:
        subprocess.Popen(argv, **kw)
    except OSError as exc:
        return False, f"could not start {argv[0]}: {exc}"
    return True, "starting"


def stop(force: bool = False, wait: float = STOP_WAIT) -> tuple[bool, str]:
    """Stop the runner gracefully (its agents are killed cleanly and the job goes back to the queue). Refuses while a job runs unless force."""
    if not is_running():
        return True, "not running"
    if job_running() and not force:
        return False, "a job is running"
    plat = platform()
    kind = autostart_kind()
    if plat == "windows" and kind == "service" and not service.is_admin():
        return _elevated("stop", ["service", "stop", "--force"] if force else ["service", "stop"])
    if kind != "off":
        kw = {"force": True} if "force" in inspect.signature(service.control).parameters else {}
        rc, out = _call(service.control, plat, "stop", **kw)
        return (True, "stopped") if rc == 0 else (False, out or "could not stop it")
    sc = stopctl()   # no autostart manages it: ask it to stop itself, else signal it
    if sc is not None:
        if sc.graceful_stop(wait):
            return True, "stopped"
        return False, "it did not stop in time"
    return _signal_stop(wait)


def _signal_stop(wait: float) -> tuple[bool, str]:
    try:
        pid = store.read_pid()
    except Exception:  # noqa: BLE001
        pid = None
    if pid is None:
        return False, "no pid file"
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)   # no graceful route without stopctl (#972)
        else:
            os.kill(pid, signal.SIGTERM)   # the runner's own Ctrl+C path: agents killed, lease released, pid file removed
    except (OSError, ProcessLookupError):
        pass
    end = time.monotonic() + wait
    while time.monotonic() < end:
        if not store.run_alive():
            return True, "stopped"
        time.sleep(0.3)
    return False, "it did not stop in time"


# --- elevation (UAC) ---------------------------------------------------------------------------------------------------
def elevated_status() -> dict:
    return dict(_elev)


def _elevated(label: str, cli_args: list[str], then=None) -> tuple[bool, str]:
    """Windows: ShellExecute 'runas' the crewboss-runner CLI with FIXED arguments (never anything from a web request). The CLI asks for the
    Windows password in its own elevated console: this process never sees it. Runs in a thread; elevated_status() reports the result."""
    if os.name != "nt":
        return False, "administrator elevation is a Windows feature"
    with _elev_lock:
        if _elev["state"] == "running":
            return False, "an administrator step is already waiting (look for the UAC prompt)"
        _elev.update(state="running", message="Waiting for administrator approval...", action=label)

    def work() -> None:
        try:
            code = run_elevated(service.runner_path(), cli_args)
            if code == 0 and then:
                then()
            ok = code == 0
            _elev.update(state="done" if ok else "failed",
                         message="Done." if ok else ("Cancelled at the administrator prompt." if code == 1223 else
                                                     "Still waiting for administrator approval: the administrator window did not finish in time. Nothing was changed by this page."
                                                     if code == STILL_WAITING else
                                                     f"The administrator step did not finish (exit code {code}). Run `crewboss-runner {' '.join(cli_args)}` "
                                                     "in an administrator PowerShell to see why."))
        except Exception as exc:  # noqa: BLE001
            _elev.update(state="failed", message=f"Could not ask for administrator rights ({exc.__class__.__name__}).")

    threading.Thread(target=work, name="elevate", daemon=True).start()
    return True, "pending"


def run_elevated(exe: str, cli_args: list[str], timeout: float = 600.0) -> int:
    """ShellExecuteEx 'runas', wait for it, return its exit code (1223 = the user cancelled the UAC prompt). Windows only; faked in tests."""
    import ctypes
    from ctypes import wintypes

    class SEI(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG), ("hwnd", wintypes.HWND), ("lpVerb", wintypes.LPCWSTR),
                    ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR), ("lpDirectory", wintypes.LPCWSTR),
                    ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE), ("lpIDList", ctypes.c_void_p),
                    ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY), ("dwHotKey", wintypes.DWORD),
                    ("hIcon", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]
    sh, k32 = ctypes.windll.shell32, ctypes.windll.kernel32   # type: ignore[attr-defined]
    info = SEI()
    info.cbSize, info.fMask, info.lpVerb, info.lpFile = ctypes.sizeof(SEI), 0x40, "runas", exe   # SEE_MASK_NOCLOSEPROCESS
    info.lpParameters, info.nShow = subprocess.list2cmdline(cli_args), 1
    if not sh.ShellExecuteExW(ctypes.byref(info)):
        return ctypes.GetLastError() or 1
    if k32.WaitForSingleObject(info.hProcess, int(timeout * 1000)) != 0:   # WAIT_TIMEOUT: not finished, so not an exit code
        k32.CloseHandle(info.hProcess)
        return STILL_WAITING
    code = wintypes.DWORD()
    k32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
    k32.CloseHandle(info.hProcess)
    return int(code.value)


def set_autostart(mode: str) -> tuple[bool, str]:
    """mode is one of the fixed keys from autostart_options()."""
    plat = platform()
    if in_runner_process() and plat != "windows":   # `service uninstall` / install stops the unit that runs this very page
        return False, OWN_PAGE_HINT + ("crewboss-runner service uninstall" if mode == "off" else "crewboss-runner service install")
    if mode not in {o["key"] for o in autostart_options(plat)}:
        return False, "that choice is not available on this PC"
    current = autostart_kind()
    if mode == current:
        return True, "already set"
    if plat == "windows":
        if mode == "service":
            return _elevated("install the Windows service", ["service", "install", "--windows-service"])
        if current == "service":   # leaving the service needs admin to remove it; the logon task needs none
            then = (lambda: _call(service.install, plat)) if mode == "logon" else None
            return _elevated("remove the Windows service", ["service", "uninstall"], then=then)
    if mode == "off":
        rc, out = _call(service.uninstall, plat)
    else:
        rc, out = _call(service.install, plat)
    return (rc == 0, (out.splitlines()[-1] if out else "done") if rc == 0 else (out or "failed"))


# --- keep awake --------------------------------------------------------------------------------------------------------
def keep_awake_info() -> dict:
    ok, how = keepawake.availability()
    return {"enabled": keepawake.enabled(), "available": ok, "how": how,
            "locked_by_env": settings.from_env(keepawake.SETTING) is not None}


def set_keep_awake(on: bool) -> tuple[bool, str]:
    if settings.from_env(keepawake.SETTING) is not None:
        return False, "this is set by an environment variable (CREWBOSS_RUNNER_KEEP_AWAKE_DURING_JOBS); change it there"
    settings.set_pref(keepawake.SETTING, "1" if on else "0")
    return True, "saved; applies from the next job"


# --- doctor + repairs --------------------------------------------------------------------------------------------------
def doctor_rows() -> list[dict]:
    return doctor.run_checks(doctor.labels_from(types.SimpleNamespace(label=[])))


def _rid(desc: str) -> str:
    return hashlib.sha1(desc.encode()).hexdigest()[:10]


def repairs() -> list[dict]:
    return [{"id": _rid(d), "text": d} for d, _fn in doctor.planned_repairs()]


def run_repair(rid: str) -> tuple[bool, str]:
    """Run ONE repair doctor already plans, chosen by its id (never a path or command from the caller)."""
    for desc, fn in doctor.planned_repairs():
        if _rid(desc) == rid:
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                return False, f"failed: {exc.__class__.__name__}"
            return True, f"done: {desc}"
    return False, "that repair is no longer needed"


# --- enrolment ---------------------------------------------------------------------------------------------------------
def enrol(code: str, name: str) -> tuple[bool, str]:
    """The pasted code goes ONLY to the existing enrol function."""
    try:
        cfg = enrol_mod.enrol(code.strip(), name, _platform.platform(), store.Secrets(), store.Config())
    except enrol_mod.EnrolError as exc:
        return False, str(exc)
    return True, f"Enrolled as device #{cfg['device_id']} with {cfg['server']}"


def log_lines(n: int = 100) -> list[str]:
    from .redact import redact
    try:
        text = Path(runlog.log_file()).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [redact(x) for x in text.splitlines()[-n:]]


# --- server profiles (#975) ---------------------------------------------------------------------------------------------
def list_profiles() -> list[dict]:
    """[{name, server, runner_name, state, active}], or [] when there are none or they cannot be read."""
    try:
        return profiles.list_profiles()
    except Exception:  # noqa: BLE001  a broken folder must not break the page or the tray
        return []


def use_profile(name: str, force: bool = False) -> tuple[bool, str]:
    """Switch the active profile (profiles.use_profile does the stop / switch / restart). The message is plain words, never a traceback."""
    if in_runner_process():
        return False, OWN_PAGE_HINT.replace("stop or remove itself", "switch servers by itself") + f"crewboss-runner use {name}"
    said: list[str] = []
    try:
        profiles.use_profile(name, force=force, restart=True, say=said.append)
    except profiles.ProfileError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, f"Could not switch servers ({exc.__class__.__name__}): {exc}"
    return True, " ".join(said) or f"Now using {name}."


# --- remove this runner from the PC -----------------------------------------------------------------------------------
FINAL_COMMAND = "pipx uninstall crewboss-runner"


def in_runner_process() -> bool:
    """True when THIS process is the running runner (its own page): it must not stop or uninstall itself in the middle of a request."""
    try:
        return store.read_pid() == os.getpid()
    except Exception:  # noqa: BLE001  a path this OS cannot make
        return False


OWN_PAGE_HINT = ("This page is served by the runner itself, which cannot stop or remove itself in the middle of a request. "
                 "Open the control panel from the tray (Windows) or run this in a terminal: ")


def remove_runner(on_done=None) -> tuple[bool, str]:
    """doctor.cmd_uninstall (stop, remove the autostart, delete key, token, config, state): reused as is, never reimplemented here.
    Returns (True, 'pending') when an administrator step was started (on_done runs only if it really finished OK), (True, text) when it
    finished here, (False, why) otherwise: nothing is reported as removed before it happened. Refuses while a job runs, with the CLI's words."""
    from . import trayauto
    ns = types.SimpleNamespace(yes=True)
    if in_runner_process():
        return False, OWN_PAGE_HINT + "crewboss-runner uninstall"
    if os.name == "nt" and platform() == "windows" and autostart_kind() == "service" and not service.is_admin():
        if job_running():   # the elevated copy would refuse too; say so here with the same words
            rc, out = _call(doctor.cmd_uninstall, ns)
            return False, out

        def finish() -> None:
            trayauto.disable()
            if on_done:
                on_done()
        return _elevated("remove the runner", ["uninstall", "--yes"], then=finish)
    if is_running() and not job_running():
        stop()   # gracefully, by whatever way it is set up; uninstall then finds nothing running
    rc, out = _call(doctor.cmd_uninstall, ns)
    if rc != 0:
        return False, out or "could not remove the runner"
    trayauto.disable()
    if on_done:
        on_done()
    return True, out
