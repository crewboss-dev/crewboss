"""Start the tray at logon (#974): a per-user, no-admin entry that is SEPARATE from the runner's own autostart (service.py).

  windows  a Scheduled Task "Crewboss runner tray", ONLOGON, /RL LIMITED, running crewboss-runner-tray (the GUI-subsystem launcher)
  macos    ~/Library/LaunchAgents/dev.crewboss.tray.plist (RunAtLoad)
  linux    ~/.config/autostart/crewboss-runner-tray.desktop (XDG autostart; WSL has no tray)
Nothing here needs administrator rights."""
from __future__ import annotations

import plistlib
import shutil
import subprocess
from pathlib import Path

from . import service

TASK_NAME = "Crewboss runner tray"
PLIST_LABEL = "dev.crewboss.tray"


def tray_exe() -> str:
    """The absolute path of crewboss-runner-tray, next to crewboss-runner."""
    exe = service.runner_path()
    p = Path(exe)
    cand = p.with_name("crewboss-runner-tray" + p.suffix)
    if cand.exists():
        return str(cand)
    return shutil.which("crewboss-runner-tray") or str(cand)


def desktop_path() -> Path:
    return Path.home() / ".config" / "autostart" / "crewboss-runner-tray.desktop"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{PLIST_LABEL}.plist"


def available(plat: str | None = None) -> bool:
    return (plat or service.detect()) == "windows"   # the tray is Windows-only (owner decision); the macOS / Linux code below is kept but unused


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def is_enabled(plat: str | None = None) -> bool:
    plat = plat or service.detect()
    if plat == "windows":
        return _run(["schtasks", "/Query", "/TN", TASK_NAME]).returncode == 0
    if plat == "macos":
        return plist_path().exists()
    return desktop_path().exists() if plat == "linux" else False


def enable(plat: str | None = None) -> tuple[bool, str]:
    plat = plat or service.detect()
    exe = tray_exe()
    try:
        if plat == "windows":
            res = _run(["schtasks", "/Create", "/TN", TASK_NAME, "/SC", "ONLOGON", "/RL", "LIMITED", "/TR", f'"{exe}"', "/F"])
            if res.returncode != 0:
                return False, (res.stderr or res.stdout).strip()
            cmd = service.schtasks_settings_command()   # 972's task settings: runs on battery, no 72 hour limit (best effort, like the runner's task)
            cmd[-1] = cmd[-1].replace(f"'{service.TASK_NAME}'", f"'{TASK_NAME}'")
            _run(cmd)
            return True, "the tray starts at logon"
        if plat == "macos":
            plist_path().parent.mkdir(parents=True, exist_ok=True)
            plist_path().write_bytes(plistlib.dumps({"Label": PLIST_LABEL, "ProgramArguments": [exe], "RunAtLoad": True}))
            return True, "the tray starts at login"
        if plat == "linux":
            desktop_path().parent.mkdir(parents=True, exist_ok=True)
            desktop_path().write_text(f"[Desktop Entry]\nType=Application\nName=Crewboss runner tray\nExec={_dq(exe)}\nTerminal=false\n"
                                      "X-GNOME-Autostart-enabled=true\n")
            return True, "the tray starts at login"
    except OSError as exc:
        return False, f"could not write it: {exc}"
    return False, "no tray on this system"


def disable(plat: str | None = None) -> tuple[bool, str]:
    plat = plat or service.detect()
    try:
        if plat == "windows":
            res = _run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
            return (res.returncode == 0 or not is_enabled(plat), "the tray no longer starts at logon")
        for path in (plist_path() if plat == "macos" else desktop_path(),):
            path.unlink(missing_ok=True)
        return True, "the tray no longer starts at login"
    except OSError as exc:
        return False, f"could not remove it: {exc}"


def _dq(path: str) -> str:
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'
