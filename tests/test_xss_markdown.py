"""P1-4: markdown bodies are sanitised; no template renders user content with |safe."""
import re
from pathlib import Path

import pytest

from app import repo
from app.templating import _render_markdown

ROOT = Path(__file__).resolve().parent.parent

PAYLOADS = [
    '<img src=x onerror="alert(1)">',
    "[click](javascript:alert(1))",
    "[click](JaVaScRiPt:alert(1))",
    '<a href="javascript:alert(1)">x</a>',
    '<a href="data:text/html,<script>alert(1)</script>">x</a>',
    '<div hx-get="/tasks/1/triage/accept" hx-trigger="load" hx-post="/projects">x</div>',
    '<button hx-on::load="alert(1)" x-init="alert(1)" @click="alert(1)">b</button>',
    "<script>alert(1)</script>",
    '<iframe src="https://evil.example"></iframe>',
    '<p style="background:url(javascript:alert(1))" class="x" onclick="alert(1)">p</p>',
    '<svg onload="alert(1)"><a xlink:href="javascript:alert(1)">s</a></svg>',
    '<form action="/projects" method="post"><input name="slug"></form>',
]


@pytest.mark.parametrize("payload", PAYLOADS)
def test_payload_is_neutralised(payload):
    html = str(_render_markdown(payload))
    low = html.lower()
    tags = " ".join(re.findall(r"<[^>]+>", low))  # real markup only (escaped text like &lt;a … is inert)
    assert "javascript:" not in tags and "data:" not in tags
    assert all(h.startswith(("http://", "https://", "mailto:")) for h in re.findall(r'href="([^"]*)"', tags))
    assert "<script" not in low and "<img" not in low and "<iframe" not in low and "<svg" not in low
    assert "<form" not in low and "<input" not in low and "<button" not in low
    assert not re.search(r"\s(on\w+|hx-[\w:-]+|x-[\w:-]+|@\w+|style|class|src|action)\s*=", low), html


def test_basic_formatting_and_safe_links_survive():
    html = str(_render_markdown("**bold** _em_ `code`\n\n- one\n- two\n\n[site](https://example.com \"t\") "
                                "[mail](mailto:a@b.c) [rel](/tasks)"))
    assert "<strong>bold</strong>" in html and "<em>em</em>" in html and "<code>code</code>" in html
    assert "<li>one</li>" in html
    assert 'href="https://example.com"' in html and 'title="t"' in html
    assert 'href="mailto:a@b.c"' in html
    assert 'href="/tasks"' not in html  # http(s)/mailto only


def test_empty_body():
    assert str(_render_markdown(None)) == ""


def test_no_template_uses_safe_filter():
    offenders = [str(p.relative_to(ROOT)) for p in (ROOT / "app/templates").rglob("*.html")
                 if re.search(r"\|\s*safe\b", p.read_text())]
    assert offenders == []


def _cookie(project):
    return {"Cookie": f"current_project={project['slug']}"}


def test_knowledge_page_renders_sanitised(client, project):
    e = repo.create_knowledge(project["id"], "note", "XSS note", '<img src=x onerror="alert(1)"> [x](javascript:alert(2)) '
                              '<div hx-post="/projects" hx-trigger="load">y</div>', [], "", False, "claude:bugs",
                              summary="s")
    page = client.get(f"/test-proj/knowledge?open={e['id']}", headers=_cookie(project)).text  # opened: body rendered
    assert "onerror" not in page and "javascript:alert" not in page
    assert 'hx-post="/projects"' not in page


def test_objectives_page_renders_sanitised(client, project):
    e = repo.create_knowledge(project["id"], "objective", "Earn", '<img src=x onerror="alert(1)">**more**', [], "",
                              False, "owner", summary="s")
    page = client.get(f"/test-proj/objectives?open={e['id']}", headers=_cookie(project)).text  # opened: body rendered
    assert "onerror" not in page and "<strong>more</strong>" in page


def test_triage_banner_in_drawer_still_closes_drawer(client, project):
    t = repo.create_task(project["id"], "T", "", "should", "bug", [], {}, "owner")
    repo.suggest_triage(t["id"], "claude:bugs", priority="must", rationale="because")
    html = client.get(f"/test-proj/tasks/{t['id']}/panel", headers=_cookie(project)).text
    # from the drawer, Accept / Keep mine / Argue answer with the drawer itself (every view refreshes itself)
    assert html.count('?from=drawer" hx-target="#drawer-content"') == 3
