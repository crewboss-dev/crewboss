"""An ADMIN-ONLY copy of the runner for the Windows service's supervisor (task #972, second review).

The service process holds the account's full administrator token. If it imported crewboss_runner, pythonservice.exe or pywin32 from the
user's own (user-writable) pipx venv, anything running as that user (a restricted child, an agent) could edit that code and get admin
at the next boot. So `service install --windows-service` (elevated) builds %ProgramFiles%\\Crewboss Runner\\: a venv made with
`--copies` from a base Python that is itself admin-only (the ACL is checked, not guessed from the path), the same runner version
installed into it from a wheel, and an ACL of Administrators + SYSTEM full control, Users read/execute, inheritance off. The service
runs pythonservice.exe from there with PYTHONNOUSERSITE=1 and a PYTHONPATH made only of that folder; its own PATH is system folders
only; its log is in that folder. The supervisor reads nothing user-writable except stop.request, run.pid and status.json, parsed as plain
numbers/JSON. The CHILD (the user's own runner exe) is unchanged: it runs with the restricted token, so its code being user-writable
is fine.

REMAINING RISK, stated plainly: the supervisor shares the user's logon session with the user's processes. The child is medium integrity
(mandatory: no launch if that fails), which blocks writes to high-integrity objects, but a same-session attack surface (per-session
DosDevices, window messages, a junction under the state folder where the supervisor writes stop.request) is reduced, not removed.
This is hardening, not a boundary. The hidden logon task has no admin process at all."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__

DIRNAME = "Crewboss Runner"
VERSION_FILE = "VERSION"
TRUSTED_SIDS = {"S-1-5-18", "S-1-5-32-544", "S-1-3-0", "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"}   # SYSTEM, Administrators, CREATOR OWNER, TrustedInstaller
# FILE_WRITE_DATA, FILE_APPEND_DATA, FILE_WRITE_EA, FILE_DELETE_CHILD, FILE_WRITE_ATTRIBUTES, DELETE, WRITE_DAC, WRITE_OWNER, GENERIC_WRITE, GENERIC_ALL
WRITE_MASK = 0x2 | 0x4 | 0x10 | 0x40 | 0x100 | 0x10000 | 0x40000 | 0x80000 | 0x40000000 | 0x10000000
ICACLS_SIDS = {"system": "*S-1-5-18", "admins": "*S-1-5-32-544", "users": "*S-1-5-32-545"}

INSTRUCTIONS = ("The service's own copy must live in an admin-only folder, built from a Python that ordinary users cannot modify. Install "
                'Python "for all users" into Program Files (python.org installer: "Install for all users" under Customize installation), '
                "then run this again with that Python's pipx/pip; or use the default, which needs no admin and no copy: "
                "crewboss-runner service install")


def program_dir() -> Path:
    return Path(os.environ.get("ProgramFiles") or r"C:\Program Files") / DIRNAME


def base_python() -> Path:
    """The interpreter the runner's venv was made from (not the venv's own python)."""
    return Path(getattr(sys, "_base_executable", "") or sys.executable)


def venv_python(root: Path | None = None) -> Path:
    return (root or program_dir()) / "venv" / "Scripts" / "python.exe"


def site_packages(root: Path | None = None) -> Path:
    return (root or program_dir()) / "venv" / "Lib" / "site-packages"


def service_exe(root: Path | None = None) -> Path:
    return site_packages(root) / "win32" / "pythonservice.exe"


def service_pythonpath(root: Path | None = None) -> str:
    """pythonservice.exe does not read pyvenv.cfg, so the venv's folders are named explicitly (.pth files are not processed from
    PYTHONPATH). Every entry is inside the admin-only folder."""
    sp = site_packages(root)
    return ";".join(str(p) for p in (sp, sp / "win32", sp / "win32" / "lib", sp / "pythonwin", sp / "pywin32_system32"))


def system_path() -> str:
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    return ";".join((rf"{root}\System32", root, rf"{root}\System32\Wbem"))


# ---- is a folder writable by anyone but admins? ----

def read_aces(path: str):
    """[(sid_string, access_mask, ace_type)] of a path's DACL (Windows, pywin32)."""
    import win32security
    sd = win32security.GetNamedSecurityInfo(path, win32security.SE_FILE_OBJECT, win32security.DACL_SECURITY_INFORMATION)
    dacl = sd.GetSecurityDescriptorDacl()
    out = []
    for i in range(dacl.GetAceCount() if dacl else 0):
        (ace_type, _flags), mask, sid = dacl.GetAce(i)
        out.append((win32security.ConvertSidToStringSid(sid), mask, ace_type))
    return out


def untrusted_writers(aces) -> list[str]:
    """The SIDs that an ALLOW ace lets write, delete or change permissions, other than SYSTEM / Administrators / TrustedInstaller."""
    return sorted({sid for sid, mask, ace_type in aces if ace_type == 0 and mask & WRITE_MASK and sid not in TRUSTED_SIDS})


def writable_by_non_admins(path: Path, aces_of=None) -> list[str]:
    return untrusted_writers((aces_of or read_aces)(str(path)))


