"""Task #972 review fixes 2 and 3: the service install order (grant right, verify the password, install, start and wait, THEN remove the
task; roll back on failure and keep the task), account name handling, clear messages, and the enrolment/account check. Faked."""
import subprocess
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runner"))

from crewboss_runner import service, stopctl, store, winsvc  # noqa: E402


@pytest.fixture
def flow(monkeypatch, tmp_path):
    ev = []
    monkeypatch.setattr(service, "is_admin", lambda: True)
    monkeypatch.setattr(service.getpass, "getpass", lambda prompt: "s3cret")
    monkeypatch.setattr(winsvc, "HAVE_PYWIN32", True)
    monkeypatch.setattr(winsvc, "is_entra", lambda env=None: False)
    monkeypatch.setattr(winsvc, "default_account", lambda env=None: r".\bob")
    monkeypatch.setattr(service, "enrolment_problem", lambda acct: "")
    monkeypatch.setattr(stopctl, "job_running", lambda: None)
    monkeypatch.setattr(winsvc, "has_logon_right", lambda a: False)
    monkeypatch.setattr(winsvc, "revoke_logon_right", lambda a: (ev.append("revoke"), True)[1])
    monkeypatch.setattr(winsvc, "grant_logon_right", lambda a: (ev.append("grant"), (True, ""))[1])
    monkeypatch.setattr(winsvc, "verify_logon", lambda a, p: (ev.append("verify"), (True, ""))[1])
    monkeypatch.setattr(winsvc, "set_service_environment", lambda p, c="", sp="", pp="": ev.append(("env", c)))
    monkeypatch.setattr(service.admincopy, "base_python_problem", lambda aces_of=None: "")
    monkeypatch.setattr(service.admincopy, "build", lambda root=None, run=None: ev.append("build"))
    monkeypatch.setattr(service.admincopy, "remove", lambda root=None: True)
    monkeypatch.setattr(service, "_wait_service_running", lambda *a, **k: (ev.append("running"), True)[1])
    monkeypatch.setattr(service, "_wait_runner_up", lambda *a, **k: (ev.append("up"), True)[1])
    monkeypatch.setattr(service, "windows_task_installed", lambda: True)
    monkeypatch.setattr(service, "windows_service_installed", lambda: False)
    monkeypatch.setattr(store, "run_alive", lambda: True)
    monkeypatch.setattr(stopctl, "graceful_stop", lambda *a, **k: (ev.append("stop-task-runner"), True)[1])
    monkeypatch.setattr(service.time, "sleep", lambda s: None)

    sc = {"exists": False}
    monkeypatch.setattr(winsvc, "install", lambda a, p, exe=None: (ev.append("install"), sc.update(exists=True))[1])
    monkeypatch.setattr(service, "_service_state", lambda: "STOPPED" if sc["exists"] else "")

    def fake_run(cmd):
        ev.append(" ".join(cmd[:2]) if cmd[0] in ("sc.exe", "schtasks") else cmd[0])
        if cmd[:2] == ["sc.exe", "delete"]:
            sc["exists"] = False
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(service, "_run", fake_run)
    exe = tmp_path / "crewboss-runner.exe"
    (tmp_path / "crewboss-runnerw.exe").write_text("")
    exe.write_text("")
    return ev, str(exe), tmp_path


def test_order_task_removed_only_after_the_service_runs(flow):
    ev, exe, tmp = flow
    assert service.install("windows", exe=exe, windows_service=True) == 0
    order = [e for e in ev if isinstance(e, str)]
    assert order.index("grant") < order.index("verify") < order.index("install")
    assert order.index("install") < order.index("sc.exe start") < order.index("running") < order.index("up")
    assert order.index("up") < order.index("schtasks /Delete")
    assert order.index("stop-task-runner") < order.index("install")           # no two runners on one device key
    assert ("env", str(tmp / "crewboss-runnerw.exe")) in ev                       # the supervisor's child exe is recorded
    assert "schtasks /Run" not in order


@pytest.mark.parametrize("fail", ["install", "start", "up"])
def test_failure_rolls_back_the_service_and_keeps_the_task(flow, monkeypatch, capsys, fail):
    ev, exe, _ = flow
    if fail == "install":
        monkeypatch.setattr(winsvc, "install", lambda a, p, exe=None: (_ for _ in ()).throw(OSError(1069, "logon failure")))
    elif fail == "start":
        monkeypatch.setattr(service, "_wait_service_running", lambda *a, **k: False)
    else:
        monkeypatch.setattr(service, "_wait_runner_up", lambda *a, **k: False)
    assert service.install("windows", exe=exe, windows_service=True) == 1
    assert ("sc.exe delete" in ev or fail == "install") and "schtasks /Delete" not in ev   # half-made service gone (if it got made), task kept
    assert "schtasks /Run" in ev                                            # and its runner is running again
    assert "revoke" in ev                                                   # the right this run granted is taken back
    assert "Rolled back" in capsys.readouterr().err


