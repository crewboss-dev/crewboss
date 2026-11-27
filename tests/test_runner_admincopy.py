"""Task #972 second review, item 1: the supervisor runs from an ADMIN-ONLY copy (Program Files), never from the user's pipx venv."""
import subprocess
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runner"))

from crewboss_runner import admincopy, doctor, runlog, service, stopctl, winsvc  # noqa: E402

ADMIN, SYSTEM, USERS, ME = "S-1-5-32-544", "S-1-5-18", "S-1-5-32-545", "S-1-5-21-1-2-3-1001"
FULL, RX = 0x1F01FF, 0x1200A9


def test_acl_check_names_untrusted_writers():
    safe = [(SYSTEM, FULL, 0), (ADMIN, FULL, 0), (USERS, RX, 0)]
    assert admincopy.untrusted_writers(safe) == []
    assert admincopy.untrusted_writers(safe + [(ME, FULL, 0)]) == [ME]                       # a per-user python.org install
    assert admincopy.untrusted_writers(safe + [("S-1-5-11", 0x4, 0)]) == ["S-1-5-11"]        # Authenticated Users: create folders
    assert admincopy.untrusted_writers(safe + [(USERS, 0x40000, 0)]) == [USERS]              # WRITE_DAC
    assert admincopy.untrusted_writers(safe + [(ME, FULL, 1)]) == []                         # a DENY ace is not a grant
    assert admincopy.untrusted_writers([("S-1-3-0", 0x10000000, 0)]) == []                   # CREATOR OWNER (inherit-only owner grant)


def test_base_python_refused_when_user_writable_and_instructions_given(monkeypatch):
    monkeypatch.setattr(admincopy, "base_python", lambda: Path("/Users/bob/AppData/Local/Programs/Python/Python312/python.exe"))
    why = admincopy.base_python_problem(lambda p: [(ME, FULL, 0), (ADMIN, FULL, 0)])
    assert "Python312" in why and ME in why and "for all users" in why and "crewboss-runner service install" in why
    assert admincopy.base_python_problem(lambda p: [(ADMIN, FULL, 0), (SYSTEM, FULL, 0), (USERS, RX, 0)]) == ""
    assert "refusing" in admincopy.base_python_problem(lambda p: (_ for _ in ()).throw(OSError("no access")))   # unreadable = refused


def test_commands_for_the_copy(tmp_path):
    root = tmp_path / "Crewboss Runner"
    acl = admincopy.icacls_commands(root)[0]
    assert acl[:3] == ["icacls", str(root), "/inheritance:r"]
    assert "*S-1-5-18:(OI)(CI)F" in acl and "*S-1-5-32-544:(OI)(CI)F" in acl and "*S-1-5-32-545:(OI)(CI)RX" in acl
    cmds = admincopy.build_commands(root, Path("w.whl"))
    assert cmds[0][1:] == ["-m", "venv", "--copies", str(root / "venv")]
    assert cmds[-1][-2:] == ["--no-deps", "--disable-pip-version-check"][-2:] or "w.whl" in cmds[-1]
    assert all(str(root) in " ".join(c) for c in cmds[1:])           # installs go into the admin venv only


def test_build_locks_the_empty_folder_before_anything_is_written(tmp_path, monkeypatch):
    monkeypatch.setattr(admincopy, "build_wheel", lambda dest: dest / "crewboss_runner-0.1.0-py3-none-any.whl")
    monkeypatch.setattr(admincopy, "copy_pywin32_dlls", lambda root=None: 0)
    ran = []
    root = tmp_path / "Crewboss Runner"
    admincopy.build(root, run=lambda c: ran.append(c))
    assert ran[0][0] == "icacls" and ran[1][1:3] == ["-m", "venv"] and any("pywin32" in a for a in ran[2])
    assert (root / "VERSION").read_text().strip() == admincopy.stamp()