def base_python_problem(aces_of=None) -> str:
    """'' if the base interpreter's folders are admin-only, else the refusal text (names the folder and who can write)."""
    base = base_python()
    for p in (base.parent, base.parent / "Lib"):
        try:
            who = writable_by_non_admins(p, aces_of)
        except Exception as exc:  # noqa: BLE001
            return f"Could not read the permissions of {p} ({exc}); refusing to build the service from it. " + INSTRUCTIONS
        if who:
            return f"{p} can be modified by {', '.join(who)} (a per-user or Store Python). " + INSTRUCTIONS
    return ""


# ---- fingerprint: has the user's runner changed since the copy was made? ----

def fingerprint(pkg_dir: Path | None = None) -> str:
    pkg = pkg_dir or Path(__file__).resolve().parent
    h = hashlib.sha256()
    for f in sorted(pkg.glob("*.py")):
        h.update(f.name.encode() + b"\0" + f.read_bytes())
    return h.hexdigest()[:16]


def stamp() -> str:
    return f"{__version__} {fingerprint()}"


def installed_stamp(root: Path | None = None) -> str:
    try:
        return ((root or program_dir()) / VERSION_FILE).read_text().strip()
    except OSError:
        return ""


def stale_reason(root: Path | None = None) -> str:
    """'' if the copy exists and matches the user's runner, else what to do."""
    got = installed_stamp(root)
    if not got:
        return ""
    if got != stamp():
        return f"the service's copy is {got.split()[0]} but this runner is {__version__} (or its code changed): re-run `crewboss-runner service install --windows-service` as administrator to update the service"
    return ""


# ---- build / remove ----

def icacls_commands(root: Path) -> list[list[str]]:
    return [["icacls", str(root), "/inheritance:r", "/grant:r", f"{ICACLS_SIDS['system']}:(OI)(CI)F", f"{ICACLS_SIDS['admins']}:(OI)(CI)F",
             f"{ICACLS_SIDS['users']}:(OI)(CI)RX"]]


def build_commands(root: Path, wheel: Path | None = None) -> list[list[str]]:
    """The command lines (after the ACL is set on the empty folder): venv with --copies from the base Python, then the wheel, pywin32."""
    vp = str(venv_python(root))
    cmds = [[str(base_python()), "-m", "venv", "--copies", str(root / "venv")],
            [vp, "-m", "pip", "install", "--no-input", "--disable-pip-version-check", "pywin32>=306"]]
    if wheel is not None:
        cmds.append([vp, "-m", "pip", "install", "--no-input", "--no-deps", "--disable-pip-version-check", str(wheel)])
    return cmds


def build_wheel(dest: Path) -> Path:
    """A wheel of the INSTALLED runner (same version, same files): from the source tree if this is a source install, else rebuilt from
    the installed distribution's file list."""
    pkg = Path(__file__).resolve().parent
    src = pkg.parent
    if (src / "pyproject.toml").exists():   # a source / editable install
        subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-input", "-w", str(dest), str(src)], check=True, capture_output=True)
        return next(dest.glob("crewboss_runner-*.whl"))
    import zipfile
    from importlib import metadata
    dist = metadata.distribution("crewboss-runner")
    tag = "py3-none-any"
    for line in (dist.read_text("WHEEL") or "").splitlines():
        if line.startswith("Tag:"):
            tag = line.split(":", 1)[1].strip()
    out = dest / f"crewboss_runner-{dist.version}-{tag}.whl"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in dist.files or []:
            rel = str(f)
            if ".." in Path(rel).parts or "__pycache__" in rel or rel.endswith(".pyc"):
                continue   # console-script exes live outside site-packages: pip makes its own
            z.write(Path(dist.locate_file(f)), rel.replace("\\", "/"))
    return out


def copy_pywin32_dlls(root: Path | None = None) -> int:
    """pythonservice.exe looks next to itself first: put pywintypes/pythoncom DLLs there, so no PATH entry is ever needed."""
    sp = site_packages(root)
    n = 0
    for dll in (sp / "pywin32_system32").glob("*.dll"):
        shutil.copy2(dll, sp / "win32" / dll.name)
        n += 1
    return n


def _run(cmd: list[str]) -> None:
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:3])} failed: {(res.stderr or res.stdout).strip()[-400:]}")


def build(root: Path | None = None, run=_run) -> Path:
    """Create the admin-only copy. The folder is locked down while still empty, so nothing is ever writable by users."""
    root = root or program_dir()
    root.mkdir(parents=True, exist_ok=True)
    for c in icacls_commands(root):
        run(c)
    import tempfile
    with tempfile.TemporaryDirectory(dir=root) as tmp:   # inside the locked folder, not %TEMP%
        wheel = build_wheel(Path(tmp))
        for c in build_commands(root, wheel):
            run(c)
    copy_pywin32_dlls(root)
    (root / VERSION_FILE).write_text(stamp() + "\n")
    return root


def remove(root: Path | None = None) -> bool:
    root = root or program_dir()
    if not root.exists():
        return True
    shutil.rmtree(root, ignore_errors=True)
    return not root.exists()
