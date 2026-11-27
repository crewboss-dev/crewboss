"""#974: the runner's tray and local control panel. pystray, Pillow and every OS call are faked; the HTTP server is real (127.0.0.1)."""
import http.client
import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "runner"))

from crewboss_runner import appctl, panel, settings, store, tray, trayauto  # noqa: E402
from crewboss_runner.ui import StatusServer  # noqa: E402


@pytest.fixture(autouse=True)
def folders(tmp_path, monkeypatch):
    monkeypatch.setenv("CREWBOSS_RUNNER_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("CREWBOSS_RUNNER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CREWBOSS_RUNNER_NO_KEYRING", "1")
    monkeypatch.delenv("CREWBOSS_RUNNER_KEEP_AWAKE_DURING_JOBS", raising=False)
    monkeypatch.delenv("MALLEN_RUNNER_KEEP_AWAKE_DURING_JOBS", raising=False)
    store.mark_dir(store.state_dir())


# --- state -> colour -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("enrolled,running,link,job,pre,fails,colour", [
    (False, False, "stopped", None, [], 0, "amber"),
    (True, False, "stopped", None, [], 0, "grey"),
    (True, True, "ready", None, [], 0, "green"),
    (True, True, "ready", {"id": 4, "project": "acme", "title": "Fix it"}, [], 0, "blue"),
    (True, True, "revoked", None, [], 0, "amber"),
    (True, True, "backoff", None, [], 0, "amber"),
    (True, True, "connecting", None, [], 0, "amber"),
    (True, True, "ready", None, ["claude is not installed"], 0, "amber"),
    (True, True, "ready", None, [], 2, "amber"),
    (True, True, "ready", {"id": 4}, [], 2, "blue"),   # a running job outranks a health warning
])
def test_state_to_colour(enrolled, running, link, job, pre, fails, colour):
    got, text = panel.classify(enrolled, running, link, job, pre, fails)
    assert got == colour and text


def test_local_state_reads_the_runner_files(monkeypatch):
    monkeypatch.setattr(appctl, "enrolled", lambda: {"server": "dash.example.test", "name": "pc1", "device_id": 7, "mode": "subscription"})
    monkeypatch.setattr(appctl, "is_running", lambda: True)
    monkeypatch.setattr(appctl, "autostart_kind", lambda: "logon")
    monkeypatch.setattr(trayauto, "is_enabled", lambda plat=None: False)
    (store.state_dir() / "status.json").write_text(json.dumps({"state": "ready", "events": [], "job": {
        "id": 3, "task_id": 9, "branch": "dash/j3", "state": "running", "project": "acme", "title": "Fix login", "started": 1000.0},
        "recent_jobs": [{"id": 2, "project": "acme", "title": "Old", "outcome": "done", "ended": 900.0}]}))
    s = panel.local_state(now=1125.0)
    assert (s["colour"], s["running"], s["job"]["elapsed_s"], s["crewboss_url"]) == ("blue", True, 125, "https://dash.example.test")
    assert s["recent_jobs"][0]["title"] == "Old" and s["keep_awake"]["holding"] in (True, False)
    assert "token" not in json.dumps(s).lower()
    monkeypatch.setattr(appctl, "is_running", lambda: False)   # a stale status.json of a stopped runner must not show a job
    s = panel.local_state()
    assert (s["colour"], s["job"], s["link"]) == ("grey", None, "stopped")


# --- the tray ------------------------------------------------------------------------------------------------------------

class FakeMenu(list):
    SEPARATOR = "-"

    def __init__(self, *items):
        super().__init__(items)


class FakeItem:
    def __init__(self, text, action, enabled=True, default=False, visible=True, checked=None, radio=False):
        self.text, self.action, self.enabled, self.default, self.visible, self.checked, self.radio = text, action, enabled, default, visible, checked, radio

    def is_visible(self):
        return self.visible(self) if callable(self.visible) else self.visible

    def label(self):
        return self.text(self) if callable(self.text) else self.text

    def is_enabled(self):
        return self.enabled(self) if callable(self.enabled) else self.enabled


class FakeIcon:
    def __init__(self, name, icon, title, menu):
        self.name, self.icon, self.title, self.menu, self.notes, self.stopped = name, icon, title, menu, [], False

    def update_menu(self): pass
    def notify(self, text, title=None): self.notes.append(text)
    def stop(self): self.stopped = True


FAKE_PYSTRAY = types.SimpleNamespace(Menu=FakeMenu, MenuItem=FakeItem, Icon=FakeIcon)


class Ctl:
    def __init__(self, running=False, job=None):
        self.running, self.job, self.calls = running, job, []

    def start(self): self.calls.append("start"); return True, "starting"
    def stop(self, force=False): self.calls.append("stop"); return True, "stopped"
    def use_profile(self, name, force=False): self.calls.append(("use_profile", name, force)); return True, "Now connected"


def make_tray(monkeypatch, state, ctl=None, opened=None):
    ctl = ctl or Ctl()
    t = tray.Tray(FAKE_PYSTRAY, types.SimpleNamespace(new=lambda *a, **k: object()), ctl=ctl, opener=(opened.append if opened is not None else lambda u: None),
                  doctor_check=lambda: 0)
    monkeypatch.setattr(tray, "make_icon", lambda colour, mod: colour)
    monkeypatch.setattr(panel, "local_state", lambda failing=0: state)
    t.icon = FakeIcon("x", None, "", None)
    return t, ctl


