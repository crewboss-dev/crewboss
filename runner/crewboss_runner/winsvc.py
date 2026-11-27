"""The optional real Windows Service (task #972): `crewboss-runner service install --windows-service`, from an administrator PowerShell.

It logs on AS THE CURRENT USER, not LocalSystem, because the keyring (Windows Credential Manager) that holds the device key and the
Claude Code login are both per-user: LocalSystem would see neither. The password is asked for with getpass, handed straight to the
Service Control Manager and never stored or logged by us.

THE SERVICE ONLY SUPERVISES. A service logon gets the account's full, unfiltered administrator token (no UAC filtering), and every
agent the runner starts would inherit it. So the service process is a thin supervisor: it starts the real runner as a CHILD with a
filtered standard-user token (CreateRestrictedToken: administrators deny-only, privileges removed, medium integrity), restarts it
with backoff if it dies, and stops it gracefully (stopctl) on SvcStop. Session 0 has no desktop: sign in to Claude Code before
installing; the service cannot open a browser.

pywin32 is imported only here and only when present, so this module imports (and its logic is tested) on Linux. The thin
`CrewbossRunnerService` class exists only where pywin32 does."""
from __future__ import annotations

import logging
import ntpath
import os
import subprocess
import threading
import time

try:   # Windows only (pyproject: pywin32; sys_platform == 'win32')
    import servicemanager
    import win32api
    import win32con
    import win32event
    import win32process
    import win32security
    import win32service
    import win32serviceutil
    HAVE_PYWIN32 = True
except ImportError:
    servicemanager = win32api = win32con = win32event = win32process = win32security = win32service = win32serviceutil = None
    HAVE_PYWIN32 = False

from . import runlog, stopctl, store  # noqa: E402

log = logging.getLogger("crewboss-runner")

SERVICE_NAME = "CrewbossRunner"
DISPLAY_NAME = "Crewboss runner"
DESCRIPTION = "Holds Crewboss's one outbound socket from this PC and runs its jobs with Claude Code, as the user it is installed for."
CLASS_STRING = "crewboss_runner.winsvc.CrewbossRunnerService"
LOGON_RIGHT = "SeServiceLogonRight"
RIGHT_HELP = ('Give the account the "Log on as a service" right: run secpol.msc, Local Policies > User Rights Assignment > '
              '"Log on as a service" > Add User or Group, then run this command again.')


def default_account(env=None) -> str:
    """`.\\user` for a local account, `DOMAIN\\user` for a domain one."""
    env = os.environ if env is None else env
    user = env.get("USERNAME") or "user"
    dom, comp = env.get("USERDOMAIN", ""), env.get("COMPUTERNAME", "")
    return f".\\{user}" if not dom or dom.lower() == comp.lower() else f"{dom}\\{user}"


def sc_config_commands() -> list[list[str]]:
    """Delayed automatic start, and restart on failure (three times, a minute apart; the counter resets after a day)."""
    return [["sc.exe", "config", SERVICE_NAME, "start=", "delayed-auto"],
            ["sc.exe", "failure", SERVICE_NAME, "reset=", "86400", "actions=", "restart/60000/restart/60000/restart/60000"]]


ENTRA_HELP = ("Entra ID (AzureAD) accounts cannot be Windows service logon accounts (they have no password the service control manager "
              "can verify). Use the hidden logon task instead: crewboss-runner service install")
LOGON_HELP = {
    1326: "Windows rejected the user name or password.",
    1385: 'the account does not have the "Log on as a service" right.',
    1069: 'the service could not log on: wrong password, or the account lacks the "Log on as a service" right.',
    1332: "Windows could not look up that account name (no mapping between account names and security IDs). "
          "For a Microsoft-account or Entra ID PC use the hidden task instead: crewboss-runner service install",
}


def lookup_name(account: str) -> str:
    """LSA does not accept `.\\user`: use COMPUTERNAME\\user for a local account."""
    if account.startswith(".\\"):
        return f"{os.environ.get('COMPUTERNAME', '')}\\{account[2:]}"
    return account


def split_account(account: str) -> tuple[str, str]:
    """(domain, user) for LogonUser: `.\\u` -> ('.', 'u'), `D\\u` -> ('D', 'u'), a UPN `u@d` -> ('', 'u@d')."""
    if "\\" in account:
        dom, user = account.split("\\", 1)
        return dom, user
    return "", account


def is_entra(env=None) -> bool:
    env = os.environ if env is None else env
    return (env.get("USERDOMAIN", "") or "").upper() == "AZUREAD"


