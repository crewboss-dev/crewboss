"""Is this runner running with administrator rights? (Windows, task #972)

The hidden logon task uses a LIMITED token. A Windows service logon would get the account's full, unfiltered administrator token, so
the service only supervises and starts the real runner with a filtered standard-user token (winsvc.launch_restricted). This is the
second line of defence: a runner that finds itself elevated (started from an administrator shell, or a service misconfiguration)
claims no jobs, because its agents would inherit that token. CREWBOSS_RUNNER_ALLOW_ELEVATED=1 overrides it."""
from __future__ import annotations

import os

from . import settings

TOKEN_QUERY = 0x0008
TOKEN_ELEVATION = 20   # TokenElevation


def is_elevated() -> bool | None:
    """True / False on Windows (TokenElevation of this process); None elsewhere or if it cannot be read."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        adv, k32 = ctypes.windll.advapi32, ctypes.windll.kernel32   # type: ignore[attr-defined]
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        tok = wintypes.HANDLE()
        if not adv.OpenProcessToken(k32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(tok)):
            return None
        try:
            val, ret = wintypes.DWORD(), wintypes.DWORD()
            if not adv.GetTokenInformation(tok, TOKEN_ELEVATION, ctypes.byref(val), ctypes.sizeof(val), ctypes.byref(ret)):
                return None
            return bool(val.value)
        finally:
            k32.CloseHandle(tok)
    except Exception:  # noqa: BLE001
        return None


def allowed() -> bool:
    return (settings.get("ALLOW_ELEVATED", "") or "").strip().lower() in ("1", "true", "yes", "on")


def refusal(elevated: bool | None = None) -> str | None:
    """A sentence for the runner's problem list when it must not start jobs, else None."""
    if elevated is None:
        elevated = is_elevated()
    if elevated and not allowed():
        return ("this runner is running with administrator rights, which its agents would inherit: start it from a normal (not "
                "elevated) PowerShell or use `crewboss-runner service install`, or set CREWBOSS_RUNNER_ALLOW_ELEVATED=1 if you accept that")
    return None
