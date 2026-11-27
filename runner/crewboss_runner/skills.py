"""Putting a job's skills into its worktree (task #718).

The dashboard sends the combined skill set with the claim (app/skill_delivery.py). This module writes it as Claude Code skills
(`.claude/skills/<key>/SKILL.md` + files) and one rules file (`.claude/rules/crewboss-rules.md`), BEFORE the agent starts.

What it will not do:
- Clobber what the repo already has: a skill folder or the rules file that exists is left alone and reported as skipped. A skill kept
  in the repo itself (`.crewboss/skills/<key>/`, the project layer from the code) wins over the server's skill of that key, and is
  copied into `.claude/skills/` so Claude Code finds it.
- Write outside the skill's own folder: keys and file names are checked again here (the server's checks are not trusted), a path with `..`,
  a leading slash, a backslash or a drive letter is refused, nothing is followed through a symlink, sizes are capped.
- Let the agent commit them by accident: every path written goes into an exclude file kept OUTSIDE the worktree, which the agent's git
  uses through its environment (git_env).
- Trust anything by parsing it: skills are copied verbatim (NOT stripped). The repo's own skills were already pinned by the project Owner
  (agentscan: deny by default, (path, sha256) pins) BEFORE this install; the server's skills are Crewboss's own, already reviewed. The
  result carries `files` {repo path: sha256} of everything written, so the worker's second look (agentscan.scan_agent_files) treats exactly
  those bytes as trusted and anything else under .claude/{agents,commands,skills} as unreviewed.
Nothing from a skill is ever executed here."""
from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
PATH_RE = re.compile(r"^[A-Za-z0-9._-][A-Za-z0-9._/-]{0,199}$")
MAX_FILE = 1024 * 1024
MAX_SKILL_BYTES = 4 * 1024 * 1024
MAX_SKILLS = 200
RULES_REL = ".claude/rules/crewboss-rules.md"


def _safe_rel(path: str) -> bool:
    return bool(PATH_RE.match(path)) and ".." not in path.split("/") and "//" not in path and "\\" not in path


def _inside(root: Path, p: Path) -> bool:
    try:
        return os.path.commonpath([root.resolve(), p.resolve()]) == str(root.resolve())
    except (OSError, ValueError):
        return False


def _write(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "xb") as fh:                       # "x": never replace a file that is already there
        fh.write(data)


def _ci_children(parent: Path, name: str) -> list[Path]:
    """Every child of `parent` whose name equals `name` ignoring case (on NTFS / APFS `.CREWBOSS` IS `.crewboss`; on Linux they are different
    folders and BOTH are looked at: the inventory in agentscan lists every spelling too). Sorted; [] when `parent` cannot be listed."""
    try:
        return [parent / n for n in sorted(os.listdir(parent)) if n.lower() == name.lower()]
    except OSError:
        return []


def _ci_child(parent: Path, name: str) -> Path:
    """The existing child of `parent` called `name` in any case (the exact spelling first), else `parent / name`."""
    found = _ci_children(parent, name)
    exact = parent / name
    return exact if exact in found or not found else found[0]


def _repo_skills(wt: Path) -> dict[str, Path]:
    """{key: folder} of the skills the repo keeps in `.crewboss/skills/` in ANY letter case (real folders with a real SKILL.md, no symlinks)."""
    out: dict[str, Path] = {}
    for cb in _ci_children(wt, ".crewboss"):
        if cb.is_symlink() or not cb.is_dir():
            continue
        for base in _ci_children(cb, "skills"):
            if base.is_symlink() or not base.is_dir() or not _inside(wt, base):
                continue
            for d in sorted(base.iterdir()):
                if (KEY_RE.match(d.name) and d.name not in out and d.is_dir() and not d.is_symlink()
                        and (d / "SKILL.md").is_file() and not (d / "SKILL.md").is_symlink()):
                    out[d.name] = d
    return out


