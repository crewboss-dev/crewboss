"""`crewboss-runner service install|uninstall|status`: keep the runner running at login, one command on every OS.

  linux    a systemd --user unit (~/.config/systemd/user/crewboss-runner.service)
  macos    a LaunchAgent (~/Library/LaunchAgents/dev.crewboss.runner.plist), logs in ~/Library/Logs/crewboss-runner.log
  windows  a per-user HIDDEN Scheduled Task "Crewboss runner" at logon (runs crewboss-runnerw.exe: no console window; log in
           %LOCALAPPDATA%/crewboss-runner/runner.log). `--windows-service` instead installs a real Windows service that runs as
           you (administrator needed, asks for your password); installing one kind removes the other
  wsl      WSL2 can stop an idle distro even when it has systemd units, so the autostart is a *Windows* Scheduled Task that runs
           `wsl.exe -d <distro> -- <runner> run` at logon. `--systemd` forces the plain systemd route instead.

Only `--windows-service` needs admin rights. `--dry-run` prints what would be written and run, and does neither."""
from __future__ import annotations

import os
import plistlib
import getpass
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import admincopy, runlog, stopctl, store, winsvc

TASK_NAME = "Crewboss runner"
UNIT_NAME = "crewboss-runner.service"
PLIST_LABEL = "dev.crewboss.runner"


def detect() -> str:
    """'wsl' | 'linux' | 'macos' | 'windows'."""
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if os.environ.get("WSL_DISTRO_NAME"):
        return "wsl"
    try:
        if "microsoft" in Path("/proc/sys/kernel/osrelease").read_text().lower():
            return "wsl"
    except OSError:
        pass
    return "linux"


def runner_path() -> str:
    """The absolute path of this crewboss-runner, for ExecStart / the task action."""
    found = shutil.which("crewboss-runner")
    if found:
        return str(Path(found).absolute())
    return str(Path(sys.argv[0]).absolute())


def runnerw_sibling(exe: str) -> str:
    """Where crewboss-runnerw (the GUI-subsystem launcher: no console window) sits: next to crewboss-runner."""
    p = Path(exe)
    return str(p.with_name("crewboss-runnerw" + p.suffix))


def runnerw_path(exe: str) -> str | None:
    """The installed crewboss-runnerw next to `exe` (or on PATH), else None (an install from before it existed)."""
    cand = runnerw_sibling(exe)
    if Path(cand).exists():
        return cand
    found = shutil.which("crewboss-runnerw")
    return str(Path(found).absolute()) if found else None


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def _q(path: str) -> str:
    return f'"{path}"' if " " in path else path


def baked_path(exe: str) -> tuple[str, bool]:
    """The PATH to bake into an autostart (service managers give a bare PATH with no ~/.local/bin, Homebrew or nvm): this shell's
    PATH, with the directories of `claude` and of the runner put on it. Returns (path, claude_found)."""
    parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    claude = shutil.which("claude")
    for found in (str(Path(exe).parent), str(Path(claude).parent) if claude else ""):
        if found and found not in parts:
            parts.insert(0, found)
    return os.pathsep.join(parts), bool(claude)


def _report_path(path: str, claude_found: bool) -> None:
    if not claude_found:
        print("WARNING: `claude` was not found on this PATH: install Claude Code first (and sign in), then run this again.", file=sys.stderr)
    print(f"PATH baked into the autostart: {path}")
    print("Re-run `crewboss-runner service install` after installing new tools, so this PATH is refreshed.")


