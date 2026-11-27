"""The Discussion of a task window in a real browser (task #830; Playwright, skipped when missing). The /ca/* endpoints are mocked. On a wide
screen opening a task does NOT open the Discussion by itself (#954, q751); the assistant button and the Discussion tab open it beside the
task and list the task's conversations; the assistant button is context-aware; the tab carries the conversation count; a view-only person
gets no composer; on a phone the Task | Discussion tabs reach it. Screenshots go to SHOTS_830 when that is set."""
import glob
import json
import os
import socket
import threading
import time

import pytest

pw = pytest.importorskip("playwright.sync_api")
uvicorn = pytest.importorskip("uvicorn")

from app import repo  # noqa: E402

CHROME = (glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell"))
          or [None])[0]
pytestmark = pytest.mark.skipif(not CHROME, reason="no Chromium for Playwright")


def _free_port():
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        return sk.getsockname()[1]


PORT = _free_port()


@pytest.fixture(scope="module")
def server():
    from app import config
    config.ORIGIN_HOSTS = tuple(config.ORIGIN_HOSTS) + (f"127.0.0.1:{PORT}",)
    from app.main import app
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error", lifespan="off"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.1)
    yield f"http://127.0.0.1:{PORT}"
    srv.should_exit = True
    th.join(5)


def _mock(page, task, can_post=True):
    items = [{"id": 11, "title": "Is the export button built?", "by": "Alice", "mine": False, "open": True, "linked": False, "busy": False,
              "updated_at": "2026-11-18T01:00:00+00:00"},
             {"id": 12, "title": "Plan the CSV columns", "by": "You", "mine": True, "open": True, "linked": False, "busy": False,
              "updated_at": "2026-11-17T01:00:00+00:00"},
             {"linked": True, "open": False, "mine": False, "by": "Bob", "updated_at": "2026-11-16T01:00:00+00:00"}]
    conv = {"id": 11, "title": "Is the export button built?", "context": {"entity": {"kind": "task", "id": task["id"], "title": task["title"], "label": f"Task #{task['id']}"}},
            "shared": True, "can_post": can_post, "live_turn": None, "looking": False, "shareable": False,
            "cards": [{"id": 1, "turn_id": 1, "kind": "confirm", "state": "pending", "result": "", "mine": False, "by": "Alice",
                       "payload": {"action": "add_task", "summary": "Add task: Export CSV", "detail": "Title: Export CSV", "params": {}}}],
            "messages": [{"id": 1, "role": "user", "content": "Is the export button built?", "author": "Alice", "mine": False, "attachments": []},
                         {"id": 2, "role": "assistant", "content": "Yes, in the reports page.", "author": "Alice", "mine": False, "attachments": []}]}
    state = {"online": True, "runners": [], "conversations": [], "first_name": "M", "speech": False, "unread": 0, "user_id": 5}
    j = lambda body: (lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(body)))  # noqa: E731
    page.route("**/ca/state", j(state))
    page.route(f"**/ca/tasks/{task['id']}/discussion", j({"task": {"id": task["id"], "title": task["title"]}, "can_post": can_post, "items": items}))
    page.route("**/ca/conversations/11", j(conv))
    page.route("**/ca/stream", lambda r: r.fulfill(status=200, content_type="text/event-stream", body=": hi\n\n"))
    page.route("**/ca/notices/read", j({}))
    # the first-run tour (#938) starts by itself on a person's first page and would sit over the page under test: this person has finished it
    page.route("**/all/tour", lambda r: r.fulfill(status=200, content_type="application/json", body='{"status": "finished", "step": "", "steps": []}')
               if r.request.method == "GET" else r.continue_())


def _shot(page, name):
    d = os.environ.get("SHOTS_830")
    if d:
        os.makedirs(d, exist_ok=True)
        page.screenshot(path=os.path.join(d, name))


