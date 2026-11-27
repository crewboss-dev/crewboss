"""Fold-away responses (task #582): an action that removes or moves an item answers an htmx request in place.

THE CONTRACT (client side: static/js/fold.js, styles: static/css/fold.css)
  * An item an action can remove carries `data-fold` (optionally `data-fold-section` on its list and
    `data-fold-count` / `data-fold-placeholder` inside it). The form or button that acts on it targets the item
    (`hx-target="closest [data-fold]" hx-swap="outerHTML"`).
  * The server answers an htmx request (HX-Request header) with HTTP 200 and an EMPTY body (the item is gone), or a
    body of `<template data-fold-into="#selector" data-fold-pos="afterbegin">row html</template>` blocks when the item
    also MOVES to another list on the same page. fold.js collapses the item, inserts the rows, moves focus to the next
    item and never reloads or scrolls. A flash rides in an `HX-Trigger: {"mdd:flash": {...}}` header (a toast).
  * WITHOUT the header (no JS, curl) the same route answers exactly as before: a 303 back with a flash cookie.
  * A whole-view fragment (the board, Focus, the plan, the checklist: `data-fold-root`) is NOT folded by the server: it
    stays the full re-render it always was, and fold.js reconciles it by each item's `data-fold="<key>"` — keys that
    vanished fold out, keys that appeared fold in, the rest are replaced under the reader without a jump.
"""
from __future__ import annotations

import json
import re
from html import escape

from fastapi import Request
from fastapi.responses import Response

# The destinations are server-side constants ("#changes-decided-list"), never user input; this is a belt-and-braces
# check that a selector can't smuggle markup into an attribute.
_SELECTOR = re.compile(r"^[#.A-Za-z0-9_\-:\[\]=\"' ]{1,80}$")
POSITIONS = ("beforebegin", "afterbegin", "beforeend", "afterend")


def is_htmx(request: Request) -> bool:
    return bool(request.headers.get("hx-request"))


def flash_header(message: str, tone: str = "info") -> dict:
    """HX-Trigger for the house toast (js/app.js mddToast). json.dumps escapes non-ASCII, so the header stays latin-1."""
    return {"HX-Trigger": json.dumps({"mdd:flash": {"message": message[:400], "tone": tone}})}


def insert_block(into: str, html: str, pos: str = "beforeend") -> str:
    """One row (or rows) to insert into another list on the page."""
    if not _SELECTOR.match(into) or pos not in POSITIONS:
        raise ValueError(f"bad fold destination {into!r} / {pos!r}")
    return f'<template data-fold-into="{escape(into, quote=True)}" data-fold-pos="{pos}">{html}</template>'


def remove_block(selector: str) -> str:
    """Another element to fold away in the same breath (a row that is being replaced by a re-rendered one)."""
    if not _SELECTOR.match(selector):
        raise ValueError(f"bad fold selector {selector!r}")
    return f'<template data-fold-remove="{escape(selector, quote=True)}"></template>'


def fold_response(message: str | None = None, *, inserts: list[str] | tuple[str, ...] = (), tone: str = "info") -> Response:
    """The htmx answer to an action that removed (and maybe moved) its item: 200, empty or just the insert blocks."""
    headers = flash_header(message, tone) if message else {}
    return Response("".join(inserts), status_code=200, media_type="text/html", headers=headers)
