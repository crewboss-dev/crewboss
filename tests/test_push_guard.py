"""The push guard (scripts/push_guard.py via scripts/hooks/pre-push) and the generic public denylist in scripts/commit_guard.py.
Each test builds a scratch git repo in tmp_path with a bare repo as its remote; no database or network is needed
(run with `python -m pytest --noconftest tests/test_push_guard.py` to skip the suite's Postgres container).
Addresses and keys are assembled from pieces so this file itself carries none."""
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("commit_guard", ROOT / "scripts" / "commit_guard.py")
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

LAN = "192." + "168.1.20"
TEN = "10." + "1.2.3"
CGNAT = "100." + "64.0.9"
PRIVATE_KEY = "-----BEGIN RSA " + "PRIVATE KEY-----"
ZERO = "0" * 40


@pytest.mark.parametrize("text", [LAN, TEN, CGNAT, "172." + "16.0.1", "172." + "31.255.254", "host=" + TEN + ":22", "100." + "127.1.1",
                                  "10." + "0.0.0/8",
                                  # item 9: three-octet prefixes, regex-escaped dots, leading zeros
                                  "10." + "240.0.", "172." + "29.100.", "192." + "168.7.", "100." + "64.1.", "10\\." + "240\\.0\\.7",
                                  "010." + "001.002.003", "172." + "016.0.1",
                                  'x.startswith("10.' + '240.0.")', "'172." + "29.100.'", "ssh admin@10." + "1.2.3"])
def test_internal_addresses_match(text):
    assert guard.INTERNAL_IP.search(text)


@pytest.mark.parametrize("text", ["192.0.2.7", "198.51.100.4", "203.0.113.9", "127.0.0.1", "8.8.8.8", "172." + "15.0.1",
                                  "172." + "32.0.1", "100." + "63.1.1", "100." + "128.1.1", "1.10." + "0.0.1", "version 10." + "1.2.3.4",
                                  "10." + "300.1.1", "0.0.0.0", "10." + "1.2.300",
                                  # version strings are not addresses (item 3 of the second review)
                                  "eslint-10." + "2.0.tgz", "foo-10." + "4.1.tar.gz", "setuptools-100." + "64.1.dist-info", "10." + "0.0.dev1",
                                  "ip-address-10." + "7.2.tgz", "eslint@10." + "2.0.", "v10." + "2.0."])
def test_documentation_public_and_non_addresses_do_not_match(text):
    assert not guard.INTERNAL_IP.search(text)


def _git(cwd, *args, check=True):
    return subprocess.run(["git", "-c", "user.email=t@example.test", "-c", "user.name=t", *args], cwd=cwd, check=check,
                          capture_output=True, text=True)


class Scratch:
    def __init__(self, tmp_path, hooked=False, push_base=True):
        self.work = tmp_path / "work"
        self.remote = tmp_path / "remote.git"
        self.home = tmp_path / "home"
        self.home.mkdir()
        subprocess.run(["git", "init", "-q", "--bare", str(self.remote)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.work)], check=True)
        _git(self.work, "remote", "add", "origin", str(self.remote))
        (self.work / "README.md").write_text("hello\n")
        _git(self.work, "add", "-A")
        _git(self.work, "commit", "-qm", "base")
        if push_base:
            _git(self.work, "push", "-q", "origin", "main")
        if hooked:                                       # the real scripts, untracked in the scratch repo, installed as its hooks
            shutil.copytree(ROOT / "scripts", self.work / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
            (self.work / ".git" / "info" / "exclude").write_text("scripts/\n")
            (self.work / "scripts" / "internal-ip-baseline.json").unlink()      # the real repo's counts mean nothing here
            _git(self.work, "config", "core.hooksPath", "scripts/hooks")

    def commit(self, files: dict, msg="change"):
        for rel, text in files.items():
            p = self.work / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(text) if isinstance(text, bytes) else p.write_text(text)
        _git(self.work, "add", "-A", "-f")
        _git(self.work, "commit", "-qm", msg)
        return _git(self.work, "rev-parse", "HEAD").stdout.strip()

    def env(self):
        env = {k: v for k, v in os.environ.items() if k not in ("CI", "CREWBOSS_REQUIRE_GITLEAKS")}
        env["CREWBOSS_HOME"] = str(self.home)
        return env

    def guard(self, local, remote="remote", local_ref="refs/heads/main", remote_ref="refs/heads/main", argv=("origin",)):
        """Run the guard as git would: a line per pushed ref on stdin, the remote's name (and URL) as arguments."""
        remote_sha = _git(self.work, "rev-parse", "origin/main").stdout.strip() if remote == "remote" else ZERO
        line = f"{local_ref} {local} {remote_ref} {remote_sha}\n"
        return subprocess.run([sys.executable, str(ROOT / "scripts" / "push_guard.py"), *argv], cwd=self.work, input=line,
                              capture_output=True, text=True, env=self.env())


@pytest.fixture
def repo(tmp_path):
    return Scratch(tmp_path)


def test_a_clean_push_passes(repo):
    sha = repo.commit({"app.py": "print('hi')\n", "notes.md": "uses 192.0.2.7 as an example\n"})
    r = repo.guard(sha)
    assert r.returncode == 0, r.stderr
    assert "BLOCKED" not in r.stderr


def test_an_env_file_blocks_and_is_named_but_not_its_value(repo):
    sha = repo.commit({"config/.env": "SECRET_THING=hunter2hunter2\n"})
    r = repo.guard(sha)
    assert r.returncode == 1
    assert "config/.env" in r.stderr and "PUSH BLOCKED" in r.stderr
    assert "hunter2" not in r.stdout + r.stderr


@pytest.mark.parametrize("name", ["backup.bak", "db.dump", "data.sqlite", "cert.pem", "keys/id_ed25519", ".env.production"])
def test_other_forbidden_file_names_block(repo, name):
    sha = repo.commit({name: "x\n"})
    r = repo.guard(sha)
    assert r.returncode == 1 and name in r.stderr


@pytest.mark.parametrize("name", [".env.example", "id_rsa.pub", "settings.env.sample", "docs/idea.md", "identity.py"])
def test_templates_public_keys_and_lookalikes_pass(repo, name):
    sha = repo.commit({name: "x\n"})
    assert repo.guard(sha).returncode == 0


def test_an_internal_address_blocks(repo):
    sha = repo.commit({"deploy.sh": f"ssh admin@{LAN}\n"})
    r = repo.guard(sha)
    assert r.returncode == 1
    assert "deploy.sh" in r.stderr and LAN not in r.stdout + r.stderr


def test_an_address_added_then_removed_in_a_later_commit_still_blocks(repo):
    repo.commit({"a.txt": f"host {TEN}\n"})
    sha = repo.commit({"a.txt": "host example.test\n"})
    r = repo.guard(sha)
    assert r.returncode == 1 and "a.txt" in r.stderr


def test_a_private_key_pattern_blocks(repo):
    sha = repo.commit({"notes.txt": PRIVATE_KEY + "\nabc\n"})
    r = repo.guard(sha)
    assert r.returncode == 1
    assert "notes.txt" in r.stderr and "private key block" in r.stderr


def test_a_denylist_value_blocks_without_echoing_it(repo):
    (repo.home / "denylist.txt").write_text("zzcorp-internal\n")
    sha = repo.commit({"a.py": "client = 'ZZCorp-Internal'\n"})
    r = repo.guard(sha)
    assert r.returncode == 1 and "denylist entry #1" in r.stderr and "zzcorp" not in (r.stdout + r.stderr).lower()


def test_the_pushed_commit_is_checked_not_the_working_tree(repo):
    sha = repo.commit({"a.py": f"x = '{LAN}'\n"})
    repo.commit({"a.py": "x = 1\n"})                       # a later commit that is NOT being pushed cleans the tree
    assert repo.guard(sha).returncode == 1


def test_a_new_branch_is_checked_against_what_the_remote_already_has(repo):
    sha = repo.commit({"a.py": "x = 1\n"})
    assert repo.guard(sha, remote="new").returncode == 0
    bad = repo.commit({".env": "A=1\n"})
    r = repo.guard(bad, remote="new")
    assert r.returncode == 1 and ".env" in r.stderr


def test_a_branch_deletion_passes(repo):
    line = f"(delete) {ZERO} refs/heads/old {'1' * 40}\n"
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "push_guard.py"), "origin"], cwd=repo.work, input=line,
                       capture_output=True, text=True, env=repo.env())
    assert r.returncode == 0