def _open_sheet(page, project, task):
    page.wait_for_function("window.htmx && window.Alpine")
    page.evaluate("([u]) => htmx.ajax('GET', u, {target: '#drawer-content', swap: 'innerHTML'})", [f"/{project['slug']}/tasks/{task['id']}/panel"])


def _open(browser, server, project, width, can_post=True):
    ctx = browser.new_context(viewport={"width": width, "height": 900})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    task = repo.create_task(project["id"], "Export the report as CSV", "Customers want the monthly report as CSV.", "must", "feature", [], {}, "owner")
    _mock(page, task, can_post)
    return page, task, errors


def test_wide_screen_the_discussion_never_opens_by_itself_but_the_button_and_tab_open_it(server, project):
    with pw.sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME)
        page, task, errors = _open(b, server, project, 1440)
        page.goto(f"{server}/{project['slug']}/board")
        _open_sheet(page, project, task)                                           # the task window opens over the board
        page.wait_for_selector(".task-sheet.is-open")
        page.wait_for_selector(".task-sheet [data-task-tab=discussion] .task-tab-n:not([hidden])")   # the list has loaded (the tab's count shows)
        assert not page.evaluate("document.documentElement.classList.contains('ca-open')")   # opening a task never opens the CA (#954)
        assert not page.is_visible("#ca-disc")
        page.click(".task-sheet [data-task-tab=discussion]")                      # the tab opens it
        page.wait_for_function("document.documentElement.classList.contains('ca-open')")
        page.wait_for_selector("#ca-disc-list .ca-disc-row")
        assert page.is_visible("#ca-disc") and "Task #" in page.inner_text("#ca-disc-title")
        page.evaluate("document.querySelector('.ca-hdr-btn').click()")            # the button closes it again ...
        page.wait_for_function("!document.documentElement.classList.contains('ca-open')")
        page.evaluate("document.querySelector('.ca-hdr-btn').click()")            # ... and opens it
        page.wait_for_function("document.documentElement.classList.contains('ca-open')")
        page.wait_for_selector("#ca-disc-list .ca-disc-row")
        rows = page.eval_on_selector_all("#ca-disc-list .ca-disc-row", "els => els.map(e => e.innerText)")
        assert len(rows) == 3 and "Is the export button built?" in rows[0] and "A chat by Bob" in rows[2]
        _shot(page, "desktop-task-sheet-discussion.png")
        page.click("#ca-disc-list .ca-disc-row >> nth=0")                         # one tap continues one
        page.wait_for_selector("#ca-msgs .ca-msg.assistant")
        assert "Alice" in page.inner_text("#ca-msgs")                              # who wrote it
        assert page.locator("#ca-msgs .ca-card").count() == 1 and page.locator("#ca-msgs .ca-card button").count() == 0   # Alice's card: nothing to tap for Bob
        assert "Waiting for Alice" in page.inner_text("#ca-msgs .ca-card")
        _shot(page, "desktop-task-sheet-conversation.png")
        # the assistant button is context-aware: pressing it with the Discussion showing closes it; again opens THAT task's Discussion
        page.evaluate("document.querySelector('.ca-hdr-btn').click()")   # the header button (layout-independent)
        assert not page.evaluate("document.documentElement.classList.contains('ca-open')")
        page.evaluate("document.querySelector('.ca-hdr-btn').click()")   # the header button (layout-independent)
        page.wait_for_function("document.documentElement.classList.contains('ca-open')")
        assert page.is_visible("#ca-disc")
        # the task closes: the Discussion strip goes with it (the assistant stays open, as the person opened it, now as the global one)
        page.click("[data-task-sheet-exit]")
        page.wait_for_function("!document.querySelector('#ca-disc') || document.querySelector('#ca-disc').hidden")
        # elsewhere the button opens or closes the global assistant: no Discussion strip
        page.click(".ca-hdr-btn[data-ca-open]")
        page.wait_for_function("!document.documentElement.classList.contains('ca-open')")
        page.click(".ca-hdr-btn[data-ca-open]")
        page.wait_for_function("document.documentElement.classList.contains('ca-open')")
        assert not page.is_visible("#ca-disc")
        assert not errors, errors
        b.close()


