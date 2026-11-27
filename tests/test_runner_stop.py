"""Task #972 review fixes: graceful stop (no hard /End first), a job-running guard, admin hints for the service, doctor uninstall not
deleting secrets when the autostart could not be removed, and the runner's own hidden console."""
import asyncio
import json
import subprocess
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runner"))

from crewboss_runner import doctor, runlog, service, stopctl, store  # noqa: E402


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("CREWBOSS_RUNNER_STATE_DIR", str(tmp_path / "state"))
    (tmp_path / "state").mkdir()
    store.state_dir().mkdir(parents=True)
    return store.state_dir()   # the active profile's folder (state/profiles/default)


@pytest.fixture
def calls(monkeypatch):
    made = []
    monkeypatch.setattr(service, "_run", lambda cmd: (made.append(cmd), subprocess.CompletedProcess(cmd, 0, "", ""))[1])
    return made


def test_stop_request_file_and_watch(state):
    stopctl.request_stop()
    assert (state / "stop.request").exists()
    hit = []

    async def go():
        await asyncio.wait_for(stopctl.watch(lambda: hit.append(1), interval=0.01), 2)
    asyncio.run(go())
    assert hit == [1] and not (state / "stop.request").exists()   # consumed: a stale file can't stop the next run


def test_graceful_stop_waits_then_reports(state, monkeypatch):
    alive = iter([True, True, False])
    assert stopctl.graceful_stop(alive=lambda: next(alive), sleep=lambda s: None) is True
    assert (state / "stop.request").exists()
    state_alive = lambda: True  # noqa: E731
    t = iter(range(0, 1000, 10))
    assert stopctl.graceful_stop(timeout=30, alive=state_alive, sleep=lambda s: None, mono=lambda: next(t)) is False
    assert not (state / "stop.request").exists()           # cleared so it cannot hit a later start
    assert stopctl.graceful_stop(alive=lambda: False) is True   # not running: nothing to do


def test_job_running_needs_a_live_runner(state, monkeypatch):
    (state / "status.json").write_text(json.dumps({"job": {"id": 7, "branch": "dash/j7-e1"}}))
    monkeypatch.setattr(store, "run_alive", lambda: True)
    assert stopctl.job_running()["id"] == 7
    monkeypatch.setattr(store, "run_alive", lambda: False)
    assert stopctl.job_running() is None


def test_task_stop_is_graceful_and_only_then_hard(state, calls, monkeypatch):
    monkeypatch.setattr(service, "windows_kind", lambda: "task")
    monkeypatch.setattr(stopctl, "graceful_stop", lambda: True)
    assert service.control("windows", "stop") == 0
    assert calls == []                                      # no /End at all
    monkeypatch.setattr(stopctl, "graceful_stop", lambda: False)
    assert service.control("windows", "stop") == 0
    assert ["schtasks", "/End", "/TN", "Crewboss runner"] in calls   # the fallback


def test_stop_refuses_while_a_job_runs_unless_forced(state, calls, monkeypatch, capsys):
    monkeypatch.setattr(service, "windows_kind", lambda: "task")
    monkeypatch.setattr(stopctl, "job_running", lambda: {"id": 3, "branch": "b"})
    monkeypatch.setattr(stopctl, "graceful_stop", lambda: True)
    assert service.control("windows", "stop") == 1 and "--force" in capsys.readouterr().err
    assert service.control("windows", "stop", force=True) == 0


def test_service_control_needs_admin(state, calls, monkeypatch, capsys):
    monkeypatch.setattr(service, "windows_kind", lambda: "service")
    monkeypatch.setattr(service, "is_admin", lambda: False)
    for action in ("stop", "start"):
        assert service.control("windows", action) == 1
        assert "administrator" in capsys.readouterr().err
    assert calls == []
    monkeypatch.setattr(service, "is_admin", lambda: True)
    assert service.control("windows", "start") == 0 and ["sc.exe", "start", "CrewbossRunner"] in calls


