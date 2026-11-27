"""Knowledge entry text rules: the one-line title, the two-line summary, and deriving a summary from a
Markdown body (used once, by the 0010 backfill, for entries written before summaries existed).

Content model (knowledge-v2): an entry is
  title    plain text, one line, <= TITLE_MAX characters (autoescaped wherever shown)
  summary  plain text, <= SUMMARY_MAX characters: what a reader needs without opening the entry
  body     MARKDOWN (CommonMark-ish + tables, fenced code, raw <img> for sized images). It is stored as
           Markdown, never as HTML, and becomes HTML in exactly one place: templating._render_markdown,
           which runs python-markdown and then the strict nh3 allowlist. The rich-text editor converts
           its HTML back to Markdown before it submits, so Claude sessions (MCP) and the owner (editor)
           write the same format.
"""
from __future__ import annotations

import re

TITLE_MAX = 90
SUMMARY_MAX = 200


def one_line(value: str | None) -> str:
    """Collapse all whitespace (incl. newlines) to single spaces and trim."""
    return " ".join((value or "").split())


# --- summary derivation ----------------------------------------------------------------------------

_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s")
_SETEXT_RE = re.compile(r"^\s{0,3}(=+|-+)\s*$")
_HR_RE = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
_TABLE_RE = re.compile(r"^\s*\|")
_LIST_RE = re.compile(r"^\s{0,3}([-*+]|\d+[.)])\s+")
_HTML_BLOCK_RE = re.compile(r"^\s*<(/?)([a-zA-Z][a-zA-Z0-9]*)[\s>/]")
_IMAGE_ONLY_RE = re.compile(r"^\s*!\[[^\]]*\]\([^)]*\)\s*$")
# " — *CHECKED*", " - **REVIEW**": a trailing all-caps status tag is metadata, not prose.
_TRAILING_TAG_RE = re.compile(r"\s*[—–-]\s*[*_]{1,2}[A-Z][A-Z ]{1,20}[*_]{1,2}\s*$")


def _blocks(body: str) -> list[tuple[str, list[str]]]:
    """Split Markdown into (kind, lines) blocks: paragraph | list | quote, skipping headings, rules,
    tables, fenced code, HTML blocks and image-only lines."""
    blocks: list[tuple[str, list[str]]] = []
    current_kind, current = None, []
    in_fence = False

    def flush():
        nonlocal current_kind, current
        if current_kind and current:
            blocks.append((current_kind, current))
        current_kind, current = None, []

    for raw in (body or "").replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        if _FENCE_RE.match(line):
            flush()
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if not line.strip():
            flush()
            continue
        if (_HEADING_RE.match(line) or _HR_RE.match(line) or _TABLE_RE.match(line)
                or _IMAGE_ONLY_RE.match(line) or line.startswith("    ") and current_kind is None):
            flush()
            continue
        if _SETEXT_RE.match(line) and current_kind == "paragraph":
            current_kind, current = None, []  # the paragraph above was a setext heading
            continue
        if _HTML_BLOCK_RE.match(line) and current_kind is None:
            flush()
            continue
        if line.lstrip().startswith(">"):
            if current_kind not in (None, "quote"):
                flush()
            current_kind = "quote"
            current.append(line.lstrip()[1:].strip())
            continue
        if _LIST_RE.match(line):
            if current_kind not in (None, "list"):
                flush()
            current_kind = "list"
            current.append(_LIST_RE.sub("", line, count=1).strip())
            continue
        if current_kind == "list" and raw.startswith((" ", "\t")):
            current[-1] = current[-1] + " " + line.strip()  # a wrapped list item
            continue
        if current_kind is None:
            current_kind = "paragraph"
        elif current_kind != "paragraph":
            flush()
            current_kind = "paragraph"
        current.append(line.strip())
    flush()
    return blocks