def _sh_quote(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


WSL_TR_MAX = 261   # Windows limits a Scheduled Task's /TR to 261 characters


def wrapper_path() -> Path:
    return Path.home() / ".local" / "share" / "crewboss-runner" / "start.sh"


def wrapper_text(exe: str, path: str) -> str:
    return f"#!/bin/sh\nexport PATH={_sh_quote(path)}\nexec {_sh_quote(exe)} run\n"


def _uid() -> int:
    return os.getuid() if hasattr(os, "getuid") else 501


# ---- the files and commands each platform needs (pure: easy to test) ----

def unit_text(exe: str, path: str | None = None) -> str:
    env = ""
    if path:   # systemd: `%` is a specifier (so %%), and the quotes keep spaces in the value
        env = 'Environment="PATH=' + path.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"\n'
    return ("[Unit]\nDescription=Crewboss runner\nAfter=network-online.target\n\n"
            f"[Service]\n{env}ExecStart={exe} run\nRestart=on-failure\nRestartSec=10\n\n"
            "[Install]\nWantedBy=default.target\n")


def unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / UNIT_NAME


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{PLIST_LABEL}.plist"


def plist_bytes(exe: str, path: str | None = None) -> bytes:
    log = str(Path.home() / "Library" / "Logs" / "crewboss-runner.log")
    data = {"Label": PLIST_LABEL, "ProgramArguments": [exe, "run"], "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 10,
            "StandardOutPath": log, "StandardErrorPath": log}
    if path:
        data["EnvironmentVariables"] = {"PATH": path}
    return plistlib.dumps(data)


def task_action(exe: str, plat: str, distro: str = "", wrapper: bool = False) -> str:
    """The command line the Scheduled Task runs at logon (WSL: the start.sh wrapper, which sets PATH, then runs the runner)."""
    if plat == "wsl" and wrapper:
        return f"wsl.exe -d {_q(distro)} -- {_q(exe)}" if distro else f"wsl.exe -- {_q(exe)}"
    if plat == "wsl":
        return f"wsl.exe -d {_q(distro)} -- {_q(exe)} run" if distro else f"wsl.exe -- {_q(exe)} run"
    return f'"{exe}" run'


def schtasks_create(action: str, exe_name: str = "schtasks.exe") -> list[str]:
    return [exe_name, "/Create", "/TN", TASK_NAME, "/SC", "ONLOGON", "/RL", "LIMITED", "/TR", action, "/F"]


def _schtasks() -> str:
    return "schtasks.exe" if detect() == "wsl" else "schtasks"


def is_admin() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())   # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return False


def schtasks_settings_command(exe_name: str = "powershell") -> list[str]:
    """schtasks /Create gives a task Windows' defaults: killed after 72 hours, not started or stopped on battery. A runner must
    survive both, so the task's settings are replaced (best effort: the task works without this, it just may be stopped)."""
    ps = (f"Set-ScheduledTask -TaskName '{TASK_NAME}' -Settings (New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries "
          "-DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable)")
    return [exe_name, "-NoProfile", "-NonInteractive", "-Command", ps]


def ctl_command(plat: str, action: str, systemd: bool = False, win_kind: str = "task") -> list[str]:
    """The command for `service stop` / `service start` (action is 'stop' or 'start')."""
    if action not in ("stop", "start"):
        raise ValueError(action)
    if plat == "windows" and win_kind == "service":
        return ["sc.exe", action, winsvc.SERVICE_NAME]
    if _uses_task(plat, systemd):
        return [_schtasks(), "/End" if action == "stop" else "/Run", "/TN", TASK_NAME]
    if plat in ("linux", "wsl"):
        return ["systemctl", "--user", action, "crewboss-runner"]
    if plat == "macos":   # the job stays defined only while it is loaded: stop = unload, start = load the plist again
        if action == "stop":
            return ["launchctl", "bootout", f"gui/{_uid()}/{PLIST_LABEL}"]
        return ["launchctl", "bootstrap", f"gui/{_uid()}", str(plist_path())]
    raise ValueError(plat)


def windows_service_installed() -> bool:
    return _run(["sc.exe", "query", winsvc.SERVICE_NAME]).returncode == 0


def windows_task_installed() -> bool:
    return _run(["schtasks", "/Query", "/TN", TASK_NAME]).returncode == 0