def test_service_environment_is_inside_the_copy(tmp_path, monkeypatch):
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    root = admincopy.program_dir()
    assert root == tmp_path / "Crewboss Runner"
    assert str(admincopy.service_exe()).endswith("pythonservice.exe") and str(root) in str(admincopy.service_exe())
    for part in admincopy.service_pythonpath().split(";"):
        assert part.startswith(str(root))                             # nothing user-writable on the supervisor's PYTHONPATH
    assert "AppData" not in admincopy.system_path() and "System32" in admincopy.system_path()


def test_child_gets_the_users_path_but_no_python_vars():
    env = winsvc.child_environment(r"C:\Users\bob\.local\bin;C:\Windows", {"PATH": "x", "PYTHONPATH": "evil", "PYTHONHOME": "h", "APPDATA": "a"})
    assert env == {"APPDATA": "a", "PATH": r"C:\Users\bob\.local\bin;C:\Windows"}


def test_registry_values(monkeypatch):
    written = {}

    class Key:
        def __enter__(self): return self
        def __exit__(self, *a): return False
    fake = types.SimpleNamespace(HKEY_LOCAL_MACHINE=1, REG_MULTI_SZ=7, REG_SZ=1, CreateKey=lambda h, p: Key(),
                                 SetValueEx=lambda k, n, r, t, v: written.update({n: v}))
    monkeypatch.setitem(sys.modules, "winreg", fake)
    winsvc.set_service_environment(r"C:\u\bin", r"C:\u\bin\crewboss-runnerw.exe", r"C:\Windows\System32", r"C:\PF\Crewboss Runner\venv\Lib\site-packages")
    env = written["Environment"]
    assert "PYTHONNOUSERSITE=1" in env and "PATH=C:\\Windows\\System32" in env and "PYTHONHOME=" in env
    assert written["ChildPath"] == r"C:\u\bin" and written["ChildExe"].endswith("crewboss-runnerw.exe")


def test_stale_copy_is_detected(tmp_path, monkeypatch):
    root = tmp_path / "Crewboss Runner"
    root.mkdir()
    assert admincopy.stale_reason(root) == ""                         # no copy: nothing to compare
    (root / "VERSION").write_text(admincopy.stamp() + "\n")
    assert admincopy.stale_reason(root) == ""
    (root / "VERSION").write_text("0.0.9 deadbeef\n")
    assert "re-run `crewboss-runner service install --windows-service` as administrator" in admincopy.stale_reason(root)


def test_doctor_row_states(tmp_path, monkeypatch):
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    root = admincopy.program_dir()
    root.mkdir()
    (root / "VERSION").write_text(admincopy.stamp() + "\n")
    monkeypatch.setattr(admincopy, "read_aces", lambda p: [(ADMIN, FULL, 0), (SYSTEM, FULL, 0), (USERS, RX, 0)])
    assert doctor._admincopy_row()["status"] == "pass"
    (root / "VERSION").write_text("0.0.1 old\n")
    r = doctor._admincopy_row()
    assert r["status"] == "warn" and "service install --windows-service" in r["detail"]
    monkeypatch.setattr(admincopy, "read_aces", lambda p: [(ME, FULL, 0)])
    assert doctor._admincopy_row()["status"] == "fail"


def test_supervisor_log_goes_into_the_given_admin_folder(tmp_path, monkeypatch):
    import logging
    monkeypatch.setenv("CREWBOSS_RUNNER_STATE_DIR", str(tmp_path / "user-state"))
    root = logging.getLogger(); before = list(root.handlers)
    monkeypatch.setattr(sys, "stdout", None); monkeypatch.setattr(sys, "stderr", None)
    try:
        path = runlog.setup(force=True, name="service.log", console=False, directory=tmp_path / "admin")
        assert path == tmp_path / "admin" / "service.log" and not (tmp_path / "user-state").exists()
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h); h.close()


# ---- install / uninstall wiring ----

