"""Chrome drift guard: the shared chrome (css/chrome.css) and colour tokens (css/theme-tokens.css) are VERBATIM copies of an
upstream design system the instance tracks. When CHROME_UPSTREAM_PATH (env, or `chrome_upstream_path` in the instance file)
names a folder holding the upstream's chrome.css and theme-tokens.css, any difference outside ALLOWED_DIFFERENCES fails.
Unset (the default, and always for a public install) = skipped, with a message saying how to switch it on."""
import difflib
from pathlib import Path

import pytest

from app import config

CSS = Path(__file__).resolve().parent.parent / "app" / "static" / "css"
FILES = ("chrome.css", "theme-tokens.css")

# Intended differences from upstream, per file: exact lines (stripped) that may appear only here or only upstream.
# Keep it empty unless a rule is deliberately dropped or added; each entry needs a reason beside it.
ALLOWED_DIFFERENCES: dict[str, set[str]] = {"chrome.css": set(), "theme-tokens.css": set()}


def drift(local: str, upstream: str, allowed: set[str]) -> list[str]:
    """The differing lines (as a unified diff body) that are not on the allowlist."""
    out = []
    for line in difflib.unified_diff(upstream.splitlines(), local.splitlines(), "upstream", "local", lineterm="", n=0):
        if line.startswith(("---", "+++", "@@")):
            continue
        if line[1:].strip() in allowed:
            continue
        out.append(line)
    return out


def test_drift_detects_a_changed_line():
    assert drift("a\nb\n", "a\nb\n", set()) == []
    assert drift("a\nB\n", "a\nb\n", set()) == ["-b", "+B"]
    assert drift("a\nB\n", "a\nb\n", {"B", "b"}) == []


@pytest.mark.parametrize("name", FILES)
def test_chrome_matches_upstream(name):
    if not config.CHROME_UPSTREAM_PATH:
        pytest.skip("CHROME_UPSTREAM_PATH is not set: no upstream chrome to compare against (set it to a folder holding "
                    "the upstream's chrome.css and theme-tokens.css to enable this guard)")
    up = Path(config.CHROME_UPSTREAM_PATH) / name
    assert up.is_file(), f"CHROME_UPSTREAM_PATH={config.CHROME_UPSTREAM_PATH}: {name} not found there"
    diff = drift((CSS / name).read_text(), up.read_text(), ALLOWED_DIFFERENCES[name])
    assert not diff, (f"css/{name} has drifted from the upstream copy; re-copy it (or add the line to ALLOWED_DIFFERENCES "
                      "with a reason):\n" + "\n".join(diff[:40]))
