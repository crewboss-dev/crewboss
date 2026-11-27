"""Jinja2 environment shared by every page template."""
from __future__ import annotations

import difflib
import json
import re

import markdown as _markdown
import nh3
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from app import config, front_door, nav_icons, proxy_check, tls_check, web_auth
from app.assets import asset
from app.knowledge_text import SUMMARY_MAX, TITLE_MAX, format_target
from app.config import CATEGORY_META, NEEDED_NOW_META, PRIORITY_META, STATUS_META, STREAM_META



def _url_context(request) -> dict:
    """Every template gets `base`: the URL prefix of the project the page belongs to ("/acme-erp"), so
    links, forms and htmx calls stay inside that project. Set by the /{slug} dependency (or, on the global
    home, to the last-visited project for the sidebar); "" when there is no project at all."""
    project = getattr(request.state, "project", None)
    return {"base": f"/{project['slug']}" if project else ""}


templates = Jinja2Templates(directory="app/templates", context_processors=[_url_context])


# Markdown bodies (knowledge, objectives) are written by anyone with a session token or the owner's
# browser, and python-markdown passes raw HTML straight through — so the rendered HTML is sanitised
# against a strict allowlist: formatting, headings, lists, tables, code, quotes and rules; links with
# http(s)/mailto hrefs only; images ONLY from this dashboard's own /media/knowledge/<id>.<ext> (uploaded
# and re-encoded here) with numeric width/height and alt. No other attributes at all (no on*, style,
# class, id, hx-*, x-*, @*, data-*), no script/style/iframe/svg/form, no javascript:/data: URLs.
_ALLOWED_TAGS = {
    "p", "br", "hr", "strong", "b", "em", "i", "del", "s", "code", "pre", "blockquote",
    "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6", "a", "img",
    "table", "thead", "tbody", "tr", "th", "td", "dl", "dt", "dd",
}
_ALLOWED_ATTRIBUTES = {"a": {"href", "title"}, "img": {"src", "alt", "width", "height"}, "ol": {"start"}}
_ALLOWED_URL_PREFIXES = ("http://", "https://", "mailto:")
MEDIA_SRC_RE = re.compile(r"^/media/knowledge/[1-9][0-9]{0,9}\.(png|jpg|webp)$")
_DIMENSION_RE = re.compile(r"^[1-9][0-9]{0,3}$")


def _filter_attribute(tag: str, attr: str, value: str) -> str | None:
    if attr == "href" and not value.strip().lower().startswith(_ALLOWED_URL_PREFIXES):
        return None  # drops relative, protocol-relative, javascript:, data:, vbscript: …
    if tag == "img" and attr == "src" and not MEDIA_SRC_RE.fullmatch(value):
        return None  # same-origin uploads only: no remote hosts, no data:, no //host, no ../
    if tag == "img" and attr in ("width", "height") and not _DIMENSION_RE.fullmatch(value):
        return None
    if tag == "ol" and attr == "start" and not (value.isascii() and value.isdigit() and len(value) < 7):
        return None
    return value


# After nh3, an <img> whose src was refused is an empty box — drop it. Safe to match with a regex here:
# nh3's output is normalised (attribute values quoted, '>' inside them escaped).
_IMG_WITHOUT_SRC_RE = re.compile(r"<img(?![^>]*\ssrc=)[^>]*>")


def sanitize_html(html: str) -> str:
    return _IMG_WITHOUT_SRC_RE.sub("", _nh3_clean(html))


def _nh3_clean(html: str) -> str:
    return nh3.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        url_schemes={"http", "https", "mailto"},
        attribute_filter=_filter_attribute,
        link_rel="noopener noreferrer nofollow",
        strip_comments=True,
    )


def _render_markdown(text: str | None) -> Markup:
    """Markdown -> SANITISED HTML, already marked safe (templates must not add |safe)."""
    if not text:
        return Markup("")
    return Markup(sanitize_html(_markdown.markdown(text, extensions=["extra", "sane_lists"])))


def _repo():
    from app import repo  # late: repo is heavier and templating is imported everywhere

    return repo