def test_the_discussion_tab_shows_how_many_conversations_the_task_has(server, project):
    with pw.sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME)
        page, task, errors = _open(b, server, project, 1440)
        page.goto(f"{server}/{project['slug']}/board")
        _open_sheet(page, project, task)
        page.wait_for_selector(".task-sheet.is-open")
        badge = ".task-sheet [data-task-tab=discussion] .task-tab-n"
        page.wait_for_selector(badge + ":not([hidden])")
        assert page.inner_text(badge) == "3" and page.is_visible(badge)           # the mock task has three conversations
        assert not page.evaluate("document.documentElement.classList.contains('ca-open')")
        assert not errors, errors
        b.close()


def test_a_task_with_no_conversations_shows_no_number(server, project):
    with pw.sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME)
        page, task, errors = _open(b, server, project, 1440)
        page.route(f"**/ca/tasks/{task['id']}/discussion", lambda r: r.fulfill(
            status=200, content_type="application/json", body=json.dumps({"task": {"id": task["id"], "title": task["title"]}, "can_post": True, "items": []})))
        page.goto(f"{server}/{project['slug']}/board")
        with page.expect_response(lambda r: f"/ca/tasks/{task['id']}/discussion" in r.url):   # loadDisc really ran (not just "pill starts hidden")
            _open_sheet(page, project, task)
        page.wait_for_selector(".task-sheet.is-open")
        badge = ".task-sheet [data-task-tab=discussion] .task-tab-n"
        page.wait_for_timeout(200)                                                 # let paintDisc apply the (empty) list
        assert page.inner_text(badge) == "" and not page.is_visible(badge)
        assert not errors, errors
        b.close()


def test_a_view_only_person_reads_but_has_no_composer(server, project):
    with pw.sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME)
        page, task, errors = _open(b, server, project, 1440, can_post=False)
        with page.expect_response(lambda r: f"/ca/tasks/{task['id']}/discussion" in r.url):   # wait until the old auto-open point has passed
            page.goto(f"{server}/{project['slug']}/tasks/{task['id']}")
        page.wait_for_selector("[data-ca-open]")
        page.wait_for_timeout(200)
        assert not page.evaluate("document.documentElement.classList.contains('ca-open')")   # not by itself (#954)
        page.evaluate("document.querySelector('.ca-hdr-btn').click()")
        page.wait_for_function("document.documentElement.classList.contains('ca-open')")
        page.wait_for_selector("#ca-disc-list .ca-disc-row")
        page.click("#ca-disc-list .ca-disc-row >> nth=0")
        page.wait_for_selector("#ca-msgs .ca-msg.assistant")
        assert "view-only" in page.inner_text("#ca-disc-note") and not page.is_visible("#ca-input")
        _shot(page, "desktop-view-only.png")
        assert not errors, errors
        b.close()


def test_phone_tabs_reach_the_discussion(server, project):
    with pw.sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME)
        page, task, errors = _open(b, server, project, 390)
        page.goto(f"{server}/{project['slug']}/board")
        _open_sheet(page, project, task)
        page.wait_for_selector(".task-sheet.is-open")
        assert page.is_visible(".task-sheet .task-tabs") and not page.evaluate("document.documentElement.classList.contains('ca-open')")
        _shot(page, "phone-task-sheet.png")
        page.click(".task-sheet [data-task-tab=discussion]")
        page.wait_for_function("document.documentElement.classList.contains('ca-open')")
        page.wait_for_selector("#ca-disc-list .ca-disc-row")
        assert page.is_visible("#ca-disc-back")
        _shot(page, "phone-task-sheet-discussion.png")
        page.click("#ca-disc-back")                                                # back to the task
        page.wait_for_function("!document.documentElement.classList.contains('ca-open')")
        assert page.is_visible(".task-sheet.is-open")
        assert not errors, errors
        b.close()