@pytest.fixture
def svc(monkeypatch, tmp_path):
    ev = []
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "PF"))
    monkeypatch.setattr(service, "is_admin", lambda: True)
    monkeypatch.setattr(service.getpass, "getpass", lambda p: "pw")
    monkeypatch.setattr(winsvc, "HAVE_PYWIN32", True)
    monkeypatch.setattr(winsvc, "is_entra", lambda env=None: False)
    monkeypatch.setattr(winsvc, "default_account", lambda env=None: r".\bob")
    monkeypatch.setattr(service, "enrolment_problem", lambda a: "")
    monkeypatch.setattr(stopctl, "job_running", lambda: None)
    monkeypatch.setattr(winsvc, "has_logon_right", lambda a: True)
    monkeypatch.setattr(winsvc, "grant_logon_right", lambda a: (True, ""))
    monkeypatch.setattr(winsvc, "verify_logon", lambda a, p: (True, ""))
    monkeypatch.setattr(winsvc, "install", lambda a, p, exe=None: ev.append(("install", exe)))
    monkeypatch.setattr(winsvc, "set_service_environment", lambda *a: ev.append(("env", a)))
    monkeypatch.setattr(service, "_wait_service_running", lambda *a, **k: True)
    monkeypatch.setattr(service, "_wait_runner_up", lambda *a, **k: True)
    monkeypatch.setattr(service, "windows_task_installed", lambda: False)
    monkeypatch.setattr(service, "windows_service_installed", lambda: False)
    monkeypatch.setattr(service, "_service_state", lambda: "")
    monkeypatch.setattr(service.store, "run_alive", lambda: False)
    monkeypatch.setattr(service.time, "sleep", lambda s: None)
    monkeypatch.setattr(service, "_run", lambda cmd: subprocess.CompletedProcess(cmd, 0, "", ""))
    monkeypatch.setattr(admincopy, "base_python_problem", lambda aces_of=None: "")
    monkeypatch.setattr(admincopy, "build", lambda root=None, run=None: (ev.append("build"), admincopy.program_dir().mkdir(parents=True, exist_ok=True))[0])
    return ev, tmp_path


def test_install_builds_the_copy_and_installs_from_it(svc):
    ev, tmp = svc
    assert service.install("windows", exe=str(tmp / "crewboss-runner.exe"), windows_service=True) == 0
    kinds = [e if isinstance(e, str) else e[0] for e in ev]
    assert kinds.index("build") < kinds.index("install") < kinds.index("env")
    exe = next(e[1] for e in ev if isinstance(e, tuple) and e[0] == "install")
    assert exe.endswith("pythonservice.exe") and "Crewboss Runner" in exe                       # never the user's venv
    env_args = next(e[1] for e in ev if isinstance(e, tuple) and e[0] == "env")
    assert "Crewboss Runner" in env_args[3] and "System32" in env_args[2]


def test_user_writable_python_is_refused_before_anything_changes(svc, monkeypatch, capsys):
    ev, tmp = svc
    monkeypatch.setattr(admincopy, "base_python_problem", lambda aces_of=None: "C:/x can be modified by Users. Install Python for all users")
    assert service.install("windows", exe=str(tmp / "x.exe"), windows_service=True) == 1
    assert ev == [] and "for all users" in capsys.readouterr().err


def test_failed_install_removes_the_copy_it_made(svc, monkeypatch):
    ev, tmp = svc
    monkeypatch.setattr(service, "_wait_runner_up", lambda *a, **k: False)
    assert service.install("windows", exe=str(tmp / "x.exe"), windows_service=True) == 1
    assert not admincopy.program_dir().exists()


def test_uninstall_removes_the_program_files_copy(svc, monkeypatch, capsys):
    ev, tmp = svc
    admincopy.program_dir().mkdir(parents=True)
    (admincopy.program_dir() / "VERSION").write_text("x")
    monkeypatch.setattr(service, "is_admin", lambda: False)
    assert service.uninstall("windows") == 1 and admincopy.program_dir().exists()            # needs admin: says so, removes nothing
    monkeypatch.setattr(service, "is_admin", lambda: True)
    assert service.uninstall("windows") == 0 and not admincopy.program_dir().exists()
