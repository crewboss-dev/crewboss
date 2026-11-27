#!/usr/bin/env python3
"""Commit guard: tracked files must not carry an instance-only value or a secret.

Two checks over every tracked file (git ls-files):
  1. each line of $CREWBOSS_HOME/denylist.txt (default ~/.crewboss/denylist.txt) as a case-insensitive substring. One value per
     line; blank lines and lines starting with # are ignored. No denylist = this check is skipped (with a note);
  2. built-in patterns for secrets (private keys, cloud and token formats).

scripts/denylist-allow.txt lists the paths that are KNOWN to still match and are waiting for a later scrub slice (one repo
path per line, a trailing / means a folder; # comments). Every other match fails. The list is the to-do list: shrink it as
slices land. An entry that no longer matches anything is reported as stale (an error when run with --strict, which the test uses).

    python3 scripts/commit_guard.py            # exit 1 on any match outside the allowlist
    python3 scripts/commit_guard.py --strict   # also exit 1 on stale allowlist entries
    python3 scripts/commit_guard.py --list     # print every match, allowlisted or not (paths only)
    python3 scripts/commit_guard.py --gitleaks-tree   # also scan the whole tree with gitleaks (CI); needs gitleaks
    python3 scripts/commit_guard.py --rev <sha>       # scan the tree AT that commit instead of the working tree (pre-push)
    python3 scripts/commit_guard.py --added-in A..B   # denylist values / secrets / internal addresses ADDED anywhere in that range
    python3 scripts/commit_guard.py --meta-range A..B [--meta-ref NAME]   # the same over commit messages, file paths, ref and tag text
    python3 scripts/commit_guard.py --files-in A..B   # forbidden file names and unscanned binary files in that range
    python3 scripts/commit_guard.py --baseline-base REV   # fail if the address baseline is higher than REV's copy
    python3 scripts/commit_guard.py --write-ip-baseline   # lower scripts/internal-ip-baseline.json after a cleanup

A third built-in check, the generic public denylist, needs no private file: RFC1918 and CGNAT address literals (10.x, 172.16-31.x,
192.168.x, 100.64-127.x). Documentation ranges (RFC 5737: 192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24), loopback and public
addresses never match. Addresses already in the tree are a shrink-only ratchet (scripts/internal-ip-baseline.json: lines per file,
may only go down, a file not listed may have none); an address ADDED by a change is blocked outright with --added-in.

gitleaks (https://github.com/gitleaks/gitleaks, config .gitleaks.toml) is a third check on top of the two above. The pre-commit
hook runs it on the STAGED changes when the `gitleaks` binary is installed, and says so (on stderr) when it is not. It is
REQUIRED, and a missing binary fails with an install hint, when CI=true, CREWBOSS_REQUIRE_GITLEAKS=1, or --gitleaks-tree is given.
Findings are always redacted (gitleaks --redact); this script never prints a matched value.

Stdlib only: it runs from the pre-commit hook on any box. A matched value is never echoed (the output could end up in logs).
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import tempfile
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ALLOW_FILE = ROOT / "scripts" / "denylist-allow.txt"
IP_BASELINE = ROOT / "scripts" / "internal-ip-baseline.json"
IP_BASELINE_REL = "scripts/internal-ip-baseline.json"

# RFC1918 (10/8, 172.16/12, 192.168/16) and CGNAT (100.64/10, the range mesh VPNs hand out) address literals. Octets are range-checked
# and may carry leading zeros; the dots may be regex-escaped; a three-octet prefix with its trailing dot matches too unless it reads as a version. A neighbouring
# digit or dot rules a match out, so dotted version strings of five parts do not match.
_S = r"\\?\."
_O = r"0*(?:25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])"
_FIRST = (rf"(?:0*10{_S}{_O}|0*172{_S}0*(?:1[6-9]|2[0-9]|3[01])|0*192{_S}0*168|"
          rf"0*100{_S}0*(?:6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7]))")
# A full address always matches. A three-octet PREFIX (three octets and a dot) is also an address-shaped hit, except where it reads as a version:
# (a lone x or X after the dot, as in a subnet written with an x, is still an address)
# after a letter, digit, "-", "_" or "@" (eslint-10.2.0.tgz, foo@10.4.1.) or before a letter or digit (10.0.0.dev1).
INTERNAL_IP = re.compile(rf"(?<![0-9.]){_FIRST}{_S}{_O}{_S}{_O}(?![0-9]|{_S}[0-9])|"
                         rf"(?<![0-9.A-Za-z_@-]){_FIRST}{_S}{_O}{_S}(?:[xX](?![0-9A-Za-z])|(?![0-9A-Za-z]))")

# File names that must never be pushed (matched on the lower-cased base name). Templates (.example/.sample/.template) and public keys are fine.
BAD_NAMES = (".env*", "*.bak", "*.dump", "*.sqlite", "*.sqlite3", "*.db", "*.pem", "*.key", "*.p12", "*.pfx", "*.p8", "*.ppk", "*.jks",
             "*.keystore", "*.kdbx", "*.tfstate", "*.tfstate.*", "id_*", "credentials.json", "credentials", "secrets.json",
             "secrets.yml", "secrets.yaml", "service-account*.json", ".npmrc", ".pgpass", ".netrc", ".htpasswd", ".git-credentials",
             ".pypirc", ".dockercfg", "kubeconfig", "*.ovpn", "*.rdp")
OK_SUFFIXES = (".example", ".sample", ".template", ".pub")
BINARY_ALLOW_REL = "scripts/binary-allow.txt"

SECRET_PATTERNS = {
    "private key block": re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"),
    "AWS access key id": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "GitHub token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    "Anthropic/OpenAI style key": re.compile(r"\bsk-(?:ant-)?[A-Za-z0-9_-]{32,}\b"),
    "Slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{20,}\b"),
    "bearer token": re.compile(r"[Bb]earer\s+[A-Za-z0-9._~+/=-]{40,}"),
    "hard-coded secret": re.compile(
        r"""(?i)\b(?:secret|password|passwd|api[_-]?key|token)\b["']?\s*[:=]\s*["'][A-Za-z0-9+/_=-]{24,}["']"""),
    "credential in a URL": re.compile(r"[a-z][a-z0-9+.-]*://[^\s/:@'\"]+:[^\s/@'\"]{6,}@(?!db[:/])[A-Za-z0-9.-]+"),
}


def home() -> Path:
    return Path(os.environ.get("CREWBOSS_HOME") or Path.home() / ".crewboss")


def lines_of(raw: str) -> list[str]:
    return [ln.strip() for ln in raw.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def read_lines(path: Path) -> list[str]:
    try:
        return lines_of(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        return []


def read_blob(root: Path, rev: str | None, rel: str) -> str | None:
    """The text of `rel` at commit `rev` (None = the working tree); None when it does not exist there."""
    if rev is None:
        try:
            return (root / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
    res = subprocess.run(["git", "show", f"{rev}:{rel}"], cwd=root, capture_output=True)
    return res.stdout.decode("utf-8", "replace") if res.returncode == 0 else None


def forbidden_name(path: str) -> bool:
    base = path.rsplit("/", 1)[-1].lower()
    if base.endswith(OK_SUFFIXES):
        return False
    return any(fnmatch.fnmatch(base, pat) for pat in BAD_NAMES)


MAGIC = {
    "png": (b"\x89PNG\r\n\x1a\n",), "jpg": (b"\xff\xd8\xff",), "jpeg": (b"\xff\xd8\xff",), "gif": (b"GIF87a", b"GIF89a"),
    "ico": (b"\x00\x00\x01\x00", b"\x00\x00\x02\x00"), "woff": (b"wOFF",), "woff2": (b"wOF2",),
    "ttf": (b"\x00\x01\x00\x00", b"true"), "otf": (b"OTTO",),
}


def magic_ok(path: str, data: bytes) -> bool:
    """Do the first bytes match what the file's extension says it is? (A database named logo.png must not pass as an asset.)"""
    ext = path.rsplit(".", 1)[-1].lower()
    if ext == "webp":
        return data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    if ext == "svg":
        return b"\0" not in data[:4096] and b"<svg" in data[:65536].lower()
    return any(data.startswith(sig) for sig in MAGIC.get(ext, ()))


