"""The commit guard (scripts/commit_guard.py, scripts/hooks/pre-commit): tracked files must carry no instance-only value
(a line of $CREWBOSS_HOME/denylist.txt) and no secret-shaped string, except the paths scripts/denylist-allow.txt lists as
waiting for a later scrub slice. The unit tests use a tmp repo and tmp denylist; only the last test reads the real
denylist (conftest keeps its folder in CREWBOSS_OUTER_HOME) and it skips when there is none."""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("commit_guard", ROOT / "scripts" / "commit_guard.py")
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


def _repo(tmp_path, files: dict):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    return tmp_path


def test_a_denylist_value_matches_case_insensitively_and_is_never_echoed(tmp_path):
    r = _repo(tmp_path, {"a.py": "host = 'Box.Secret-Corp.test'\n", "b.py": "nothing here\n"})
    found = guard.scan(guard.tracked(r), ["box.secret-corp.test"], r)
    assert list(found) == ["a.py"] and found["a.py"][0][0] == 1
    assert "secret-corp" not in found["a.py"][0][1]


@pytest.mark.parametrize("line", [
    "-----BEGIN RSA " + "PRIVATE KEY-----",
    "key = AK" + "IAABCDEFGHIJKLMNOP",
    "t = gh" + "p_" + "a1" * 20,
    "api" + "_key = '" + "abcdefghijklmnopqrstuvwxyz012345" + "'",
    "url = postgres://user:" + "hunter2hunter2@db.example.com/x",
])
def test_builtin_secret_patterns_match(tmp_path, line):
    r = _repo(tmp_path, {"c.txt": line + "\n"})
    assert guard.scan(guard.tracked(r), [], r)


def test_ordinary_text_does_not_match(tmp_path):
    r = _repo(tmp_path, {"c.txt": "token = os.environ['TOKEN']\npostgresql://dashboard:dashboard@db:5432/x\n"})
    assert not guard.scan(guard.tracked(r), [], r)


def test_allowlist_files_and_folders_and_stale_entries():
    found = {"x/y.py": [(1, "r")], "z.py": [(2, "r")]}
    assert guard.allowed("x/y.py", ["x/y.py"]) and guard.allowed("x/y.py", ["x/"]) and not guard.allowed("z.py", ["x/"])
    assert guard.stale(found, ["x/", "gone.py"], []) == ["gone.py"]


def _run(tmp_path, home, *args):
    env = {**os.environ, "CREWBOSS_HOME": str(home)}
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "commit_guard.py"), "--no-gitleaks", *args], cwd=ROOT, env=env,
                          capture_output=True, text=True)


def test_the_script_fails_on_a_blocked_match_in_the_real_repo(tmp_path):
    (tmp_path / "denylist.txt").write_text("# comment\n\ncrewboss\n")     # a word every tracked file here uses
    r = _run(tmp_path, tmp_path)
    assert r.returncode == 1 and "denylist entry #1" in r.stderr


def test_the_hook_is_executable_and_runs_the_guard():
    hook = ROOT / "scripts" / "hooks" / "pre-commit"
    assert os.access(hook, os.X_OK) and "commit_guard.py" in hook.read_text()
    assert os.access(ROOT / "scripts" / "install-hooks.sh", os.X_OK)


def test_tracked_files_have_no_secret_shaped_strings_outside_the_allowlist(tmp_path):
    assert _run(tmp_path, tmp_path).returncode == 0       # an empty home: the built-in patterns only


OUTER = Path(os.environ.get("CREWBOSS_OUTER_HOME", ""))


@pytest.mark.skipif(not (OUTER / "denylist.txt").is_file(), reason="no denylist.txt in CREWBOSS_HOME on this box")
def test_tracked_files_carry_no_instance_only_value_outside_the_allowlist():
    r = _run(OUTER, OUTER, "--strict")
    assert r.returncode == 0, r.stderr


# ---- gitleaks (third check): optional in a pre-commit, required in CI ------------------------------------------------

def _fake_gitleaks(tmp_path, body: str):
    exe = tmp_path / "bin" / "gitleaks"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n" + body + "\n")
    exe.chmod(0o755)
    return exe.parent


