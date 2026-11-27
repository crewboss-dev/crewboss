"""The runner's environment probes (#810 step 6, runner/crewboss_runner/envcheck.py). Everything is mocked, as in tests/test_runner_os.py: no
real program is started, no real SDK or keystore is read. What is pinned: each KIND is a built-in probe, the dashboard's text is only ever a
lookup key or a plain argument (never a command), unknown kinds are "unsupported", names are validated before they are used, the replies are
compact, and the loop claims, checks and finishes over the socket."""
import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runner"))

from crewboss_runner import envcheck  # noqa: E402

FP = ":".join(["AB"] * 32)


def done(stdout="", stderr="", rc=0):
    return subprocess.CompletedProcess([], rc, stdout, stderr)


@pytest.fixture
def started(monkeypatch):
    """Every program the probes start: recorded, answered by `answers` (argv[0] basename -> CompletedProcess), never real."""
    calls, answers = [], {}

    def fake_run(argv, timeout, stdin=None):
        calls.append((argv, stdin))
        return answers.get(Path(argv[0]).name, done())

    monkeypatch.setattr(envcheck, "_run", fake_run)
    monkeypatch.setattr(envcheck, "os_name", lambda: "linux")
    monkeypatch.setattr(envcheck.shutil, "which", lambda name, **k: f"/usr/bin/{name}" if name in answers or name in ("git",) else None)
    return calls, answers


# --- versions --------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text,want", [("openjdk version \"17.0.9\" 2023-10-17", (17, 0, 9)), ("v20.11.1", (20, 11, 1)),
                                       ("Python 3.12.3", (3, 12, 3)), ("java version \"1.8.0_292\"", (8, 0)), ("no number", None)])
def test_parse_version(text, want):
    assert envcheck.parse_version(text) == want


def test_version_comparison_pads_and_orders():
    assert envcheck.at_least((17, 0, 9), "17") and envcheck.at_least((3, 12), "3.11") and not envcheck.at_least((3, 9), "3.11")
    assert envcheck.at_least((11,), "11.0") and not envcheck.at_least((11,), "11.0.1")


# --- tool --------------------------------------------------------------------------------------------------------------

def test_tool_present_and_new_enough(started):
    _calls, answers = started
    answers["java"] = done(stderr='openjdk version "17.0.9" 2023-10-17')           # java prints its version on stderr
    assert envcheck.probe_tool("java", {"min_version": "17"}) == {"s": "ok", "v": "17.0.9"}
    assert envcheck.probe_tool("java", {}) == {"s": "ok", "v": ""}                  # no minimum: on PATH is enough, nothing is run


def test_tool_too_old_missing_or_unreadable(started):
    calls, answers = started
    answers["java"] = done(stdout='openjdk version "11.0.2"')
    assert envcheck.probe_tool("java", {"min_version": "17"}) == {"s": "old", "v": "11.0.2"}
    assert envcheck.probe_tool("node", {"min_version": "18"}) == {"s": "missing", "v": ""}     # not on PATH: no program was started for it
    assert not [c for c in calls if Path(c[0][0]).name == "node"]
    answers["java"] = done(stdout="banner without a number")
    assert envcheck.probe_tool("java", {"min_version": "17"})["s"] == "old"


def test_tool_runs_only_the_program_it_found_with_fixed_flags(started):
    calls, answers = started
    answers["node"] = done("v20.11.1")
    envcheck.probe_tool("node", {"min_version": "18"})
    assert calls == [(["/usr/bin/node", "--version"], None)]                       # argv is [found path, a fixed flag]: no shell, no extras


def test_only_known_tools_are_ever_started(started, monkeypatch):
    calls, answers = started
    answers["curl"] = done("curl 8.0")
    monkeypatch.setattr(envcheck.shutil, "which", lambda name, **k: pytest.fail("an unknown tool is never even looked up"))
    for name in ("curl", "bash", "powershell", "calc", "tool1"):
        assert envcheck.probe_tool(name, {"min_version": "1"}) == {"s": "unsupported", "v": ""}
    assert calls == []