def read_bytes(root: Path, rev: str | None, rel: str) -> bytes | None:
    if rev is None:
        try:
            return (root / rel).read_bytes()
        except OSError:
            return None
    res = subprocess.run(["git", "show", f"{rev}:{rel}"], cwd=root, capture_output=True)
    return res.stdout if res.returncode == 0 else None


def binary_findings(binaries: list[tuple[str, str]], allow: list[str], root: Path, rev: str | None, say) -> list[str]:
    """Report every binary file (each blob, at the commit that added it); return the paths blocked: outside the asset allowlist, or
    whose bytes do not match the extension."""
    blocked, said = [], set()
    for commit, b in binaries:
        if not binary_allowed(b, allow):
            msg = f"{b}: binary file, not content-scanned, and not an asset path"
            bad = True
        else:
            data = read_bytes(root, commit, b)
            bad = data is None or not magic_ok(b, data)
            msg = (f"{b}: binary file whose bytes do not match its extension (in commit {commit[:10]}): blocked" if bad else
                   f"{b}: binary file, not content-scanned (allowed: an asset path, bytes match the extension)")
        if bad and b not in blocked:
            blocked.append(b)
        if msg not in said:
            said.add(msg)
            say(msg)
    return blocked


def binary_allowed(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, pat) for pat in patterns)


