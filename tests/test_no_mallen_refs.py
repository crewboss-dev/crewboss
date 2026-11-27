"""Ratchet: the public code must not mention Mallen (the owner's business); each install's specifics live in its instance
config (app/instance.py). The count of 'mallen' (case-insensitive) in EVERY tracked text file (git ls-files, not only app/ and
db/) may only go DOWN against tests/mallen_refs_baseline.json; a file that is not in the baseline may have none.
After a cleanup, lower the baseline:  python -m tests.test_no_mallen_refs --write-baseline

The instance's own denylist (CREWBOSS_HOME/denylist.txt: hosts, addresses, names, logins) is enforced by
scripts/commit_guard.py (tests/test_commit_guard.py), and scripts/denylist-allow.txt must stay empty."""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASELINE = os.path.join(ROOT, "tests", "mallen_refs_baseline.json")
SELF_BASELINE = "tests/mallen_refs_baseline.json"


def _tracked() -> list:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    return [p for p in out.decode("utf-8", "replace").split("\0") if p]


def scan() -> dict:
    """{relative path: count of 'mallen'} for every tracked text file that mentions it."""
    found = {}
    for rel in _tracked():
        if rel == SELF_BASELINE:
            continue
        try:
            with open(os.path.join(ROOT, rel), "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        if b"\0" in data[:4096]:
            continue
        n = data.decode("utf-8", "ignore").lower().count("mallen")
        if n:
            found[rel] = n
    return dict(sorted(found.items()))


def write_baseline() -> None:
    counts = scan()
    with open(BASELINE, "w", encoding="utf-8") as fh:
        json.dump(counts, fh, indent=1, sort_keys=True)
        fh.write("\n")
    print(f"wrote {BASELINE}: {len(counts)} files, {sum(counts.values())} references")


def test_no_new_mallen_references():
    with open(BASELINE, encoding="utf-8") as fh:
        base = json.load(fh)
    over = [f"  {f}: {n} (baseline {base.get(f, 0)})" for f, n in scan().items() if n > base.get(f, 0)]
    assert not over, ("New references to Mallen in the public tree. New code must not mention Mallen: put install specifics "
                      "in instance config (instance.yaml, read by app/instance.py).\n" + "\n".join(over) +
                      "\nIf you REMOVED references elsewhere, lower the baseline: python -m tests.test_no_mallen_refs --write-baseline")


def test_the_baseline_has_no_stale_headroom():
    """The baseline may only shrink: an entry above the real count means someone cleaned up and forgot to lower it."""
    with open(BASELINE, encoding="utf-8") as fh:
        base = json.load(fh)
    now = scan()
    slack = [f"  {f}: baseline {n}, now {now.get(f, 0)}" for f, n in base.items() if now.get(f, 0) < n]
    assert not slack, ("The baseline is higher than the tree. Lower it: python -m tests.test_no_mallen_refs --write-baseline\n"
                       + "\n".join(slack))


def test_the_commit_guard_allowlist_is_empty():
    """scripts/denylist-allow.txt waved known leftovers through; the public release has none."""
    with open(os.path.join(ROOT, "scripts", "denylist-allow.txt"), encoding="utf-8") as fh:
        live = [ln for ln in fh.read().splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    assert live == [], f"scripts/denylist-allow.txt must have no entries, found {live}"


if __name__ == "__main__":
    if "--write-baseline" in sys.argv:
        write_baseline()
    else:
        print(__doc__)