def strip_inline_markdown(text: str) -> str:
    """Markdown inline syntax -> the words a reader sees."""
    t = text
    t = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", t)            # images -> alt
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)             # links -> text
    t = re.sub(r"<([a-z]+:[^>\s]+)>", r"\1", t)                # autolinks
    t = re.sub(r"<[^>]+>", "", t)                             # stray inline HTML
    t = re.sub(r"`+([^`]*)`+", r"\1", t)                       # code spans
    t = re.sub(r"(\*\*|__)(.+?)\1", r"\2", t)                  # bold
    t = re.sub(r"(?<![\w*])([*_])(?!\s)(.+?)(?<!\s)\1(?![\w*])", r"\2", t)  # italic
    t = re.sub(r"~~(.+?)~~", r"\1", t)
    t = re.sub(r"\\([\\`*_{}\[\]()#+\-.!|>])", r"\1", t)       # backslash escapes
    return one_line(t)


_SENTENCE_END_RE = re.compile(r"[.!?:;](?=\s|$)|\s[—–]\s")


def trim_to_limit(text: str, limit: int = SUMMARY_MAX) -> str:
    """Fit `text` into `limit` characters. Prefer ending at a sentence boundary; otherwise at a word
    boundary with an ellipsis. Anything that was cut ends in '…'."""
    text = one_line(text)
    if len(text) <= limit:
        return text
    window = text[: limit - 1]
    ends = [m.end() for m in _SENTENCE_END_RE.finditer(window)]
    ends = [e for e in ends if e >= limit * 0.4]
    if ends:
        cut = window[: ends[-1]].rstrip(" —–;:,.!?")
        return cut + "…"
    space = window.rfind(" ")
    cut = window[:space] if space > limit * 0.5 else window
    return cut.rstrip(" .,;:—–") + "…"


def _is_code_heavy(raw_lines: list[str]) -> bool:
    joined = " ".join(raw_lines)
    code = sum(len(m) for m in re.findall(r"`[^`]*`", joined))
    return bool(joined) and code / len(joined) > 0.6


def derive_summary(body: str | None, limit: int = SUMMARY_MAX) -> str:
    """The first meaningful paragraph of a Markdown body, as plain text, trimmed to `limit` at a
    sentence boundary (ending in '…' if cut). Headings, tables, code, rules and images are skipped;
    a list counts (its items joined) when it comes before any paragraph. '' when there is no prose."""
    blocks = _blocks(body or "")
    fallback = ""
    for kind, lines in blocks:
        if kind == "list":
            items = [strip_inline_markdown(_TRAILING_TAG_RE.sub("", item)) for item in lines]
            text = ""
            for item in filter(None, items):
                text += (item if not text else (" " if text.endswith((".", "!", "?", ";")) else "; ") + item)
        else:
            if _is_code_heavy(lines):
                fallback = fallback or " ".join(lines)
                continue
            text = strip_inline_markdown(_TRAILING_TAG_RE.sub("", " ".join(lines)))
        text = _TRAILING_TAG_RE.sub("", text).strip()
        if len(re.findall(r"[A-Za-z]{2,}", text)) >= 3:
            return trim_to_limit(text, limit)
    return trim_to_limit(strip_inline_markdown(fallback), limit) if fallback else ""


# --- objective targets ------------------------------------------------------------------------------

_PERIOD_WORD = {"weekly": "week", "monthly": "month", "quarterly": "quarter", "yearly": "year"}


def compact_number(value) -> str:
    """40000 -> '40k', 1250000 -> '1.25M', 12.5 -> '12.5'."""
    v = float(value)
    sign = "-" if v < 0 else ""
    v = abs(v)
    for size, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "k")):
        if v >= size:
            return f"{sign}{round(v / size, 2):g}{suffix}"
    return f"{sign}{round(v, 2):g}"


def format_target(value, unit: str | None, period: str | None) -> str:
    """The objective chip: '$40k / month', '95% / week', '20 h / week', '12 / quarter'."""
    if value is None:
        return ""
    n = compact_number(value)
    text = {"money": f"${n}", "percent": f"{n}%", "hours": f"{n} h"}.get(unit or "", n)
    word = _PERIOD_WORD.get(period or "")
    return f"{text} / {word}" if word else text
