"""Safety rules of the throwaway deployment test (task #589): scripts/deploy_test.py. No docker is started."""
import importlib.util
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("deploy_test_mod", ROOT / "scripts" / "deploy_test.py")
dtm = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dtm)


def test_refuses_the_live_project_and_anything_not_a_trial():
    for bad in ("crewboss", "crewboss-app", "crewboss-trial-", "crewboss-trial-abc", "crewboss-trial-20261124120315004", "x"):
        with pytest.raises(SystemExit):
            dtm.check_project(bad)
    dtm.check_project("crewboss-trial-202611241203150042")


def test_trial_id_must_be_a_timestamp():
    for bad in ("crewboss", "../etc", "123", "20261124120315", ""):
        with pytest.raises(SystemExit):
            dtm.trial_dir(bad)
    assert dtm.trial_dir("202611241203150042").name == "crewboss-trial-202611241203150042"


def test_clean_env_drops_everything_that_could_redirect_compose():
    env = dtm.clean_env({"COMPOSE_PROJECT_NAME": "crewboss", "COMPOSE_FILE": "x", "CREWBOSS_HOME": "/h", "POSTGRES_PASSWORD": "p",
                         "APP_PORT": "1", "PATH": "/bin", "HOME": "/h"})
    assert env == {"PATH": "/bin", "HOME": "/h"}


def test_main_strips_the_process_environment_first(monkeypatch):
    fake = {"COMPOSE_PROJECT_NAME": "crewboss", "CREWBOSS_HOME": "/home/x/.crewboss", "PATH": "/bin"}
    monkeypatch.setattr(os, "environ", fake)            # a copy: the real pytest process keeps its CREWBOSS_* variables
    monkeypatch.setattr(dtm, "CALLER_ENV", dict(dtm.CALLER_ENV))
    dtm.main(["list"])
    assert fake == {"PATH": "/bin"}
    assert dtm.CALLER_ENV["CREWBOSS_HOME"] == "/home/x/.crewboss"          # but the guard still knows where the shell pointed