def word_diff(old: str, new: str) -> list[tuple[str, str]]:
    """Word-level diff for a proposed rewording: [(op, text)] with op in equal|del|ins. The template
    renders each piece AUTOESCAPED inside <del>/<ins> — no HTML is built here."""
    a, b = re.findall(r"\S+|\s+", old or ""), re.findall(r"\S+|\s+", new or "")
    out: list[tuple[str, str]] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes():
        if tag == "equal":
            out.append(("equal", "".join(a[i1:i2])))
            continue
        if i2 > i1:
            out.append(("del", "".join(a[i1:i2])))
        if j2 > j1:
            out.append(("ins", "".join(b[j1:j2])))
    return out


def _timeago(dt) -> str:
    if dt is None:
        return ""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    seconds = (now - dt).total_seconds()
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    if seconds < 86400 * 7:
        return f"{int(seconds // 86400)}d ago"
    return dt.strftime("%d %b")


templates.env.filters["markdown"] = _render_markdown
templates.env.globals["format_target"] = format_target
# knowledge_links(ids, project_id): the titles of the knowledge a task links to, resolved ONLY within the task's own
# account (task #368); without a project id nothing resolves.
templates.env.globals["knowledge_links"] = lambda ids, project_id=None: (
    _repo().knowledge_titles(ids if isinstance(ids, list) else [], within_project_id=project_id)
    if project_id is not None else [])
templates.env.globals["TITLE_MAX"] = TITLE_MAX
templates.env.globals["SUMMARY_MAX"] = SUMMARY_MAX
templates.env.filters["timeago"] = _timeago


def _actor_label(actor) -> str:
    """'owner' -> 'You'; the overseer -> 'dash (overseer)'; 'claude:bugs' -> 'AI · bugs';
    'gemini:bugs' -> 'Gemini · bugs'; 'client:Jo' -> 'Jo'. Claude is the incumbent 'AI'; any other AI is
    named, so the dashboard honestly shows which AI did what."""
    a = actor or ""
    if a == "owner":
        return "You"
    if a == "claude:overseer":
        return "dash (overseer)"
    if a.startswith("claude:"):
        return "AI · " + a[len("claude:"):]
    if a.startswith("claude-desktop:"):
        return "Desktop AI · " + a[len("claude-desktop:"):]
    if a.startswith("gemini:"):
        return "Gemini · " + a[len("gemini:"):]
    if a.startswith("client:"):
        return a[len("client:"):] + " (client)"
    return a


templates.env.filters["actor_label"] = _actor_label
templates.env.filters["tojson_attr"] = lambda v: json.dumps(v)
templates.env.filters["tojson"] = lambda v: json.dumps(v)


def _json_script(v) -> Markup:
    """JSON for a <script type="application/json"> body (the shared canvas's inline data, #393): raw, not
    HTML-escaped — but with <, > and & as unicode escapes, so nothing in it can close the script or open a comment."""
    return Markup(json.dumps(v, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e")
                  .replace("&", "\\u0026").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


templates.env.filters["json_script"] = _json_script
templates.env.globals["word_diff"] = word_diff
templates.env.globals["asset"] = asset
# icon('view_kanban') -> the Material Symbols path data for <svg viewBox="0 -960 960 960"> (the ERP's set, #479)
templates.env.globals["icon"] = lambda name: nav_icons.PATHS.get(name, "")
templates.env.globals["ERP_URL"] = config.ERP_URL
templates.env.globals["menu_links"] = lambda: config.MENU_LINKS     # read per render, so a test can change config.MENU_LINKS
templates.env.globals["signin_label"] = web_auth.signin_label
templates.env.globals["tls_banner"] = tls_check.banner     # task #707
templates.env.globals["front_door_seen"] = front_door.public_seen     # #976 D9: owner-only banner
templates.env.globals["proxy_banner"] = proxy_check.banner   # R1: an untrusted reverse proxy; None = no banner
templates.env.filters["pretty_json"] = lambda v: json.dumps(v, indent=2, ensure_ascii=False)
templates.env.globals["STREAM_META"] = STREAM_META
templates.env.globals["STATUS_META"] = STATUS_META
templates.env.globals["PRIORITY_META"] = PRIORITY_META
templates.env.globals["CATEGORY_META"] = CATEGORY_META
templates.env.globals["NEEDED_NOW_META"] = NEEDED_NOW_META
# Fallback for a stream/status/priority value the templates don't know (e.g. a stored suggestion
# from before validation): templates use META.get(value, UNKNOWN_META), never META[value].
templates.env.globals["UNKNOWN_META"] = {
    "label": "Unknown", "classes": "bg-charcoal/10 text-charcoal",
}