def test_the_hook_blocks_a_real_git_push_and_lets_a_clean_one_through(tmp_path):
    repo = Scratch(tmp_path, hooked=True)
    repo.commit({"ok.py": "x = 1\n"})
    ok = _git(repo.work, "push", "origin", "main", check=False)
    assert ok.returncode == 0, ok.stderr
    repo.commit({"server.pem": "x\n"})
    bad = subprocess.run(["git", "push", "origin", "main"], cwd=repo.work, capture_output=True, text=True, env=repo.env())
    assert bad.returncode != 0 and "server.pem" in bad.stderr and "PUSH BLOCKED" in bad.stderr
    on_remote = subprocess.run(["git", "log", "--name-only", "--format=", "main"], cwd=repo.remote, capture_output=True, text=True).stdout
    assert "server.pem" not in on_remote


def test_the_tree_ratchet_flags_files_above_their_baseline(tmp_path):
    root = tmp_path / "r"
    (root / "scripts").mkdir(parents=True)
    (root / "a.txt").write_text(f"{LAN}\n")
    (root / "scripts" / "internal-ip-baseline.json").write_text('{"a.txt": 1}\n')
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    _git(root, "add", "-A")
    assert guard.ip_ratchet(guard.tracked(root), root) == []
    (root / "a.txt").write_text(f"{LAN}\n{TEN}\n")
    (root / "b.txt").write_text(f"{TEN}\n")
    _git(root, "add", "-A")
    over = guard.ip_ratchet(guard.tracked(root), root)
    assert len(over) == 2 and all(LAN not in o and TEN not in o for o in over)


# --- the per-merge AI diff read (scripts/ai_diff_read.py): the API call is replaced, nothing leaves the machine ----------------------
airead = importlib.util.module_from_spec(importlib.util.spec_from_file_location("ai_diff_read", ROOT / "scripts" / "ai_diff_read.py"))
airead.__spec__.loader.exec_module(airead)


def _diff(tmp_path, text="diff --git a/a b/a\n+x\n"):
    p = tmp_path / "pr.diff"
    p.write_text(text)
    return str(p)


