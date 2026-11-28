"""Journey: watching a job work (#89). A read-only runner conversation in the task window: Claude's messages as bubbles (Markdown, escaped), tool calls
as collapsed rows, results collapsed, Gemini as a second speaker, new frames arriving live, auto-scroll unless scrolled up, no input box.
The panel mounts on any element carrying data-live-activity (the job card's Activity slot is one; this test mounts one itself so it does not depend on it)."""
import json

import pytest

from app import repo, tree
from app.db import get_cursor

EVIL = '<img src=x onerror="window.__pwned=1"> <script>window.__pwned=1</script> **bold** `code` [ok](https://example.com/a) [bad](javascript:window.__pwned=1)'


@pytest.fixture
def world(seed):
    a = seed["a"]
    tree.set_details(a["id"], "owner", git_remote="https://github.com/acme/app.git")
    with get_cursor() as cur:
        cur.execute("INSERT INTO users (oid, display_name, role) VALUES ('11111111-0000-4000-8000-0000000000aa', 'Ann', 'owner') RETURNING id")
        uid = cur.fetchone()["id"]
        cur.execute("INSERT INTO device_tokens (user_id, account_id, name, mode, token_hash, pubkey, pubkey_fp, expires_at) "
                    "VALUES (%s, %s, 'anns-desktop', 'subscription', 'h', 'k', 'f', now() + interval '1 day') RETURNING id", (uid, a["id"]))
        did = cur.fetchone()["id"]
        cur.execute("SELECT id FROM agent_profiles WHERE name = 'builder'")
        pid = cur.fetchone()["id"]
        cur.execute("INSERT INTO jobs (task_id, account_id, user_id, profile_id, approved_at, approved_by, state, device_id, fence, claimed_at, lease_expires_at) "
                    "VALUES (%s, %s, %s, %s, now(), %s, 'running', %s, 4, now(), now() + interval '1 hour') RETURNING id", (seed["t2"]["id"], a["id"], uid, pid, uid, did))
        jid = cur.fetchone()["id"]
    return {**seed, "job": jid, "task": seed["t2"]}


_seq = [0]


def frame(job, kind, text, speaker="claude", **meta):
    with get_cursor() as cur:
        cur.execute("SELECT COALESCE(max(seq), 0) + 1 AS n FROM job_activity WHERE job_id = %s", (job,))
        n = cur.fetchone()["n"]
        cur.execute("INSERT INTO job_activity (job_id, seq, fence, rseq, speaker, kind, text, meta) VALUES (%s, %s, 4, %s, %s, %s, %s, %s)",
                    (job, n, n, speaker, kind, text, json.dumps(meta)))


def mount(page, server, w):
    page.goto(f"{server}/test-proj/tasks/{w['task']['id']}")
    page.wait_for_selector("h1, h2, [data-task-title]", state="attached")
    page.evaluate("""(url) => { const d = document.createElement('div'); d.id = 'mount'; d.setAttribute('data-live-activity', '');
        d.setAttribute('data-activity-url', url); d.style.cssText = 'position:fixed;right:8px;top:60px;width:min(560px,94vw);z-index:5000;background:Canvas';
        document.body.appendChild(d); window.CrewbossActivity.sweep(); }""", f"/test-proj/tasks/{w['task']['id']}/run/activity")
    page.wait_for_selector("#mount .jact")