def _copy_repo_skill(src: Path, dest: Path) -> None:
    total = 0
    for root, dirs, files in os.walk(src, followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(root) / d).is_symlink()]
        for f in files:
            p = Path(root) / f
            rel = p.relative_to(src).as_posix()
            if p.is_symlink() or not _safe_rel(rel):
                continue
            size = p.stat().st_size
            total += size
            if size > MAX_FILE or total > MAX_SKILL_BYTES:
                continue
            _write(dest / rel, p.read_bytes())


def install(wt: Path, bundle: dict | None, job_dir: Path | None = None) -> dict:
    """Write the bundle into `wt`. -> {"written": [keys, 'rules' for the rules file], "skipped": [keys], "notes": [text], "exclude_file",
    "files": {repo path: sha256 of every file written into .claude/skills/}}."""
    written: list[str] = []
    skipped: list[str] = []
    notes: list[str] = []
    wt = Path(wt)
    root = _ci_child(wt, ".claude")
    skills_dir = _ci_child(root, "skills")
    if root.is_symlink() or skills_dir.is_symlink() or _ci_child(root, "rules").is_symlink() or not _inside(wt, root):
        return {"written": [], "skipped": [], "notes": ["the repository's .claude folder is a link: no skills were written"], "exclude_file": None,
                "files": {}}
    paths: list[str] = []
    dirs: list[Path] = []                               # every skill folder this install created
    sources: dict[str, str] = {}                        # {copied file (repo path): the repo file it was copied FROM}
    repo = _repo_skills(wt)
    for key, src in repo.items():
        dest = skills_dir / key
        if os.path.lexists(dest):
            continue                                    # already a Claude Code skill of the repo's own
        _copy_repo_skill(src, dest)
        dirs.append(dest)
        paths.append(f"/.claude/skills/{key}/")
        for droot, _dn, dfiles in os.walk(dest, followlinks=False):
            for f in dfiles:
                d = Path(droot) / f
                sources[d.relative_to(wt).as_posix()] = (src / d.relative_to(dest)).relative_to(wt).as_posix()
    for s in list((bundle or {}).get("skills") or [])[:MAX_SKILLS]:
        key = s.get("key") if isinstance(s, dict) else None
        if not isinstance(key, str) or not KEY_RE.match(key):
            continue
        dest = skills_dir / key
        if key in repo or os.path.lexists(dest):
            skipped.append(key)
            notes.append(f"{key}: the repository has its own, so the server's was not written")
            continue
        try:
            body = str(s.get("skill_md") or "")
            if not body.strip() or len(body) > MAX_SKILL_BYTES:
                raise ValueError("no usable SKILL.md")
            files = []
            total = len(body)
            for f in s.get("files") or []:
                rel = str(f.get("path") or "")
                if not _safe_rel(rel) or rel == "SKILL.md":
                    raise ValueError(f"bad file path {rel[:60]!r}")
                data = base64.b64decode(str(f.get("b64") or ""), validate=True)
                total += len(data)
                if len(data) > MAX_FILE or total > MAX_SKILL_BYTES:
                    raise ValueError("a file is too large")
                files.append((rel, data))
            paths.append(f"/.claude/skills/{key}/")                 # excluded from a commit even if the write below fails half way
            names = {r for r, _d in files}
            if any(o.startswith(r + "/") for r in names for o in names):
                raise ValueError("a file path is also a folder")
            # Write into a temporary sibling folder and rename it into place only when it is complete: a failure can never leave a
            # half-written skill folder behind.
            tmp = dest.with_name(f".{key}.tmp-{os.getpid()}")
            shutil.rmtree(tmp, ignore_errors=True)
            try:
                tmp.mkdir(parents=True)
                _write(tmp / "SKILL.md", body.encode("utf-8"))
                for rel, data in files:
                    target = tmp / rel
                    if not _inside(tmp, target) or target.is_symlink():
                        raise ValueError(f"bad file path {rel[:60]!r}")
                    _write(target, data)
                tmp.rename(dest)
                dirs.append(dest)
            except BaseException:
                shutil.rmtree(tmp, ignore_errors=True)
                raise
        except (ValueError, OSError, binascii.Error) as exc:
            skipped.append(key)
            notes.append(f"{key}: not written ({exc})"[:200])
            continue
        written.append(key)
    rules = (bundle or {}).get("rules")
    text = ""
    if isinstance(rules, dict) and str(rules.get("text") or "").strip():
        text = str(rules["text"])
    index = [i for i in (bundle or {}).get("index") or [] if isinstance(i, dict) and KEY_RE.match(str(i.get("key") or ""))]
    if index:
        text = (text.rstrip() + "\n\n" if text else "# Crewboss rules for this job\n\n") + "## Skills not loaded for this kind of job\n\n" + \
            "\n".join(f"- {i['key']}: {' '.join(str(i.get('description') or '').split())[:200]}" for i in index) + "\n"
    if text and len(text) <= MAX_SKILL_BYTES:
        rules_path = root / "rules" / "crewboss-rules.md"
        if os.path.lexists(rules_path):
            skipped.append("rules")
            notes.append("rules: the repository already has a crewboss-rules.md")
        else:
            try:
                _write(rules_path, text.encode("utf-8"))
                written.append("rules")
                paths.append("/" + RULES_REL)
            except OSError as exc:
                skipped.append("rules")
                notes.append(f"rules: not written ({exc})"[:200])
    if paths:
        paths.append("/.claude/skills/.*.tmp-*/")
    ex = exclude(wt, paths, job_dir) if paths else None
    files = _written_hashes(wt, dirs)
    return {"written": written, "skipped": skipped, "notes": notes, "exclude_file": ex, "files": files,
            "sources": {d: s for d, s in sources.items() if d in files}}


