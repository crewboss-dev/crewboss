"""Task #806: the runner on every OS. `crewboss-runner service install|uninstall|status` per platform (no real systemctl / schtasks /
launchctl: every call is faked), spawn.py's Windows branch (pywinpty / taskkill mocked), and the three OS cards on the Runners page."""
import plistlib
import subprocess
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runner"))

from crewboss_runner import cli, service, spawn  # noqa: E402

EXE = "/home/u/.local/bin/crewboss-runner"


@pytest.fixture
def calls(monkeypatch, tmp_path):
    made = []

    def fake_run(cmd):
        made.append(cmd)
        rc = 1 if cmd[:2] == ["sc.exe", "query"] else 0   # no Windows service installed (#972)
        return subprocess.CompletedProcess(cmd, rc, "Status: Running\n" if "/Query" in cmd else "enabled\n", "")

    monkeypatch.setattr(service, "_run", fake_run)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(service, "runner_path", lambda: EXE)
    return made


def test_detect(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert service.detect() == "windows"
    monkeypatch.setattr(sys, "platform", "darwin")
    assert service.detect() == "macos"
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    assert service.detect() == "wsl"


def test_linux_install_writes_unit(calls, tmp_path):
    assert service.install("linux") == 0
    unit = (tmp_path / ".config/systemd/user/crewboss-runner.service").read_text()
    assert f"ExecStart={EXE} run" in unit and "Restart=on-failure" in unit and "WantedBy=default.target" in unit
    assert ["systemctl", "--user", "enable", "--now", "crewboss-runner"] in calls
    assert service.uninstall("linux") == 0
    assert not (tmp_path / ".config/systemd/user/crewboss-runner.service").exists()
    assert service.status("linux") == 0


def test_macos_plist(calls, tmp_path):
    assert service.install("macos") == 0
    data = plistlib.loads((tmp_path / "Library/LaunchAgents/dev.crewboss.runner.plist").read_bytes())
    assert data["ProgramArguments"] == [EXE, "run"] and data["RunAtLoad"] is True and data["KeepAlive"] == {"SuccessfulExit": False}
    assert data["StandardOutPath"].endswith("Library/Logs/crewboss-runner.log")
    assert any(c[:2] == ["launchctl", "bootstrap"] and c[2].startswith("gui/") for c in calls)
    service.uninstall("macos")
    assert not (tmp_path / "Library/LaunchAgents/dev.crewboss.runner.plist").exists()


def test_windows_native_task(calls):
    assert service.install("windows") == 0
    create = next(c for c in calls if c[:2] == ["schtasks", "/Create"])
    assert create[0] == "schtasks" and "/SC" in create and create[create.index("/SC") + 1] == "ONLOGON"
    assert create[create.index("/RL") + 1] == "LIMITED" and create[create.index("/TN") + 1] == "Crewboss runner"
    assert create[create.index("/TR") + 1] == f'"{EXE}" run'
    assert ["schtasks", "/Run", "/TN", "Crewboss runner"] in calls
    assert service.uninstall("windows") == 0 and ["schtasks", "/Delete", "/TN", "Crewboss runner", "/F"] in calls


def test_wsl_uses_windows_task(calls, monkeypatch):
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    monkeypatch.setattr(service.shutil, "which", lambda n: "/mnt/c/Windows/System32/schtasks.exe")
    assert service.install("wsl") == 0
    assert calls[0][0] == "schtasks.exe" and calls[0][calls[0].index("/TR") + 1] .startswith("wsl.exe -d Ubuntu -- ") and calls[0][calls[0].index("/TR") + 1].endswith("start.sh")
    calls.clear()
    assert service.install("wsl", systemd=True) == 0   # --systemd: the plain unit instead
    assert calls[0] == ["systemctl", "--user", "daemon-reload"]


def test_dry_run_does_nothing(calls, tmp_path, capsys):
    for plat in ("linux", "macos", "windows", "wsl"):
        assert service.install(plat, dry=True) == 0
        assert service.uninstall(plat, dry=True) == 0
    assert calls == [] and not list(tmp_path.rglob("*.service")) and not list(tmp_path.rglob("*.plist"))
    out = capsys.readouterr().out
    assert "ExecStart=" in out and "schtasks" in out and "launchctl bootstrap" in out


def test_cli_service_subcommand(calls, monkeypatch, capsys):
    monkeypatch.setattr(service, "detect", lambda: "linux")
    assert cli.main(["service", "install", "--dry-run"]) == 0
    assert "systemctl --user enable --now crewboss-runner" in capsys.readouterr().out
    assert cli.main(["service", "status"]) == 0


# ---- spawn.py on Windows ----

class FakePty:
    pid = 4242
    exitstatus = None

    def __init__(self):
        self.alive, self.written, self.reads = True, [], ["hello"]

    def isalive(self):
        return self.alive

    def read(self, n):
        if self.reads:
            return self.reads.pop(0)
        self.alive = False
        raise EOFError

    def write(self, s):
        self.written.append(s)


def test_agent_windows_pty(monkeypatch, tmp_path):
    fake = FakePty()
    spawned = {}
    winpty = types.ModuleType("winpty")
    winpty.PtyProcess = types.SimpleNamespace(spawn=lambda argv, cwd=None, env=None: spawned.update(argv=argv, cwd=cwd) or fake)
    monkeypatch.setitem(sys.modules, "winpty", winpty)
    monkeypatch.setattr(spawn.os, "name", "nt")
    log = tmp_path / "agent.log"
    agent = spawn.Agent(["claude", "x"], str(tmp_path), {}, True, str(log))
    assert spawned["argv"] == ["claude", "x"] and agent.send(b"y\r") and fake.written == ["y\r"]
    agent.wait()
    assert agent.proc.poll() == 0
    import time
    for _ in range(50):
        if log.exists() and log.read_bytes() == b"hello":
            break
        time.sleep(0.05)
    assert log.read_bytes() == b"hello"


def test_kill_tree_windows(monkeypatch):
    monkeypatch.setattr(spawn.os, "name", "nt")
    monkeypatch.setattr(spawn.signal, "CTRL_BREAK_EVENT", 1, raising=False)
    ran = []
    monkeypatch.setattr(spawn.subprocess, "run", lambda cmd, **kw: ran.append(cmd))

    class P:
        pid = 77
        def poll(self): return None
        def wait(self, timeout=None): raise subprocess.TimeoutExpired("x", timeout)

    monkeypatch.setattr(spawn.os, "kill", lambda pid, sig: None)
    spawn.kill_tree(P(), grace=0.01)   # CTRL_BREAK is ignored (timeout), so the tree is force-killed
    assert ["taskkill", "/T", "/F", "/PID", "77"] in ran


def test_posix_group_kwargs():
    assert spawn.new_group_kwargs() == {"start_new_session": True}


# ---- the Runners page ----

def test_howto_three_cards_and_tabs():
    from app.templating import templates
    html = templates.env.get_template("partials/runner_howto.html").render(devices=[])
    for key in ("linux", "windows", "macos"):
        assert f'data-os-pick="{key}"' in html and f'data-os="{key}"' in html
    assert 'data-wtab-pick="wsl"' in html and 'data-wtab-pick="ps"' in html and "PowerShell" in html and "WSL2" in html
    assert "crewboss-runner service install" in html and "usbipd attach --wsl" in html and "Create dev drive" in html
    assert "irm https://claude.ai/install.ps1 | iex" in html and "brew install --cask claude-code" in html
    # the default Windows tab is the one word `win_default` (PowerShell for now)
    ps = html.split('data-wtab-pick="ps"')[0].rsplit("<button", 1)[1]
    assert 'aria-selected="true"' in ps   # PowerShell is the default Windows tab (win_default)
    assert html.count('role="tab" class="os-card" id="os-tab-linux" aria-selected="true"') == 1
    assert "<details" in html and " open>" in html.split('data-section="howto"')[1].split(">")[0] + ">"


def test_howto_repo_address_and_windows_commands():
    """The clone lines show this install's repo (HTTPS), never the ORG placeholder; Windows uses the py launcher (#806)."""
    from app.templating import templates
    tpl = templates.env.get_template("partials/runner_howto.html")
    html = tpl.render(devices=[], runner_repo="https://github.com/Acme/Crewboss.git")
    assert html.count("git clone https://github.com/Acme/Crewboss.git") == 4 and "ORG/crewboss" not in html
    assert "py -m pip install --user pipx" in html and "py -m pipx install .\\runner" in html
    assert "setx PATH" not in html.replace("Don't use <code class=\"font-mono\">setx PATH</code>", "")
    assert "git clone https://github.com/ORG/crewboss.git" in tpl.render(devices=[])   # neutral fallback
    # every command block has its copy icon; Android uses Studio's bundled Java (no separate JDK)
    assert html.count("data-copy") == html.count("<pre ") > 10 and 'aria-label="Copy to clipboard"' in html
    assert "Temurin" not in html and 'JAVA_HOME", $jbr' in html
    # the repo is private and GitHub refuses passwords for git: every Unix tab signs in with gh first (owner 11-16, Omarchy laptop)
    assert "gh auth login" not in html and "github-cli" in html   # a public repo: no sign-in step
    priv = tpl.render(devices=[], runner_repo="https://github.com/Acme/Crewboss.git", runner_repo_private=True)
    assert priv.count("gh auth login\ngh auth setup-git") == 3


# ---- the autostart's PATH (service managers start with a bare one) ----

def test_baked_path_everywhere(calls, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PATH", "/opt/my tools/bin:/usr/bin:/odd%dir")
    monkeypatch.setattr(service.shutil, "which", lambda n: "/home/u/.nvm/bin/claude" if n == "claude" else None)
    path, found = service.baked_path(EXE)
    assert found and path.split(":")[:2] == ["/home/u/.nvm/bin", "/home/u/.local/bin"] and "/opt/my tools/bin" in path
    service.install("linux")
    unit = (tmp_path / ".config/systemd/user/crewboss-runner.service").read_text()
    assert 'Environment="PATH=' + path.replace("%", "%%") + '"' in unit and "/odd%%dir" in unit
    service.install("macos")
    plist = plistlib.loads((tmp_path / "Library/LaunchAgents/dev.crewboss.runner.plist").read_bytes())
    assert plist["EnvironmentVariables"]["PATH"] == path
    assert f"PATH baked into the autostart: {path}" in capsys.readouterr().out


def test_missing_claude_warns(calls, monkeypatch, capsys):
    monkeypatch.setattr(service.shutil, "which", lambda n: None)
    service.install("linux")
    assert "install Claude Code first" in capsys.readouterr().err


def test_wsl_wrapper_script(calls, tmp_path, monkeypatch):
    monkeypatch.setenv("WSL_DISTRO_NAME", "Ubuntu")
    monkeypatch.setenv("PATH", "/mnt/c/Program Files/x:/it's/bin:/usr/bin")
    monkeypatch.setattr(service.shutil, "which", lambda n: "/x/schtasks.exe" if n == "schtasks.exe" else None)
    assert service.install("wsl") == 0
    wrapper = tmp_path / ".local/share/crewboss-runner/start.sh"
    text = wrapper.read_text()
    assert text.startswith("#!/bin/sh\nexport PATH='") and "it'\\''s/bin" in text
    assert f"exec '{EXE}' run" in text and oct(wrapper.stat().st_mode & 0o777) == "0o700"
    tr = calls[0][calls[0].index("/TR") + 1]
    assert tr == f"wsl.exe -d Ubuntu -- {wrapper}" and len(tr) <= 261


def test_wsl_tr_too_long_fails(calls, monkeypatch, capsys):
    monkeypatch.setenv("WSL_DISTRO_NAME", "D" * 300)
    monkeypatch.setattr(service.shutil, "which", lambda n: "/x/schtasks.exe")
    assert service.install("wsl") == 1 and "261" in capsys.readouterr().err and calls == []


def test_howto_profile_line_for_a_second_server():
    """#975: the enrol step tells a PC that already has a runner to add this Crewboss as a profile, with a suggested name."""
    from app.templating import templates
    tpl = templates.env.get_template("partials/runner_howto.html")
    html = tpl.render(devices=[], profile_hint="acme-dev")
    assert "Already have a runner for another Crewboss?" in html
    assert "crewboss-runner enrol '&lt;code&gt;' --name acme-dev" in html and "crewboss-runner use acme-dev" in html
    assert "--name work" in tpl.render(devices=[])   # no suggestion: a neutral name
    assert "--runner-name" in html
    assert "can read the others' keys and repositories" in html
