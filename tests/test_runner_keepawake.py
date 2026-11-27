"""Task #972: keep the PC awake only while a job runs. OS calls are faked; runs on Linux."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runner"))

from crewboss_runner import doctor, keepawake  # noqa: E402
from crewboss_runner.keepawake import KeepAwake  # noqa: E402


class Fake:
    def __init__(self):
        self.log = []

    def start(self):
        self.log.append("start")

    def stop(self):
        self.log.append("stop")


def mk(is_enabled=lambda: True):
    fake = Fake()
    return KeepAwake(lambda: fake, is_enabled), fake


def test_acquire_on_first_hold_across_overlap_release_after_last():
    ka, f = mk()
    assert f.log == []                    # idle: nothing held
    ka.acquire()
    ka.acquire()                          # a second overlapping job
    assert f.log == ["start"] and ka.held and ka.count == 2
    ka.release()
    assert f.log == ["start"] and ka.held                # still one job
    ka.release()
    assert f.log == ["start", "stop"] and not ka.held


def test_release_on_exception_and_extra_release_is_safe():
    ka, f = mk()
    with pytest.raises(RuntimeError):
        with ka:
            raise RuntimeError("job crashed")
    assert f.log == ["start", "stop"]
    ka.release()
    assert ka.count == 0 and f.log == ["start", "stop"]


def test_release_all_on_shutdown():
    ka, f = mk()
    ka.acquire(); ka.acquire()
    ka.release_all()
    assert f.log == ["start", "stop"] and ka.count == 0 and not ka.held


def test_setting_off_makes_no_os_calls():
    called = []
    ka = KeepAwake(lambda: called.append("factory"), lambda: False)
    with ka:
        pass
    assert called == []


def test_setting_parsing(monkeypatch):
    monkeypatch.delenv("CREWBOSS_RUNNER_KEEP_AWAKE_DURING_JOBS", raising=False)
    assert keepawake.enabled()
    for v in ("0", "false", "No", "off"):
        monkeypatch.setenv("CREWBOSS_RUNNER_KEEP_AWAKE_DURING_JOBS", v)
        assert not keepawake.enabled()
    monkeypatch.setenv("CREWBOSS_RUNNER_KEEP_AWAKE_DURING_JOBS", "1")
    assert keepawake.enabled()


def test_backend_failure_is_logged_once_and_job_carries_on(caplog):
    def boom():
        raise OSError("no systemd")
    ka = KeepAwake(boom, lambda: True)
    for _ in range(2):
        with ka:
            pass
    assert sum("keep-awake unavailable" in r.message for r in caplog.records) == 1


def test_windows_backend_flags_and_clear():
    calls = []
    b = keepawake.WindowsBackend(set_state=lambda flags: calls.append(flags) or 1)
    b.start()
    assert calls == [0x80000000 | 0x00000001]
    assert not calls[0] & 0x00000002 and not calls[0] & 0x00000040   # never ES_DISPLAY_REQUIRED / ES_AWAYMODE_REQUIRED
    b.stop()
    assert calls[1] == 0x80000000                                    # ES_CONTINUOUS alone clears it


def test_windows_set_and_clear_on_same_thread():
    import threading
    tids = []
    b = keepawake.WindowsBackend(set_state=lambda flags: tids.append(threading.get_ident()) or 1)
    b.start(); b.stop()
    assert len(tids) == 2 and tids[0] == tids[1] != threading.get_ident()


def test_proc_backend_starts_and_kills_child(monkeypatch):
    events = []

    class P:
        def __init__(self, argv, **kw):
            events.append(("popen", argv))

        def terminate(self):
            events.append("terminate")

        def wait(self, t=None):
            events.append("wait")
    monkeypatch.setattr(keepawake.subprocess, "Popen", P)
    b = keepawake.ProcBackend(["caffeinate", "-i", "-w", "1"])
    b.start(); b.stop(); b.stop()
    assert events[0] == ("popen", ["caffeinate", "-i", "-w", "1"]) and events[1:] == ["terminate", "wait"]


def test_backend_choice_per_os(monkeypatch):
    monkeypatch.setattr(keepawake.shutil, "which", lambda n: "/usr/bin/" + n)
    assert isinstance(keepawake.make_backend("windows"), keepawake.WindowsBackend)
    mac = keepawake.make_backend("macos")
    assert mac.argv[:3] == ["caffeinate", "-i", "-w"]
    lin = keepawake.make_backend("linux")
    assert lin.argv[:2] == ["systemd-inhibit", "--what=sleep"] and lin.argv[-2:] == ["sleep", "infinity"] and "--mode=block" in lin.argv
    assert keepawake.make_backend("wsl") is None
    monkeypatch.setattr(keepawake.shutil, "which", lambda n: None)
    assert keepawake.make_backend("linux") is None


def test_availability(monkeypatch):
    monkeypatch.setattr(keepawake.shutil, "which", lambda n: None)
    assert keepawake.availability("windows")[0]
    assert not keepawake.availability("linux")[0]
    ok, why = keepawake.availability("wsl")
    assert not ok and "Windows" in why


def test_doctor_reports_keep_awake(monkeypatch):
    monkeypatch.setattr(keepawake, "availability", lambda plat=None: (True, "SetThreadExecutionState (system sleep only)"))
    monkeypatch.setenv("CREWBOSS_RUNNER_KEEP_AWAKE_DURING_JOBS", "0")
    row = {r["id"]: r for r in doctor.run_checks()}["keep_awake"]
    assert "OFF" in row["detail"] and row["status"] == "warn"
    monkeypatch.setenv("CREWBOSS_RUNNER_KEEP_AWAKE_DURING_JOBS", "1")
    row = {r["id"]: r for r in doctor.run_checks()}["keep_awake"]
    assert row["status"] == "pass" and "on" in row["detail"]


def _loop_worker(run_job):
    import asyncio
    from types import SimpleNamespace
    from crewboss_runner import worker as w
    wk = object.__new__(w.Worker)
    ready = asyncio.Event(); ready.set()

    async def rpc(name):
        wk.stopping = True
        return {"ok": True, "job": {"job_id": 1}}
    wk.link = SimpleNamespace(ready=ready, rpc=rpc, nudges=asyncio.Event())
    wk.stopping, wk.idle_poll, wk.results = False, 0.01, []
    wk.status = SimpleNamespace(set=lambda **k: None)
    wk.preflight = lambda: []
    async def rep(kinds): pass
    wk._report = rep
    wk.problem_kinds = []
    wk.run_job = run_job
    wk._fail_delay, wk.failure_backoff, wk.failure_backoff_max = 0.0, 1, 1
    return wk


@pytest.mark.parametrize("fails", [False, True])
def test_worker_loop_holds_only_while_the_job_runs(monkeypatch, fails):
    import asyncio
    ka, f = mk()
    monkeypatch.setattr(keepawake, "GLOBAL", ka)
    seen = []

    async def run_job(job):
        seen.append(list(f.log))        # held while the job runs
        if fails:
            raise asyncio.CancelledError()
        return "done"
    wk = _loop_worker(run_job)
    try:
        asyncio.run(wk.loop())
    except asyncio.CancelledError:
        assert fails
    assert seen == [["start"]] and f.log == ["start", "stop"] and ka.count == 0
