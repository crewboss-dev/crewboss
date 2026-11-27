"""The runner's half of skill delivery (task #718): the claim's skills and rules are written into the job's worktree as Claude Code skills
plus one rules file, never over what the repo already has, never outside the skill's own folder, and kept out of a commit. Real git."""
import asyncio
import base64
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runner"))

from crewboss_runner import skills, worker  # noqa: E402


def git(*args, cwd, env=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                               "PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(cwd), **(env or {})}).stdout


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def skill(key, body="BODY", files=()):
    return {"key": key, "description": "d", "layer": 1, "n": 1, "version_id": 1, "content_hash": "x" * 64,
            "skill_md": f"---\nname: {key}\ndescription: d\n---\n\n{body}\n", "files": [{"path": p, "b64": b64(d)} for p, d in files], "warnings": []}


@pytest.fixture
def wt(tmp_path):
    """A linked worktree like the runner makes (a bare cache + `git worktree add`) of a repo that has its own skill and a project-layer one."""
    src = tmp_path / "src"
    src.mkdir()
    git("init", "-q", "-b", "main", cwd=src)
    (src / "README.md").write_text("repo\n")
    (src / ".claude" / "skills" / "repo-own").mkdir(parents=True)
    (src / ".claude" / "skills" / "repo-own" / "SKILL.md").write_text("REPO'S OWN\n")
    (src / ".crewboss" / "skills" / "in-code").mkdir(parents=True)
    (src / ".crewboss" / "skills" / "in-code" / "SKILL.md").write_text("---\nname: in-code\ndescription: d\n---\nFROM THE REPO\n")
    (src / ".crewboss" / "skills" / "in-code" / "notes.md").write_text("notes\n")
    git("add", "-A", cwd=src)
    git("commit", "-q", "-m", "init", cwd=src)
    bare = tmp_path / "bare.git"
    git("clone", "-q", "--bare", str(src), str(bare), cwd=tmp_path)
    work = tmp_path / "job" / "work"
    work.parent.mkdir()
    git("worktree", "add", "-q", "-b", "dash/j1-e1", str(work), "main", cwd=bare)
    return work


def porcelain(wt, out=None):
    """What git sees in the worktree, with the agent's environment (the exclude file the runner hands it)."""
    return git("status", "--porcelain", "--untracked-files=all", cwd=wt, env=skills.git_env((out or {}).get("exclude_file")))


def test_skills_and_rules_are_written_as_claude_code_skills_and_one_rules_file(wt):
    bundle = {"skills": [skill("colours", "PALETTE", [("references/tone.md", b"Plain words."), ("tokens.css", b":root{}")]), skill("look", "LAYOUT")],
              "rules": {"text": "# Crewboss rules for this job\n\n## Locked rules\n\nNever skip sign-in.\n", "count": 1, "locked": 1}, "index": []}
    out = skills.install(wt, bundle, wt.parent)
    assert out["written"] == ["colours", "look", "rules"] and out["skipped"] == []
    d = wt / ".claude" / "skills" / "colours"
    assert (d / "SKILL.md").read_text().startswith("---\nname: colours") and (d / "references" / "tone.md").read_text() == "Plain words."
    assert (d / "tokens.css").read_bytes() == b":root{}"
    assert "Never skip sign-in." in (wt / ".claude" / "rules" / "crewboss-rules.md").read_text()
    # none of it is picked up by a commit, in the agent's environment; without that environment git would see it (the check bites)
    assert ".claude/skills/colours/" in porcelain(wt)
    env = skills.git_env(out["exclude_file"])
    assert porcelain(wt, out) == ""
    git("add", "-A", cwd=wt, env=env)
    assert git("diff", "--cached", "--name-only", cwd=wt, env=env) == ""
    assert Path(out["exclude_file"]).parent == wt.parent and wt not in Path(out["exclude_file"]).parents       # kept outside the worktree


def test_the_repos_own_skills_are_never_overwritten_and_its_project_layer_wins(wt):
    bundle = {"skills": [skill("repo-own", "SERVER VERSION"), skill("in-code", "SERVER VERSION TOO"), skill("other", "OK")], "rules": None, "index": []}
    out = skills.install(wt, bundle, wt.parent)
    assert out["written"] == ["other"] and sorted(out["skipped"]) == ["in-code", "repo-own"] and len(out["notes"]) == 2
    assert (wt / ".claude" / "skills" / "repo-own" / "SKILL.md").read_text() == "REPO'S OWN\n"          # the tracked file is untouched
    # the skill kept in the repo's .crewboss/skills is copied where Claude Code reads it (all its files)
    assert (wt / ".claude" / "skills" / "in-code" / "SKILL.md").read_text().endswith("FROM THE REPO\n")
    assert (wt / ".claude" / "skills" / "in-code" / "notes.md").read_text() == "notes\n"
    assert porcelain(wt, out) == "" and git("diff", "--stat", cwd=wt) == ""


def test_an_existing_rules_file_of_the_repo_is_left_alone(wt):
    (wt / ".claude" / "rules").mkdir(parents=True)
    (wt / ".claude" / "rules" / "crewboss-rules.md").write_text("THE REPO'S\n")
    out = skills.install(wt, {"skills": [], "rules": {"text": "# rules\n\nx\n", "count": 1, "locked": 0}}, wt.parent)
    assert out["written"] == [] and out["skipped"] == ["rules"] and (wt / ".claude" / "rules" / "crewboss-rules.md").read_text() == "THE REPO'S\n"


def test_skills_not_loaded_for_this_kind_of_job_are_listed_in_the_rules_file(wt):
    out = skills.install(wt, {"skills": [], "rules": None, "index": [{"key": "review-only", "description": "Only for reviews."}]}, wt.parent)
    assert out["written"] == ["rules"]
    assert "review-only: Only for reviews." in (wt / ".claude" / "rules" / "crewboss-rules.md").read_text()


@pytest.mark.parametrize("key", ["../evil", "a/b", "A", "", ".hidden", "x" * 80, "with space", "a\\b"])
def test_a_bad_skill_key_is_dropped(wt, key):
    out = skills.install(wt, {"skills": [skill(key, "X")], "rules": None}, wt.parent)
    assert out["written"] == [] and not (wt.parent / "evil").exists() and not any(p.name == "evil" for p in wt.rglob("*"))
    assert porcelain(wt, out) == ""


@pytest.mark.parametrize("bad", ["../evil.txt", "/abs.txt", "a/../../evil.txt", "a\\b.txt", "C:/x.txt", "SKILL.md", "a//b.txt", ""])
def test_a_bad_file_path_stops_that_skill_and_writes_nothing_outside(wt, bad):
    out = skills.install(wt, {"skills": [skill("bad", "X", [(bad, b"x")]), skill("good", "Y")], "rules": None}, wt.parent)
    assert out["written"] == ["good"] and out["skipped"] == ["bad"]
    assert not (wt / "evil.txt").exists() and not (wt.parent / "evil.txt").exists() and not Path("/abs.txt").exists()


def test_bad_encoding_and_oversize_files_are_refused(wt):
    s = skill("enc", "X")
    s["files"] = [{"path": "a.txt", "b64": "!!!not base64!!!"}]
    big = skill("big", "X", [("a.bin", b"x" * (skills.MAX_FILE + 1))])
    out = skills.install(wt, {"skills": [s, big, skill("ok", "Y")], "rules": None}, wt.parent)
    assert out["written"] == ["ok"] and sorted(out["skipped"]) == ["big", "enc"]


def test_a_symlinked_claude_folder_means_nothing_is_written(wt, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    import shutil
    shutil.rmtree(wt / ".claude")
    (wt / ".claude").symlink_to(outside)
    out = skills.install(wt, {"skills": [skill("x", "Y")], "rules": {"text": "r", "count": 1, "locked": 0}}, wt.parent)
    assert out["written"] == [] and "link" in out["notes"][0] and list(outside.iterdir()) == []


def test_a_symlinked_skill_folder_in_the_repos_project_layer_is_not_followed(wt, tmp_path):
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "SKILL.md").write_text("STOLEN")
    (wt / ".crewboss" / "skills" / "linked").symlink_to(secret)
    out = skills.install(wt, {"skills": [], "rules": None}, wt.parent)
    assert not (wt / ".claude" / "skills" / "linked").exists() and out["written"] == []


def test_nothing_in_the_bundle_means_nothing_written(wt):
    out = skills.install(wt, None, wt.parent)
    assert out["written"] == [] and out["skipped"] == [] and out["notes"] == []
    assert skills.install(wt, {"skills": [], "rules": None, "index": []}, wt.parent)["written"] == []


# --- the worker -------------------------------------------------------------------------------------------------------------

class FakeLink:
    def __init__(self):
        self.sent = []

    async def rpc(self, kind, **kw):
        self.sent.append((kind, kw))
        return {"ok": True}


def make_worker(tmp_path):
    return worker.Worker(FakeLink(), {}, tmp_path, worker.Status(), "http://x")


def test_the_worker_installs_before_the_agent_and_reports_what_it_wrote(wt, tmp_path):
    w = make_worker(tmp_path)
    job = {"job_id": 1, "fence": 1, "skills": {"skills": [skill("colours", "P"), skill("repo-own", "S")], "rules": None, "index": []}}
    state = tmp_path / "state"
    (state / "jobs" / "j1-e1").mkdir(parents=True)
    report = w._install_skills(wt, job)
    assert report["written"] == ["colours"] and report["skipped"] == ["repo-own"]
    asyncio.run(w._report_skills(job, report))
    assert w.link.sent == [("skills", {"job_id": 1, "fence": 1, "written": ["colours"], "skipped": ["repo-own"]})]


def test_an_old_server_sends_no_skills_so_the_runner_sends_no_new_frame(wt, tmp_path):
    w = make_worker(tmp_path)
    assert w._install_skills(wt, {"job_id": 1, "fence": 1}) is None
    asyncio.run(w._report_skills({"job_id": 1, "fence": 1}, None))
    assert w.link.sent == []


def test_a_failing_install_never_stops_the_job(wt, tmp_path, monkeypatch):
    w = make_worker(tmp_path)
    monkeypatch.setattr(skills, "install", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")))
    job = {"job_id": 1, "fence": 1, "skills": {"skills": [skill("a", "x")], "rules": None}}
    report = w._install_skills(wt, job)
    assert report["written"] == [] and report["skipped"] == ["a"] and "disk full" in report["notes"][0]


def test_a_failed_skill_leaves_no_half_written_folder_and_is_still_excluded(wt):
    bad = skill("clash", "X", [("scripts", b"a file"), ("scripts/run.py", b"under it")])        # a file and a folder of the same name
    out = skills.install(wt, {"skills": [bad, skill("good", "Y")], "rules": None}, wt.parent)
    assert out["written"] == ["good"] and out["skipped"] == ["clash"]
    assert not (wt / ".claude" / "skills" / "clash").exists()
    assert not [p for p in (wt / ".claude" / "skills").iterdir() if ".tmp-" in p.name]               # no temporary folder left either
    assert "/.claude/skills/clash/" in Path(out["exclude_file"]).read_text()                          # every attempted path is excluded
    assert porcelain(wt, out) == ""


def test_a_write_that_fails_half_way_is_rolled_back(wt, monkeypatch):
    real = skills._write
    calls = []

    def flaky(dest, data):
        calls.append(dest.name)
        if dest.name == "second.txt":
            raise OSError("disk full")
        return real(dest, data)
    monkeypatch.setattr(skills, "_write", flaky)
    out = skills.install(wt, {"skills": [skill("half", "X", [("first.txt", b"1"), ("second.txt", b"2")])], "rules": None}, wt.parent)
    assert out["skipped"] == ["half"] and not (wt / ".claude" / "skills" / "half").exists()
    assert not [p for p in (wt / ".claude" / "skills").iterdir() if p.name.startswith(".half")]
    assert porcelain(wt, out) == ""


# --- the second look trusts a copied repo skill only through the pin of its SOURCE (#829 review 3) -------------------------------------------

def test_a_copied_repo_skill_is_trusted_only_when_its_source_path_is_pinned_with_those_bytes(wt):
    import hashlib
    out = skills.install(wt, {"skills": [skill("srv", "S")], "rules": None}, wt.parent)
    src = ".crewboss/skills/in-code/SKILL.md"
    dest = ".claude/skills/in-code/SKILL.md"
    assert out["sources"][dest] == src and out["sources"][".claude/skills/in-code/notes.md"] == ".crewboss/skills/in-code/notes.md"
    assert ".claude/skills/srv/SKILL.md" in out["files"] and ".claude/skills/srv/SKILL.md" not in out["sources"]     # the server's own
    digest = lambda rel: hashlib.sha256((wt / rel).read_bytes()).hexdigest()  # noqa: E731
    trusted = skills.trusted_files(out, set())
    assert (".claude/skills/srv/SKILL.md", digest(".claude/skills/srv/SKILL.md")) in trusted      # server skills stay trusted
    assert (dest, digest(dest)) not in trusted                                                   # a copy never vouches for itself
    trusted = skills.trusted_files(out, {(src, digest(src))})
    assert (dest, digest(dest)) in trusted and (".claude/skills/in-code/notes.md", digest(".claude/skills/in-code/notes.md")) not in trusted
    assert (dest, digest(dest)) not in skills.trusted_files(out, {(src, "0" * 64)})              # pinned bytes differ from the bytes copied


def test_install_finds_the_repo_skills_and_dot_claude_in_any_letter_case(tmp_path):
    wt = tmp_path / "w"
    (wt / ".CREWBOSS" / "Skills" / "up").mkdir(parents=True)
    (wt / ".CREWBOSS" / "Skills" / "up" / "SKILL.md").write_text("---\nname: up\ndescription: d\n---\nx\n")
    (wt / ".CLAUDE").mkdir()
    out = skills.install(wt, {"skills": [skill("srv", "S")], "rules": None})
    assert out["sources"] == {".CLAUDE/skills/up/SKILL.md": ".CREWBOSS/Skills/up/SKILL.md"}      # real on-disk names, not the spelling in the code
    assert set(out["files"]) == {".CLAUDE/skills/up/SKILL.md", ".CLAUDE/skills/srv/SKILL.md"} and not (wt / ".claude").exists()