def trusted_files(report: dict | None, pins) -> set[tuple[str, str]]:
    """What the worker's second look may trust beyond the Owner's pins: {(repo path, sha256)}. A file the SERVER delivered is Crewboss's own.
    A file COPIED from the repo's `.crewboss/skills` is trusted only if the pin of its SOURCE path matches the bytes copied (never because the
    copy hashes to itself): a skill the pre-install scan never saw (e.g. a differently-cased folder) stays unreviewed."""
    pinned = set(pins or ())
    files = (report or {}).get("files") or {}
    sources = (report or {}).get("sources") or {}
    return {(p, h) for p, h in files.items() if p not in sources or (sources[p], h) in pinned}


def _written_hashes(wt: Path, dirs: list[Path]) -> dict[str, str]:
    """{repo path: sha256} of every regular file under the skill folders this install wrote (never follows a link)."""
    out: dict[str, str] = {}
    for base in dirs:
        for root, dirs_, files in os.walk(base, followlinks=False):
            for f in files:
                p = Path(root) / f
                if not p.is_symlink() and p.is_file():
                    out[p.relative_to(wt).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def exclude(wt: Path, patterns: list[str], job_dir: Path | None) -> str | None:
    """Keep the written paths out of `git add -A`. With a job folder: a gitignore-style file there (outside the worktree), returned so the
    worker can hand it to the agent as `core.excludesFile` through its environment (git_env). The worktree's own config is NOT touched:
    a bare cache plus `extensions.worktreeConfig` makes git refuse to run in the worktree. Without one, the repository's info/exclude."""
    lines = "\n".join(patterns) + "\n"
    if job_dir is not None:
        Path(job_dir).mkdir(parents=True, exist_ok=True)
        f = Path(job_dir) / "skills.exclude"
        f.write_text(lines, encoding="utf-8")
        return str(f)
    try:
        res = subprocess.run(["git", "rev-parse", "--git-path", "info/exclude"], cwd=wt, capture_output=True, text=True, timeout=30)
        target = Path(wt, res.stdout.strip())
        target.parent.mkdir(parents=True, exist_ok=True)
        have = target.read_text(encoding="utf-8").splitlines() if target.exists() else []
        with open(target, "a", encoding="utf-8") as fh:
            fh.write("".join(f"{p}\n" for p in patterns if p not in have))
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def git_env(exclude_file: str | None) -> dict:
    """Environment entries that make every git command of the agent (and its children) ignore the delivered paths."""
    if not exclude_file:
        return {}
    return {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.excludesFile", "GIT_CONFIG_VALUE_0": exclude_file}