def test_the_conversation_is_watch_only_safe_and_live(new_page, server, world, sign_in, device):
    job = world["job"]
    frame(job, "status", "", speaker="runner", state="working")
    frame(job, "assistant_text", "I'll start by reading the router.\n\n- first\n- second")
    frame(job, "tool_call", "Read app/routers/pages.py", tool="Read", path="app/routers/pages.py")
    frame(job, "tool_result", "1\tfrom fastapi import APIRouter", tool="Read", status="ok")
    frame(job, "tool_call", "Edit app/x.py (+12 −3)", tool="Edit", path="app/x.py")
    frame(job, "tool_call", "$ pytest tests/test_x.py -q", tool="Bash")
    frame(job, "tool_result", "1 failed", tool="Bash", status="error", is_error=True)
    frame(job, "assistant_text", EVIL)
    frame(job, "tool_call", "[plan] Should we shard by tenant?", speaker="gemini")
    frame(job, "assistant_text", "Yes: shard by tenant, keep one global user table.", speaker="gemini")
    frame(job, "permission_request", "Waiting for permission to use Bash", tool="Bash")
    frame(job, "status", "", speaker="runner", state="working", session_url="https://claude.ai/code/session_abc12345")
    page = new_page(device)
    sign_in(page)
    mount(page, server, world)
    page.wait_for_selector("#mount .jact-msg.gemini")
    # watch only: there is no way to type into it
    assert page.locator("#mount input, #mount textarea, #mount [contenteditable]").count() == 0
    assert page.text_content("#mount .jact-watch").lower() == "watch only"
    # Claude's bubbles and Gemini as a second speaker
    who = page.locator("#mount .jact-who").all_text_contents()
    assert "Claude" in who and "Gemini" in who and "Claude → Gemini" in who
    assert page.locator("#mount .jact-md ul li").count() == 2
    # tool calls are compact collapsed rows, results collapsed
    rows = page.locator("#mount details.jact-tool")
    assert rows.count() == 3 and page.locator("#mount details.jact-tool[open], #mount details.jact-result[open]").count() == 0
    assert "Read" in rows.nth(0).inner_text() and "app/routers/pages.py" in rows.nth(0).inner_text() and "(+12 −3)" in rows.nth(1).inner_text() and "$ pytest" in rows.nth(2).inner_text()
    assert page.locator("#mount details.jact-result.err").count() == 1
    rows.nth(0).locator("summary").click()
    assert rows.nth(0).get_attribute("open") is not None
    # untrusted text: shown as text, never run
    assert page.locator("#mount img, #mount script, #mount iframe").count() == 0 and page.evaluate("window.__pwned") is None
    assert "<img src=x" in page.inner_text("#mount")
    links = page.locator("#mount a.jact-link")
    assert links.count() == 1 and links.first.get_attribute("href") == "https://example.com/a" and "noopener" in links.first.get_attribute("rel")
    assert page.locator("#mount [href^='javascript']").count() == 0 and "[bad](javascript:window.__pwned=1)" not in page.inner_text("#mount") and "bad" in page.inner_text("#mount")
    assert "Waiting for permission to use Bash" in page.inner_text("#mount .jact-perm")
    # accessible and linked out
    assert page.get_attribute("#mount .jact-log", "role") == "log" and page.get_attribute("#mount .jact-log", "aria-live") == "polite"
    assert page.locator("#mount a.jact-btn").get_attribute("href") == "https://claude.ai/code/session_abc12345"
    page.shot("conversation")
    # live: a new frame appears with no reload, and the view follows it
    page.evaluate("window.__keep = 1")
    page.locator("#mount .jact-log").evaluate("e => { e.scrollTop = e.scrollHeight; }")        # back to the end (opening a row scrolled it)
    page.wait_for_timeout(200)
    frame(job, "assistant_text", "All done, the change is committed.")
    page.wait_for_function("document.querySelector('#mount .jact-log').innerText.includes('All done, the change is committed')", timeout=9000)
    assert page.evaluate("window.__keep") == 1
    log = page.locator("#mount .jact-log")
    assert page.evaluate("(e) => e.scrollHeight - e.scrollTop - e.clientHeight < 60", log.element_handle())
    page.shot("live")


def test_reading_earlier_messages_is_not_interrupted_and_a_finished_job_stops_the_stream(new_page, server, world, sign_in):
    job = world["job"]
    for i in range(40):
        frame(job, "assistant_text", f"Step {i}: working through the list of files " + "and checking each one. " * 4)
    page = new_page("desktop")
    sign_in(page)
    mount(page, server, world)
    page.wait_for_function("document.querySelectorAll('#mount .jact-msg').length === 40")
    log = page.locator("#mount .jact-log")
    log.evaluate("e => { e.scrollTop = 0; e.dispatchEvent(new Event('scroll')); }")
    frame(job, "assistant_text", "A brand new message arrives.")
    page.wait_for_selector("#mount .jact-more:not([hidden])", timeout=9000)
    assert log.evaluate("e => e.scrollTop") < 20                              # the reader stayed where they were
    page.locator("#mount .jact-more").click()
    assert log.evaluate("e => e.scrollHeight - e.scrollTop - e.clientHeight < 60")
    page.shot("scrolled-up")
    with get_cursor() as cur:
        cur.execute("UPDATE jobs SET state = 'done', finished_at = now() WHERE id = %s", (job,))
    page.wait_for_function("document.querySelector('#mount .jact-status').innerText.trim() === 'Done'", timeout=30000)
    assert page.locator("#mount .jact-typing").is_hidden()


def test_a_run_whose_live_view_is_unavailable_says_so(new_page, server, world, sign_in):
    frame(world["job"], "status", "live view unavailable for this run", speaker="runner", state="unavailable")
    page = new_page("desktop")
    sign_in(page)
    mount(page, server, world)
    assert "live view unavailable for this run" in page.inner_text("#mount .jact-note")


def test_dark_theme(new_page, server, world, sign_in):
    frame(world["job"], "assistant_text", "Dark mode check **bold**.")
    frame(world["job"], "tool_call", "$ ls", tool="Bash")
    frame(world["job"], "assistant_text", "Gemini says hi.", speaker="gemini")
    page = new_page("desktop")
    sign_in(page)
    mount(page, server, world)
    page.evaluate("document.documentElement.classList.add('dark')")
    page.wait_for_selector("#mount .jact-msg.gemini")
    page.shot("dark")