def windows_kind() -> str:
    """'service' | 'task' | '' (nothing installed)."""
    if windows_service_installed():
        return "service"
    return "task" if windows_task_installed() else ""


NEEDS_ADMIN = ("Installing or removing a Windows service needs an administrator: open PowerShell as administrator and run this again. "
               "Without admin, the default does the job: crewboss-runner service install  (a hidden logon task, no window).")


SERVICE_WAIT = 45.0


def _service_state() -> str:
    """'RUNNING' | 'STOPPED' | ... | '' when the service does not exist (sc query: 1060) or is gone."""
    res = _run(["sc.exe", "query", winsvc.SERVICE_NAME])
    for word in ("RUNNING", "STOP_PENDING", "START_PENDING", "STOPPED", "PAUSED"):
        if word in res.stdout:
            return word
    return ""


def _wait(what: str, done, timeout: float = SERVICE_WAIT, poll: float = 1.0) -> bool:
    """Poll done() until true, printing progress; False on timeout."""
    end = time.monotonic() + timeout
    t0 = time.monotonic()
    n = 0
    while not done():
        if time.monotonic() >= end:
            return False
        time.sleep(poll)
        n += 1
        if n % 5 == 0:
            print(f"  still waiting for {what} ({int(time.monotonic() - t0)} s)...", flush=True)
    return True


def stop_and_delete_service(timeout: float = SERVICE_WAIT) -> bool:
    """sc stop, wait for STOPPED, sc delete, wait until the service is really gone (not merely marked for deletion: 1072). Two runners
    must never share one key, and CreateService fails with 1072 while the old one lingers."""
    if _service_state() not in ("STOPPED", ""):
        _run(["sc.exe", "stop", winsvc.SERVICE_NAME])
        if not _wait("the service to stop", lambda: _service_state() in ("STOPPED", ""), timeout):
            print(f"The service did not stop within {timeout:.0f} s.", file=sys.stderr)
            return False
    if _service_state() == "":
        return True
    _run(["sc.exe", "delete", winsvc.SERVICE_NAME])
    if not _wait("the service to be removed", lambda: _service_state() == "", timeout):
        print("The service is marked for deletion but still present (close services.msc and any Event Viewer / Process Explorer "
              "windows holding it, or reboot), then run this again.", file=sys.stderr)
        return False
    return True


def _remove_windows_service(dry: bool) -> bool:
    """Stop and delete the service if there is one (so there is never a second runner)."""
    if dry:
        _step(dry, ["sc.exe", "stop", winsvc.SERVICE_NAME], check=False)
        return _step(dry, ["sc.exe", "delete", winsvc.SERVICE_NAME])
    if not windows_service_installed():
        return True
    if not is_admin():
        print("A Windows service for the runner is installed and removing it needs an administrator. " + NEEDS_ADMIN, file=sys.stderr)
        return False
    print("$ sc.exe stop / delete " + winsvc.SERVICE_NAME)
    return stop_and_delete_service()