def test_tray_refresh_sets_icon_colour_and_tooltip(monkeypatch):
    for colour in ("grey", "green", "blue", "amber"):
        t, _ = make_tray(monkeypatch, {"colour": colour, "status": "S " + colour, "running": colour != "grey", "job": None, "enrolled": True})
        t.refresh()
        assert t.icon.icon == colour and t.icon.title == "Crewboss runner: S " + colour


def test_tray_menu_items_and_actions(monkeypatch):
    t, ctl = make_tray(monkeypatch, {"colour": "grey", "status": "Stopped", "running": False, "job": None, "enrolled": True})
    t.refresh()
    items = [i for i in t.menu() if isinstance(i, FakeItem) and i.is_visible()]   # the Server submenu is hidden with one profile
    assert [i.label() for i in items] == ["Stopped", "Start", "Stop", "Open control panel", "Quit tray"]
    assert [i.is_enabled() for i in items] == [False, True, False, True, True]
    assert items[3].default
    t._start()
    assert ctl.calls == ["start"]
    t.state = {**t.state, "running": True, "colour": "green"}
    assert [i.is_enabled() for i in [i for i in t.menu() if isinstance(i, FakeItem) and i.is_visible()]][1:3] == [False, True]
    t._stop_runner()
    assert ctl.calls == ["start", "stop"]


def test_tray_stop_with_a_job_opens_the_panel_instead(monkeypatch):
    opened = []
    t, ctl = make_tray(monkeypatch, {"colour": "blue", "status": "x", "running": True, "job": {"id": 1}, "enrolled": True}, opened=opened)
    t.refresh()
    t.make_server = lambda: StatusServer(on_exchange=t._remove_launcher, actions=panel.Actions()).start()
    t.on_stop()
    import time
    for _ in range(50):
        if opened:
            break
        time.sleep(0.05)
    t.server.stop()
    assert "stop" not in ctl.calls and opened and t.icon.notes


def test_tray_opens_the_panel_without_a_secret_on_the_command_line(monkeypatch):
    opened = []
    t, _ = make_tray(monkeypatch, {"colour": "grey", "status": "Stopped", "running": False, "job": None, "enrolled": True}, opened=opened)
    t.make_server = lambda: StatusServer(actions=panel.Actions()).start()
    try:
        path = t.open_panel()
        assert opened == [store.state_dir().joinpath(tray.LAUNCHER).as_uri()]   # only a file path is handed to the browser
        assert t.server.bootstrap not in opened[0]
        text = open(path).read()
        assert t.server.bootstrap in text and f"http://127.0.0.1:{t.server.port}/#" in text   # the key is in the user-only file
        if os.name != "nt":
            assert oct(os.stat(path).st_mode & 0o777) == "0o600"
        first = t.server.bootstrap
        t.open_panel()
        assert t.server.bootstrap != first   # a fresh link each time
    finally:
        t.server.stop()


def test_tray_without_pystray_says_how_to_install(monkeypatch, capsys):
    monkeypatch.setattr(tray.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "pystray", None)
    assert tray.cmd_tray() == 1 and "pip install pystray Pillow" in capsys.readouterr().err


@pytest.mark.parametrize("plat", ["linux", "darwin"])
def test_tray_is_windows_only(monkeypatch, capsys, plat):
    monkeypatch.setattr(tray.sys, "platform", plat)
    assert tray.cmd_tray() == 1 and "Windows feature" in capsys.readouterr().err


# --- the control panel over HTTP -----------------------------------------------------------------------------------------

class Calls:
    def __init__(self, mp):
        self.log = []
        self.job = None
        self.mp = mp
        for name, ret in (("start", (True, "starting")), ("stop", (True, "stopped")), ("set_autostart", (True, "saved")),
                          ("set_keep_awake", (True, "saved")), ("run_repair", (True, "done")), ("enrol", (True, "Enrolled"))):
            mp.setattr(appctl, name, self._rec(name, ret))
        mp.setattr(appctl, "job_running", lambda: self.job)
        mp.setattr(appctl, "enrolled", lambda: {"server": "s.example.test", "name": "pc", "device_id": 1})
        mp.setattr(appctl, "is_running", lambda: True)
        mp.setattr(appctl, "autostart_kind", lambda: "off")
        mp.setattr(appctl, "platform", lambda: "windows")
        mp.setattr(appctl, "doctor_rows", lambda: [{"id": "git", "status": "pass", "detail": "ok"}])
        mp.setattr(appctl, "repairs", lambda: [{"id": "abc", "text": "x"}])
        mp.setattr(trayauto, "is_enabled", lambda plat=None: False)

    def _rec(self, name, ret):
        def fn(*a, **kw):
            self.log.append((name, a, kw))
            return ret
        return fn


