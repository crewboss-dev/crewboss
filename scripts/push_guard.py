#!/usr/bin/env python3
"""Push guard: what is about to leave this machine must carry no secret, no private value and no internal address.

Run by scripts/hooks/pre-push, which git feeds one line per ref being pushed on stdin ("<local ref> <local sha> <remote ref>
<remote sha>"); the remote's name and URL are arguments 1 and 2. For each pushed ref it checks, in order:

  1. FILE NAMES AND BINARIES. No commit in the pushed range adds or changes a secret-bearing file (.env*, *.bak, *.dump, *.sqlite,
     *.db, *.pem, *.key, *.p12, id_*, credentials.json, *.tfstate, .npmrc ... see commit_guard.BAD_NAMES; templates ending .example,
     .sample or .template and *.pub keys are fine). A binary file cannot be content-scanned, so each is LISTED and blocked unless its
     path matches an asset pattern in scripts/binary-allow.txt;
  2. THE TREE AT THE PUSHED COMMIT: scripts/commit_guard.py --strict over that commit's files (not the working tree), reading the
     baseline and allowlist from that commit too: private denylist, built-in secret patterns, the ratchet on internal addresses;
  3. EVERYTHING ADDED across the pushed commits, even if a later commit removed it again: private denylist values, secret patterns
     and RFC1918 / CGNAT addresses (merge commits count: their conflict resolutions are scanned);
  4. METADATA: commit messages and author names, every file PATH added or changed, the ref names and annotated tag objects being
     pushed, against the private denylist, secret patterns and addresses;
  5. THE ADDRESS BASELINE may not rise above the copy at the commit the push builds on;
  6. GITLEAKS over the pushed commits. A missing binary is skipped with a note unless CI=true or CREWBOSS_REQUIRE_GITLEAKS=1.

First push of ONE commit to an EMPTY remote (for example the public repository's first release): what the commit adds is its whole tree,
so the tree checks in 2 stand in for the added-address check in 3 and the baseline comparison in 5 (the baseline in that commit is
still enforced against the tree). A longer history pushed to an empty remote is checked commit by commit: squash it first.

Every message names files, rules and counts, never a matched value. A deleted ref pushes nothing and passes. Exit 1 blocks the push.
Stdlib only.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import commit_guard  # noqa: E402

ZERO = "0" * 40
forbidden_name = commit_guard.forbidden_name


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def toplevel() -> Path:
    out = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if out.returncode:
        raise SystemExit("push guard: not inside a git repository")
    return Path(out.stdout.strip())


def remote_view(root: Path, remote: str, url: str) -> tuple[list[str], bool, bool]:
    """(commit ids the remote HAS that exist here, whether it is EMPTY, whether we could tell at all). Always asked of the remote itself
    with ls-remote: refs/remotes/* are never trusted, because they describe whatever `origin` used to be (a repo whose origin was
    re-pointed at another remote still carries the old one's tracking refs). When the remote cannot be asked, `ok` is False and the
    caller scans the whole history being pushed."""
    target = url or remote
    if not target:
        return [], False, False
    if (root, target) in _VIEW:
        return _VIEW[(root, target)]
    _VIEW[(root, target)] = view = _remote_view(root, target)
    return view


_VIEW: dict = {}


def _remote_view(root: Path, target: str) -> tuple[list[str], bool, bool]:
    try:
        res = subprocess.run(["git", "ls-remote", "--heads", "--tags", target], cwd=root, capture_output=True, text=True, timeout=30)
    except (subprocess.TimeoutExpired, OSError):
        return [], False, False
    if res.returncode:
        return [], False, False
    shas = sorted({ln.split()[0] for ln in res.stdout.splitlines() if ln.strip()})
    if not shas:
        return [], True, True
    return [s for s in shas if _git(root, "cat-file", "-e", f"{s}^{{commit}}").returncode == 0], False, True


def pushed_spec(root: Path, local_sha: str, remote_sha: str, known: list[str]) -> list[str]:
    """git rev-list arguments naming exactly the commits this push adds: the new tip, minus what the remote already has."""
    known = sorted(set(known) | ({remote_sha} if remote_sha != ZERO and _git(root, "cat-file", "-e", f"{remote_sha}^{{commit}}").returncode == 0
                                  else set()))
    return [local_sha, "--not", *known] if known else [local_sha]


def root_commit(root: Path, local_sha: str) -> str | None:
    out = _git(root, "rev-list", "--max-parents=0", local_sha).stdout.split()
    return out[-1] if out else None


def default_head(root: Path, remote: str, url: str) -> str | None:
    """The sha of the remote's DEFAULT branch (ls-remote --symref HEAD, else refs/heads/main), or None. Never a tag."""
    target = url or remote
    if not target:
        return None
    key = ("default", root, target)
    if key in _VIEW:
        return _VIEW[key]
    sha = None
    try:
        res = subprocess.run(["git", "ls-remote", "--symref", target, "HEAD"], cwd=root, capture_output=True, text=True, timeout=30)
        lines = res.stdout.splitlines() if res.returncode == 0 else []
        sym = next((ln.split()[1] for ln in lines if ln.startswith("ref:")), None)
        if sym and sym.startswith("refs/heads/"):
            r2 = subprocess.run(["git", "ls-remote", target, sym], cwd=root, capture_output=True, text=True, timeout=30)
            sha = next((ln.split()[0] for ln in r2.stdout.splitlines() if ln.split()[1:] == [sym]), None) if r2.returncode == 0 else None
        if sha is None:
            r3 = subprocess.run(["git", "ls-remote", target, "refs/heads/main"], cwd=root, capture_output=True, text=True, timeout=30)
            sha = next((ln.split()[0] for ln in r3.stdout.splitlines() if ln.split()[1:] == ["refs/heads/main"]), None) \
                if r3.returncode == 0 else None
    except (subprocess.TimeoutExpired, OSError):
        sha = None
    if sha and _git(root, "cat-file", "-e", f"{sha}^{{commit}}").returncode != 0:
        sha = None
    _VIEW[key] = sha
    return sha


def base_commit(root: Path, local_sha: str, remote_sha: str, known: list[str], default: str | None = None) -> str | None:
    """The commit this push builds on, whose copy of the baseline the pushed one may not exceed (and whose binary allowlist applies):
    the old tip of the ref being updated, or, for a NEW branch, the merge-base with the remote's DEFAULT branch head. Never picked from
    tags or other branches (an old tag would let a baseline that was lowered since come back); unknown = None = strict."""
    if remote_sha != ZERO and _git(root, "cat-file", "-e", f"{remote_sha}^{{commit}}").returncode == 0:
        return remote_sha
    if default:
        mb = _git(root, "merge-base", local_sha, default)
        if mb.returncode == 0 and mb.stdout.strip():
            return mb.stdout.strip()
    return None


def check_ref(root: Path, remote: str, url: str, local_ref: str, local_sha: str, remote_ref: str, remote_sha: str, say) -> int:
    # A commit is NEW only if no ref the remote advertises already reaches it: pushing main to a commit that is already on the remote
    # under another branch sends nothing. The remote is asked live (ls-remote), never the tracking refs.
    known, empty_remote, ok = remote_view(root, remote, url)
    if remote_sha != ZERO and _git(root, "cat-file", "-e", f"{remote_sha}^{{commit}}").returncode == 0:
        known, empty_remote, ok = sorted(set(known) | {remote_sha}), False, True    # the push handshake itself named this tip
    spec = pushed_spec(root, local_sha, remote_sha, known)
    commits = _git(root, "rev-list", *spec).stdout.split()
    deny = commit_guard.read_lines(commit_guard.home() / "denylist.txt")
    tag_shas = [local_sha] if commit_guard.tag_text(root, local_sha) else []
    ref_names = [n for n in (local_ref, remote_ref) if n]
    if not ok:
        say("push guard: the remote could not be asked what it already has (ls-remote failed), so the WHOLE history being pushed is "
            "checked.")
    if not commits:
        # Nothing new to send, but a tag or branch NAME can still be: its text and name are public.
        meta = commit_guard.metadata_hits([], root, deny, ref_names, tag_shas)
        for line in meta:
            say(line)
        return 1 if meta else 0
    tip_only = empty_remote and len(commits) == 1
    bad = 0
    # 1. file names and binaries
    names, binaries = commit_guard.range_files(spec, root)
    for n in names:
        say(f"{n}: a file of this kind (secret, key, dump, credentials or environment file) must not be pushed")
    base = None if tip_only else base_commit(root, local_sha, remote_sha, known, default_head(root, remote, url))
    real_base = base                                       # a base the remote really has; NOT the fail-closed root fallback below
    if not ok and base is None:
        base = root_commit(root, local_sha)                # fail closed: the policy files of the oldest commit, not the pushed one
    # the allowlist of the commit this builds on, so a push cannot widen its own; the pushed one only for a first single commit
    policy = base or (local_sha if tip_only else root_commit(root, local_sha) or local_sha)
    ballow = commit_guard.lines_of(commit_guard.read_blob(root, policy, commit_guard.BINARY_ALLOW_REL) or "")
    blocked = commit_guard.binary_findings(binaries, ballow, root, local_sha, say)
    if blocked:
        say(f"push guard: binary files outside the asset folders in {commit_guard.BINARY_ALLOW_REL} (or whose bytes do not match their "
            "extension) cannot be checked, so they are blocked: remove them, or (for a real asset) add its path pattern to that file "
            "in a reviewed change.")
    if names:
        say("push guard: remove those files from every commit being pushed (git rm, then amend or rebase); "
            "a template ending .example is fine.")
    bad |= 1 if (names or blocked) else 0
    # 2. the tree at the pushed commit (secrets, private denylist, address ratchet, stale headroom); gitleaks runs below
    bad |= 1 if commit_guard.main(["--strict", "--no-gitleaks", "--rev", local_sha], root) else 0
    # 3. everything ADDED across the pushed commits (denylist, secrets, addresses; not addresses for a first push to an empty remote)
    # Addresses: ONE commit that BUILDS ON a known base (a release pull request) is judged by VALUE: an added address is allowed only when
    # that exact value is already in the same file at the base (or on a line the commit removes). A value new to the file is blocked as
    # before; touching a line that carries a known fixture address is not. The tree ratchet (step 2) and step 5 still apply. With several
    # commits (an add that a later commit removes is still in history) or no known base, every added address is blocked.
    adds = commit_guard.added_hits(spec, root, deny, ips=not tip_only, ip_base=real_base if (real_base and len(commits) == 1) else None)
    for line in adds:
        say(line)
    if adds:
        say("push guard: values from the private denylist, secrets and internal addresses (10.x, 172.16-31.x, 192.168.x, "
            "100.64-127.x) must not be added to the public tree, even if a later commit removes them: rewrite the history. Use "
            "documentation addresses (192.0.2.x, 198.51.100.x, 203.0.113.x) and .example/.test/.invalid names.")
        bad = 1
    # 4. metadata: messages, author names, paths, ref names, tag objects
    meta = commit_guard.metadata_hits(spec, root, deny, ref_names, tag_shas)
    for line in meta:
        say(line)
    if meta:
        say("push guard: commit messages, file paths, ref names and tag text are public too: rewrite them (amend, rebase, or "
            "re-create the tag) so they carry no instance-only value or internal address.")
        bad = 1
    # 5. the baseline may only shrink
    if base:
        grew = commit_guard.baseline_grew(root, base, local_sha)
        for line in grew:
            say(line)
        bad |= 1 if grew else 0
    # 6. gitleaks over the pushed commits
    bad |= gitleaks_range(root, " ".join(spec))
    return bad


def gitleaks_range(root: Path, log_opts: str) -> int:
    import shutil
    exe = shutil.which("gitleaks")
    required = commit_guard.gitleaks_required()
    if not exe:
        if required:
            print("push guard: gitleaks is required here but is not installed: " + commit_guard.GITLEAKS_HINT, file=sys.stderr)
            return 1
        print("push guard: gitleaks not installed, the history scan was skipped (" + commit_guard.GITLEAKS_HINT + "). CI runs it.",
              file=sys.stderr)
        return 0
    config = root / ".gitleaks.toml"
    common = ["--redact", "--no-banner", "--exit-code", "1", f"--log-opts={log_opts}"] + (["-c", str(config)] if config.is_file() else [])
    # `gitleaks git` is the current spelling; releases before 8.19 call it `detect --source .`
    attempts = [[exe, "git", ".", *common], [exe, "detect", "--source", ".", *common]]
    rc = commit_guard._run_attempts(attempts, root, required)
    if rc:
        print("push guard: gitleaks found a possible secret in the commits being pushed (values redacted above). Rewrite those "
              "commits to drop it, and rotate the secret: it was in your history.", file=sys.stderr)
    return rc


def main(argv: list[str], stdin) -> int:
    remote = argv[0] if argv else ""
    url = argv[1] if len(argv) > 1 else ""
    root = toplevel()
    say = lambda m: print(m, file=sys.stderr)          # noqa: E731
    bad = 0
    for line in stdin:
        parts = line.split()
        if len(parts) < 4 or parts[1] == ZERO:
            continue                                   # blank, or a branch deletion
        bad |= check_ref(root, remote, url, parts[0], parts[1], parts[2], parts[3], say)
    if bad:
        say("push guard: PUSH BLOCKED. Nothing was sent. Fix the above and push again; do not bypass the hook.")
    return bad


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], sys.stdin))
