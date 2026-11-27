"""Task #972 review fix 1 (q822 option A): the Windows service only SUPERVISES. The runner child gets a restricted standard-user
token; the supervisor restarts it with backoff, does not restart after exit 1 / 2, and stops it gracefully. The runner refuses to
claim jobs when elevated. pywin32 is faked: this runs on Linux."""
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runner"))

from crewboss_runner import elevation, winsvc, worker as worker_mod  # noqa: E402
from crewboss_runner.winsvc import Supervisor  # noqa: E402


class Child:
    def __init__(self, codes):
        self.codes, self.pid, self.killed, self.n = list(codes), 42, False, 0

    def poll(self):
        self.n += 1
        return self.codes[min(self.n - 1, len(self.codes) - 1)] if self.codes else None

    def kill_tree(self):
        self.killed = True


def sup(launch, **kw):
    return Supervisor(launch, request_child_stop=kw.pop("stop", lambda pid=None: None), poll=0.001, restart_min=0.001, restart_max=0.004,
                      stop_wait=0.05, **kw)


def test_restarts_with_backoff_then_stops_on_request():
    children = []

    def launch():
        c = Child([None, 3])   # runs, then dies with 3
        children.append(c)
        if len(children) == 3:
            s.request_stop()
        return c
    s = sup(launch)
    assert s.run() == 0
    assert len(children) == 3


@pytest.mark.parametrize("code", [1, 2])
def test_final_exits_are_not_restarted(code):
    launched = []
    s = sup(lambda: (launched.append(1), Child([code]))[1])
    assert s.run() == code
    assert launched == [1] and "Not restarting" in s.final


def test_launch_failure_backs_off_and_retries():
    n = []

    def launch():
        n.append(1)
        if len(n) < 3:
            raise OSError("no token")
        s.request_stop()
        return Child([None])
    s = sup(launch)
    assert s.run() == 0 and len(n) == 3


def test_stop_asks_child_gracefully_then_kills_the_tree(monkeypatch):
    monkeypatch.setattr(winsvc.stopctl, "read_run_pid", lambda: 777)   # the runner's own pid from run.pid (the exe launcher's pid differs)
    asked = []
    c = Child([])   # never exits
    s = sup(lambda: (s.request_stop(), c)[1], stop=lambda pid=None: asked.append(pid or 1))   # a stop arrives while the child runs
    assert s.run() == 0
    assert asked == [777] and c.killed
    c2 = Child([None, None, 0])   # exits after the request: no kill
    s2 = sup(lambda: (s2.request_stop(), c2)[1], stop=lambda pid=None: None)
    assert s2.run() == 0 and not c2.killed


def test_core_stop_before_start_and_log_name(monkeypatch, tmp_path):
    monkeypatch.setenv("CREWBOSS_RUNNER_STATE_DIR", str(tmp_path / "s"))
    seen = {}
    monkeypatch.setattr(winsvc.runlog, "setup", lambda **kw: seen.update(kw))
    core = winsvc.ServiceCore(launch=lambda: Child([]))
    core.request_stop()   # SvcStop before the supervisor existed
    assert core.run() == 0
    assert seen == {"force": True, "name": "service.log", "console": False, "directory": None}   # directory: the admin folder on Windows


# ---- the restricted token (pywin32 faked) ----

def fake_win32(monkeypatch):
    rec = {}
    sec = types.SimpleNamespace(
        WinBuiltinAdministratorsSid=26, TokenIntegrityLevel=25,
        OpenProcessToken=lambda p, access: rec.update(open=(p, access)) or "own-token",
        CreateWellKnownSid=lambda kind, dom: ("sid", kind),
        CreateRestrictedToken=lambda tok, flags, dis, priv, restr: rec.update(restricted=(tok, flags, dis, priv, restr)) or "restricted",
        ConvertStringSidToSid=lambda s: ("sid-str", s),
        SetTokenInformation=lambda tok, cls, val: rec.update(integrity=(tok, cls, val)))
    proc = types.SimpleNamespace(
        STARTUPINFO=lambda: "si",
        CreateProcessAsUser=lambda *a: rec.update(create=a) or ("hproc", "hthread", 1234, 1),
        GetExitCodeProcess=lambda h: 7)
    api = types.SimpleNamespace(GetCurrentProcess=lambda: "me", CloseHandle=lambda h: rec.setdefault("closed", []).append(h))
    ev = types.SimpleNamespace(WaitForSingleObject=lambda h, t: rec.get("wait", 258), WAIT_OBJECT_0=0)
    con = types.SimpleNamespace(TOKEN_ALL_ACCESS=0xF01FF)
    for name, mod in (("win32security", sec), ("win32process", proc), ("win32api", api), ("win32event", ev), ("win32con", con)):
        monkeypatch.setattr(winsvc, name, mod)
    return rec


def test_launch_restricted_uses_a_filtered_token_for_the_child(monkeypatch):
    rec = fake_win32(monkeypatch)
    child = winsvc.launch_restricted(r"C:\Py\Scripts\crewboss-runnerw.exe")
    tok, flags, disable, priv, restrict = rec["restricted"]
    assert tok == "own-token" and flags == winsvc.DISABLE_MAX_PRIVILEGE | winsvc.LUA_TOKEN == 0x5
    assert disable == [(("sid", 26), 0)]            # Administrators SID, deny-only (attributes 0), not removed from the list
    assert rec["integrity"][0] == "restricted" and rec["integrity"][2][0] == ("sid-str", "S-1-16-8192")   # medium integrity
    create = rec["create"]
    assert create[0] == "restricted" and create[1] is None and create[2] == r'"C:\Py\Scripts\crewboss-runnerw.exe" run'
    assert create[6] == 0                           # GUI exe: no flags (it makes its own hidden console)
    assert child.pid == 1234 and child.poll() is None
    rec["wait"] = 0
    assert child.poll() == 7