def tracked(root: Path = ROOT, rev: str | None = None) -> list[str]:
    """Tracked paths: the index's (git ls-files), or those of commit `rev` (git ls-tree; submodules and symlinks left out)."""
    if rev:
        out = subprocess.run(["git", "ls-tree", "-r", "-z", "--full-tree", rev], cwd=root, capture_output=True, check=True).stdout
        paths = []
        for ent in out.decode("utf-8", "replace").split("\0"):
            meta, _, path = ent.partition("\t")
            if path and meta.split()[:1] and meta.split()[0] in ("100644", "100755"):
                paths.append(path)
        return paths
    out = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True).stdout
    return [p for p in out.decode("utf-8", "replace").split("\0") if p]


def read_files(files: list[str], root: Path = ROOT, rev: str | None = None):
    """Yield (path, bytes) for each readable file: from the working tree, or from commit `rev` through one `git cat-file --batch`."""
    if not rev:
        for rel in files:
            try:
                yield rel, (root / rel).read_bytes()
            except OSError:
                continue
        return
    proc = subprocess.Popen(["git", "cat-file", "--batch"], cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        for rel in files:
            proc.stdin.write(f"{rev}:{rel}\n".encode("utf-8"))
            proc.stdin.flush()
            head = proc.stdout.readline().split()
            if len(head) != 3:
                continue                                  # "<spec> missing"
            data = proc.stdout.read(int(head[2]))
            proc.stdout.read(1)
            yield rel, data
    finally:
        proc.stdin.close()
        proc.stdout.close()
        proc.wait()


def ip_counts(files: list[str], root: Path = ROOT, rev: str | None = None) -> dict[str, int]:
    """{path: number of lines that carry an RFC1918 or CGNAT address literal}."""
    out: dict[str, int] = {}
    for rel, data in read_files(files, root, rev):
        if b"\0" in data[:4096]:
            continue
        n = sum(1 for line in data.decode("utf-8", "replace").splitlines() if INTERNAL_IP.search(line))
        if n:
            out[rel] = n
    return dict(sorted(out.items()))


def _ip_baseline(root: Path = ROOT, rev: str | None = None) -> dict:
    try:
        return json.loads(read_blob(root, rev, IP_BASELINE_REL) or "{}")
    except ValueError:
        return {}


def ip_ratchet(files: list[str], root: Path = ROOT, rev: str | None = None) -> list[str]:
    """Files whose count of internal-address lines went UP against the baseline (paths and counts, never the addresses)."""
    base = _ip_baseline(root, rev)
    return [f"{rel}: {n} line(s) with an internal address (baseline {base.get(rel, 0)})"
            for rel, n in ip_counts([f for f in files if f != IP_BASELINE_REL], root, rev).items() if n > base.get(rel, 0)]


def baseline_grew(root: Path, base_rev: str, rev: str | None) -> list[str]:
    """The baseline may only shrink: any file whose count is higher than in `base_rev`'s copy (a file the base did not list counts
    as 0). No copy at the base (the baseline is being introduced) = nothing to compare with."""
    raw = read_blob(root, base_rev, IP_BASELINE_REL)
    if raw is None:
        return []
    try:
        old = json.loads(raw)
    except ValueError:
        return []
    new = _ip_baseline(root, rev)
    return [f"{IP_BASELINE_REL}: the baseline for {rel} was RAISED from {old.get(rel, 0)} to {n}: it may only go down"
            for rel, n in new.items() if n > old.get(rel, 0)]


def _log(root: Path, *args: str) -> str:
    res = subprocess.run(["git", "-c", "core.quotepath=false", "log", "--no-color", "--no-ext-diff", *args],
                         cwd=root, capture_output=True, check=True)
    return res.stdout.decode("utf-8", "replace")


def _spec(rng: str | list[str]) -> list[str]:
    return rng.split() if isinstance(rng, str) else list(rng)


def added_lines(rng: str | list[str], root: Path = ROOT):
    """Yield (path, text) for every line ADDED by the commits of `rng`, parsed from `git log -p --cc` as a diff, not by prefix guessing:
    inside a hunk every line is content (so a content line "++ x" is not a header); a merge shows only what its resolution added
    relative to ALL parents (a combined diff, '+' in every parent column). An add-then-remove is still reported: it is in history."""
    out = _log(root, "-p", "--cc", "-U0", "--format=", *_spec(rng))
    in_hunk, cols, path = False, 1, "?"
    for line in out.split("\n"):
        if line.startswith("@@"):
            in_hunk, cols = True, len(line) - len(line.lstrip("@"))
            cols = max(cols - 1, 1)
        elif line.startswith("diff "):
            in_hunk, path = False, "?"
        elif in_hunk:
            if line[:cols] == "+" * cols and len(line) >= cols:
                yield path, line[cols:]
        elif line.startswith("+++ "):
            p = line[4:].split("\t")[0]
            path = p[2:] if p.startswith("b/") else p


def line_hits(text: str, low_deny: list[str], ips: bool = True, secrets: bool = True) -> list[str]:
    """Rule names (never values) that `text` trips: private denylist entries, built-in secret patterns, internal addresses."""
    ll = text.lower()
    hits = [f"denylist entry #{i}" for i, d in enumerate(low_deny, 1) if d in ll]
    if secrets:
        hits += [name for name, rx in SECRET_PATTERNS.items() if rx.search(text)]
    if ips and INTERNAL_IP.search(text):
        hits.append("an internal (RFC1918 / CGNAT) address")
    return hits


def added_hits(rng: str | list[str], root: Path = ROOT, deny: list[str] = (), ips: bool = True,
               exclude_rev: str | None = None) -> list[str]:
    """Denylist values, secret patterns and (with ips) internal addresses on lines ADDED anywhere in `rng`. Names the file, never the value."""
    low = [d.lower() for d in deny]
    out = set()
    known: dict[str, set] = {}
    for path, text in added_lines(rng, root):
        if path == IP_BASELINE_REL:
            continue
        if exclude_rev:                  # an orphan history: a line the default branch's own copy of the file has is not "added"
            if path not in known:
                known[path] = set((read_blob(root, exclude_rev, path) or "").split("\n"))
            if text in known[path]:
                continue
        for rule in line_hits(text, low, ips):
            out.add(f"{path}: an added line matches {rule}")
    return sorted(out)


def ip_added_by(rng: str | list[str], root: Path = ROOT) -> list[str]:
    return [h for h in added_hits(rng, root, [], True) if "internal" in h]


def tag_text(root: Path, sha: str) -> str:
    """The text of an annotated tag object (and any tags it points to), or '' when `sha` is not a tag."""
    out = []
    for _ in range(5):
        res = subprocess.run(["git", "cat-file", "-t", sha], cwd=root, capture_output=True, text=True)
        if res.returncode or res.stdout.strip() != "tag":
            break
        body = subprocess.run(["git", "cat-file", "-p", sha], cwd=root, capture_output=True, text=True).stdout
        out.append(body)
        first = body.split("\n", 1)[0].split()
        if len(first) != 2 or first[0] != "object":
            break
        sha = first[1]
    return "\n".join(out)


def metadata_hits(rng: str | list[str], root: Path = ROOT, deny: list[str] = (), names: list[str] = (), tag_shas: list[str] = ()) -> list[str]:
    """The same checks over what a push carries besides file CONTENT: commit messages and author/committer names, every file path
    added or changed, the ref names being pushed and annotated tag objects. Messages say where, never the value (a path is not echoed:
    in CI logs it would publish it)."""
    low = [d.lower() for d in deny]
    hits = []
    log = _log(root, "--format=%x00%H%x1f%an%x1f%ae%x1f%cn%x1f%ce%x1f%B%x1e", *_spec(rng)) if _spec(rng) else ""
    for rec in log.split("\x1e"):
        rec = rec.strip("\n").lstrip("\x00")
        if not rec:
            continue
        sha, *rest = rec.split("\x1f", 5)
        for rule in line_hits("\n".join(rest), low):
            hits.append(f"commit {sha[:10]}: its message or author names match {rule}")
    cur = None
    names_log = (_log(root, "--name-only", "--format=%x00%H", "--diff-filter=AMR", "--no-renames", *_spec(rng))
                 if _spec(rng) else "")
    for ln in names_log.split("\n"):
        if ln.startswith("\x00"):
            cur = ln[1:11]
        elif ln:
            for rule in line_hits(ln, low):
                hits.append(f"commit {cur}: a file PATH it adds or changes matches {rule}")
    for n in names:
        for rule in line_hits(n, low):
            hits.append(f"the ref name being pushed matches {rule}")
    for sha in tag_shas:
        for rule in line_hits(tag_text(root, sha), low):
            hits.append(f"an annotated tag being pushed carries text that matches {rule}")
    return sorted(set(hits))


def range_files(rng: str | list[str], root: Path = ROOT, rev: str | None = None) -> tuple[list[str], list[tuple[str, str]]]:
    """(forbidden file names, binary blobs) added or changed by `rng`. Every binary blob is returned as (commit, path), one per commit
    that touched it, so a bad file replaced by a good one in a later commit of the same push is still seen."""
    names, binaries, seen, cur = [], [], set(), None
    for ln in _log(root, "--numstat", "--format=%x00%H", "--diff-filter=AMR", "--no-renames", *_spec(rng)).split("\n"):
        if ln.startswith("\x00"):
            cur = ln[1:]
            continue
        parts = ln.split("\t", 2)
        if len(parts) != 3 or not cur:
            continue
        if forbidden_name(parts[2]) and parts[2] not in names:
            names.append(parts[2])
        if parts[0] == "-" and parts[1] == "-" and (cur, parts[2]) not in seen:
            seen.add((cur, parts[2]))
            binaries.append((cur, parts[2]))
    return sorted(names), binaries


def scan(files: list[str], denylist: list[str], root: Path = ROOT, rev: str | None = None) -> dict[str, list[tuple[int, str]]]:
    """{path: [(line number, what matched)]}. `what` names the rule or the denylist entry's NUMBER, never the value."""
    low = [d.lower() for d in denylist]
    found: dict[str, list[tuple[int, str]]] = {}
    for rel, data in read_files(files, root, rev):
        if b"\0" in data[:4096]:
            continue
        text = data.decode("utf-8", "replace")
        for n, line in enumerate(text.splitlines(), 1):
            ll = line.lower()
            for i, d in enumerate(low, 1):
                if d in ll:
                    found.setdefault(rel, []).append((n, f"denylist entry #{i}"))
            for name, rx in SECRET_PATTERNS.items():
                if rx.search(line):
                    found.setdefault(rel, []).append((n, name))
    return found


def allowed(rel: str, allow: list[str]) -> bool:
    return any(rel == a or (a.endswith("/") and rel.startswith(a)) for a in allow)


def stale(found: dict, allow: list[str], files: list[str]) -> list[str]:
    """Allowlist entries that match no tracked file with a hit (the work is done: delete the line)."""
    return [a for a in allow if not any(allowed(f, [a]) for f in found)]


GITLEAKS_HINT = ("install gitleaks (https://github.com/gitleaks/gitleaks#installing: a release binary, `brew install gitleaks` or "
                 "`go install github.com/zricethezav/gitleaks/v8@latest`) and put it on PATH")


def gitleaks_required() -> bool:
    return os.environ.get("CI", "").lower() in ("1", "true", "yes") or os.environ.get("CREWBOSS_REQUIRE_GITLEAKS") == "1"


def run_gitleaks(tree: bool, required: bool, root: Path = ROOT) -> int:
    """0 = clean (or skipped because the binary is absent and not required), 1 = findings or required-but-missing."""
    exe = shutil.which("gitleaks")
    if not exe:
        if required:
            print(f"commit guard: gitleaks is required here (CI or CREWBOSS_REQUIRE_GITLEAKS) but is not installed: {GITLEAKS_HINT}",
                  file=sys.stderr)
            return 1
        print(f"commit guard: gitleaks not installed, that check was skipped ({GITLEAKS_HINT}). CI runs it.", file=sys.stderr)
        return 0
    config = root / ".gitleaks.toml"
    common = ["--redact", "--no-banner", "--exit-code", "1"] + (["-c", str(config)] if config.is_file() else [])
    # The current spelling first; older releases call the staged scan `protect --staged` and the tree scan `detect --no-git`.
    # cwd=root and a relative source ".", so paths in the report (and the allowlist patterns) are repo-relative either way
    attempts = ([[exe, "dir", ".", *common], [exe, "detect", "--no-git", "--source", ".", *common]] if tree
                else [[exe, "git", "--staged", ".", *common], [exe, "protect", "--staged", "--source", ".", *common]])
    with tempfile.TemporaryDirectory(prefix="crewboss-gitleaks-") as tmp:
        cwd = root
        if tree:
            cwd = _export_tracked(root, Path(tmp))
        return _run_attempts(attempts, cwd, required)


def _export_tracked(root: Path, dest: Path) -> Path:
    """Copy only the tracked files into dest (same relative paths), so a tree scan never sees .env*, .venv*, caches, *.db or
    other agents' worktrees that happen to sit in a dev box's working folder. A fresh CI checkout gives the same set."""
    for rel in tracked(root):
        src = root / rel
        if src.is_symlink() or not src.is_file():
            continue                                    # deleted in the working tree, a symlink, or a submodule
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest / rel)
    return dest


