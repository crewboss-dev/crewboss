"""Design-layer drift guard: css/erp-design.css is the upstream design system's page-level CSS, copied section by section by
tools/erp_design.py (the upstream keeps it inline in its base.html). Like tests/test_chrome_drift.py this compares against the
upstream when it is reachable: CHROME_UPSTREAM_PATH names the folder holding the upstream's chrome.css, and its base.html
is in the sibling `templates` folder (or DESIGN_UPSTREAM_BASE names the file). Unset = skipped. A section whose rules differ from
the upstream's (ignoring comments, blank lines, indentation) fails; re-run `python -m tools.erp_design <base.html>`."""
import os
import re
from pathlib import Path

import pytest

from app import config
from tools import erp_design

CSS = Path(__file__).resolve().parent.parent / "app" / "static" / "css" / "erp-design.css"


def _upstream_base():
    explicit = os.environ.get("DESIGN_UPSTREAM_BASE")
    if explicit:
        return Path(explicit)
    if config.CHROME_UPSTREAM_PATH:
        return Path(config.CHROME_UPSTREAM_PATH).parent / "templates" / "base.html"
    return None


def test_the_section_parser_round_trips():
    src = "<style>\n  a{x:1}\n  /* c */\n  b{y:2}\n  c{z:3}\n</style>"
    lines = erp_design.style_block(src)
    assert erp_design.section_lines(lines, None, "a{", "c{") == ["  a{x:1}", "  /* c */", "  b{y:2}"]
    assert erp_design.normalise("  a{x:1}\n/* c */\n\n  b{y:2}") == ["a{x:1}", "b{y:2}"]
    assert erp_design.parse("/* @@ one */\na{x:1}\n/* @@ two */\nb{}") == {"one": ["a{x:1}"], "two": ["b{}"]}


def test_every_section_is_in_the_file():
    mine = erp_design.parse(CSS.read_text())
    assert list(mine) == [s[0] for s in erp_design.SECTIONS]
    assert all(mine.values())


def test_design_layer_matches_upstream():
    up = _upstream_base()
    if up is None or not up.is_file():
        pytest.skip("CHROME_UPSTREAM_PATH is not set (or its sibling templates/base.html is missing): no upstream design layer to "
                    "compare against; set DESIGN_UPSTREAM_BASE to the upstream base.html to enable this guard")
    theirs = erp_design.upstream_sections(up.read_text())
    mine = erp_design.parse(CSS.read_text())
    bad = [n for n in theirs if theirs[n] != mine.get(n)]
    assert not bad, f"css/erp-design.css has drifted from the upstream in: {', '.join(bad)}; re-run python -m tools.erp_design <base.html>"