def test_wrong_password_changes_nothing(flow, monkeypatch, capsys):
    ev, exe, _ = flow
    monkeypatch.setattr(winsvc, "verify_logon", lambda a, p: (False, "bad -- Windows rejected the user name or password."))
    assert service.install("windows", exe=exe, windows_service=True) == 1
    assert "install" not in ev and "sc.exe delete" not in ev and "schtasks /Delete" not in ev and "stop-task-runner" not in ev
    assert "revoke" in ev                       # we granted the right in this run, so it is removed again
    err = capsys.readouterr().err
    assert "Entra ID" in err and "untouched" in err


def test_existing_logon_right_is_never_revoked(flow, monkeypatch):
    ev, exe, _ = flow
    monkeypatch.setattr(winsvc, "has_logon_right", lambda a: True)
    monkeypatch.setattr(service, "_wait_runner_up", lambda *a, **k: False)
    assert service.install("windows", exe=exe, windows_service=True) == 1
    assert "revoke" not in ev


def test_ctrl_c_during_install_rolls_back(flow, monkeypatch, capsys):
    ev, exe, _ = flow
    monkeypatch.setattr(service, "_wait_service_running", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        service.install("windows", exe=exe, windows_service=True)
    assert "sc.exe delete" in ev and "revoke" in ev and "schtasks /Delete" not in ev and "schtasks /Run" in ev


def test_a_hand_started_runner_is_stopped_before_install_even_without_a_task(flow, monkeypatch):
    ev, exe, _ = flow
    monkeypatch.setattr(service, "windows_task_installed", lambda: False)
    assert service.install("windows", exe=exe, windows_service=True) == 0
    assert ev.index("stop-task-runner") < ev.index("install")


def test_password_never_reaches_a_command_line_or_any_output(flow, monkeypatch, capsys, caplog):
    ev, exe, _ = flow
    import logging
    caplog.set_level(logging.DEBUG)
    cmds = []
    inner = service._run
    monkeypatch.setattr(service, "_run", lambda cmd: (cmds.append(list(cmd)), inner(cmd))[1])
    monkeypatch.setattr(winsvc, "install", lambda a, p, exe=None: (ev.append(("pw-to-scm", p)), None)[1])
    assert service.install("windows", exe=exe, windows_service=True) == 0
    assert ("pw-to-scm", "s3cret") in ev                       # it goes to the SCM call only
    out = capsys.readouterr()
    assert not any("s3cret" in part for c in cmds for part in c)
    assert "s3cret" not in out.out and "s3cret" not in out.err and "s3cret" not in caplog.text
    assert not any(isinstance(e, str) and "s3cret" in e for e in ev)


def test_waits_for_stopped_then_gone_before_create(monkeypatch, capsys):
    states = iter(["RUNNING", "STOP_PENDING", "STOP_PENDING", "STOPPED", "STOPPED", "STOPPED", "", ""])
    cmds = []
    monkeypatch.setattr(service, "_service_state", lambda: next(states))
    monkeypatch.setattr(service, "_run", lambda cmd: (cmds.append(cmd[:2]), subprocess.CompletedProcess(cmd, 0, "", ""))[1])
    monkeypatch.setattr(service.time, "sleep", lambda s: None)
    assert service.stop_and_delete_service() is True
    assert cmds == [["sc.exe", "stop"], ["sc.exe", "delete"]]
    stuck = iter(["RUNNING"] + ["STOP_PENDING"] * 100)
    monkeypatch.setattr(service, "_service_state", lambda: next(stuck))
    t = iter(range(0, 100000, 10))
    monkeypatch.setattr(service.time, "monotonic", lambda: next(t))
    assert service.stop_and_delete_service(timeout=30) is False       # never deletes a service that has not stopped
    assert "did not stop" in capsys.readouterr().err


def test_marked_for_deletion_is_not_gone(monkeypatch, capsys):
    monkeypatch.setattr(service, "_service_state", lambda: "STOPPED")   # delete issued but still listed (1072)
    monkeypatch.setattr(service, "_run", lambda cmd: subprocess.CompletedProcess(cmd, 0, "", ""))
    monkeypatch.setattr(service.time, "sleep", lambda s: None)
    t = iter(range(0, 100000, 10))
    monkeypatch.setattr(service.time, "monotonic", lambda: next(t))
    assert service.stop_and_delete_service(timeout=30) is False
    assert "marked for deletion" in capsys.readouterr().err


def test_no_logon_right_prints_instructions(flow, monkeypatch, capsys):
    ev, exe, _ = flow
    monkeypatch.setattr(winsvc, "grant_logon_right", lambda a: (False, "denied"))
    assert service.install("windows", exe=exe, windows_service=True) == 1
    assert "secpol.msc" in capsys.readouterr().err and "install" not in ev


def test_entra_account_is_refused_before_anything(flow, monkeypatch, capsys):
    ev, exe, _ = flow
    monkeypatch.setattr(winsvc, "is_entra", lambda env=None: True)
    assert service.install("windows", exe=exe, windows_service=True) == 1
    assert "Entra ID" in capsys.readouterr().err and ev == []


def test_account_without_enrolment_is_refused_before_anything(flow, monkeypatch, capsys):
    ev, exe, _ = flow
    monkeypatch.setattr(service, "enrolment_problem", lambda acct: f"There is no enrolment for {acct} on this PC")
    assert service.install("windows", exe=exe, windows_service=True) == 1
    assert "no enrolment" in capsys.readouterr().err and ev == []


def test_enrolment_problem_reads_this_accounts_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("CREWBOSS_RUNNER_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("CREWBOSS_RUNNER_NO_KEYRING", "1")
    assert "no enrolment for .\\bob" in service.enrolment_problem(".\\bob")
    store.mark_dir(store.config_dir())
    store.Config().save({"server": "x", "device_id": 1})
    store.Secrets().set("device_key", "k")
    assert "no enrolment" in service.enrolment_problem(".\\bob")   # the token is missing too
    store.Secrets().set("device_token", "t")
    assert service.enrolment_problem(".\\bob") == ""


def test_job_running_blocks_install_unless_forced(flow, monkeypatch, capsys):
    ev, exe, _ = flow
    monkeypatch.setattr(stopctl, "job_running", lambda: {"id": 4, "branch": "b"})
    assert service.install("windows", exe=exe, windows_service=True) == 1 and "--force" in capsys.readouterr().err
    assert service.install("windows", exe=exe, windows_service=True, force=True) == 0


def test_account_names(monkeypatch):
    monkeypatch.setenv("COMPUTERNAME", "PC1")
    assert winsvc.lookup_name(".\\bob") == "PC1\\bob" and winsvc.lookup_name("CORP\\bob") == "CORP\\bob"
    assert winsvc.split_account(".\\bob") == (".", "bob") and winsvc.split_account("CORP\\bob") == ("CORP", "bob")
    assert winsvc.split_account("bob@corp.example") == ("", "bob@corp.example")
    assert winsvc.is_entra({"USERDOMAIN": "AzureAD"}) and not winsvc.is_entra({"USERDOMAIN": "PC1"})


def test_lsa_lookup_uses_computername_form_and_verify_uses_service_logon(monkeypatch):
    monkeypatch.setenv("COMPUTERNAME", "PC1")
    seen = {}
    sec = types.SimpleNamespace(
        POLICY_ALL_ACCESS=1, LookupAccountName=lambda sysn, n: seen.update(name=n) or ("sid", "d", 1),
        LsaOpenPolicy=lambda a, b: "pol", LsaAddAccountRights=lambda p, sid, rights: seen.update(rights=rights), LsaClose=lambda p: None,
        LogonUser=lambda u, d, p, t, prov: seen.update(logon=(u, d, t, prov)) or "h")
    monkeypatch.setattr(winsvc, "HAVE_PYWIN32", True)
    monkeypatch.setattr(winsvc, "win32security", sec)
    monkeypatch.setattr(winsvc, "win32con", types.SimpleNamespace(LOGON32_LOGON_SERVICE=5, LOGON32_PROVIDER_DEFAULT=0))
    monkeypatch.setattr(winsvc, "win32api", types.SimpleNamespace(CloseHandle=lambda h: None))
    assert winsvc.grant_logon_right(".\\bob") == (True, "") and seen["name"] == "PC1\\bob" and seen["rights"] == ("SeServiceLogonRight",)
    assert winsvc.verify_logon(".\\bob", "pw") == (True, "") and seen["logon"] == ("bob", ".", 5, 0)


def test_error_messages():
    class E(Exception):
        winerror = 1332
    assert "no mapping" in winsvc.explain_error(E("x")).lower() or "could not look up" in winsvc.explain_error(E("x")).lower()
    assert "Entra" in winsvc.explain_error(E("x"))
    class W(Exception):
        winerror = 1069
    assert "Log on as a service" in winsvc.explain_error(W("x")) and "password" in winsvc.explain_error(W("x"))