def _run_attempts(attempts: list[list[str]], cwd: Path, required: bool) -> int:
    for cmd in attempts:
        res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
        if res.returncode == 0:
            return 0
        out = res.stdout + res.stderr
        if "unknown command" in out or "unknown flag" in out or "unknown shorthand" in out:
            continue            # this release spells it differently: try the next form
        sys.stderr.write(out)    # --redact: gitleaks masks the values itself
        print("commit guard: gitleaks found a possible secret (values redacted above). Remove it, or if it is a deliberate "
              "fake add the file to [allowlist] in .gitleaks.toml with a reason.", file=sys.stderr)
        return 1
    print("commit guard: could not run this gitleaks release (no known command form worked)", file=sys.stderr)
    return 1 if required else 0


class _Parser(argparse.ArgumentParser):
    def error(self, message):                 # never exit from inside a library call: the caller turns this into rc 2
        raise ValueError(message)


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Options are parsed, never searched for: a value (a branch name, a range) can never be read as a flag. Callers should pass values
    as --option=value, which is the only way a value that starts with a dash is accepted."""
    ap = _Parser(prog="commit_guard.py", add_help=False, allow_abbrev=False)
    for flag in ("--strict", "--list", "--no-gitleaks", "--gitleaks-tree", "--tip-only", "--write-ip-baseline"):
        ap.add_argument(flag, action="store_true")
    for opt in ("--rev", "--added-in", "--ips-diff", "--meta-range", "--files-in", "--baseline-base", "--meta-ref", "--added-excluding"):
        ap.add_argument(opt, default=None)
    return ap.parse_args(argv)


def main(argv: list[str], root: Path = ROOT) -> int:
    try:
        a = parse_args(argv)
    except ValueError as exc:
        print(f"commit guard: {exc}", file=sys.stderr)
        return 2
    rev = a.rev or None
    added_in = a.added_in or a.ips_diff or None
    meta_range = a.meta_range or None
    files_in = a.files_in or None
    base_rev = a.baseline_base or None
    exclude_rev = a.added_excluding or None
    meta_ref = a.meta_ref or None
    tip_only = a.tip_only                  # first push to an empty remote: the tip's tree is checked, its addresses are not "added"
    allow = lines_of(read_blob(root, rev, "scripts/denylist-allow.txt") or "")
    deny_file = home() / "denylist.txt"
    deny = read_lines(deny_file)
    if not deny:
        print(f"commit guard: no private denylist at {deny_file}: only the built-in secret patterns and the generic public "
              "denylist (internal address literals) are checked", file=sys.stderr)
    files = [f for f in tracked(root, rev) if f != "scripts/denylist-allow.txt"]
    ipfiles = [f for f in files if f != IP_BASELINE_REL]
    if a.write_ip_baseline:
        counts = ip_counts(ipfiles, root, rev)
        (root / IP_BASELINE_REL).write_text(json.dumps(counts, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote {IP_BASELINE_REL}: {len(counts)} files, {sum(counts.values())} lines")
        return 0
    found = scan(files, deny, root, rev)
    if a.list:
        for rel in sorted(found):
            print(("allowed  " if allowed(rel, allow) else "BLOCKED  ") + rel)
        return 0
    bad = {r: h for r, h in found.items() if not allowed(r, allow)}
    for rel in sorted(bad):
        for n, what in bad[rel][:5]:
            print(f"{rel}:{n}: {what}", file=sys.stderr)
    code = 0
    if bad:
        print(f"commit guard: {len(bad)} file(s) carry an instance-only value or a secret. Move it to CREWBOSS_HOME, or "
              "(only for work a later slice will do) add the path to scripts/denylist-allow.txt.", file=sys.stderr)
        code = 1
    ips = ip_ratchet(files, root, rev)
    if added_in:
        ips += added_hits(added_in, root, deny, ips=not tip_only, exclude_rev=exclude_rev)
    if base_rev:
        ips += baseline_grew(root, base_rev, rev)
    for line in ips:
        print(line, file=sys.stderr)
    if ips:
        print("commit guard: instance-only values or internal-looking addresses (10.x, 172.16-31.x, 192.168.x, 100.64-127.x) must "
              "not be added to the public tree. Use a documentation address (192.0.2.x, 198.51.100.x, 203.0.113.x: RFC 5737) or a "
              "name ending .example, .test or .invalid. If you REMOVED some elsewhere, lower the baseline: "
              "python3 scripts/commit_guard.py --write-ip-baseline (it may never go up).", file=sys.stderr)
        code = 1
    if meta_range or meta_ref:
        meta = metadata_hits(meta_range or [], root, deny, [meta_ref] if meta_ref else [],
                             [t for t in [_ref_tag(root, meta_ref)] if t] if meta_ref else [])
        for line in meta:
            print(line, file=sys.stderr)
        if meta:
            print("commit guard: commit messages, file paths, ref names and tag text are public too: rewrite those commits "
                  "(amend or rebase) so they carry no instance-only value or internal address.", file=sys.stderr)
            code = 1
    if files_in:
        names, binaries = range_files(files_in, root, rev)
        for n in names:
            print(f"{n}: a file of this kind (secret, key, dump, credentials or environment file) must not be pushed", file=sys.stderr)
        ballow = lines_of(read_blob(root, base_rev or rev, BINARY_ALLOW_REL) or "")   # base first: a push cannot widen its own allowlist
        blocked = binary_findings(binaries, ballow, root, rev, lambda m: print(m, file=sys.stderr))
        if blocked:
            print(f"commit guard: binary files outside the asset folders listed in {BINARY_ALLOW_REL}, or whose bytes do not match "
                  "their extension, cannot be checked and are blocked: remove them, or (for a real asset) add its path pattern to "
                  "that file in a reviewed change.", file=sys.stderr)
        if names or blocked:
            code = 1
    if a.strict and deny:
        old = stale(found, allow, files)
        for a in old:
            print(f"scripts/denylist-allow.txt: stale entry {a} (no longer matches): delete the line", file=sys.stderr)
        code = code or (1 if old else 0)
    if a.strict:
        now = ip_counts(ipfiles, root, rev)
        slack = [f"{r}: baseline {n}, now {now.get(r, 0)}" for r, n in _ip_baseline(root, rev).items() if now.get(r, 0) < n]
        for line in slack:
            print(f"{IP_BASELINE_REL}: stale headroom, {line}: lower it with --write-ip-baseline", file=sys.stderr)
        code = code or (1 if slack else 0)
    if not a.list and not a.no_gitleaks and not rev:
        tree = a.gitleaks_tree
        code = code or run_gitleaks(tree, required=tree or gitleaks_required(), root=root)
    return code


def _ref_tag(root: Path, name: str) -> str | None:
    """The object id of the annotated tag refs/tags/<name> (None when it is a branch or a lightweight tag)."""
    res = subprocess.run(["git", "rev-parse", "--verify", "-q", f"refs/tags/{name}"], cwd=root, capture_output=True, text=True)
    return res.stdout.strip() or None


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