def error_code(exc) -> int | None:
    code = getattr(exc, "winerror", None)
    if code is None and getattr(exc, "args", None) and isinstance(exc.args[0], int):
        code = exc.args[0]
    return code


def explain_error(exc) -> str:
    code = error_code(exc)
    help_ = LOGON_HELP.get(code or 0, "")
    return f"{exc}" + (f" -- {help_}" if help_ else "")


def verify_logon(account: str, password: str) -> tuple[bool, str]:
    """Prove the password and the service-logon right BEFORE anything is installed: LogonUser(LOGON32_LOGON_SERVICE)."""
    if not HAVE_PYWIN32:
        return False, "pywin32 is not installed"
    dom, user = split_account(account)
    try:
        handle = win32security.LogonUser(user, dom, password, win32con.LOGON32_LOGON_SERVICE, win32con.LOGON32_PROVIDER_DEFAULT)
    except Exception as exc:  # noqa: BLE001  (pywintypes.error; its text never contains the password)
        return False, explain_error(exc)
    win32api.CloseHandle(handle)
    return True, ""


def _with_policy(account: str, fn):
    sid, _domain, _type = win32security.LookupAccountName(None, lookup_name(account))
    policy = win32security.LsaOpenPolicy(None, win32security.POLICY_ALL_ACCESS)
    try:
        return fn(policy, sid)
    finally:
        win32security.LsaClose(policy)


def has_logon_right(account: str) -> bool:
    """Does the account already hold Log on as a service? (So a failed install revokes only what IT granted.)"""
    if not HAVE_PYWIN32:
        return True
    try:
        return LOGON_RIGHT in (_with_policy(account, lambda pol, sid: win32security.LsaEnumerateAccountRights(pol, sid)) or ())
    except Exception:  # noqa: BLE001  no rights at all raises "file not found" (2)
        return False


def revoke_logon_right(account: str) -> bool:
    try:
        _with_policy(account, lambda pol, sid: win32security.LsaRemoveAccountRights(pol, sid, False, (LOGON_RIGHT,)))
        return True
    except Exception:  # noqa: BLE001
        return False


def grant_logon_right(account: str) -> tuple[bool, str]:
    """LsaAddAccountRights(SeServiceLogonRight): the SCM grants it through services.msc but not always through the API."""
    if not HAVE_PYWIN32:
        return False, "pywin32 is not installed"
    try:
        sid, _domain, _type = win32security.LookupAccountName(None, lookup_name(account))
        policy = win32security.LsaOpenPolicy(None, win32security.POLICY_ALL_ACCESS)
        try:
            win32security.LsaAddAccountRights(policy, sid, (LOGON_RIGHT,))
        finally:
            win32security.LsaClose(policy)
    except Exception as exc:  # noqa: BLE001  (pywintypes.error)
        return False, explain_error(exc)
    return True, ""


def install(account: str, password: str, exe_name: str | None = None) -> None:
    """exe_name: the ADMIN-ONLY copy's pythonservice.exe (admincopy.service_exe), never one from the user's own venv."""
    win32serviceutil.InstallService(CLASS_STRING, SERVICE_NAME, DISPLAY_NAME, startType=win32service.SERVICE_AUTO_START,
                                    userName=account, password=password, description=DESCRIPTION, exeName=exe_name)


def set_service_environment(child_path: str, child_exe: str = "", system_path: str = "", pythonpath: str = "") -> None:
    """The service's own Environment (HKLM, admin-only): a SYSTEM-folders-only PATH, no user site, a PYTHONPATH inside the admin-only
    copy. The user's PATH (claude, git, adb) is stored separately and given only to the restricted child."""
    import winreg
    with winreg.CreateKey(winreg.HKEY_LOCAL_MACHINE, rf"SYSTEM\CurrentControlSet\Services\{SERVICE_NAME}") as key:
        winreg.SetValueEx(key, "Environment", 0, winreg.REG_MULTI_SZ,
                          [f"PATH={system_path}", "PYTHONNOUSERSITE=1", f"PYTHONPATH={pythonpath}", "PYTHONHOME=", "PYTHONSTARTUP="])
        winreg.SetValueEx(key, CHILD_PATH_VALUE, 0, winreg.REG_SZ, child_path)
        if child_exe:
            winreg.SetValueEx(key, CHILD_VALUE, 0, winreg.REG_SZ, child_exe)


def child_environment(child_path: str, base: dict | None = None) -> dict:
    """The child's environment: the supervisor's, minus anything PYTHON*, with the user's PATH."""
    env = {k: v for k, v in (os.environ if base is None else base).items() if not k.upper().startswith("PYTHON")}
    env["PATH"] = child_path
    return env