@pytest.fixture
def app(monkeypatch):
    calls = Calls(monkeypatch)
    ui = StatusServer().start()
    port = ui.port

    def req(method, path, body=None, headers=None, host=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        h = {"Host": host or f"127.0.0.1:{port}", **(headers or {})}
        c.request(method, path, body=body, headers=h)
        r = c.getresponse()
        data = r.read()
        return r.status, dict(r.getheaders()), data

    status, hdrs, body = req("POST", "/api/exchange", json.dumps({"token": ui.bootstrap}), {"Content-Type": "application/json"})
    assert status == 200 and not [k for k in hdrs if k.lower() == "set-cookie"]   # no cookies, ever
    sess = {"Authorization": "Bearer " + json.loads(body)["session"]}

    def post(name, data=None, bearer=True, **kw):
        h = {**(sess if bearer else {}), "Content-Type": "application/json", **kw.pop("headers", {})}
        return req("POST", f"/api/action/{name}", json.dumps(data or {}), h, **kw)

    yield types.SimpleNamespace(ui=ui, req=req, post=post, sess=sess, calls=calls)
    ui.stop()


def test_get_needs_the_session(app):
    for path in ("/api/state", "/api/health", "/api/log", "/api/status"):
        assert app.req("GET", path)[0] == 401
        assert app.req("GET", path, headers=app.sess)[0] == 200
    assert app.req("GET", "/api/state", headers={"Cookie": f"cb_session_{app.ui.port}={app.ui.session}"})[0] == 401   # no cookie auth
    assert app.req("GET", "/")[0] == 200 and app.req("GET", "/app.js")[0] == 200   # the page and its assets are public (they hold nothing)


def test_post_without_or_with_a_wrong_bearer_is_refused(app):
    assert app.post("start", bearer=False)[0] == 401
    assert app.post("start", bearer=False, headers={"Authorization": "Bearer nope"})[0] == 401
    assert app.post("start", bearer=False, headers={"Cookie": f"cb_session_{app.ui.port}={app.ui.session}"})[0] == 401   # a cookie is never accepted
    assert app.calls.log == []
    # not JSON (a cross-site form post cannot set this)
    assert app.req("POST", "/api/action/start", "a=b", {**app.sess, "Content-Type": "text/plain"})[0] == 403
    assert app.calls.log == []


def test_wrong_host_or_origin_is_403(app):
    assert app.post("start", host="evil.example")[0] == 403
    assert app.post("start", host=f"evil.example:{app.ui.port}")[0] == 403
    assert app.post("start", headers={"Origin": "http://evil.example"})[0] == 403
    assert app.post("start", headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
    assert app.req("GET", "/api/state", headers=app.sess, host="evil.example")[0] == 403
    assert app.post("start", host=f"localhost:{app.ui.port}", headers={"Origin": f"http://localhost:{app.ui.port}"})[0] == 200
    assert [c[0] for c in app.calls.log] == ["start"]


def test_start_stop_autostart_keep_awake_call_the_adapter(app):
    assert app.post("start")[0] == 200
    assert app.post("stop")[0] == 200
    assert app.post("autostart", {"mode": "logon"})[0] == 200
    assert app.post("keep_awake", {"on": False})[0] == 200
    assert app.post("repair", {"id": "abc"})[0] == 200
    assert [(c[0], c[1], c[2]) for c in app.calls.log] == [("start", (), {}), ("stop", (), {"force": False}), ("set_autostart", ("logon",), {}),
                                                            ("set_keep_awake", (False,), {}), ("run_repair", ("abc",), {})]


def test_stop_with_a_job_needs_the_confirm(app):
    app.calls.job = {"id": 5}
    s, _, b = app.post("stop")
    assert s == 409 and json.loads(b)["needs_confirm"] and not [c for c in app.calls.log if c[0] == "stop"]
    assert app.post("stop", {"confirm": True})[0] == 200
    assert app.calls.log[-1] == ("stop", (), {"force": True})


def test_bad_inputs_are_refused_and_nothing_takes_a_path_or_command(app):
    assert app.post("nope")[0] == 404
    assert app.post("autostart", {"mode": 5})[0] == 400
    assert app.post("keep_awake", {"on": "yes"})[0] == 400
    assert app.post("repair", {"id": "../../etc/passwd" * 3})[0] == 400
    assert app.post("enrol", {"code": "x"})[0] == 409   # already enrolled in this fixture
    assert app.calls.log == []


def test_enrol_only_when_not_enrolled_and_only_to_the_enrol_function(app, monkeypatch):
    monkeypatch.setattr(appctl, "enrolled", lambda: None)
    assert app.post("enrol", {"code": "dash.example.test:8000#abcd1234-secret"})[0] == 200
    name, args, _ = app.calls.log[-1]
    assert name == "enrol" and args[0] == "dash.example.test:8000#abcd1234-secret"
    assert app.post("enrol", {"code": "short"})[0] == 400
    assert app.post("enrol", {"code": 12345678})[0] == 400


def test_actions_are_rate_limited(app):
    codes = [app.post("recheck")[0] for _ in range(25)]
    assert codes[:20] == [200] * 20 and 429 in codes[20:]


def test_state_exposes_no_secret_and_the_csrf_is_derived(app):
    s, _, b = app.req("GET", "/api/state", headers=app.sess)
    d = json.loads(b)
    assert "csrf" not in d and app.ui.session not in b.decode() and app.ui.bootstrap not in b.decode()
    assert d["autostart"]["options"][-1]["key"] == "service"   # windows only


def test_page_assets_are_local_and_the_csp_is_self_only(app):
    s, h, b = app.req("GET", "/")
    assert "http://" not in b.decode().replace("http://127", "") and "https://" not in b.decode()
    assert "script-src 'self'" in h["Content-Security-Policy"] and "unsafe-inline" not in h["Content-Security-Policy"]
    assert "https://" not in app.req("GET", "/app.js")[2].decode() and "https://" not in app.req("GET", "/app.css")[2].decode()
    assert 'name="color-scheme"' in b.decode() and "prefers-color-scheme:dark" in app.req("GET", "/app.css")[2].decode()


# --- the adapter: service choice, elevation, keep awake, stop/start routing --------------------------------------------

def test_service_choice_launches_the_elevation_helper_with_fixed_args(monkeypatch):
    seen = []
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(appctl, "platform", lambda: "windows")
    monkeypatch.setattr(appctl, "autostart_kind", lambda: "logon")
    monkeypatch.setattr(appctl, "run_elevated", lambda exe, args, timeout=600: seen.append((exe, args)) or 0)
    monkeypatch.setattr(appctl.service, "runner_path", lambda: r"C:\x\crewboss-runner.exe")
    appctl._elev.update(state="idle", message="")
    ok, msg = appctl.set_autostart("service")
    assert (ok, msg) == (True, "pending")
    import time
    for _ in range(50):
        if appctl._elev["state"] != "running":
            break
        time.sleep(0.05)
    assert seen == [(r"C:\x\crewboss-runner.exe", ["service", "install", "--windows-service"])]   # never a password, never request data
    assert appctl.elevated_status()["state"] == "done"
    assert not [a for a in sys.modules["inspect"].signature(appctl.set_autostart).parameters if "pass" in a]


def test_service_choice_reports_a_cancelled_uac_prompt(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(appctl, "platform", lambda: "windows")
    monkeypatch.setattr(appctl, "autostart_kind", lambda: "off")
    monkeypatch.setattr(appctl, "run_elevated", lambda exe, args, timeout=600: 1223)
    monkeypatch.setattr(appctl.service, "runner_path", lambda: "crewboss-runner.exe")
    appctl._elev.update(state="idle", message="")
    appctl.set_autostart("service")
    import time
    for _ in range(50):
        if appctl._elev["state"] != "running":
            break
        time.sleep(0.05)
    assert appctl._elev["state"] == "failed" and "Cancelled" in appctl._elev["message"]


def test_service_is_not_offered_off_windows(monkeypatch):
    for plat in ("linux", "macos", "wsl"):
        assert [o["key"] for o in appctl.autostart_options(plat)] == ["off", "logon"]
    assert [o["key"] for o in appctl.autostart_options("windows")] == ["off", "logon", "service"]
    monkeypatch.setattr(appctl, "platform", lambda: "linux")
    assert appctl.set_autostart("service") == (False, "that choice is not available on this PC")
    assert "LaunchAgent" in appctl.autostart_options("macos")[1]["label"] and "systemd" in appctl.autostart_options("linux")[1]["label"]


def test_start_routes_by_how_autostart_is_set_up(monkeypatch):
    calls = []
    monkeypatch.setattr(appctl, "is_running", lambda: False)
    monkeypatch.setattr(appctl, "platform", lambda: "linux")
    monkeypatch.setattr(appctl, "_call", lambda fn, *a, **k: calls.append(("svc", a)) or (0, ""))
    monkeypatch.setattr(appctl, "_detached_run", lambda: calls.append("detached") or (True, "starting"))
    monkeypatch.setattr(appctl, "autostart_kind", lambda: "logon")
    assert appctl.start() == (True, "starting") and calls == [("svc", ("linux", "start"))]
    calls.clear()
    monkeypatch.setattr(appctl, "autostart_kind", lambda: "off")
    appctl.start()
    assert calls == ["detached"]


def test_detached_start_runs_the_gui_launcher(monkeypatch):
    seen = {}
    monkeypatch.setattr(appctl.service, "runner_path", lambda: "/x/crewboss-runner")
    monkeypatch.setattr(appctl.service, "runnerw_path", lambda exe: "/x/crewboss-runnerw")
    monkeypatch.setattr(appctl.subprocess, "Popen", lambda argv, **kw: seen.update(argv=argv, kw=kw))
    assert appctl._detached_run() == (True, "starting")
    assert seen["argv"] == ["/x/crewboss-runnerw", "run"] and seen["kw"]["start_new_session"] is True


def test_stop_refuses_a_running_job_without_force_and_uses_the_right_route(monkeypatch):
    calls = []
    monkeypatch.setattr(appctl, "is_running", lambda: True)
    monkeypatch.setattr(appctl, "platform", lambda: "linux")
    monkeypatch.setattr(appctl, "job_running", lambda: {"id": 1})
    assert appctl.stop() == (False, "a job is running")
    monkeypatch.setattr(appctl, "autostart_kind", lambda: "logon")
    monkeypatch.setattr(appctl, "_call", lambda fn, *a, **k: calls.append((a, k)) or (0, ""))
    assert appctl.stop(force=True)[0] is True and calls[0][0] == ("linux", "stop")
    # no autostart: the graceful stop of 972 when it exists
    monkeypatch.setattr(appctl, "autostart_kind", lambda: "off")
    fake = types.SimpleNamespace(graceful_stop=lambda wait: calls.append("graceful") or True)
    monkeypatch.setattr(appctl, "stopctl", lambda: fake)
    assert appctl.stop(force=True) == (True, "stopped") and calls[-1] == "graceful"


def test_keep_awake_is_persisted_in_the_settings_file_and_read_back():
    from crewboss_runner import keepawake
    assert keepawake.enabled() is True
    assert appctl.set_keep_awake(False)[0] is True
    assert json.loads(settings.prefs_path().read_text()) == {"keep_awake_during_jobs": "0"}
    assert keepawake.enabled() is False and appctl.keep_awake_info()["enabled"] is False
    appctl.set_keep_awake(True)
    assert keepawake.enabled() is True
    with pytest.raises(ValueError):
        settings.set_pref("STATE_DIR", "/etc")   # the panel may write one setting only


def test_keep_awake_env_wins_and_locks_the_toggle(monkeypatch):
    monkeypatch.setenv("CREWBOSS_RUNNER_KEEP_AWAKE_DURING_JOBS", "0")
    assert appctl.keep_awake_info()["locked_by_env"] is True
    assert appctl.set_keep_awake(True)[0] is False


def test_tray_autostart_files(tmp_path, monkeypatch):
    monkeypatch.setattr(trayauto.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(trayauto, "tray_exe", lambda: "/x/crewboss-runner-tray")
    assert not trayauto.is_enabled("linux")
    assert trayauto.enable("linux")[0] and trayauto.is_enabled("linux")
    assert "Exec=\"/x/crewboss-runner-tray\"" in trayauto.desktop_path().read_text()
    assert trayauto.disable("linux")[0] and not trayauto.is_enabled("linux")
    assert trayauto.enable("macos")[0] and trayauto.plist_path().exists()
    seen = []
    monkeypatch.setattr(trayauto, "_run", lambda cmd: seen.append(cmd) or types.SimpleNamespace(returncode=0, stdout="", stderr=""))
    trayauto.enable("windows")
    assert seen[0][:8] == ["schtasks", "/Create", "/TN", "Crewboss runner tray", "/SC", "ONLOGON", "/RL", "LIMITED"]   # per user, limited, no admin
    assert trayauto.available("windows") and not trayauto.available("linux") and not trayauto.available("macos") and not trayauto.available("wsl")


def test_cli_has_the_tray_command_and_the_gui_script():
    from crewboss_runner import cli
    import pathlib
    text = (pathlib.Path(__file__).parent.parent / "runner" / "pyproject.toml").read_text()
    assert 'crewboss-runner-tray = "crewboss_runner.tray:main"' in text and "sys_platform == 'win32'" in text.split("pystray")[1].split("\n")[0]
    assert "tray" in [a for a in cli.__doc__.split()]


# --- danger zone: remove the runner from this PC ---------------------------------------------------------------------------

def test_remove_needs_csrf_host_origin_and_the_typed_word(app, monkeypatch):
    removed = []
    monkeypatch.setattr(appctl, "remove_runner", lambda on_done=None: removed.append(1) or (True, "ok"))
    monkeypatch.setattr(appctl, "in_runner_process", lambda: False)
    assert app.post("remove", {"confirm": "REMOVE"}, bearer=False)[0] == 401
    assert app.post("remove", {"confirm": "REMOVE"}, bearer=False, headers={"Authorization": "Bearer " + "x" * 40})[0] == 401
    assert app.post("remove", {"confirm": "REMOVE"}, host="evil.example")[0] == 403
    assert app.post("remove", {"confirm": "REMOVE"}, headers={"Origin": "http://evil.example"})[0] == 403
    assert app.post("remove", {"confirm": "remove"})[0] == 400 and app.post("remove", {})[0] == 400
    assert removed == []
    s, _, b = app.post("remove", {"confirm": "REMOVE"})
    assert s == 200 and json.loads(b)["final_command"] == "pipx uninstall crewboss-runner" and removed == [1]


def test_remove_is_refused_while_a_job_runs_with_the_cli_message(tmp_path, monkeypatch):
    from crewboss_runner import doctor
    monkeypatch.setattr(appctl, "in_runner_process", lambda: False)
    monkeypatch.setattr(os, "name", "posix")
    monkeypatch.setattr(appctl, "job_running", lambda: {"id": 1})
    monkeypatch.setattr(appctl, "is_running", lambda: True)
    monkeypatch.setattr(doctor, "_active_job", lambda: True)
    deleted = []
    monkeypatch.setattr(doctor.shutil, "rmtree", lambda *a, **k: deleted.append(a))
    ok, msg = appctl.remove_runner()
    assert ok is False and "A job is running on this PC right now" in msg and deleted == []
    code, obj = panel.Actions().run("remove", {"confirm": "REMOVE"})
    assert code == 409 and "A job is running" in obj["message"]


def test_remove_reuses_cmd_uninstall_and_drops_the_tray_entry(monkeypatch):
    from crewboss_runner import doctor
    seen = []
    monkeypatch.setattr(appctl, "is_running", lambda: False)
    monkeypatch.setattr(appctl, "in_runner_process", lambda: False)
    monkeypatch.setattr(doctor, "cmd_uninstall", lambda ns: seen.append(ns.yes) or 0)
    monkeypatch.setattr(trayauto, "disable", lambda plat=None: seen.append("tray") or (True, ""))
    assert appctl.remove_runner(on_done=lambda: seen.append("quit"))[0] is True and seen == [True, "tray", "quit"]


def test_a_failed_removal_reports_the_error_and_does_not_quit_the_tray(monkeypatch):
    from crewboss_runner import doctor
    seen = []
    monkeypatch.setattr(appctl, "is_running", lambda: False)
    monkeypatch.setattr(appctl, "in_runner_process", lambda: False)
    monkeypatch.setattr(doctor, "cmd_uninstall", lambda ns: (print("NOT deleted: x"), 1)[1])
    monkeypatch.setattr(trayauto, "disable", lambda plat=None: seen.append("tray") or (True, ""))
    a = panel.Actions(on_removed=lambda: seen.append("quit"))
    code, obj = a.run("remove", {"confirm": "REMOVE"})
    assert code == 409 and obj["ok"] is False and "NOT deleted" in obj["message"] and "final_command" not in obj and seen == []


def test_the_elevated_removal_is_pending_and_the_tray_quits_only_after_the_child_succeeds(monkeypatch):
    import threading
    from crewboss_runner import doctor
    seen, gate = [], threading.Event()
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(appctl, "platform", lambda: "windows")
    monkeypatch.setattr(appctl, "autostart_kind", lambda: "service")
    monkeypatch.setattr(appctl, "in_runner_process", lambda: False)
    monkeypatch.setattr(appctl, "job_running", lambda: None)
    monkeypatch.setattr(appctl.service, "is_admin", lambda: False)
    monkeypatch.setattr(appctl.service, "runner_path", lambda: "crewboss-runner.exe")
    monkeypatch.setattr(trayauto, "disable", lambda plat=None: seen.append("tray") or (True, ""))
    results = iter([1223, 0])
    monkeypatch.setattr(appctl, "run_elevated", lambda exe, args, timeout=600: (gate.wait(5), next(results))[1])
    a = panel.Actions(on_removed=lambda: seen.append("quit"))
    monkeypatch.setattr(threading, "Timer", lambda delay, fn: types.SimpleNamespace(start=fn))
    for expect_state in ("failed", "done"):
        appctl._elev.update(state="idle", message="")
        gate.clear()
        code, obj = a.run("remove", {"confirm": "REMOVE"})
        assert code == 202 and obj["pending"] is True and "final_command" in obj
        assert appctl.elevated_status()["state"] == "running" and seen == ([] if expect_state == "failed" else seen)
        assert a.run("remove", {"confirm": "REMOVE"})[0] == 409   # a second request while waiting is refused (atomic check-and-set)
        gate.set()
        import time
        for _ in range(100):
            if appctl._elev["state"] != "running":
                break
            time.sleep(0.03)
        assert appctl._elev["state"] == expect_state
        if expect_state == "failed":
            assert "Cancelled" in appctl._elev["message"] and seen == []   # kept everything: no tray removal, no quit
    assert seen == ["tray", "quit"]


def test_the_wait_timeout_is_reported_as_still_waiting_not_exit_259(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    monkeypatch.setattr(appctl.service, "runner_path", lambda: "crewboss-runner.exe")
    monkeypatch.setattr(appctl, "run_elevated", lambda exe, args, timeout=600: appctl.STILL_WAITING)
    appctl._elev.update(state="idle", message="")
    appctl._elevated("x", ["service", "install"])
    import time
    for _ in range(100):
        if appctl._elev["state"] != "running":
            break
        time.sleep(0.03)
    assert "Still waiting for administrator approval" in appctl._elev["message"] and "259" not in appctl._elev["message"]


def test_the_runners_own_page_refuses_remove_and_autostart_changes_with_the_command(monkeypatch):
    monkeypatch.setattr(appctl, "in_runner_process", lambda: True)
    monkeypatch.setattr(appctl, "platform", lambda: "linux")
    monkeypatch.setattr(appctl, "job_running", lambda: None)
    ok, msg = appctl.remove_runner()
    assert not ok and "crewboss-runner uninstall" in msg
    ok, msg = appctl.set_autostart("off")
    assert not ok and "crewboss-runner service uninstall" in msg


def test_autostart_changes_need_the_job_confirm_on_every_os(monkeypatch):
    calls = []
    monkeypatch.setattr(appctl, "job_running", lambda: {"id": 1})
    monkeypatch.setattr(appctl, "autostart_kind", lambda: "logon")
    monkeypatch.setattr(appctl, "set_autostart", lambda mode: calls.append(mode) or (True, "ok"))
    a = panel.Actions()
    code, obj = a.run("autostart", {"mode": "off"})
    assert code == 409 and obj["needs_confirm"] and calls == []
    assert a.run("autostart", {"mode": "off", "confirm": True})[0] == 200 and calls == ["off"]


def test_autostart_status_is_cached_not_asked_every_poll(monkeypatch):
    n = []
    panel.invalidate()
    monkeypatch.setattr(appctl, "enrolled", lambda: {"server": "s", "name": "p", "device_id": 1})
    monkeypatch.setattr(appctl, "is_running", lambda: False)
    monkeypatch.setattr(appctl, "autostart_kind", lambda: n.append(1) or "logon")
    monkeypatch.setattr(trayauto, "is_enabled", lambda plat=None: n.append(2) or False)
    for _ in range(5):
        panel.local_state()
    assert n == [1, 2]
    panel.invalidate()
    panel.local_state()
    assert n == [1, 2, 1, 2]


def test_keep_awake_holding_comes_from_the_runner_status(monkeypatch):
    monkeypatch.setattr(appctl, "enrolled", lambda: {"server": "s", "name": "p", "device_id": 1})
    monkeypatch.setattr(appctl, "is_running", lambda: True)
    monkeypatch.setattr(appctl, "autostart_kind", lambda: "off")
    monkeypatch.setattr(trayauto, "is_enabled", lambda plat=None: False)
    panel.invalidate()
    (store.state_dir() / "status.json").write_text(json.dumps({"state": "ready", "job": {"id": 1, "task_id": 2},
                                                               "keep_awake": {"held": False, "enabled": False, "env": True}}))
    ka = panel.local_state()["keep_awake"]
    assert ka["holding"] is False and ka["enabled"] is False and ka["locked_by_env"] is True   # a job without a held inhibitor is NOT "keeping awake"
    (store.state_dir() / "status.json").write_text(json.dumps({"state": "ready", "keep_awake": {"held": True, "enabled": True, "env": False}}))
    assert panel.local_state()["keep_awake"]["holding"] is True
    assert not hasattr(appctl, "keep_awake_holding")


def test_the_worker_writes_keepawake_state_and_no_task_text():
    from crewboss_runner import worker
    st = worker.keepawake_status()
    assert set(st) == {"held", "enabled", "env"}
    import inspect
    src = inspect.getsource(worker.Worker.run_job)
    assert "task_title" not in src and "project_slug" not in src


def test_tray_logon_task_gets_972s_task_settings(monkeypatch):
    seen = []
    monkeypatch.setattr(trayauto, "tray_exe", lambda: "C:/x/crewboss-runner-tray.exe")
    monkeypatch.setattr(trayauto, "_run", lambda cmd: seen.append(cmd) or types.SimpleNamespace(returncode=0, stdout="", stderr=""))
    assert trayauto.enable("windows")[0]
    ps = seen[1][-1]
    assert "-TaskName 'Crewboss runner tray'" in ps and "AllowStartIfOnBatteries" in ps and "ExecutionTimeLimit" in ps


def test_tray_icon_is_the_crewboss_mark_in_terracotta():
    from PIL import Image
    px = tray.make_icon("green", Image).getpixel((20, 100))   # lower left area of the tile, away from dots and the badge
    assert px[:3] == tray.TERRACOTTA
    assert tray.make_icon("blue", Image).size == (128, 128)


def test_health_shows_the_service_copy_row_and_the_elevated_state(app, monkeypatch):
    monkeypatch.setattr(appctl, "doctor_rows", lambda: [{"id": "service_copy", "status": "warn", "detail": "the service runs an old copy", "fix": "crewboss-runner service install --windows-service"}])
    s, _, b = app.req("GET", "/api/health", headers=app.sess)
    assert json.loads(b)["rows"][0]["id"] == "service_copy" and json.loads(b)["warning"] == 1
    (store.state_dir() / "status.json").write_text(json.dumps({"state": "ready", "elevated": True}))
    panel.invalidate()
    assert json.loads(app.req("GET", "/api/state", headers=app.sess)[2])["elevated"] is True
    assert "s.elevated" in app.req("GET", "/app.js")[2].decode()   # the page prints it in the health area


def test_the_job_link_is_the_servers_task_route(app):
    js = app.req("GET", "/app.js")[2].decode()
    assert "'/all/tasks/' + encodeURIComponent(j.task_id)" in js and "tasks?open" not in js


# --- server profiles in the tray and the panel (#974 + #975) ---------------------------------------------------------------
PROFILES = [{"name": "acme", "server": "dash.acme.test", "state": "enrolled", "active": True},
            {"name": "home", "server": "dash.home.test", "state": "enrolled", "active": False}]


def _server_menu(t):
    sub = [i for i in t.menu() if isinstance(i, FakeItem) and i.text == "Server"][0]
    return sub, list(sub.action[0]())


def test_tray_server_submenu_lists_profiles_and_switches(monkeypatch):
    t, ctl = make_tray(monkeypatch, {"colour": "green", "status": "ok", "running": True, "job": None, "enrolled": True, "profiles": PROFILES})
    t.refresh()
    sub, items = _server_menu(t)
    assert sub.is_visible() and [i.label() for i in items] == ["dash.acme.test (acme)", "dash.home.test (home)"]
    assert [i.checked(i) for i in items] == [True, False] and all(i.radio for i in items)
    t._switch("home", "dash.home.test")
    assert ctl.calls == [("use_profile", "home", False)] and "Now using dash.home.test" in t.icon.notes[-1]
    t._switch("acme", "dash.acme.test")   # already active: nothing to do
    assert len(ctl.calls) == 1


def test_tray_server_submenu_hidden_with_one_profile(monkeypatch):
    t, _ = make_tray(monkeypatch, {"colour": "green", "status": "ok", "running": True, "job": None, "enrolled": True, "profiles": PROFILES[:1]})
    t.refresh()
    sub, _ = _server_menu(t)
    assert not sub.is_visible()


def test_tray_switch_with_a_job_asks_first(monkeypatch):
    st = {"colour": "blue", "status": "x", "running": True, "job": {"id": 3}, "enrolled": True, "profiles": PROFILES}
    t, ctl = make_tray(monkeypatch, st)
    t.refresh()
    asked = []
    t.confirm = lambda text: asked.append(text) or False
    t._switch("home", "dash.home.test")
    assert asked and "cancels it" in asked[0] and ctl.calls == []   # No: nothing switched
    t.confirm = lambda text: True
    t._switch("home", "dash.home.test")
    assert ctl.calls == [("use_profile", "home", True)]


def test_tray_switch_failure_is_shown_in_words(monkeypatch):
    t, ctl = make_tray(monkeypatch, {"colour": "green", "status": "ok", "running": True, "job": None, "enrolled": True, "profiles": PROFILES})
    ctl.use_profile = lambda name, force=False: (False, "the runner did not stop in time")
    t.refresh()
    t._switch("home", "dash.home.test")
    assert t.icon.notes[-1] == "the runner did not stop in time"


def test_panel_use_profile_action(app, monkeypatch):
    monkeypatch.setattr(appctl, "list_profiles", lambda: PROFILES)
    app.calls.mp.setattr(appctl, "use_profile", app.calls._rec("use_profile", (True, "Now connected to dash.home.test")))
    s, _, b = app.post("use_profile", {"name": "home"})
    assert s == 200 and json.loads(b)["message"] == "Now connected to dash.home.test"
    assert app.calls.log[-1] == ("use_profile", ("home",), {"force": False})
    assert app.post("use_profile", {"name": "../x"})[0] == 400 and app.post("use_profile", {"name": 5})[0] == 400 and app.post("use_profile")[0] == 400
    s, _, b = app.post("use_profile", {"name": "nope"})
    assert s == 409 and "no server profile" in json.loads(b)["message"]
    assert app.post("use_profile", {"name": "home"}, bearer=False)[0] == 401
    assert app.req("POST", "/api/action/use_profile", "{}", {**app.sess, "Content-Type": "text/plain"})[0] == 403
    assert app.post("use_profile", {"name": "home"}, headers={"Origin": "http://evil.example"})[0] == 403


def test_panel_use_profile_with_a_job_needs_the_confirm(app, monkeypatch):
    monkeypatch.setattr(appctl, "list_profiles", lambda: PROFILES)
    app.calls.mp.setattr(appctl, "use_profile", app.calls._rec("use_profile", (True, "ok")))
    app.calls.job = {"id": 5}
    s, _, b = app.post("use_profile", {"name": "home"})
    assert s == 409 and json.loads(b)["needs_confirm"] and not [c for c in app.calls.log if c[0] == "use_profile"]
    assert app.post("use_profile", {"name": "home", "confirm": True})[0] == 200
    assert app.calls.log[-1] == ("use_profile", ("home",), {"force": True})


def test_panel_use_profile_error_comes_back_in_plain_words(app, monkeypatch):
    monkeypatch.setattr(appctl, "list_profiles", lambda: PROFILES)
    app.calls.mp.setattr(appctl, "use_profile", lambda name, force=False: (False, "the runner did not stop in time"))
    s, _, b = app.post("use_profile", {"name": "home"})
    assert s == 409 and json.loads(b) == {"ok": False, "message": "the runner did not stop in time"}


def test_appctl_use_profile_maps_errors_and_refuses_in_the_runner_process(monkeypatch):
    from crewboss_runner import profiles
    monkeypatch.setattr(appctl, "in_runner_process", lambda: False)
    def busy(name, force=False, restart=True, say=print): raise profiles.ProfileBusy("A job is running (#4 on x).")
    monkeypatch.setattr(profiles, "use_profile", busy)
    assert appctl.use_profile("home") == (False, "A job is running (#4 on x).")
    monkeypatch.setattr(profiles, "use_profile", lambda name, force=False, restart=True, say=print: say("Now connected to s"))
    assert appctl.use_profile("home") == (True, "Now connected to s")
    monkeypatch.setattr(appctl, "in_runner_process", lambda: True)
    ok, msg = appctl.use_profile("home")
    assert not ok and "crewboss-runner use home" in msg


def test_state_carries_the_profiles_without_secrets(monkeypatch):
    monkeypatch.setattr(appctl, "list_profiles", lambda: [{**PROFILES[0], "runner_name": "pc1"}, PROFILES[1]])
    monkeypatch.setattr(appctl, "enrolled", lambda: {"server": "dash.acme.test", "name": "pc1", "device_id": 7})
    monkeypatch.setattr(appctl, "is_running", lambda: False)
    st = panel.local_state()
    assert [p["name"] for p in st["profiles"]] == ["acme", "home"] and set(st["profiles"][0]) == {"name", "server", "state", "active"}


def test_the_page_has_a_server_switcher_that_posts_use_profile():
    from crewboss_runner import panel_page
    src = panel_page.PAGE if hasattr(panel_page, "PAGE") else open(panel_page.__file__).read()
    assert "use_profile" in src and "server-sel" in src and "needs_confirm" in src