def test_ai_read_skips_cleanly_without_the_secret(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(airead, "ask", lambda *a: pytest.fail("must not call the API"))
    assert airead.main([_diff(tmp_path)]) == 0 and "skipped" in capsys.readouterr().out


@pytest.mark.parametrize("answer,rc", [("PASS", 0), ("pass.", 0), ("a.py: names a real client", 1), ("PASS\nbut b.py leaks a host", 1),
                                       ("ERROR: the Anthropic API call failed (HTTP 500)", 1), ("", 1)])
def test_ai_read_passes_only_on_exactly_pass(tmp_path, monkeypatch, answer, rc):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setattr(airead, "ask", lambda key, diff: answer)
    assert airead.main([_diff(tmp_path)]) == rc


def test_ai_read_splits_on_file_boundaries_and_refuses_what_is_too_big(tmp_path, monkeypatch):
    monkeypatch.setattr(airead, "CHUNK_CHARS", 100)
    parts = airead.split_diff("".join(f"diff --git a/f{i} b/f{i}\n+{'x' * 60}\n" for i in range(6)))
    assert len(parts) > 1 and all(p.startswith("diff --git") for p in parts)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setattr(airead, "MAX_CHUNKS", 2)
    monkeypatch.setattr(airead, "ask", lambda key, diff: "PASS")
    big = "".join(f"diff --git a/f{i} b/f{i}\n+{'x' * 60}\n" for i in range(6))
    assert airead.main([_diff(tmp_path, big)]) == 1


# --- review round: metadata, history, merges, the baseline, an empty remote, pinned actions, names and binaries ---------------------------
SECRET = "zzcorp-internal"            # a private denylist value for these tests (never a real one)


def _deny(repo):
    (repo.home / "denylist.txt").write_text(SECRET + "\n")


def _clean(r):
    out = r.stdout + r.stderr
    assert SECRET not in out.lower() and LAN not in out and TEN not in out
    return out


def _head(repo):
    return _git(repo.work, "rev-parse", "HEAD").stdout.strip()


def test_item1_a_commit_message_with_a_denylist_value_or_an_address_blocks(repo):
    _deny(repo)
    sha = repo.commit({"a.py": "x = 1\n"}, msg=f"fix route to {TEN} for {SECRET.upper()}")
    r = repo.guard(sha)
    assert r.returncode == 1 and "its message or author names match" in _clean(r)
    assert "denylist entry #1" in r.stderr and "internal" in r.stderr


def test_item1_an_address_only_in_a_commit_message_blocks_without_a_denylist(repo):
    sha = repo.commit({"a.py": "x = 1\n"}, msg=f"route via {LAN}")
    assert repo.guard(sha).returncode == 1


def test_item1_an_annotated_tag_with_a_value_blocks(repo):
    _deny(repo)
    repo.commit({"a.py": "x = 1\n"})
    _git(repo.work, "tag", "-a", "v1", "-m", f"{SECRET} at {TEN}")
    tag = _git(repo.work, "rev-parse", "v1").stdout.strip()
    r = repo.guard(tag, local_ref="refs/tags/v1", remote_ref="refs/tags/v1")
    assert r.returncode == 1 and "annotated tag" in _clean(r)


def test_item1_a_file_path_with_a_value_blocks_and_is_not_echoed(repo):
    _deny(repo)
    sha = repo.commit({f"docs/{SECRET}-migration.md": "plain text\n"})
    r = repo.guard(sha)
    assert r.returncode == 1 and "a file PATH it adds or changes matches denylist entry #1" in _clean(r)


def test_item1_a_ref_name_with_a_value_blocks(repo):
    _deny(repo)
    sha = repo.commit({"a.py": "x = 1\n"})
    r = repo.guard(sha, local_ref=f"refs/heads/{SECRET}", remote_ref=f"refs/heads/{SECRET}")
    assert r.returncode == 1 and "ref name" in _clean(r)


def test_item1_ci_runs_the_metadata_checks_without_a_denylist(repo):
    """CI's call: no private denylist, built-ins and addresses only, over a range, a ref name and a tag."""
    base = _head(repo)
    sha = repo.commit({f"docs/at-{LAN}.md": "x\n"}, msg=f"talk to {TEN}")
    out = guard.metadata_hits(f"{base}..{sha}", repo.work, [], [f"feature/{CGNAT}"])
    kinds = " | ".join(out)
    assert "message" in kinds and "PATH" in kinds and "ref name" in kinds
    assert LAN not in kinds and TEN not in kinds and CGNAT not in kinds


def test_item2_a_denylist_value_added_then_removed_still_blocks(repo):
    _deny(repo)
    repo.commit({"a.py": f"client = '{SECRET}'\n"})
    sha = repo.commit({"a.py": "client = 'x'\n"})
    r = repo.guard(sha)
    assert r.returncode == 1 and "a.py: an added line matches denylist entry #1" in _clean(r)


def _conflict(repo, resolution):
    """main gets one line, a side branch another, and the merge resolves the conflict by hand."""
    repo.commit({"c.txt": "start\n"})
    _git(repo.work, "push", "-q", "origin", "main")
    _git(repo.work, "checkout", "-q", "-b", "side")
    repo.commit({"c.txt": "side value\n"})
    _git(repo.work, "checkout", "-q", "main")
    repo.commit({"c.txt": "main value\n"})
    assert _git(repo.work, "merge", "side", check=False).returncode != 0
    (repo.work / "c.txt").write_text(resolution)
    _git(repo.work, "add", "c.txt")
    _git(repo.work, "commit", "-qm", "merge side")
    return _head(repo)


def test_item3_a_merge_conflict_resolution_that_adds_an_address_blocks(repo):
    sha = _conflict(repo, f"host = {TEN}\n")
    r = repo.guard(sha)
    assert r.returncode == 1 and "c.txt" in r.stderr and "internal" in r.stderr


def test_item3_a_clean_merge_resolution_passes(repo):
    sha = _conflict(repo, "both values\n")
    assert repo.guard(sha).returncode == 0


def test_item4_the_baseline_may_not_rise_above_the_base_commits_copy(repo):
    repo.commit({"scripts/internal-ip-baseline.json": '{"old.txt": 1}\n', "old.txt": f"{LAN}\n"}, msg="baselined")
    _git(repo.work, "push", "-q", "origin", "main")
    base = _head(repo)
    # raise it to cover a new address: the tree ratchet is now satisfied, only the comparison with the base catches it
    sha = repo.commit({"scripts/internal-ip-baseline.json": '{"old.txt": 1, "new.txt": 1}\n', "new.txt": f"{TEN}\n"})
    grew = guard.baseline_grew(repo.work, base, sha)
    assert grew and "new.txt" in grew[0] and TEN not in grew[0]
    assert guard.ip_ratchet(guard.tracked(repo.work, sha), repo.work, sha) == []
    r = repo.guard(sha)
    assert r.returncode == 1 and "RAISED" in r.stderr
    lowered = repo.commit({"scripts/internal-ip-baseline.json": "{}\n", "old.txt": "no address now\n", "new.txt": "x\n"})
    assert guard.baseline_grew(repo.work, base, lowered) == []


def test_item4_ci_falls_back_to_the_merge_base_on_a_new_branch():
    wf = (ROOT / ".github" / "workflows" / "guard.yml").read_text()
    sh = (ROOT / "scripts" / "ci_guard.sh").read_text()
    assert "ci_guard.sh range" in wf and "ci_guard.sh run" in wf and "ip_range=" not in wf
    assert "merge-base" in sh and "DEFAULT_BRANCH" in sh and "--baseline-base" in sh and "--added-in" in sh


def _squashed(r, files):
    """One orphan commit holding `files`, as the release cut makes."""
    _git(r.work, "checkout", "-q", "--orphan", "release")
    for rel, text in files.items():
        p = r.work / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git(r.work, "add", "-A", "-f")
    _git(r.work, "commit", "-qm", "first public release")
    return _head(r)


def test_item5_the_first_push_of_one_commit_to_an_empty_remote_passes_with_its_baselined_lines(tmp_path):
    r = Scratch(tmp_path, push_base=False)
    one = _squashed(r, {"scripts/internal-ip-baseline.json": '{"fixture.txt": 2}\n', "fixture.txt": f"{LAN}\n{TEN}\n"})
    for argv in (("origin",), (str(r.remote), str(r.remote))):          # a named remote, and a bare URL
        out = r.guard(one, remote="new", argv=argv)
        assert out.returncode == 0, out.stderr


def test_item5_an_empty_remote_still_checks_the_tree_and_the_message(tmp_path):
    r = Scratch(tmp_path, push_base=False)
    assert r.guard(_squashed(r, {"fixture.txt": f"{LAN}\n"}), remote="new").returncode == 1      # an address in no baseline
    _deny(r)
    (r.work / "fixture.txt").write_text("fine\n")
    _git(r.work, "add", "-A")
    _git(r.work, "commit", "-q", "--amend", "-m", f"release for {SECRET}")
    assert r.guard(_head(r), remote="new").returncode == 1


def test_item5_a_longer_history_to_an_empty_remote_is_not_waved_through(tmp_path):
    r = Scratch(tmp_path, push_base=False)
    r.commit({"scripts/internal-ip-baseline.json": '{"fixture.txt": 1}\n', "fixture.txt": f"{LAN}\n"})
    sha = r.commit({"b.txt": "x\n"})
    assert r.guard(sha, remote="new").returncode == 1


def test_item5_a_real_push_to_an_empty_remote_works_without_no_verify(tmp_path):
    r = Scratch(tmp_path, hooked=True, push_base=False)
    (r.work / "scripts" / "internal-ip-baseline.json").write_text('{"fixture.txt": 1}\n')
    _squashed(r, {"scripts/internal-ip-baseline.json": '{"fixture.txt": 1}\n', "fixture.txt": f"{LAN}\n"})
    out = subprocess.run(["git", "push", str(r.remote), "release:main"], cwd=r.work, capture_output=True, text=True, env=r.env())
    assert out.returncode == 0, out.stderr


def test_item6_every_action_is_pinned_by_commit_sha_and_the_gitleaks_download_is_verified():
    import re
    wf = (ROOT / ".github" / "workflows" / "guard.yml").read_text()
    uses = re.findall(r"uses:\s*(\S+)", wf)
    assert uses and all(re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", u) for u in uses), uses
    assert "sha256sum -c" in wf and "@latest" not in wf


@pytest.mark.parametrize("name", ["data.sqlite3", "app.db", "tls.key", "cert.p12", "cert.pfx", "vault.kdbx", "credentials.json",
                                  "infra/terraform.tfstate", ".npmrc", "home/.pgpass", "secrets.yml", "kubeconfig", "x/.netrc"])
def test_item7_more_secret_file_names_block(repo, name):
    r = repo.guard(repo.commit({name: "x\n"}))
    assert r.returncode == 1 and name in r.stderr


def test_item7_a_binary_file_is_listed_and_blocked_unless_it_is_an_asset(repo):
    repo.commit({"scripts/binary-allow.txt": "app/static/*.png\n"})
    _git(repo.work, "push", "-q", "origin", "main")
    sha = repo.commit({"app/static/logo.png": b"\x89PNG\r\n\x1a\n\x00data", "tools/payload.bin": b"\x00\x01\x02secret"})
    r = repo.guard(sha)
    assert r.returncode == 1
    assert "tools/payload.bin: binary file, not content-scanned, and not an asset path" in r.stderr
    assert "app/static/logo.png: binary file, not content-scanned (allowed: an asset path, bytes match the extension)" in r.stderr
    _git(repo.work, "reset", "-q", "--hard", "origin/main")
    ok = repo.commit({"app/static/more.png": b"\x89PNG\r\n\x1a\n\x00\x02"})
    assert repo.guard(ok).returncode == 0


def test_item7_a_push_cannot_widen_its_own_binary_allowlist(repo):
    sha = repo.commit({"scripts/binary-allow.txt": "*\n", "evil.bin": b"\x00\x01"})
    assert repo.guard(sha).returncode == 1


def test_item8_with_rev_the_baseline_comes_from_the_pushed_commit_not_the_working_tree(repo):
    sha = repo.commit({"scripts/internal-ip-baseline.json": '{"f.txt": 1}\n', "f.txt": f"{LAN}\n"})
    (repo.work / "scripts" / "internal-ip-baseline.json").write_text("{}\n")             # a different working tree
    assert guard.main(["--no-gitleaks", "--rev", sha], repo.work) == 0
    assert guard.main(["--no-gitleaks"], repo.work) == 1


def test_item10_a_content_line_that_looks_like_a_diff_header_is_still_scanned(repo):
    base = _head(repo)
    sha = repo.commit({"notes.txt": f"+ b/x\n++ {TEN}\n"})       # added lines "++ b/x" and "+++ <addr>" in the diff
    assert any("notes.txt" in h for h in guard.added_hits(f"{base}..{sha}", repo.work, []))
    sha2 = repo.commit({"other.txt": f"++ {LAN}\n"})
    assert any("other.txt" in h for h in guard.added_hits(f"{sha}..{sha2}", repo.work, []))


def test_item11_the_ai_job_warns_when_skipped_and_the_jobs_have_timeouts():
    wf = (ROOT / ".github" / "workflows" / "guard.yml").read_text()
    assert "::warning" in wf and wf.count("timeout-minutes:") == 2


def test_item11_codeowners_cover_the_guards():
    text = (ROOT / ".github" / "CODEOWNERS").read_text()
    for path in (".github/workflows/", "scripts/commit_guard.py", "scripts/push_guard.py", "scripts/ai_diff_read.py", ".gitleaks.toml",
                 "scripts/internal-ip-baseline.json"):
        assert any(ln.startswith("/" + path) and "@OWNER" in ln for ln in text.splitlines()), path
    assert "@OWNER" in (ROOT / "CONTRIBUTING.md").read_text()


# --- second review: stale tracking refs, tags on pushed commits, version strings, magic bytes, the owner placeholder -------------------------
def _private_then_public(tmp_path, **kw):
    """The scenario: work was pushed to a private remote, then `origin` was re-pointed at an EMPTY public one."""
    r = Scratch(tmp_path, **kw)
    _deny(r)
    r.commit({"a.py": f"client = '{SECRET}'\nhost = '{TEN}'\n"}, msg=f"for {SECRET}")
    r.commit({"a.py": "client = 'x'\nhost = 'h'\n"})
    top = r.commit({"b.py": "y = 2\n"})
    return r, top


def test_review2_item1_a_repointed_origin_does_not_hide_history_behind_stale_tracking_refs(tmp_path):
    r, top = _private_then_public(tmp_path)
    _git(r.work, "push", "-q", "origin", "main")                       # to the private remote: origin/main now exists locally
    public = tmp_path / "public.git"
    subprocess.run(["git", "init", "-q", "--bare", str(public)], check=True)
    _git(r.work, "remote", "set-url", "origin", str(public))
    assert _git(r.work, "rev-parse", "origin/main").returncode == 0     # the stale ref is still there
    out = r.guard(top, remote="new", argv=("origin", str(public)))
    assert out.returncode == 1, out.stderr
    assert "denylist entry #1" in out.stderr and _clean(out)


def test_review2_item1_a_real_push_after_set_url_is_blocked_by_the_hook(tmp_path):
    r = Scratch(tmp_path, hooked=True)
    _deny(r)
    r.commit({"a.py": "x = 1\n"}, msg=f"for {SECRET} at {TEN}")      # clean content, a message the pre-commit hook does not read
    pushed = subprocess.run(["git", "push", "--no-verify", "origin", "main"], cwd=r.work, capture_output=True, text=True, env=r.env())
    assert pushed.returncode == 0, pushed.stderr                       # fixture only: put the commit on the private remote
    public = tmp_path / "public.git"
    subprocess.run(["git", "init", "-q", "--bare", str(public)], check=True)
    _git(r.work, "remote", "set-url", "origin", str(public))
    out = subprocess.run(["git", "push", "origin", "main"], cwd=r.work, capture_output=True, text=True, env=r.env())
    assert out.returncode != 0 and "PUSH BLOCKED" in out.stderr
    assert subprocess.run(["git", "rev-parse", "--verify", "-q", "main"], cwd=public, capture_output=True).returncode != 0


def test_review2_item1_when_the_remote_cannot_be_asked_the_whole_history_is_checked(tmp_path):
    r = Scratch(tmp_path)
    r.commit({"a.py": f"host = '{LAN}'\n"})
    top = r.commit({"a.py": "host = 'h'\n"})
    gone = str(tmp_path / "no-such-remote.git")
    out = r.guard(top, remote="new", argv=("origin", gone))
    assert out.returncode == 1 and "ls-remote failed" in out.stderr
    (tmp_path / "c").mkdir()
    clean = Scratch(tmp_path / "c")
    ok = clean.commit({"a.py": "x = 1\n"})
    assert clean.guard(ok, remote="new", argv=("origin", gone)).returncode == 0


def test_review2_item1_ci_checks_the_whole_history_when_there_is_nothing_to_compare_with():
    sh = (ROOT / "scripts" / "ci_guard.sh").read_text()
    assert 'range="$PUSH_AFTER"' in sh and "--tip-only" in sh and "rev-list --count" in sh   # behaviour: the test_ci_* tests below


def test_review2_item2_a_tag_on_an_already_pushed_commit_is_still_checked(repo):
    _deny(repo)
    sha = repo.commit({"a.py": "x = 1\n"})
    _git(repo.work, "push", "-q", "origin", "main")
    _git(repo.work, "tag", "-a", "v2", "-m", f"release for {SECRET} at {TEN}")
    tag = _git(repo.work, "rev-parse", "v2").stdout.strip()
    r = repo.guard(tag, local_ref="refs/tags/v2", remote_ref="refs/tags/v2", remote="new")
    assert r.returncode == 1 and "annotated tag" in _clean(r)
    _git(repo.work, "tag", "-a", "v3", "-m", "plain release")
    ok = repo.guard(_git(repo.work, "rev-parse", "v3").stdout.strip(), local_ref="refs/tags/v3", remote_ref="refs/tags/v3", remote="new")
    assert ok.returncode == 0, ok.stderr
    assert sha


def test_review2_item3_real_prefixes_in_code_still_match_and_versions_do_not():
    assert guard.INTERNAL_IP.search("if host.startswith('10." + "240.0.'):")
    assert guard.INTERNAL_IP.search('NET = "172.' + '29.100."')
    assert not guard.INTERNAL_IP.search("npm i eslint@10." + "2.0 and ip-address-10." + "8.0.tgz")


def test_review2_item3_the_lockfile_version_line_is_no_longer_baselined():
    import json
    base = json.loads((ROOT / "scripts" / "internal-ip-baseline.json").read_text())
    assert "sessions/channel/package-lock.json" not in base
    lock = (ROOT / "sessions" / "channel" / "package-lock.json").read_text()
    assert not any(guard.INTERNAL_IP.search(ln) for ln in lock.splitlines())


def test_review2_item6_asset_bytes_must_match_the_extension(repo):
    repo.commit({"scripts/binary-allow.txt": "app/static/*.png\napp/static/*.woff2\n"})
    _git(repo.work, "push", "-q", "origin", "main")
    sqlite = repo.commit({"app/static/logo.png": b"SQLite format 3\x00" + b"\x00" * 20})
    r = repo.guard(sqlite)
    assert r.returncode == 1 and "do not match its extension" in r.stderr
    _git(repo.work, "reset", "-q", "--hard", "origin/main")
    good = repo.commit({"app/static/logo.png": b"\x89PNG\r\n\x1a\n\x00x", "app/static/f.woff2": b"wOF2\x00\x01"})
    assert repo.guard(good).returncode == 0


@pytest.mark.parametrize("name,data,ok", [
    ("a.png", b"\x89PNG\r\n\x1a\nxx", True), ("a.jpg", b"\xff\xd8\xff\xe0", True), ("a.gif", b"GIF89a..", True),
    ("a.webp", b"RIFF\x00\x00\x00\x00WEBPVP8", True), ("a.ico", b"\x00\x00\x01\x00\x01", True), ("a.woff", b"wOFF\x00", True),
    ("a.woff2", b"wOF2\x00", True), ("a.ttf", b"\x00\x01\x00\x00\x00", True), ("a.otf", b"OTTO\x00", True),
    ("a.svg", b"<?xml?><svg xmlns='x'></svg>", True),
    ("a.png", b"SQLite format 3\x00", False), ("a.jpg", b"\x89PNG\r\n\x1a\n", False), ("a.webp", b"RIFFxxxxWAVE", False),
    ("a.svg", b"\x00\x01<svg", False), ("a.bin", b"\x00\x01", False)])
def test_review2_item6_magic_bytes(name, data, ok):
    assert guard.magic_ok(name, data) is ok


def test_review2_item7_the_workflow_warns_while_codeowners_is_a_placeholder():
    wf = (ROOT / ".github" / "workflows" / "guard.yml").read_text()
    assert "@OWNER" in wf and "::warning title=CODEOWNERS" in wf


def test_review2_docs_say_how_to_publish_to_a_new_remote():
    text = (ROOT / "CONTRIBUTING.md").read_text()
    assert "never re-point an existing" in text and "one-commit history" in text


# --- third review: CI's range logic (scripts/ci_guard.sh, run for real against scratch repos), subnets, every binary blob ----------------
ZERO40 = "0" * 40


class CI:
    """A public-like repo with a bare 'GitHub' remote, and a clone of it as the CI runner would see it after actions/checkout."""

    def __init__(self, tmp_path, second_commit=True, extra_ref=False, shallow=False):
        self.tmp = tmp_path
        self.bare = tmp_path / "gh.git"
        src = tmp_path / "src"
        self.home = tmp_path / "home"
        self.home.mkdir()
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.bare)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
        shutil.copytree(ROOT / "scripts", src / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
        (src / "scripts" / "internal-ip-baseline.json").write_text('{"app/config.py": 1}\n')
        (src / "scripts" / "binary-allow.txt").write_text("app/static/*.png\n")
        (src / "app").mkdir()
        (src / "app" / "config.py").write_text(f"TRUSTED = '{LAN}'\n")
        (src / "README.md").write_text("hello\n")
        _git(src, "add", "-A")
        _git(src, "commit", "-qm", "public cut")
        if second_commit:
            (src / "README.md").write_text("hello again\n")
            _git(src, "commit", "-qam", "second")
        _git(src, "remote", "add", "origin", str(self.bare))
        _git(src, "push", "-q", "origin", "main")
        if extra_ref:
            _git(src, "push", "-q", "origin", "main:refs/heads/other")
        self.src = src
        self.ci = tmp_path / "ci"
        self.reclone(shallow)

    def reclone(self, shallow=False):
        """What actions/checkout leaves on the runner: a fresh clone of the remote as it is now."""
        shutil.rmtree(self.ci, ignore_errors=True)
        url = f"file://{self.bare}"                       # a transfer, so unreachable old objects are not copied along
        subprocess.run(["git", "clone", "-q", *(["--depth", "1"] if shallow else []), url, str(self.ci)], check=True)
        self.tip = _git(self.ci, "rev-parse", "origin/main").stdout.strip()

    def range_raw(self, after, ref, before=ZERO40):
        _git(self.ci, "checkout", "-q", "--detach", after)
        return subprocess.run(["bash", "scripts/ci_guard.sh", "range"], cwd=self.ci, capture_output=True, text=True,
                              env=self.env(PUSH_BEFORE=before, PUSH_AFTER=after, REF=ref))

    def env(self, **kw):
        env = {k: v for k, v in os.environ.items() if k not in ("CI", "CREWBOSS_REQUIRE_GITLEAKS", "GITHUB_OUTPUT")}
        env.update(CREWBOSS_HOME=str(self.home), EVENT="push", DEFAULT_BRANCH="main")
        env.update(kw)
        return env

    def run(self, after, ref, before=ZERO40, ref_name=None):
        """Mimic the workflow: range step, then the guard step with its outputs. Returns (rc, stderr, range outputs)."""
        _git(self.ci, "checkout", "-q", "--detach", after)
        env = self.env(PUSH_BEFORE=before, PUSH_AFTER=after, REF=ref)
        rng = subprocess.run(["bash", "scripts/ci_guard.sh", "range"], cwd=self.ci, capture_output=True, text=True, env=env)
        assert rng.returncode == 0, rng.stderr
        out = dict(ln.split("=", 1) for ln in rng.stdout.splitlines())
        env = self.env(RANGE=out["range"], BASE=out["base"], EXCLUDE=out["exclude"], TIP_ONLY=out["tip_only"],
                       REF_NAME=ref_name or ref.rsplit("/", 1)[-1])
        res = subprocess.run(["bash", "scripts/ci_guard.sh", "run"], cwd=self.ci, capture_output=True, text=True, env=env)
        return res.returncode, res.stderr, out


@pytest.fixture
def ci(tmp_path):
    return CI(tmp_path)


def test_ci_s1_a_release_tag_on_main_adds_nothing_and_passes(ci):
    rc, err, out = ci.run(ci.tip, "refs/tags/v1.0.0")
    assert rc == 0, err
    assert out["meta_only"] == "1" and out["range"] == "" and out["tip_only"] == ""


def test_ci_s1_a_new_branch_at_an_existing_commit_passes(ci):
    rc, err, out = ci.run(ci.tip, "refs/heads/release")
    assert rc == 0, err
    assert out["meta_only"] == "1"


def test_ci_s1_the_tag_text_and_name_are_still_checked(ci):
    _git(ci.ci, "tag", "-a", "v2.0.0", "-m", f"release for {TEN}", ci.tip)
    rc, err, _ = ci.run(ci.tip, "refs/tags/v2.0.0")
    assert rc == 1 and "annotated tag" in err and TEN not in err
    rc, err, _ = ci.run(ci.tip, "refs/heads/rel/" + CGNAT)
    assert rc == 1 and "ref name" in err


def test_ci_s1_a_normal_new_branch_is_checked_since_the_merge_base(ci):
    _git(ci.ci, "checkout", "-q", "-b", "feat", ci.tip)
    (ci.ci / "new.py").write_text(f"x = '{TEN}'\n")
    _git(ci.ci, "add", "-A")
    _git(ci.ci, "commit", "-qm", "adds an address")
    sha = _git(ci.ci, "rev-parse", "HEAD").stdout.strip()
    rc, err, out = ci.run(sha, "refs/heads/feat")
    assert rc == 1 and out["range"] == f"{sha} --not {ci.tip}" and "new.py" in err


def _orphan(ci, files, msg="orphan"):
    _git(ci.ci, "checkout", "-q", "--orphan", "o")
    for rel, text in files.items():
        p = ci.ci / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text) if isinstance(text, bytes) else p.write_text(text)
    _git(ci.ci, "add", "-A")
    _git(ci.ci, "commit", "-qm", msg)
    return _git(ci.ci, "rev-parse", "HEAD").stdout.strip()


def test_ci_s2_a_one_commit_orphan_on_an_existing_repo_is_not_a_fresh_cut(ci):
    """The reviewer's exploit: an orphan adds an address, raises its own baseline, widens its own binary allowlist, adds a non-asset binary."""
    _git(ci.ci, "checkout", "-q", "--detach", ci.tip)
    _git(ci.ci, "checkout", "-q", "--orphan", "o")
    (ci.ci / "app" / "config.py").write_text(f"TRUSTED = '{LAN}'\nHOST = '{TEN}'\n")
    (ci.ci / "scripts" / "internal-ip-baseline.json").write_text('{"app/config.py": 2}\n')
    (ci.ci / "scripts" / "binary-allow.txt").write_text("*\n")
    (ci.ci / "app" / "static").mkdir(exist_ok=True)
    (ci.ci / "app" / "static" / "data.bin").write_bytes(b"SQLite format 3\x00")
    _git(ci.ci, "add", "-A", "-f")
    _git(ci.ci, "commit", "-qm", "orphan")
    sha = _git(ci.ci, "rev-parse", "HEAD").stdout.strip()
    rc, err, out = ci.run(sha, "refs/heads/o")
    assert rc == 1 and out["tip_only"] == "" and out["exclude"] == ci.tip and out["base"] == ci.tip
    assert "RAISED" in err and "app/config.py" in err and "data.bin" in err
    assert TEN not in err


def test_ci_s2_a_clean_orphan_with_the_same_tree_passes(ci):
    _git(ci.ci, "checkout", "-q", "--detach", ci.tip)
    _git(ci.ci, "checkout", "-q", "--orphan", "o")
    _git(ci.ci, "commit", "-qm", "same tree, new history")
    sha = _git(ci.ci, "rev-parse", "HEAD").stdout.strip()
    rc, err, out = ci.run(sha, "refs/heads/o")
    assert rc == 0, err
    assert out["exclude"] == ci.tip


def test_ci_s2_tip_only_is_for_the_first_push_of_the_default_branch_to_an_otherwise_empty_repo(tmp_path):
    ci = CI(tmp_path, second_commit=False)
    rc, err, out = ci.run(ci.tip, "refs/heads/main")
    assert rc == 0, err                                                    # the cut's baselined line is not "added"
    assert out["tip_only"] == "1" and out["range"] == ci.tip


def test_ci_s2_a_default_branch_push_to_a_repo_with_other_refs_is_not_tip_only(tmp_path):
    (tmp_path / "a").mkdir()
    ci = CI(tmp_path / "a", second_commit=False, extra_ref=True)
    _, _, out = ci.run(ci.tip, "refs/heads/main")
    assert out["tip_only"] == ""


def test_ci_s2_a_longer_first_history_is_checked_strictly(tmp_path):
    ci = CI(tmp_path)                                                      # two commits, the first carries a baselined line
    rc, err, out = ci.run(ci.tip, "refs/heads/main")
    assert out["tip_only"] == "" and rc == 1                               # judged commit by commit: squash it first


@pytest.mark.parametrize("text", ["10." + "240.0.x", "10." + "240.0.X/24", "172." + "29.100.x", "net 192." + "168.7.x", "100." + "64.0.x"])
def test_n1_subnet_notation_matches(text):
    assert guard.INTERNAL_IP.search(text)


@pytest.mark.parametrize("text", ["foo-10." + "4.x", "eslint@10." + "2.x", "10." + "0.0.dev1", "10." + "0.0.x86", "v10." + "2.x"])
def test_n1_version_strings_still_do_not_match(text):
    assert not guard.INTERNAL_IP.search(text)


def test_n2_a_bad_binary_replaced_by_a_good_one_in_the_same_push_still_blocks(repo):
    repo.commit({"scripts/binary-allow.txt": "app/static/*.png\n"})
    _git(repo.work, "push", "-q", "origin", "main")
    repo.commit({"app/static/logo.png": b"SQLite format 3\x00" + b"\x00" * 20}, msg="oops")
    sha = repo.commit({"app/static/logo.png": b"\x89PNG\r\n\x1a\n\x00x"}, msg="fixed")
    r = repo.guard(sha)
    assert r.returncode == 1 and "do not match its extension" in r.stderr and "in commit" in r.stderr
    bins = guard.range_files(f"{_git(repo.work, 'rev-parse', 'origin/main').stdout.strip()}..{sha}", repo.work)[1]
    assert len([b for _, b in bins if b == "app/static/logo.png"]) == 2


def test_s2_added_excluding_only_forgives_lines_the_base_tree_has(repo):
    base = repo.commit({"c.py": f"a = '{LAN}'\n"})
    _git(repo.work, "checkout", "-q", "--orphan", "o")
    (repo.work / "c.py").write_text(f"a = '{LAN}'\nb = '{TEN}'\n")
    _git(repo.work, "add", "-A")
    _git(repo.work, "commit", "-qm", "o")
    tip = _head(repo)
    hits = guard.added_hits(tip, repo.work, [], exclude_rev=base)
    assert len(hits) == 1 and "c.py" in hits[0]


def test_review3_docs_say_images_need_a_human_look():
    assert "Images are not content-scanned by any automatic check: a human looks at every added image before a release." in (
        ROOT / "CONTRIBUTING.md").read_text()


# --- fourth review: a ref name can never be read as an option; tip-only only on a true first push; shallow clones fail ---------------------
def _feature_commit(ci, text):
    _git(ci.ci, "checkout", "-q", "-b", "feat", ci.tip)
    (ci.ci / "new.py").write_text(text)
    _git(ci.ci, "add", "-A")
    _git(ci.ci, "commit", "-qm", "adds an address")
    return _git(ci.ci, "rev-parse", "HEAD").stdout.strip()


@pytest.mark.parametrize("name", ["--list", "--write-ip-baseline", "-x", "--tip-only", "--strict", "--no-gitleaks", "--rev=HEAD"])
def test_review4_item1_a_branch_named_like_an_option_still_blocks_an_added_address(ci, name):
    sha = _feature_commit(ci, f"x = '{TEN}'\n")
    before = (ci.ci / "scripts" / "internal-ip-baseline.json").read_text()
    rc, err, out = ci.run(sha, "refs/heads/feat", ref_name=name)
    assert rc == 1 and "new.py" in err, (rc, err)
    assert (ci.ci / "scripts" / "internal-ip-baseline.json").read_text() == before     # --write-ip-baseline was not executed
    assert out["tip_only"] == ""


def test_review4_item1_a_value_that_looks_like_an_option_is_rejected_not_obeyed():
    assert guard.main(["--meta-ref", "--list"], ROOT) == 2
    assert guard.main(["--rev"], ROOT) == 2
    assert guard.main(["--lis"], ROOT) == 2                 # no abbreviations either


def _recut(ci):
    """Friday's state: main (only main, no tags) is force-pushed to a one-commit re-cut that adds an address and raises its own baseline."""
    old = ci.tip
    _git(ci.src, "checkout", "-q", "--orphan", "recut")
    (ci.src / "app" / "config.py").write_text(f"TRUSTED = '{LAN}'\nHOST = '{TEN}'\n")
    (ci.src / "scripts" / "internal-ip-baseline.json").write_text('{"app/config.py": 2}\n')
    _git(ci.src, "add", "-A")
    _git(ci.src, "commit", "-qm", "re-cut")
    _git(ci.src, "push", "-q", "-f", "origin", "recut:main")
    ci.reclone()
    assert _git(ci.ci, "cat-file", "-e", f"{old}^{{commit}}", check=False).returncode != 0      # the old tip is not in the new clone
    return old


def test_review4_item2_a_force_pushed_recut_is_not_a_first_push_and_the_job_fails_if_the_old_tip_is_missing(ci):
    old = _recut(ci)
    _git(ci.bare, "reflog", "expire", "--expire=now", "--all")
    _git(ci.bare, "gc", "-q", "--prune=now")                  # the old tip is gone from the remote too: it cannot be fetched
    r = ci.range_raw(ci.tip, "refs/heads/main", before=old)
    assert r.returncode != 0 and "cannot find the previous tip" in r.stderr
    assert "tip_only=1" not in r.stdout


def test_review4_item2_when_the_old_tip_can_be_fetched_the_recut_is_compared_with_it_and_blocks(ci):
    """The remote still serves the old commit (as GitHub usually does): the job fetches it and compares the re-cut with it."""
    old = _recut(ci)
    rc, err, out = ci.run(ci.tip, "refs/heads/main", before=old)
    assert out["tip_only"] == "" and out["base"] == old and out["range"] == f"{old}..{ci.tip}"
    assert rc == 1 and "RAISED" in err


def test_review4_item2_a_shallow_clone_fails_the_job(tmp_path):
    ci = CI(tmp_path, shallow=True)
    r = ci.range_raw(ci.tip, "refs/heads/main")
    assert r.returncode != 0 and "shallow" in r.stderr


def test_review4_item2_a_real_first_push_still_gets_tip_only(tmp_path):
    ci = CI(tmp_path, second_commit=False)
    rc, err, out = ci.run(ci.tip, "refs/heads/main", before=ZERO40)
    assert rc == 0, err
    assert out["tip_only"] == "1"


# --- fifth: a commit is new only if no ref the remote advertises already reaches it -----------------------------------------------------
def _commit_as(repo, files, author, msg="change"):
    for rel, text in files.items():
        p = repo.work / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    _git(repo.work, "add", "-A", "-f")
    env = {**os.environ, "GIT_AUTHOR_NAME": author, "GIT_COMMITTER_NAME": author}
    subprocess.run(["git", "-c", "user.email=t@example.test", "commit", "-qm", msg], cwd=repo.work, check=True, env=env, capture_output=True)
    return _head(repo)


def _rc_branch_on_the_remote(repo):
    """Two commits by a denylisted author (one adds a denylisted value) are pushed under the branch rc; the remote's main is still the base."""
    _deny(repo)
    _git(repo.work, "checkout", "-q", "-b", "rc")
    _commit_as(repo, {"a.py": f"client = '{SECRET}'\n"}, SECRET, msg=f"for {SECRET}")
    tip = _commit_as(repo, {"a.py": "client = 'x'\n", "b.py": "y = 2\n"}, SECRET)       # the value is removed again: only history has it
    _git(repo.work, "push", "-q", "origin", "rc")
    _git(repo.work, "checkout", "-q", "main")
    _git(repo.work, "merge", "-q", "--ff-only", "rc")
    return tip


def test_review5_a_fast_forward_to_a_commit_already_on_the_remote_under_another_branch_passes(repo):
    tip = _rc_branch_on_the_remote(repo)
    r = repo.guard(tip)                       # main: base -> tip, and tip is already advertised as refs/heads/rc
    assert r.returncode == 0, r.stderr


def test_review5_one_genuinely_new_commit_on_top_blocks_and_names_only_that_commit(repo):
    old_tip = _rc_branch_on_the_remote(repo)
    new = _commit_as(repo, {"c.py": "z = 3\n"}, "t", msg=f"adds {SECRET}")
    r = repo.guard(new)
    assert r.returncode == 1
    assert new[:10] in r.stderr and "denylist entry #1" in r.stderr
    assert old_tip[:10] not in r.stderr and "a.py" not in r.stderr and "b.py" not in r.stderr
    old_parent = _git(repo.work, "rev-parse", f"{old_tip}~1").stdout.strip()
    assert old_parent[:10] not in r.stderr


def test_review5_the_remote_is_asked_live_so_an_unreachable_remote_is_still_failing_closed_for_a_new_ref(repo):
    _rc_branch_on_the_remote(repo)
    sha = _head(repo)
    gone = str(repo.work.parent / "no-such-remote.git")
    r = repo.guard(sha, remote="new", argv=("origin", gone))             # a new ref, remote cannot be asked: the whole history counts
    assert r.returncode == 1 and "ls-remote failed" in r.stderr


def test_review5_the_set_url_scenario_still_blocks_with_the_new_rule(tmp_path):
    test_review2_item1_a_repointed_origin_does_not_hide_history_behind_stale_tracking_refs(tmp_path)


def _commit_on(ci, branch, msg, tag=None):
    """Commit on `branch` in the source repo (creating it from main when new) and return the sha; nothing is pushed here."""
    exists = _git(ci.src, "rev-parse", "--verify", "-q", branch, check=False).returncode == 0
    _git(ci.src, "checkout", "-q", *(() if exists else ("-b",)), branch, *(() if exists else ("main",)))
    (ci.src / "n.py").write_text(f"x = {len(msg)}\n")
    _git(ci.src, "add", "-A")
    _git(ci.src, "commit", "-qm", msg)
    sha = _git(ci.src, "rev-parse", "HEAD").stdout.strip()
    if tag:
        _git(ci.src, "tag", "-a", tag, "-m", "release")
    return sha


def test_review5_ci_follow_tags_a_main_and_tag_pushed_together_still_scans_the_commit(tmp_path):
    ci = CI(tmp_path)
    old = ci.tip
    sha = _commit_on(ci, "main", f"route via {TEN}", tag="v1.1")
    _git(ci.src, "push", "-q", "--follow-tags", "origin", "main")        # main and the annotated tag arrive in ONE push
    ci.reclone()
    rc, err, out = ci.run(sha, "refs/heads/main", before=old)             # the main job: its checkout already shows the tag
    assert rc == 1 and "its message or author names match" in err and out["range"] == f"{old}..{sha}"
    rc_tag, err_tag, out_tag = ci.run(sha, "refs/tags/v1.1", ref_name="v1.1")   # the tag job adds no content of its own
    assert out_tag["meta_only"] == "1" and "tag" not in out_tag["range"]


def test_review5_ci_race_a_branch_at_the_same_commit_pushed_first_does_not_hide_it_from_mains_run(tmp_path):
    ci = CI(tmp_path)
    old = ci.tip
    sha = _commit_on(ci, "main", f"route via {TEN}")
    _git(ci.src, "push", "-q", "origin", "main:refs/heads/feat")          # feat first ...
    _git(ci.src, "push", "-q", "origin", "main")                          # ... then main
    ci.reclone()
    rc, err, out = ci.run(sha, "refs/heads/main", before=old)
    assert rc == 1 and "its message or author names match" in err
    rc_feat, err_feat, out_feat = ci.run(sha, "refs/heads/feat")          # feat's own run: main has these commits, nothing to add
    assert out_feat["meta_only"] == "1" and rc_feat == 0


def test_review5_ci_a_new_branch_whose_commits_are_all_on_the_default_branch_is_meta_only(ci):
    rc, err, out = ci.run(ci.tip, "refs/heads/release-1")
    assert rc == 0, err
    assert out["meta_only"] == "1" and out["range"] == ""