def test_uninstall_asks_the_runner_to_stop_before_deleting_the_task(state, calls, monkeypatch):
    order = []
    monkeypatch.setattr(stopctl, "graceful_stop", lambda: order.append("graceful") or True)
    monkeypatch.setattr(service, "_run", lambda cmd: (order.append(cmd[:2]), subprocess.CompletedProcess(cmd, 1 if cmd[0] == "sc.exe" else 0, "", ""))[1])
    assert service.uninstall("windows") == 0
    assert order.index("graceful") < order.index(["schtasks", "/End"])


def test_doctor_uninstall_keeps_secrets_when_autostart_removal_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CREWBOSS_RUNNER_CONFIG_DIR", str(tmp_path / "crewboss-runner"))
    monkeypatch.setenv("CREWBOSS_RUNNER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CREWBOSS_RUNNER_NO_KEYRING", "1")
    cfg = store.config_dir()
    store.mark_dir(cfg)
    store.Secrets().set("device_key", "k")
    store.Config().save({"server": "x"})
    monkeypatch.setattr(service, "uninstall", lambda plat: 1)
    monkeypatch.setattr(doctor, "_active_job", lambda: False)
    assert doctor.cmd_uninstall(types.SimpleNamespace(yes=True)) == 1
    assert store.Secrets().get("device_key") == "k" and store.Config().load() and cfg.exists()
    assert "nothing else was removed" in capsys.readouterr().err


def test_own_console_off_windows_is_false_and_setup_falls_back(monkeypatch, tmp_path):
    assert runlog.own_console() is False
    hidden = []
    monkeypatch.setattr(runlog, "hide_child_windows", lambda: hidden.append(1))
    monkeypatch.setattr(runlog, "own_console", lambda: False)
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    import logging
    root = logging.getLogger(); before = list(root.handlers)
    monkeypatch.setenv("CREWBOSS_RUNNER_STATE_DIR", str(tmp_path / "state"))
    try:
        runlog.setup()
        assert hidden == [1]
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h); h.close()
    hidden.clear()
    monkeypatch.setattr(runlog, "own_console", lambda: True)   # a console of our own: children share it, no CREATE_NO_WINDOW patch
    try:
        runlog.setup()
        assert hidden == []
    finally:
        for h in list(root.handlers):
            if h not in before:
                root.removeHandler(h); h.close()


def test_stop_request_is_scoped_to_a_pid(state, monkeypatch):
    import os
    hit = []
    stopctl.request_stop(os.getpid() + 1)                 # meant for an OLDER process still shutting down
    async def go(n):
        try:
            await asyncio.wait_for(stopctl.watch(lambda: hit.append(1), interval=0.01), n)
        except asyncio.TimeoutError:
            pass
    asyncio.run(go(0.15))
    assert hit == [] and (state / "stop.request").exists()   # the new runner neither stops nor clears it
    stopctl.request_stop(os.getpid())
    asyncio.run(go(2))
    assert hit == [1] and not (state / "stop.request").exists()


def test_graceful_stop_names_the_pid_in_run_pid(state, monkeypatch):
    (state / "run.pid").write_text("4242\n")
    seen = []
    monkeypatch.setattr(stopctl, "request_stop", lambda pid=None: seen.append(pid))
    stopctl.graceful_stop(alive=iter([True, False]).__next__, sleep=lambda s: None)
    assert seen == [4242]


def test_run_pid_is_parsed_defensively_and_released_only_if_ours(state):
    import os
    for junk in ("../../etc", "12 34", "-5", "9" * 40, "0", "\x00"):
        (state / "run.pid").write_text(junk)
        assert store.read_pid() is None
    (state / "run.pid").write_text(str(os.getpid() + 1))     # a newer runner's pid: ours must not remove it
    store.release_pid()
    assert (state / "run.pid").exists()
    (state / "run.pid").write_text(str(os.getpid()))
    store.release_pid()
    assert not (state / "run.pid").exists()