def test_console_exe_child_gets_create_no_window(monkeypatch):
    rec = fake_win32(monkeypatch)
    winsvc.launch_restricted(r"C:\Py\Scripts\crewboss-runner.exe")
    assert rec["create"][6] == winsvc.CREATE_NO_WINDOW


def test_integrity_failure_refuses_to_launch_and_stops_supervision(monkeypatch):
    rec = fake_win32(monkeypatch)
    winsvc.win32security.SetTokenInformation = lambda *a: (_ for _ in ()).throw(OSError("denied"))
    with pytest.raises(winsvc.FatalLaunchError):
        winsvc.launch_restricted("x.exe")
    assert "create" not in rec                    # no process was started
    s = sup(lambda: winsvc.launch_restricted("x.exe"))
    assert s.run() == winsvc.FATAL_LAUNCH == 3 and "medium integrity" in s.final   # no retry loop; the service stops with an Event Log message


# ---- elevation ----

def test_elevation_refusal(monkeypatch):
    monkeypatch.delenv("CREWBOSS_RUNNER_ALLOW_ELEVATED", raising=False)
    assert elevation.refusal(False) is None and elevation.refusal(None) is None
    assert "administrator" in elevation.refusal(True)
    monkeypatch.setenv("CREWBOSS_RUNNER_ALLOW_ELEVATED", "1")
    assert elevation.refusal(True) is None
    assert elevation.is_elevated() is None   # not Windows


def test_preflight_blocks_an_elevated_runner(monkeypatch):
    monkeypatch.delenv("CREWBOSS_RUNNER_ALLOW_ELEVATED", raising=False)
    monkeypatch.setattr(elevation, "is_elevated", lambda: True)
    w = object.__new__(worker_mod.Worker)
    w.claude, w.cfg, w.auth_cache, w._signed_in_at = "claude", {"mode": "api"}, 1e9, 0.0
    w.scan_cache, w._scanned_at, w.scan_findings, w.job_findings = 1e9, 1e18, [], []
    w.refresh_gemini = lambda: None
    problems = w.preflight()
    assert any("administrator rights" in p for p in problems)
    monkeypatch.setenv("CREWBOSS_RUNNER_ALLOW_ELEVATED", "1")
    assert not any("administrator rights" in p for p in w.preflight())


def test_doctor_elevation_row(tmp_path, monkeypatch):
    from crewboss_runner import doctor, store
    monkeypatch.setattr(store, "run_alive", lambda: True)
    monkeypatch.delenv("CREWBOSS_RUNNER_ALLOW_ELEVATED", raising=False)
    (tmp_path / "status.json").write_text('{"elevated": false}')
    assert doctor._elevation_row(tmp_path)["status"] == "pass"
    (tmp_path / "status.json").write_text('{"elevated": true}')
    assert doctor._elevation_row(tmp_path)["status"] == "fail"
    monkeypatch.setenv("CREWBOSS_RUNNER_ALLOW_ELEVATED", "1")
    assert doctor._elevation_row(tmp_path)["status"] == "warn"
    monkeypatch.setattr(store, "run_alive", lambda: False)
    assert doctor._elevation_row(tmp_path)["status"] == "pass"


def test_preflight_reports_the_elevated_kind(monkeypatch):
    monkeypatch.delenv("CREWBOSS_RUNNER_ALLOW_ELEVATED", raising=False)
    monkeypatch.setattr(elevation, "is_elevated", lambda: True)
    w = object.__new__(worker_mod.Worker)
    w.claude, w.cfg, w.auth_cache, w._signed_in_at = "claude", {"mode": "api"}, 1e9, 0.0
    w.scan_cache, w._scanned_at, w.scan_findings, w.job_findings = 1e9, 1e18, [], []
    w.refresh_gemini = lambda: None
    w.preflight()
    assert "elevated" in w.problem_kinds


def test_assistant_worker_claims_nothing_when_elevated(monkeypatch):
    import asyncio
    from crewboss_runner import ca
    monkeypatch.delenv("CREWBOSS_RUNNER_ALLOW_ELEVATED", raising=False)
    monkeypatch.setattr(elevation, "is_elevated", lambda: True)
    rpcs = []

    async def rpc(name):
        rpcs.append(name)
        return {"ok": True, "turn": None}
    ready = asyncio.Event()
    ready.set()
    wk = ca.CaWorker(types.SimpleNamespace(ready=ready, rpc=rpc), {}, Path("/tmp"), "http://x", idle_poll=0.01)

    async def go():
        t = asyncio.create_task(wk.loop())
        await asyncio.sleep(0.1)
        wk.stopping = True
        await asyncio.wait_for(t, 2)
    asyncio.run(go())
    assert rpcs == []
    monkeypatch.setenv("CREWBOSS_RUNNER_ALLOW_ELEVATED", "1")
    wk.stopping = False
    ready2 = asyncio.Event(); ready2.set()
    wk.link = types.SimpleNamespace(ready=ready2, rpc=rpc, ca_nudges=asyncio.Event())

    async def go2():
        t = asyncio.create_task(wk.loop())
        await asyncio.sleep(0.1)
        wk.stopping = True
        await asyncio.wait_for(t, 2)
    asyncio.run(go2())
    assert "ca_claim" in rpcs