# ---- the restricted child ----
DISABLE_MAX_PRIVILEGE = 0x1
LUA_TOKEN = 0x4
SE_GROUP_INTEGRITY = 0x20
MEDIUM_INTEGRITY_SID = "S-1-16-8192"
CREATE_NO_WINDOW = 0x08000000
FATAL_LAUNCH = 3
SWITCH_EXIT = 3   # the CHILD's exit code for `use <profile>` (a switch, not a failure): restart at once and reset the backoff
NO_RESTART = {1: "this PC is not enrolled (crewboss-runner enrol <code>)", 2: "the runner was revoked or its token expired (forget, then enrol again)"}
CHILD_PATH_VALUE = "ChildPath"   # the user's PATH, baked at install, for the child only
CHILD_VALUE = "ChildExe"   # registry value on the service key: the runner exe the supervisor starts (set at install)


class FatalLaunchError(Exception):
    """The child must not be started and retrying cannot help (e.g. its token cannot be made medium integrity)."""


def child_cmdline(exe: str) -> str:
    return f'"{exe}" run'


def child_creation_flags(exe: str) -> int:
    """The GUI-subsystem runner has no console and makes its own hidden one; a console exe gets a windowless one."""
    return 0 if ntpath.basename(exe).lower().startswith("crewboss-runnerw") else CREATE_NO_WINDOW


class RestrictedChild:
    def __init__(self, handle, pid: int):
        self.handle, self.pid = handle, pid

    def poll(self):
        if win32event.WaitForSingleObject(self.handle, 0) == win32event.WAIT_OBJECT_0:
            return win32process.GetExitCodeProcess(self.handle)
        return None

    def kill_tree(self) -> None:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.pid)], capture_output=True)


def launch_restricted(exe: str, child_path: str = "") -> RestrictedChild:
    """Start `exe run` with a RESTRICTED copy of this service's own token: administrators deny-only, all privileges removed except
    SeChangeNotify, medium integrity (UAC-like). A restricted version of the caller's own primary token needs no extra privilege
    for CreateProcessAsUser. Every descendant (claude, git, adb) inherits it."""
    me = win32api.GetCurrentProcess()
    own = win32security.OpenProcessToken(me, win32con.TOKEN_ALL_ACCESS)
    admins = win32security.CreateWellKnownSid(win32security.WinBuiltinAdministratorsSid, None)
    token = win32security.CreateRestrictedToken(own, DISABLE_MAX_PRIVILEGE | LUA_TOKEN, [(admins, 0)], [], [])
    try:   # MANDATORY: the supervisor shares this logon session, and a medium-integrity child cannot write to its high-integrity objects
        level = win32security.ConvertStringSidToSid(MEDIUM_INTEGRITY_SID)
        win32security.SetTokenInformation(token, win32security.TokenIntegrityLevel, (level, SE_GROUP_INTEGRITY))
    except Exception as exc:  # noqa: BLE001
        win32api.CloseHandle(token)
        raise FatalLaunchError(f"could not set medium integrity on the runner's token ({exc}); refusing to start the runner") from exc
    si = win32process.STARTUPINFO()
    hproc, hthread, pid, _tid = win32process.CreateProcessAsUser(token, None, child_cmdline(exe), None, None, False,
                                                                  child_creation_flags(exe), child_environment(child_path) if child_path else None, None, si)
    win32api.CloseHandle(hthread)
    return RestrictedChild(hproc, pid)


def read_child_settings() -> tuple[str, str]:
    """(child exe, child PATH) from the service's HKLM key: admin-only, so the supervisor takes nothing from user-writable places."""
    import winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, rf"SYSTEM\CurrentControlSet\Services\{SERVICE_NAME}") as key:
        return winreg.QueryValueEx(key, CHILD_VALUE)[0], winreg.QueryValueEx(key, CHILD_PATH_VALUE)[0]


