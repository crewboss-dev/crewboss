"""Task #972: the hidden Windows logon task, the optional Windows service, stop/start per OS, the log-to-file fallback when there is
no console, and the status page link. No real schtasks / sc / pywin32 / Windows: every call is faked, so this runs on Linux."""
import io
import logging
import subprocess
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runner"))

from crewboss_runner import cli, doctor, runlog, service, store, winsvc  # noqa: E402
from crewboss_runner.ui import StatusServer  # noqa: E402

EXE = "/home/u/.local/bin/crewboss-runner"


@pytest.fixture
def made(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1 if cmd[:2] == ["sc.exe", "query"] else 0, "Status: Running\n", "")

    monkeypatch.setattr(service, "_run", fake_run)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("CREWBOSS_RUNNER_STATE_DIR", "")
    return calls


def test_runnerw_sibling_and_resolution(tmp_path, monkeypatch):
    assert service.runnerw_sibling(r"C:\Py\Scripts\crewboss-runner.exe").endswith("crewboss-runnerw.exe")
    assert service.runnerw_sibling("/x/bin/crewboss-runner") == "/x/bin/crewboss-runnerw"
    exe = tmp_path / "crewboss-runner.exe"
    exe.write_text("")
    monkeypatch.setattr(service.shutil, "which", lambda n: None)
    assert service.runnerw_path(str(exe)) is None
    (tmp_path / "crewboss-runnerw.exe").write_text("")
    assert service.runnerw_path(str(exe)) == str(tmp_path / "crewboss-runnerw.exe")


def test_pyproject_declares_gui_script_and_pywin32():
    text = (Path(__file__).resolve().parent.parent / "runner" / "pyproject.toml").read_text()
    assert '[project.gui-scripts]\n' in text.replace("\r", "") and 'crewboss-runnerw = "crewboss_runner.cli:main"' in text
    assert "pywin32>=306; sys_platform == 'win32'" in text


def test_default_windows_install_is_hidden_task(made, tmp_path, monkeypatch):
    exe = tmp_path / "crewboss-runner.exe"
    (tmp_path / "crewboss-runnerw.exe").write_text("")
    exe.write_text("")
    assert service.install("windows", exe=str(exe)) == 0
    create = next(c for c in made if c[:2] == ["schtasks", "/Create"])
    assert create[create.index("/TR") + 1] == f'"{tmp_path / "crewboss-runnerw.exe"}" run'
    assert create[create.index("/SC") + 1] == "ONLOGON" and create[create.index("/RL") + 1] == "LIMITED"
    assert any(c[0] == "powershell" and "ExecutionTimeLimit" in c[-1] for c in made)   # not killed after 72h / on battery
    assert ["schtasks", "/Run", "/TN", "Crewboss runner"] in made