def test_gitleaks_missing_is_skipped_locally_but_fails_clearly_when_required(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PATH", str(tmp_path))                      # no gitleaks anywhere
    assert guard.run_gitleaks(tree=False, required=False) == 0
    assert "gitleaks not installed" in capsys.readouterr().err
    assert guard.run_gitleaks(tree=False, required=True) == 1
    err = capsys.readouterr().err
    assert "required" in err and "install gitleaks" in err


def test_gitleaks_required_by_ci_or_the_env_switch(monkeypatch):
    for k in ("CI", "CREWBOSS_REQUIRE_GITLEAKS"):
        monkeypatch.delenv(k, raising=False)
    assert not guard.gitleaks_required()
    monkeypatch.setenv("CI", "true")
    assert guard.gitleaks_required()
    monkeypatch.delenv("CI")
    monkeypatch.setenv("CREWBOSS_REQUIRE_GITLEAKS", "1")
    assert guard.gitleaks_required()


def test_gitleaks_clean_and_findings_exit_codes_and_config_is_passed(tmp_path, monkeypatch, capsys):
    log = tmp_path / "args.txt"
    monkeypatch.setenv("PATH", str(_fake_gitleaks(tmp_path, f'echo "$@" > {log}; exit 0')) + os.pathsep + os.environ["PATH"])
    assert guard.run_gitleaks(tree=False, required=False) == 0
    args = log.read_text()
    assert "--staged" in args and "--redact" in args and ".gitleaks.toml" in args
    assert guard.run_gitleaks(tree=True, required=False) == 0 and "dir" in log.read_text()
    monkeypatch.setenv("PATH", str(_fake_gitleaks(tmp_path / "x", 'echo "Finding: ****"; exit 1')) + os.pathsep + os.environ["PATH"])
    assert guard.run_gitleaks(tree=False, required=False) == 1
    assert "possible secret" in capsys.readouterr().err


def test_gitleaks_older_release_spelling_is_tried(tmp_path, monkeypatch):
    body = 'case "$1" in git|dir) echo "unknown command \\"$1\\""; exit 1;; *) exit 0;; esac'
    monkeypatch.setenv("PATH", str(_fake_gitleaks(tmp_path, body)) + os.pathsep + os.environ["PATH"])
    assert guard.run_gitleaks(tree=False, required=True) == 0       # falls back to `protect --staged`
    assert guard.run_gitleaks(tree=True, required=True) == 0        # falls back to `detect --no-git`


def test_the_gitleaks_config_exists_and_extends_the_default_rules():
    cfg = (ROOT / ".gitleaks.toml").read_text()
    assert "useDefault = true" in cfg and "crewboss-api-token" in cfg


def test_gitleaks_runs_from_the_repo_root_with_a_relative_source_and_unanchored_allowlist(tmp_path, monkeypatch):
    log = tmp_path / "args.txt"
    monkeypatch.setenv("PATH", str(_fake_gitleaks(tmp_path, f'echo "$PWD $@" > {log}; exit 0')) + os.pathsep + os.environ["PATH"])
    assert guard.run_gitleaks(tree=True, required=True) == 0
    assert log.read_text().split()[0] != str(guard.ROOT) and " . " in log.read_text()   # tree scan runs in a tracked-files export
    assert "(^|/)tests/test_deploys" in (ROOT / ".gitleaks.toml").read_text()


def test_gitleaks_tree_scan_sees_only_tracked_files(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    for cmd in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", *cmd], cwd=repo, check=True)
    (repo / "kept.txt").write_text("hello")
    (repo / "sub").mkdir()
    (repo / "sub" / "also.txt").write_text("hi")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "x"], cwd=repo, check=True)
    (repo / ".env").write_text("SECRET=not-for-gitleaks")          # untracked: must never reach gitleaks
    (repo / "untracked.py").write_text("x = 1")
    log = tmp_path / "seen.txt"
    monkeypatch.setenv("PATH", str(_fake_gitleaks(tmp_path, f'find . -type f | sort > {log}; exit 0')) + os.pathsep + os.environ["PATH"])
    assert guard.run_gitleaks(tree=True, required=True, root=repo) == 0
    assert log.read_text().split() == ["./kept.txt", "./sub/also.txt"]