class Supervisor:
    """Start the child, keep it running, stop it gracefully. launch() returns a child with .poll() (None = running, else the exit
    code), .pid and .kill_tree(). Exit 1 (not enrolled) or 2 (revoked) is final: restarting cannot help, so supervision ends."""

    def __init__(self, launch, request_child_stop=stopctl.request_stop_root, stop_wait: float = stopctl.STOP_WAIT, poll: float = 0.5,
                 restart_min: float = 5.0, restart_max: float = 300.0, stable: float = 60.0, mono=time.monotonic):
        self.launch, self.request_child_stop = launch, request_child_stop
        self.stop_wait, self.poll, self.restart_min, self.restart_max, self.stable, self.mono = stop_wait, poll, restart_min, restart_max, stable, mono
        self._stop = threading.Event()
        self.final: str = ""

    def request_stop(self) -> None:
        self._stop.set()

    def _stop_child(self, child) -> None:
        self.request_child_stop(stopctl.read_run_pid())   # the runner watches for it: kills its agents, releases the job, exits
        end = self.mono() + self.stop_wait
        while child.poll() is None and self.mono() < end:
            time.sleep(min(self.poll, 0.2))
        if child.poll() is None:
            log.warning("the runner did not stop in %.0fs: ending its process tree", self.stop_wait)
            child.kill_tree()

    def run(self) -> int:
        """0 = stopped on request; otherwise the exit code that ended supervision (self.final says why)."""
        delay = self.restart_min
        while not self._stop.is_set():
            try:
                child = self.launch()
            except FatalLaunchError as exc:
                self.final = str(exc)
                log.error(self.final)
                return FATAL_LAUNCH
            except Exception as exc:  # noqa: BLE001
                log.exception("could not start the runner")
                child, rc = None, -1
                log.warning("will try again in %.0fs (%s)", delay, exc)
            started = self.mono()
            while child is not None and child.poll() is None and not self._stop.is_set():
                self._stop.wait(self.poll)
            if child is not None and child.poll() is None:   # a stop was asked for
                self._stop_child(child)
                return 0
            if child is not None:
                rc = child.poll()
                if rc == SWITCH_EXIT:   # `use` asked the runner to stop so it comes back on another profile: not a failure
                    log.info("the runner stopped to switch profile; starting it again")
                    delay = self.restart_min
                    if self._stop.wait(1.0):
                        return 0
                    continue
                if rc in NO_RESTART:
                    self.final = f"the runner exited with {rc}: {NO_RESTART[rc]}. Not restarting."
                    log.error(self.final)
                    return rc
                log.warning("the runner exited (code %s); restarting in %.0fs", rc, delay)
                if self.mono() - started >= self.stable:
                    delay = self.restart_min
            if self._stop.wait(delay):
                return 0
            delay = min(delay * 2, self.restart_max)
        return 0


class ServiceCore:
    """What the service does, with no pywin32 in it (the launch function is injected)."""

    def __init__(self, launch=None, **kw):
        self._launch, self.kw = launch, kw
        self.supervisor: Supervisor | None = None
        self._want_stop = False
        self._lock = threading.Lock()

    def request_stop(self) -> None:
        with self._lock:
            self._want_stop = True
            if self.supervisor:
                self.supervisor.request_stop()

    def run(self) -> int:
        from . import admincopy
        store.forbid_migration()   # this admin process never migrates a user's layout nor reads their `active` file
        try:
            return self._run_supervised(admincopy)
        finally:
            store._no_migrate = False

    def _run_supervised(self, admincopy) -> int:
        # the supervisor logs INSIDE the admin-only folder (an admin process never writes into a folder the user controls, which a junction
        # could redirect); the child writes runner.log in the user's own folder
        runlog.setup(force=True, name="service.log", console=False, directory=admincopy.program_dir() if os.name == "nt" else None)
        launch = self._launch or (lambda: launch_restricted(*read_child_settings()))
        with self._lock:
            self.supervisor = Supervisor(launch, **self.kw)
            if self._want_stop:   # SvcStop arrived before we were up
                self.supervisor.request_stop()
        return self.supervisor.run()


if HAVE_PYWIN32:
    class CrewbossRunnerService(win32serviceutil.ServiceFramework):
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = DISPLAY_NAME
        _svc_description_ = DESCRIPTION

        def __init__(self, args):
            super().__init__(args)
            self.core = ServiceCore()

        def SvcDoRun(self):
            servicemanager.LogMsg(servicemanager.EVENTLOG_INFORMATION_TYPE, servicemanager.PYS_SERVICE_STARTED, (self._svc_name_, ""))
            try:
                rc = self.core.run()
            except Exception:  # noqa: BLE001
                log.exception("the service's supervisor crashed")
                raise   # a failed service is restarted by the recovery actions
            if rc:   # a final exit (not enrolled / revoked): say so in the Event Log and stop; recovery does not restart a clean stop
                final = self.core.supervisor.final if self.core.supervisor else f"exit {rc}"
                servicemanager.LogErrorMsg(f"{DISPLAY_NAME}: {final}")

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING, waitHint=90000)
            self.core.request_stop()