def test_old_install_without_runnerw_falls_back_to_console(made, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(service.shutil, "which", lambda n: None)
    assert service.install("windows", exe=str(tmp_path / "crewboss-runner.exe")) == 0
    assert "console window" in capsys.readouterr().err


def test_task_install_refuses_when_service_exists_and_not_admin(made, monkeypatch, capsys):
    monkeypatch.setattr(service, "_run", lambda cmd: subprocess.CompletedProcess(cmd, 0, "", ""))
    monkeypatch.setattr(service, "is_admin", lambda: False)
    assert service.install("windows", exe="C:/x/crewboss-runner.exe") == 1
    assert "administrator" in capsys.readouterr().err


def test_task_install_removes_existing_service_when_admin(made, monkeypatch, tmp_path):
    seen = []
    sc = {"up": True}
    monkeypatch.setattr(service, "_run", lambda cmd: (seen.append(cmd), sc.update(up=sc["up"] and cmd[:2] != ["sc.exe", "delete"]),
                                                      subprocess.CompletedProcess(cmd, 0, "STATE : 1 STOPPED" if sc["up"] else "", ""))[2])
    monkeypatch.setattr(service.time, "sleep", lambda s: None)
    monkeypatch.setattr(service, "is_admin", lambda: True)
    assert service.install("windows", exe=str(tmp_path / "crewboss-runner.exe")) == 0
    assert ["sc.exe", "delete", "CrewbossRunner"] in seen


def test_windows_service_dry_run(made, capsys):
    assert service.install("windows", dry=True, exe=r"C:\Py\Scripts\crewboss-runner.exe", windows_service=True) == 0
    out = capsys.readouterr().out
    assert "CrewbossRunner" in out and "never stored or logged" in out and "LsaAddAccountRights" in out
    assert "start= delayed-auto" in out and "restart/60000" in out and "$ sc.exe start CrewbossRunner" in out
    assert not any(c[0] == "sc.exe" for c in made)   # dry: nothing ran


def test_windows_service_needs_admin(made, monkeypatch, capsys):
    monkeypatch.setattr(service, "is_admin", lambda: False)
    assert service.install("windows", exe="x", windows_service=True) == 1
    err = capsys.readouterr().err
    assert "administrator" in err and "crewboss-runner service install" in err


def test_windows_service_only_on_windows(capsys):
    assert service.install("linux", dry=True, windows_service=True) == 1


def test_default_account():
    assert winsvc.default_account({"USERNAME": "bob", "USERDOMAIN": "PC1", "COMPUTERNAME": "PC1"}) == ".\\bob"
    assert winsvc.default_account({"USERNAME": "bob", "USERDOMAIN": "CORP", "COMPUTERNAME": "PC1"}) == "CORP\\bob"


def test_uninstall_removes_whichever_kind(made, monkeypatch):
    seen = []
    sc = {"up": True}
    monkeypatch.setattr(service, "_run", lambda cmd: (seen.append(cmd), sc.update(up=sc["up"] and cmd[:2] != ["sc.exe", "delete"]),
                                                      subprocess.CompletedProcess(cmd, 0, "STATE : 1 STOPPED" if sc["up"] else "", ""))[2])
    monkeypatch.setattr(service.time, "sleep", lambda s: None)
    monkeypatch.setattr(service, "is_admin", lambda: True)
    assert service.uninstall("windows") == 0
    assert ["sc.exe", "delete", "CrewbossRunner"] in seen and ["schtasks", "/Delete", "/TN", "Crewboss runner", "/F"] in seen


@pytest.mark.parametrize("plat,action,kind,expect", [
    ("windows", "stop", "task", ["schtasks", "/End", "/TN", "Crewboss runner"]),
    ("windows", "start", "task", ["schtasks", "/Run", "/TN", "Crewboss runner"]),
    ("windows", "stop", "service", ["sc.exe", "stop", "CrewbossRunner"]),
    ("windows", "start", "service", ["sc.exe", "start", "CrewbossRunner"]),
    ("linux", "stop", "task", ["systemctl", "--user", "stop", "crewboss-runner"]),
    ("linux", "start", "task", ["systemctl", "--user", "start", "crewboss-runner"]),
    ("macos", "stop", "task", None),
    ("macos", "start", "task", None),
])
def test_ctl_command(plat, action, kind, expect, monkeypatch):
    monkeypatch.setattr(service, "_uid", lambda: 501)
    cmd = service.ctl_command(plat, action, False, kind)
    if expect:
        assert cmd == expect
    elif action == "stop":
        assert cmd == ["launchctl", "bootout", "gui/501/dev.crewboss.runner"]
    else:
        assert cmd[:3] == ["launchctl", "bootstrap", "gui/501"] and cmd[3].endswith("dev.crewboss.runner.plist")


def test_ctl_wsl_uses_windows_task_unless_systemd(monkeypatch):
    monkeypatch.setattr(service, "detect", lambda: "wsl")
    assert service.ctl_command("wsl", "stop") == ["schtasks.exe", "/End", "/TN", "Crewboss runner"]
    assert service.ctl_command("wsl", "start", systemd=True) == ["systemctl", "--user", "start", "crewboss-runner"]


def test_cli_stop_start(made, monkeypatch):
    monkeypatch.setattr(service, "detect", lambda: "linux")
    assert cli.main(["service", "stop"]) == 0 and cli.main(["service", "start"]) == 0
    assert made == [["systemctl", "--user", "stop", "crewboss-runner"], ["systemctl", "--user", "start", "crewboss-runner"]]
    assert cli.main(["service", "install", "--windows-service", "--dry-run"]) == 1   # not Windows


def test_windows_stop_not_installed(made, monkeypatch, capsys):
    monkeypatch.setattr(service, "_run", lambda cmd: subprocess.CompletedProcess(cmd, 1, "", ""))
    assert service.control("windows", "stop") == 1


def test_status_reports_kind_and_url(made, monkeypatch, capsys):
    out = "Status: Running\nTask To Run: \"C:\\x\\crewboss-runnerw.exe\" run\n"
    monkeypatch.setattr(service, "_run", lambda cmd: subprocess.CompletedProcess(cmd, 1 if cmd[:2] == ["sc.exe", "query"] else 0, out, ""))
    monkeypatch.setattr(runlog, "read_url", lambda: "http://127.0.0.1:5/#tok")
    assert service.status("windows") == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == 'installed: hidden logon task "Crewboss runner", running'
    assert lines[1].endswith("http://127.0.0.1:5/#tok")
    monkeypatch.setattr(service, "_run", lambda cmd: subprocess.CompletedProcess(cmd, 0, "STATE : 4 RUNNING", ""))
    service.status("windows")
    assert 'Windows service "Crewboss runner" (as a user), running' in capsys.readouterr().out
    assert doctor._service_text("windows")[1].startswith('installed: Windows service')


# ---- no console: log to a file ----

def test_headless_setup_logs_prints_to_rotating_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CREWBOSS_RUNNER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        path = runlog.setup()
        assert path == store.state_dir() / "runner.log" and path.parent == tmp_path / "state" / "profiles" / "default"
        print("Secrets are kept in: OS keyring")       # would raise on a real None stdout
        print("boom", file=sys.stderr)
        sys.stdout.flush()
        logging.getLogger("x").warning("hello")
        text = path.read_text()
        assert "Secrets are kept in: OS keyring" in text and "boom" in text and "hello" in text
        assert runlog.headless_started() and runlog.setup() == path            # idempotent: one handler
        assert sum(1 for h in root.handlers if getattr(h, "_crewboss", False)) == 1
        assert any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers)
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h)
                h.close()