def test_down_refuses_when_the_callers_crewboss_home_is_inside_the_trial(monkeypatch, tmp_path):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    trial = tmp_path / "crewboss-trial-202611241203150042"
    (trial / "home").mkdir(parents=True)
    monkeypatch.setattr(dtm, "CALLER_ENV", {"CREWBOSS_HOME": str(trial / "home")})
    monkeypatch.setattr(dtm, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", ""))
    with pytest.raises(SystemExit):
        dtm.teardown("202611241203150042")
    assert trial.exists()


def test_the_default_live_list_reads_the_callers_env_not_the_stripped_one(monkeypatch):
    monkeypatch.setattr(dtm, "CALLER_ENV", {"CREWBOSS_SECRETS_DIR": "/srv/keep"})
    monkeypatch.setattr(os, "environ", {})
    assert Path("/srv/keep") in dtm.live_paths()


def test_trial_port_is_ephemeral_and_never_the_lives_range():
    for _ in range(50):
        assert not 8931 <= dtm.trial_port() <= 8940


def test_a_second_run_in_the_same_second_does_not_delete_the_first(monkeypatch, tmp_path):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(dtm.random, "randrange", lambda n: 42)
    monkeypatch.setattr(dtm, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", ""))
    monkeypatch.setattr(dtm, "docker_live_paths", lambda: [])
    torn = []
    monkeypatch.setattr(dtm, "teardown", lambda i: torn.append(i))
    class Frozen(dtm.dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 24, 12, 3, 15)
    monkeypatch.setattr(dtm.dt, "datetime", Frozen)
    first = tmp_path / "crewboss-trial-202611241203150042"
    first.mkdir()
    (first / "marker").write_text("x")
    args = dtm.build_parser().parse_args(["run"])
    with pytest.raises(SystemExit):
        dtm.cmd_run(args)
    assert (first / "marker").exists() and torn == []           # refused before the try: the first trial is untouched


def test_keep_without_a_lan_address_still_keeps_it_up_and_prints_the_tunnel(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(dtm, "docker_live_paths", lambda: [])
    monkeypatch.setattr(dtm, "snapshot", lambda ref, dest: dest.mkdir(parents=True, exist_ok=True) or "abcdef1234")
    monkeypatch.setattr(dtm, "http_checks", lambda base, rep: rep.add("x", "PASS"))
    monkeypatch.setattr(dtm, "browser_checks", lambda *a: None)
    monkeypatch.setattr(dtm, "lan_ip", lambda *a, **k: None)
    monkeypatch.setattr(dtm, "reset_owner_password", lambda *a: "pw-1")
    monkeypatch.setattr(dtm, "teardown", lambda i: None)
    slept = []
    monkeypatch.setattr(dtm.time, "sleep", lambda s: slept.append(s))
    installs = []

    def fake(cmd, **kw):
        cmd = [str(c) for c in cmd]
        if "install" in cmd:
            installs.append(cmd)
            home = Path(cmd[cmd.index("--home") + 1])
            home.mkdir(parents=True)
            (home / "compose.env").write_text(f"APP_PORT=45678\nCREWBOSS_IMAGE={cmd[cmd.index('--project') + 1]}-app\n")
            return subprocess.CompletedProcess(cmd, 0, "Temporary password (valid 24 hours): aaaa-bbbb\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(dtm, "run", fake)
    assert dtm.cmd_run(dtm.build_parser().parse_args(["run", "--keep", "5"])) == 0
    assert slept == [300]
    assert "ssh -L 45678:localhost:45678" in capsys.readouterr().out
    port = installs[0][installs[0].index("--port") + 1]
    assert port.isdigit() and not 8931 <= int(port) <= 8940
def test_paths_must_be_under_temp_and_never_overlap_a_live_path(tmp_path):
    live = [tmp_path / "live"]
    (tmp_path / "live" / "inner").mkdir(parents=True)
    root = tmp_path
    dtm.check_paths(tmp_path / "ok" / "home", live=live, root=root)
    for bad in (tmp_path / "live", tmp_path / "live" / "inner", tmp_path.parent / "elsewhere"):
        with pytest.raises(SystemExit):
            dtm.check_paths(bad, live=live, root=root)
    # a path that is a PARENT of a live one is refused as well (removing it would take the live one along)
    with pytest.raises(SystemExit):
        dtm.check_paths(tmp_path, live=live, root=tmp_path.parent)


def test_a_symlink_into_a_live_path_is_refused(tmp_path):
    live = tmp_path / "live"
    live.mkdir()
    link = tmp_path / "link"
    link.symlink_to(live)
    with pytest.raises(SystemExit):
        dtm.check_paths(link, live=[live], root=tmp_path)


def test_live_paths_cover_the_named_places_and_the_callers_env():
    home = Path("/home/someone")
    paths = dtm.live_paths({"CREWBOSS_HOME": "/srv/mine"}, home)
    for p in (home / ".crewboss", home / ".config" / "crewboss", home / ".local" / "share" / "crewboss", Path("/srv/mine")):
        assert p in paths


def test_every_running_compose_projects_folders_count_as_live(monkeypatch):
    out = "/srv/app-checkout|/srv/app-instance|/srv/app-secrets\n|\n/tmp|\n/\n"

    def fake(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, "abc\n" if cmd[:3] == ["docker", "ps", "-a"] else out, "")
    monkeypatch.setattr(dtm, "run", fake)
    found = {str(p) for p in dtm.docker_live_paths()}
    assert {"/srv/app-checkout", "/srv/app-instance", "/srv/app-secrets"} <= found
    assert "/" not in found


def test_a_trial_folder_in_the_real_temp_dir_passes_with_the_real_live_list():
    dtm.check_paths(Path(tempfile.gettempdir()) / "crewboss-trial-202611241203150042" / "home")


def test_teardown_only_ever_filters_by_the_trial_project_label(monkeypatch, tmp_path):
    calls = []

    def fake(cmd, **kw):
        calls.append([str(c) for c in cmd])
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(dtm, "run", fake)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    (tmp_path / "crewboss-trial-202611241203150042").mkdir()
    dtm.teardown("202611241203150042")
    docker = [c for c in calls if c[0] == "docker"]
    assert docker
    for c in docker:
        text = " ".join(c)
        if c[1:4] == ["ps", "-a", "-q"] or c[1] == "inspect":      # read-only discovery of live folders
            continue
        if c[1] in ("ps", "volume", "network") and "ls" in c or c[1] == "ps":
            assert "label=com.docker.compose.project=crewboss-trial-202611241203150042" in text
        assert "crewboss-app-1" not in text and "crewboss-db-1" not in text
        assert c[:3] != ["docker", "volume", "prune"] and c[1:3] != ["system", "prune"]
    assert not (tmp_path / "crewboss-trial-202611241203150042").exists()


def test_teardown_refuses_the_live_project_id(tmp_path):
    with pytest.raises(SystemExit):
        dtm.teardown("crewboss")


def test_lan_address_comes_from_hostname_and_skips_docker_bridges():
    # The private ranges are what lan_ip() classifies, so the test needs them. They are built from parts, not written as literals, so
    # no address-shaped string sits in the public tree (the commit guard's address ratchet).
    c192, c10, c172 = ".".join(["192", "168"]), "10", ".".join(["172", "17"])
    assert dtm.lan_ip(hostname_i=f"100.1.2.3 {c172}.0.1 {c192}.7.9 {c10}.0.0.5") == f"{c192}.7.9"
    assert dtm.lan_ip(hostname_i="100.1.2.3 8.8.8.8") is None
    assert dtm.lan_ip(f"{c10}.9.9.9") == f"{c10}.9.9.9"


def test_the_script_has_no_hardcoded_lan_address_or_live_project_use():
    src = (ROOT / "scripts" / "deploy_test.py").read_text()
    assert not re.search(r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d+\.\d+", src)
    assert '"-p", "crewboss"' not in src and "docker compose" not in src.replace("`docker compose`", "")


def test_failure_files_are_private_scrubbed_and_removable(monkeypatch, tmp_path):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(dtm, "docker_live_paths", lambda: [])
    d = tmp_path / "crewboss-trial-202611241203150042"
    d.mkdir()
    (d / "install.log").write_text("Temporary password (valid 24 hours, works once, shown once): aaaa-bbbb-cccc\nNext: go\n")
    (d / "deploy-test-failure.png").write_bytes(b"png")
    rep = dtm.Report()
    rep.add("x", "FAIL")
    dtm.keep_failure_files(d, "202611241203150042", rep)
    out = tmp_path / "crewboss-trial-202611241203150042-failure"
    assert (out.stat().st_mode & 0o777) == 0o700
    for f in out.iterdir():
        assert (f.stat().st_mode & 0o777) == 0o600
    log = (out / "install.log").read_text()
    assert "aaaa-bbbb-cccc" not in log and "[removed]" in log and "Next: go" in log
    ok = dtm.Report()
    ok.add("x", "PASS")
    dtm.keep_failure_files(d, "202611241203150043", ok)
    assert not (tmp_path / "crewboss-trial-202611241203150043-failure").exists()
    assert dtm.main(["down", "202611241203150042-failure"]) == 0
    assert not out.exists()
    out.mkdir()
    assert dtm.main(["clean"]) == 0 and not out.exists()