def test_every_known_tool_has_its_own_version_flag():
    assert envcheck.TOOLS["java"] == ("-version",) and envcheck.TOOLS["adb"] == ("version",) and envcheck.TOOLS["go"] == ("version",)
    assert envcheck.TOOLS["node"] == ("--version",) and envcheck.TOOLS["msbuild"] == ("-version",) and envcheck.TOOLS["xcodebuild"] == ("-version",)
    for name in ("java", "javac", "node", "npm", "python3", "python", "py", "dotnet", "git", "gradle", "adb", "kotlin", "flutter", "go", "cargo",
                 "rustc", "swift", "xcodebuild", "wix", "msbuild"):
        assert name in envcheck.TOOLS
    assert all(len(f) == 1 for f in envcheck.TOOLS.values())            # one fixed flag each: no generic "try version too" fallback


def test_a_tool_is_looked_up_on_path_only_and_never_in_the_current_folder_on_windows(monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(envcheck.shutil, "which", lambda name, path=None: seen.append(path) or str(tmp_path / "java.exe"))
    monkeypatch.setattr(envcheck, "os_name", lambda: "windows")
    monkeypatch.setenv("PATH", "C:\\tools")
    monkeypatch.chdir(tmp_path)
    assert envcheck.probe_tool("java", {}) == {"s": "missing", "v": ""}          # the hit is in the current folder: dropped
    assert seen == ["C:\\tools"]                                                # an explicit PATH was handed to which()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setattr(envcheck.shutil, "which", lambda name, path=None: str(elsewhere / "java.exe"))
    assert envcheck.probe_tool("java", {})["s"] == "ok"                         # found on PATH, outside the current folder


def test_python_aliases_are_a_fixed_list(started, monkeypatch):
    _calls, answers = started
    answers["python"] = done("Python 3.12.3")
    monkeypatch.setattr(envcheck.shutil, "which", lambda name, **k: "/usr/bin/python" if name == "python" else None)
    assert envcheck.probe_tool("python3", {"min_version": "3.11"})["s"] == "ok"      # python3 -> python on a box that only has `python`


@pytest.mark.parametrize("bad", ["../../bin/sh", "java;rm", "a b", "$(id)", "", "-x", "x" * 65, "java\nid"])
def test_tool_names_are_validated_before_any_lookup(started, bad, monkeypatch):
    monkeypatch.setattr(envcheck.shutil, "which", lambda name, **k: pytest.fail("a bad name must never be looked up"))
    assert envcheck.probe_tool(bad, {}) == {"s": "error", "v": "bad name"}


def test_tool_rejects_a_bad_minimum(started):
    assert envcheck.probe_tool("java", {"min_version": "17; id"}) == {"s": "error", "v": "bad version"}


# --- env ---------------------------------------------------------------------------------------------------------------

def test_env_set_points_at_a_folder_or_does_not(monkeypatch, tmp_path):
    monkeypatch.setenv("ANDROID_HOME", str(tmp_path))
    assert envcheck.probe_env("ANDROID_HOME", {})["s"] == "ok"
    monkeypatch.setenv("ANDROID_HOME", str(tmp_path / "gone"))
    assert envcheck.probe_env("ANDROID_HOME", {}) == {"s": "missing", "v": "folder not found"}
    monkeypatch.delenv("ANDROID_HOME")
    assert envcheck.probe_env("ANDROID_HOME", {})["s"] == "missing"
    monkeypatch.setenv("FLAVOUR", "release")
    assert envcheck.probe_env("FLAVOUR", {})["s"] == "ok"                           # not a path: set is enough
    assert envcheck.probe_env("bad name", {})["s"] == "error"


# --- android_sdk_pkg -----------------------------------------------------------------------------------------------------

SDK = """Installed packages:
  Path                 | Version | Description                | Location
  -------              | ------- | -------                    | -------
  build-tools;34.0.0   | 34.0.0  | Android SDK Build-Tools 34 | build-tools/34.0.0
  platforms;android-34 | 3       | Android SDK Platform 34    | platforms/android-34
"""


def test_sdk_packages_parsed_from_list_installed():
    assert envcheck.installed_packages(SDK) == {"build-tools;34.0.0", "platforms;android-34"}


def test_android_sdk_pkg_via_sdkmanager(monkeypatch, tmp_path):
    exe = tmp_path / "cmdline-tools" / "latest" / "bin" / "sdkmanager"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    monkeypatch.setenv("ANDROID_HOME", str(tmp_path))
    monkeypatch.setattr(envcheck, "os_name", lambda: "linux")
    calls = []
    monkeypatch.setattr(envcheck, "_run", lambda argv, timeout, stdin=None: calls.append(argv) or done(SDK))
    assert envcheck.probe_android_sdk_pkg("platforms;android-34", {})["s"] == "ok"
    assert envcheck.probe_android_sdk_pkg("platforms;android-35", {})["s"] == "missing"
    assert calls[0] == [str(exe), "--list_installed"]
    assert envcheck.probe_android_sdk_pkg("a;b c", {})["s"] == "error"


def test_android_sdk_pkg_without_an_sdk(monkeypatch):
    monkeypatch.delenv("ANDROID_HOME", raising=False)
    monkeypatch.delenv("ANDROID_SDK_ROOT", raising=False)
    monkeypatch.setattr(envcheck.shutil, "which", lambda name, **k: None)
    assert envcheck.probe_android_sdk_pkg("platforms;android-34", {}) == {"s": "missing", "v": "no sdkmanager"}


# --- file --------------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["//attacker/share/x", "\\\\attacker\\share\\x", "\\\\?\\C:\\x", "\\\\.\\pipe\\x", "/\\host/x", "\\//host/x", "~/a@host"])
def test_network_and_device_paths_are_never_touched(bad, monkeypatch):
    for plat in ("linux", "windows"):
        monkeypatch.setattr(envcheck, "os_name", lambda plat=plat: plat)
        monkeypatch.setattr(envcheck.os.path, "exists", lambda p: pytest.fail(f"touched {p!r}"))
        monkeypatch.setattr(envcheck.os.path, "isfile", lambda p: pytest.fail(f"touched {p!r}"))
        assert envcheck.probe_file(bad, {})["s"] == "error"
        assert envcheck.probe_keystore(bad, {"fingerprint": FP})["s"] == "error"


@pytest.mark.parametrize("path,ok", [("~/keys/up.jks", True), ("~", True), ("C:\\keys\\up.jks", True), ("c:/keys/up.jks", True),
                                     ("/opt/keys", False), ("keys\\up.jks", False), ("C:keys", False), ("C:\\a\\b:stream", False)])
def test_windows_accepts_only_tilde_or_drive_paths(path, ok, monkeypatch):
    monkeypatch.setattr(envcheck, "os_name", lambda: "windows")
    assert (envcheck._path(path) is not None) is ok


def test_file_exists_with_tilde_expanded(monkeypatch, tmp_path):
    (tmp_path / "keys").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert envcheck.probe_file("~/keys", {})["s"] == "ok"
    assert envcheck.probe_file("~/nothing", {})["s"] == "missing"
    assert envcheck.probe_file("$(id)", {})["s"] == "error"                           # shell characters never reach the filesystem call


# --- keystore ------------------------------------------------------------------------------------------------------------------

KEYTOOL = f"Alias name: upload\nCertificate fingerprints:\n\t SHA1: 11:22\n\t SHA256: {FP}\n"


def test_keystore_matches_by_fingerprint_and_never_sends_the_key(monkeypatch, tmp_path):
    ks = tmp_path / "upload.jks"
    ks.write_bytes(b"SECRET-KEY-MATERIAL")
    calls = []
    monkeypatch.setattr(envcheck, "_keytool", lambda: "/usr/bin/keytool")
    monkeypatch.setattr(envcheck, "_run", lambda argv, timeout, stdin=None: calls.append((argv, stdin)) or done(KEYTOOL))
    assert envcheck.probe_keystore(str(ks), {"fingerprint": FP}) == {"s": "ok", "v": ""}
    assert calls == [(["/usr/bin/keytool", "-list", "-v", "-keystore", str(ks)], "\n")]      # an empty line answers the password prompt
    other = ":".join(["CD"] * 32)
    assert envcheck.probe_keystore(str(ks), {"fingerprint": other}) == {"s": "mismatch", "v": ""}
    result = json.dumps(envcheck.run_probe({"i": 0, "kind": "keystore", "name": str(ks), "args": {"fingerprint": FP}}))
    assert "SECRET-KEY-MATERIAL" not in result and FP not in result                      # nothing of the keystore or its fingerprint is sent back


def test_keystore_missing_file_unreadable_or_no_fingerprint_given(monkeypatch, tmp_path):
    ks = tmp_path / "upload.jks"
    assert envcheck.probe_keystore(str(ks), {"fingerprint": FP})["s"] == "missing"
    ks.write_bytes(b"x")
    monkeypatch.setattr(envcheck, "_keytool", lambda: "/usr/bin/keytool")
    monkeypatch.setattr(envcheck, "_run", lambda argv, timeout, stdin=None: done("keytool error: password was incorrect", rc=1))
    assert envcheck.probe_keystore(str(ks), {"fingerprint": FP}) == {"s": "error", "v": "unreadable"}
    assert envcheck.probe_keystore(str(ks), {}) == {"s": "error", "v": "no fingerprint"}
    assert envcheck.probe_keystore(str(ks), {"fingerprint": "not-a-fingerprint"})["s"] == "error"
    monkeypatch.setattr(envcheck, "_keytool", lambda: None)
    assert envcheck.probe_keystore(str(ks), {"fingerprint": FP}) == {"s": "missing", "v": "no keytool"}


# --- the dispatcher --------------------------------------------------------------------------------------------------------------

def test_an_unknown_kind_is_unsupported_and_nothing_runs(monkeypatch):
    monkeypatch.setattr(envcheck, "_run", lambda *a, **k: pytest.fail("nothing may be started for an unknown kind"))
    monkeypatch.setattr(envcheck.shutil, "which", lambda name, **k: pytest.fail("nor looked up"))
    assert envcheck.run_probe({"i": 3, "kind": "shell", "name": "rm -rf /", "args": {"cmd": "id"}}) == {"i": 3, "s": "unsupported", "v": ""}
    assert envcheck.run_probe({"i": 4, "kind": "tool", "name": ["not", "a", "string"]})["s"] == "error"
    assert envcheck.run_probe({"i": 5})["s"] == "error"


def test_a_row_for_another_os_is_not_applicable(started):
    assert envcheck.run_probe({"i": 0, "kind": "tool", "name": "msbuild", "os": "windows", "args": {}}) == {"i": 0, "s": "na", "v": ""}
    assert envcheck.run_probe({"i": 1, "kind": "tool", "name": "git", "os": "linux", "args": {}})["s"] == "ok"


def test_a_crashing_probe_is_an_error_not_a_dead_check(monkeypatch):
    monkeypatch.setitem(envcheck.PROBES, "tool", lambda name, args: 1 / 0)
    assert envcheck.run_probe({"i": 0, "kind": "tool", "name": "java"}) == {"i": 0, "s": "error", "v": "ZeroDivisionError"}


def test_replies_are_compact_enough_for_a_frame():
    rows = [{"i": i, "kind": "tool", "name": f"tool{i}", "args": {}} for i in range(envcheck.MAX_ROWS + 20)]
    results = envcheck.run_check(rows)
    assert len(results) == envcheck.MAX_ROWS                                              # bounded
    assert len(json.dumps({"type": "env_finish", "id": "r1", "check_id": 1, "results": results, "os": "windows"})) < 4096


def test_os_name_maps_the_platform(monkeypatch):
    for plat, want in (("win32", "windows"), ("darwin", "macos"), ("linux", "linux")):
        monkeypatch.setattr(sys, "platform", plat)
        assert envcheck.os_name() == want


# --- the loop ----------------------------------------------------------------------------------------------------------------------

class FakeLink:
    def __init__(self, checks):
        self.ready, self.env_nudges = asyncio.Event(), asyncio.Event()
        self.ready.set()
        self.checks, self.sent = list(checks), []

    async def rpc(self, mtype, timeout=30.0, **fields):
        self.sent.append((mtype, fields))
        if mtype == "env_claim":
            return {"ok": True, "check": self.checks.pop(0) if self.checks else None}
        return {"ok": True}


def test_sdkmanager_runs_once_per_check_not_once_per_row(monkeypatch, tmp_path):
    exe = tmp_path / "cmdline-tools" / "latest" / "bin" / "sdkmanager"
    exe.parent.mkdir(parents=True)
    exe.write_text("")
    monkeypatch.setenv("ANDROID_HOME", str(tmp_path))
    monkeypatch.setattr(envcheck, "os_name", lambda: "linux")
    calls = []
    monkeypatch.setattr(envcheck, "_run", lambda argv, timeout, stdin=None: calls.append(argv) or done(SDK))
    rows = [{"i": 1, "kind": "android_sdk_pkg", "name": "platforms;android-34"}, {"i": 2, "kind": "android_sdk_pkg", "name": "build-tools;34.0.0"},
            {"i": 3, "kind": "android_sdk_pkg", "name": "platforms;android-35"}]
    assert [r["s"] for r in envcheck.run_check(rows)] == ["ok", "ok", "missing"]
    assert len(calls) == 1
    envcheck.probe_android_sdk_pkg("platforms;android-34", {})              # outside a check nothing is cached
    envcheck.probe_android_sdk_pkg("platforms;android-34", {})
    assert len(calls) == 3


def test_the_answers_echo_each_rows_own_id(monkeypatch):
    monkeypatch.setattr(envcheck, "os_name", lambda: "linux")
    monkeypatch.setattr(envcheck.shutil, "which", lambda name, **k: "/usr/bin/git" if name == "git" else None)
    out = envcheck.run_check([{"i": 902, "kind": "tool", "name": "java"}, {"i": 17, "kind": "tool", "name": "git"}])
    assert out == [{"i": 902, "s": "missing", "v": ""}, {"i": 17, "s": "ok", "v": ""}]


def test_the_loop_claims_checks_and_finishes_over_the_socket(monkeypatch):
    monkeypatch.setattr(envcheck, "os_name", lambda: "linux")
    monkeypatch.setattr(envcheck.shutil, "which", lambda name, **k: "/usr/bin/git" if name == "git" else None)
    link = FakeLink([{"check_id": 7, "project_id": 1, "rows": [{"i": 0, "kind": "tool", "name": "git", "os": "any", "args": {}},
                                                               {"i": 1, "kind": "tool", "name": "java", "os": "any", "args": {}}]}])
    worker = envcheck.EnvWorker(link, idle_poll=0.05)

    async def go():
        task = asyncio.create_task(worker.loop())
        await asyncio.sleep(0.3)
        worker.stopping = True
        link.env_nudges.set()
        await asyncio.wait_for(task, 2)

    asyncio.run(go())
    finish = next(f for t, f in link.sent if t == "env_finish")
    assert finish["check_id"] == 7 and finish["os"] == "linux"
    assert finish["results"] == [{"i": 0, "s": "ok", "v": ""}, {"i": 1, "s": "missing", "v": ""}]
    assert worker.results == [7]