def _remove_windows_task(dry: bool, stop_runner: bool = True) -> None:
    if dry or windows_task_installed():
        if not dry and stop_runner:
            stopctl.graceful_stop()   # never a hard /End first: that orphans the agent
        _step(dry, ["schtasks", "/End", "/TN", TASK_NAME], check=False)
        _step(dry, ["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], check=False)


def install_windows_task(exe: str, dry: bool) -> int:
    hidden = runnerw_path(exe)
    if hidden is None and dry:
        hidden = runnerw_sibling(exe)
    if hidden is None:
        print("WARNING: crewboss-runnerw was not found next to crewboss-runner (an older install): a console window will show. "
              "Update the runner (pip install -U ...) and run this again to hide it.", file=sys.stderr)
        hidden = exe
    if not _remove_windows_service(dry):
        return 1
    ok = _step(dry, schtasks_create(task_action(hidden, "windows"), "schtasks"))
    if ok:
        _step(dry, schtasks_settings_command(), check=False)
        ok = _step(dry, ["schtasks", "/Run", "/TN", TASK_NAME])
    if ok:
        print(f'Created the hidden Scheduled Task "{TASK_NAME}" (runs at logon, as you, no admin, no window) and started it. '
              f"Log: {runlog.log_file()}. Find the status page link with: crewboss-runner status. "
              "Stop / start it with: crewboss-runner service stop / start")
    return 0 if ok else 1


def enrolment_problem(account: str) -> str:
    """'' if the account running this command holds the enrolment (config + secrets under ITS profile), else why not. The elevated
    shell may belong to an IT admin, not the person whose runner this is: the service would run as that admin with nothing enrolled."""
    cfg = store.Config().load()
    secrets = store.Secrets()
    if cfg and secrets.get("device_key") and secrets.get("device_token"):
        return ""
    return (f"There is no enrolment for {account} on this PC (looked in {store.config_dir()} and {secrets.describe()}). The service "
            "runs as the account you run this from, so run it from the account that enrolled the runner (an administrator PowerShell "
            "opened by that user), or enrol first: crewboss-runner enrol <code>")


def _wait_service_running(timeout: float = 60.0, poll: float = 1.0) -> bool:
    return _wait("the service to start", lambda: _service_state() == "RUNNING", timeout, poll)


def _wait_runner_up(timeout: float = 30.0, poll: float = 1.0) -> bool:
    return _wait("the runner to come up", store.run_alive, timeout, poll)


def _rollback(st: dict, had_task: bool) -> None:
    """Undo what this run did: the half-made service goes, a right WE granted is revoked, and the logon task's runner (the task itself is
    never removed before the service is confirmed running) is started again."""
    did = []
    if st.get("service"):
        stop_and_delete_service()
        did.append("the half-made service was removed")
    if st.get("copy") and admincopy.remove():
        did.append("the admin-only copy was removed")
    if st.get("granted_new") and winsvc.revoke_logon_right(st["account"]):
        did.append('the "Log on as a service" right this run granted was revoked')
    if st.get("runner_stopped"):
        if had_task:
            _step(False, ["schtasks", "/Run", "/TN", TASK_NAME], check=False)
            did.append("the logon task's runner is running again")
        else:
            did.append("the runner you had running was stopped: start it again with crewboss-runner run")
    if did:
        print("Rolled back: " + "; ".join(did) + ".", file=sys.stderr)


def install_windows_service(exe: str, dry: bool, force: bool = False) -> int:
    account = winsvc.default_account()
    path, claude_found = baked_path(exe)
    child_exe = runnerw_path(exe) or (runnerw_sibling(exe) if dry else exe)
    if winsvc.is_entra():
        print(winsvc.ENTRA_HELP, file=sys.stderr)
        return 1
    if not dry:
        if not is_admin():
            print(NEEDS_ADMIN, file=sys.stderr)
            return 1
        if not winsvc.HAVE_PYWIN32:
            print("pywin32 is not installed in this Python (it is a dependency of crewboss-runner on Windows): "
                  "reinstall the runner, or use the default: crewboss-runner service install", file=sys.stderr)
            return 1
        why = admincopy.base_python_problem()
        if why:   # the supervisor's admin-only copy needs an admin-only base Python: refuse before anything is changed
            print("Not installing the service: " + why, file=sys.stderr)
            return 1
        why = enrolment_problem(account)
        if why:   # before anything is changed
            print(why, file=sys.stderr)
            return 1
        if _stop_blocked_by_job(force):
            return 1
    print(f"The service logs on as {account}: the device key lives in that user's Credential Manager and the Claude Code login is that "
          "user's too, which the built-in SYSTEM account cannot see. The service only supervises: the runner itself is started with a "
          "standard-user token, so your agents do not get administrator rights. A service has no desktop: sign in to Claude Code "
          "(run `claude` once) before installing; the service cannot open a browser.")
    if dry:
        print(f"ask for the password of {account} (getpass; handed to the Service Control Manager, never stored or logged)")
        print(f"grant {winsvc.lookup_name(account)} the {winsvc.LOGON_RIGHT} (Log on as a service) right: LsaAddAccountRights")
        print("verify the password: LogonUser(LOGON32_LOGON_SERVICE)")
        print("remove an older service, if any; stop the logon task's runner gracefully (the task stays until the service runs)")
        print(f"win32serviceutil.InstallService({winsvc.CLASS_STRING}, {winsvc.SERVICE_NAME!r}, {winsvc.DISPLAY_NAME!r}, auto start, {account})")
        print(f"check that the base Python ({admincopy.base_python()}) is not writable by non-admins (ACL), else refuse")
        print(f"build the ADMIN-ONLY copy {admincopy.program_dir()}: lock its ACL (Administrators + SYSTEM full, Users read/execute, no inheritance), then:")
        for c in admincopy.icacls_commands(admincopy.program_dir()) + admincopy.build_commands(admincopy.program_dir(), Path("<wheel of this runner>")):
            print("$ " + " ".join(_q(x) for x in c))
        print(f"the service runs {admincopy.service_exe()} (PYTHONNOUSERSITE=1, PYTHONPATH inside that folder only, system-folders PATH)")
        print(f"set {winsvc.CHILD_VALUE}={child_exe} and the child's PATH on the service key (HKLM); the child gets a restricted token")
        ok = all(_step(dry, c) for c in winsvc.sc_config_commands()) and _step(dry, ["sc.exe", "start", winsvc.SERVICE_NAME])
        print("wait for the service to be RUNNING and the runner to come up; only then delete the logon task. "
              "On any failure: delete the half-made service and keep the task.")
        _report_path(path, claude_found)
        return 0 if ok else 1
    pw = getpass.getpass(f"Windows password for {account} (not stored; Microsoft account: its password, not the PIN): ")
    if not pw:
        print("An account with no password cannot run a service. Use the default instead: crewboss-runner service install", file=sys.stderr)
        return 1
    st = {"account": account, "service": False, "granted_new": False, "runner_stopped": False}
    had_task = windows_task_installed()
    done = False
    try:   # ANY exit that is not success (an error, Ctrl+C) undoes what this run did
        st["granted_new"] = not winsvc.has_logon_right(account)
        granted, why = winsvc.grant_logon_right(account)
        if not granted:
            st["granted_new"] = False
            print(f"Could not grant the Log on as a service right ({why}). " + winsvc.RIGHT_HELP, file=sys.stderr)
            return 1
        verified, why = winsvc.verify_logon(account, pw)
        if not verified:
            print(f"Windows would not log {account} on as a service: {why}\nNothing else was changed; the logon task (if any) is untouched. "
                  "If this account is a Microsoft or Entra ID (AzureAD) one, use the hidden task: crewboss-runner service install",
                  file=sys.stderr)
            return 1
        if not _remove_windows_service(False):
            return 1
        if store.run_alive():   # any running runner, task or hand-started: two runners on one device key must not overlap
            print("Stopping the running runner (up to %d s)..." % stopctl.STOP_WAIT, flush=True)
            if not stopctl.graceful_stop():
                print("The running runner did not stop in time; nothing was changed.", file=sys.stderr)
                return 1
            st["runner_stopped"] = True
        print("Building the admin-only copy of the runner (this runs pip; it can take a minute)...", flush=True)
        admincopy.remove()   # a reinstall starts from nothing: the old service is gone, so its files are unlocked
        st["copy"] = True
        try:
            admincopy.build()
        except Exception as exc:  # noqa: BLE001
            print(f"Could not build the admin-only copy: {exc}", file=sys.stderr)
            return 1
        st["service"] = True
        try:
            winsvc.install(account, pw, str(admincopy.service_exe()))
        except Exception as exc:  # noqa: BLE001  (pywintypes.error: its text never contains the password)
            print(f"Installing the service failed: {winsvc.explain_error(exc)}", file=sys.stderr)
            return 1
        finally:
            pw = ""
        try:
            winsvc.set_service_environment(path, child_exe, admincopy.system_path(), admincopy.service_pythonpath())
        except OSError as exc:
            print(f"Could not write the service's settings ({exc}).", file=sys.stderr)
            return 1
        if not all(_step(False, c) for c in winsvc.sc_config_commands()):
            return 1
        _report_path(path, claude_found)
        _step(False, ["sc.exe", "start", winsvc.SERVICE_NAME], check=False)
        print("Waiting for the service and the runner to come up...", flush=True)
        if not (_wait_service_running() and _wait_runner_up()):
            print("The service did not come up (error 1069 means a wrong password or no Log on as a service right; see "
                  f"{runlog.log_dir() / 'service.log'} and runner.log).", file=sys.stderr)
            return 1
        done = True
    finally:
        pw = ""
        if not done:
            _rollback(st, had_task)
    _remove_windows_task(False, stop_runner=False)   # only now, with the service confirmed running
    print(f'Installed the Windows service "{winsvc.DISPLAY_NAME}" (automatic, delayed start, restarts on failure) and it is running. '
          f"It runs the runner as {account} with a standard-user token. Logs: {runlog.log_dir()} (service.log, runner.log). "
          "Status page link: crewboss-runner status. Manage it in services.msc.")
    return 0


# ---- actions ----

def _step(dry: bool, cmd: list[str], check: bool = True) -> bool:
    print("$ " + " ".join(_q(c) for c in cmd))
    if dry:
        return True
    res = _run(cmd)
    if res.returncode != 0 and check:
        print((res.stderr or res.stdout or "").strip() or f"exit {res.returncode}", file=sys.stderr)
        return False
    return True


def _write(dry: bool, path: Path, data: str | bytes) -> None:
    print(f"write {path}:")
    print((data if isinstance(data, str) else data.decode()).rstrip())
    if dry:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data)
    else:
        path.write_bytes(data)


def _uses_task(plat: str, systemd: bool) -> bool:
    return plat == "windows" or (plat == "wsl" and not systemd)


def install(plat: str, dry: bool = False, systemd: bool = False, exe: str | None = None, windows_service: bool = False,
            force: bool = False) -> int:
    exe = exe or runner_path()
    if windows_service and plat != "windows":
        print("--windows-service is for native Windows only.", file=sys.stderr)
        return 1
    if plat == "wsl" and not systemd:
        distro = os.environ.get("WSL_DISTRO_NAME", "")
        if not dry and not shutil.which("schtasks.exe"):
            print("Cannot reach Windows from this WSL (schtasks.exe not found). Use: crewboss-runner service install --systemd", file=sys.stderr)
            return 1
        path, claude_found = baked_path(exe)
        wrapper = wrapper_path()
        action = task_action(str(wrapper), "wsl", distro, wrapper=True)
        if len(action) > WSL_TR_MAX:
            print(f"The Scheduled Task command is {len(action)} characters; Windows allows {WSL_TR_MAX}. The WSL user name or distro name "
                  "is too long: use `crewboss-runner service install --systemd` instead.", file=sys.stderr)
            return 1
        _write(dry, wrapper, wrapper_text(exe, path))
        if not dry:
            wrapper.chmod(0o700)
        else:
            print(f"chmod 0700 {wrapper}")
        _report_path(path, claude_found)
        ok = (_step(dry, schtasks_create(action))
              and _step(dry, ["schtasks.exe", "/Run", "/TN", TASK_NAME]))
        if ok:
            print(f'Created the Windows Scheduled Task "{TASK_NAME}": it starts this runner inside WSL ({distro or "default distro"}) '
                  "each time you log in to Windows, and it is running now. A console window stays open (minimise it).")
        return 0 if ok else 1
    if plat in ("linux", "wsl"):
        path, claude_found = baked_path(exe)
        _write(dry, unit_path(), unit_text(exe, path))
        _report_path(path, claude_found)
        ok = (_step(dry, ["systemctl", "--user", "daemon-reload"])
              and _step(dry, ["systemctl", "--user", "enable", "--now", "crewboss-runner"]))
        if ok:
            print("Installed. Logs: journalctl --user -u crewboss-runner -f")
        return 0 if ok else 1
    if plat == "macos":
        path, claude_found = baked_path(exe)
        _write(dry, plist_path(), plist_bytes(exe, path))
        _report_path(path, claude_found)
        _step(dry, ["launchctl", "bootout", f"gui/{_uid()}/{PLIST_LABEL}"], check=False)
        ok = _step(dry, ["launchctl", "bootstrap", f"gui/{_uid()}", str(plist_path())])
        if ok:
            print("Installed. Logs: ~/Library/Logs/crewboss-runner.log")
        return 0 if ok else 1
    if plat == "windows":
        return install_windows_service(exe, dry, force) if windows_service else install_windows_task(exe, dry)
    print(f"unsupported platform {plat!r}", file=sys.stderr)
    return 1


def uninstall(plat: str, dry: bool = False, systemd: bool = False, force: bool = False) -> int:
    if plat == "windows":   # whichever kind is installed (or both, from an interrupted change)
        if not dry and windows_kind() and _stop_blocked_by_job(force):
            return 1
        if not _remove_windows_service(dry):
            return 1
        if dry:
            print(f"remove {admincopy.program_dir()} (the service's admin-only copy), if present")
        elif admincopy.program_dir().exists():
            if not is_admin():
                print(f"{admincopy.program_dir()} (the service's copy) can only be removed by an administrator. " + NEEDS_ADMIN, file=sys.stderr)
                return 1
            if not admincopy.remove():
                print(f"Could not remove {admincopy.program_dir()}; close anything using it and run this again.", file=sys.stderr)
                return 1
        _remove_windows_task(dry)
        return 0
    if _uses_task(plat, systemd):
        st = _schtasks()
        if plat == "wsl" and not dry:
            wrapper_path().unlink(missing_ok=True)
        _step(dry, [st, "/End", "/TN", TASK_NAME], check=False)
        return 0 if _step(dry, [st, "/Delete", "/TN", TASK_NAME, "/F"]) else 1
    if plat in ("linux", "wsl"):
        _step(dry, ["systemctl", "--user", "disable", "--now", "crewboss-runner"], check=False)
        print(f"remove {unit_path()}")
        if not dry:
            unit_path().unlink(missing_ok=True)
            wrapper_path().unlink(missing_ok=True)
        return 0 if _step(dry, ["systemctl", "--user", "daemon-reload"]) else 1
    if plat == "macos":
        _step(dry, ["launchctl", "bootout", f"gui/{_uid()}/{PLIST_LABEL}"], check=False)
        print(f"remove {plist_path()}")
        if not dry:
            plist_path().unlink(missing_ok=True)
        return 0
    return 1


def _windows_status() -> int:
    kind = windows_kind()
    if not kind:
        print("not installed; run: crewboss-runner service install")
        return 1
    if kind == "service":
        out = _run(["sc.exe", "query", winsvc.SERVICE_NAME]).stdout
        state = "running" if "RUNNING" in out else "not running"
        print(f'installed: Windows service "{winsvc.DISPLAY_NAME}" (as a user), {state}')
        return 0
    res = _run(["schtasks", "/Query", "/TN", TASK_NAME, "/FO", "LIST", "/V"])
    lines = res.stdout.splitlines()
    state = next((ln.split(":", 1)[1].strip() for ln in lines if ln.strip().startswith("Status")), "?")
    action = next((ln.split(":", 1)[1].strip() for ln in lines if ln.strip().startswith("Task To Run")), "")
    hidden = "runnerw" in action.lower()
    print(f'installed: {"hidden " if hidden else ""}logon task "{TASK_NAME}", {"running" if state.lower() == "running" else state}'
          + ("" if hidden or not action else "  (console version: run `crewboss-runner service install` again to hide the window)"))
    return 0


def status(plat: str, systemd: bool = False) -> int:
    rc = _status(plat, systemd)
    url = runlog.read_url()
    if url:
        print(f"status page (this PC only): {url}")
    return rc


def _status(plat: str, systemd: bool = False) -> int:
    if plat == "windows":
        return _windows_status()
    if _uses_task(plat, systemd):
        res = _run([_schtasks(), "/Query", "/TN", TASK_NAME, "/FO", "LIST"])
        if res.returncode != 0:
            print(f'not installed (no Scheduled Task "{TASK_NAME}"); run: crewboss-runner service install')
            return 1
        state = next((ln.split(":", 1)[1].strip() for ln in res.stdout.splitlines() if ln.strip().startswith("Status")), "?")
        print(f'installed: Scheduled Task "{TASK_NAME}", status {state}')
        return 0
    if plat in ("linux", "wsl"):
        enabled = _run(["systemctl", "--user", "is-enabled", "crewboss-runner"]).stdout.strip() or "not installed"
        active = _run(["systemctl", "--user", "is-active", "crewboss-runner"]).stdout.strip() or "inactive"
        print(f"systemd user unit: {enabled}, {active}")
        return 0 if enabled == "enabled" else 1
    if plat == "macos":
        res = _run(["launchctl", "print", f"gui/{_uid()}/{PLIST_LABEL}"])
        if res.returncode != 0:
            print("not installed; run: crewboss-runner service install")
            return 1
        print(f"installed: LaunchAgent {PLIST_LABEL}" + (", running" if "state = running" in res.stdout else ", not running"))
        return 0
    return 1


def _stop_blocked_by_job(force: bool) -> bool:
    job = stopctl.job_running()
    if job and not force:
        print(f"A job is running on this PC (#{job.get('id', '?')} on {job.get('branch', '?')}). Stopping now cancels it and it goes back "
              "to the queue. Wait for it to finish, or add --force.", file=sys.stderr)
        return True
    return False


def control(plat: str, action: str, dry: bool = False, systemd: bool = False, force: bool = False) -> int:
    kind = "task"
    if plat == "windows" and not dry:
        kind = windows_kind()
        if not kind:
            print("not installed; run: crewboss-runner service install", file=sys.stderr)
            return 1
        if kind == "service" and not is_admin():
            print(NEEDS_ADMIN, file=sys.stderr)
            return 1
    elif plat == "windows":
        kind = "service" if windows_service_installed() else "task"
    if action == "stop" and not dry:
        if _stop_blocked_by_job(force):
            return 1
        if kind == "task" and _uses_task(plat, systemd):   # a hard `schtasks /End` orphans the agent: ask the runner to stop itself first
            print("asking the runner to stop (up to %d s)..." % stopctl.STOP_WAIT)
            if stopctl.graceful_stop():
                print("Stopped.")
                return 0
            print("It did not stop in time: ending it.")
    cmd = ctl_command(plat, action, systemd, kind)
    ok = _step(dry, cmd)
    if ok:
        print("Stopped." if action == "stop" else "Started. Status page link: crewboss-runner status (give it a few seconds)")
    return 0 if ok else 1


def main(args) -> int:
    plat = detect()
    dry = getattr(args, "dry_run", False)
    systemd = getattr(args, "systemd", False)
    if args.action in ("stop", "start"):
        return control(plat, args.action, dry, systemd, getattr(args, "force", False))
    if args.action == "install":
        return install(plat, dry, systemd, windows_service=getattr(args, "windows_service", False),
                       force=getattr(args, "force", False))
    if args.action == "uninstall":
        return uninstall(plat, dry, systemd, getattr(args, "force", False))
    return status(plat, systemd)