def test_setup_is_a_noop_with_a_console(monkeypatch):
    monkeypatch.setattr(sys, "stdout", io.StringIO())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    assert runlog.setup() is None


def test_log_dir_windows_is_localappdata(monkeypatch, tmp_path):
    monkeypatch.setattr(runlog, "os", types.SimpleNamespace(name="nt"))   # not os.name itself: pathlib would break
    monkeypatch.setattr(runlog.store, "state_dir", lambda: tmp_path / "crewboss-runner" / "state")   # what store picks from LOCALAPPDATA
    monkeypatch.setattr(runlog.settings, "get", lambda k, *a: "")
    assert runlog.log_file() == tmp_path / "crewboss-runner" / "runner.log"


def test_hide_child_windows_adds_flag(monkeypatch):
    monkeypatch.setattr(runlog, "os", types.SimpleNamespace(name="nt"))
    seen = []

    class P:
        def __init__(self, *a, **kw):
            seen.append(kw.get("creationflags"))
    monkeypatch.setattr(runlog.subprocess, "Popen", P)
    runlog.hide_child_windows()
    P(["x"])
    P(["x"], creationflags=0x200)
    P(["x"], creationflags=0x10)   # asked for its own console: left alone
    assert seen == [runlog.CREATE_NO_WINDOW, 0x200 | runlog.CREATE_NO_WINDOW, 0x10]


# ---- the status page link ----

def test_status_page_rearms_after_exchange_and_writes_link():
    got = []
    ui = StatusServer(lambda: {}, on_rearm=got.append)
    first = ui.bootstrap
    import json, urllib.request
    def ex(tok):
        req = urllib.request.Request(f"http://127.0.0.1:{ui.port}/api/exchange", data=json.dumps({"token": tok}).encode(),
                                     headers={"Content-Type": "application/json", "Host": f"127.0.0.1:{ui.port}"}, method="POST")
        try:
            return urllib.request.urlopen(req).status
        except urllib.error.HTTPError as e:
            return e.code
    ui.start()
    try:
        assert ex(first) == 200
        assert ex(first) == 403                      # one-use still holds for a given key
        assert got and got[0].endswith("#" + ui.bootstrap) and ui.bootstrap != first
        assert ex(ui.bootstrap) == 200
    finally:
        ui.stop()


def test_read_url_needs_a_live_runner(tmp_path, monkeypatch):
    monkeypatch.setenv("CREWBOSS_RUNNER_STATE_DIR", str(tmp_path))
    runlog.write_url("http://127.0.0.1:9/#t")
    assert (store.state_dir() / "ui-url.txt").read_text().strip() == "http://127.0.0.1:9/#t"
    monkeypatch.setattr(store, "run_alive", lambda: False)
    assert runlog.read_url() == ""
    monkeypatch.setattr(store, "run_alive", lambda: True)
    assert runlog.read_url() == "http://127.0.0.1:9/#t"
    runlog.clear_url()
    assert runlog.read_url() == ""


# ---- the service core (no pywin32) ----

def test_winsvc_imports_without_pywin32():
    assert winsvc.SERVICE_NAME == "CrewbossRunner" and winsvc.CLASS_STRING == "crewboss_runner.winsvc.CrewbossRunnerService"
    ok, why = winsvc.grant_logon_right("x") if not winsvc.HAVE_PYWIN32 else (False, "pywin32 is not installed")
    assert not ok and "pywin32" in why
