"""#816 in a real browser (Playwright; skipped when it or its Chromium is missing): the assistant's record links open the record's FULL PAGE in the
main area without a document load (the CA drawer stays the SAME node), Back works, a phone's sheet steps aside and comes back with the same
conversation, and the owner's conversation rules (11-16): 1. open stays open on the same thread whatever the navigation, 2. closed + same page =
the same conversation, 3. closed then another page = a fresh one. The CA's /ca/* endpoints (a signed-in session) are mocked in the browser."""
import glob
import json
import os
import threading
import time

import pytest

pw = pytest.importorskip("playwright.sync_api")
uvicorn = pytest.importorskip("uvicorn")

from app import repo  # noqa: E402

CHROME = (glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell"))
          or [None])[0]
pytestmark = pytest.mark.skipif(not CHROME, reason="no Chromium for Playwright")
import socket


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
    cfg = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error", lifespan="off")
    srv = uvicorn.Server(cfg)
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.1)
    yield f"http://127.0.0.1:{PORT}"
    srv.should_exit = True
    th.join(5)


def _mock_ca(page, text):
    conv = {"id": 7, "title": "t", "context": {}, "messages": [{"id": 1, "role": "assistant", "content": text, "attachments": []}],
            "cards": [], "live_turn": None}
    state = {"online": True, "runners": [], "conversations": [], "first_name": "M", "speech": False, "unread": 0}
    page.route("**/ca/state", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(state)))
    page.route("**/ca/conversations/7", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(conv)))
    page.route("**/ca/stream", lambda r: r.fulfill(status=200, content_type="text/event-stream", body=": hi\n\n"))
    page.route("**/ca/notices/read", lambda r: r.fulfill(status=200, content_type="application/json", body="{}"))
    # the first-run tour (#938) starts by itself on a person's first page and would sit over the page under test: this person has finished it
    page.route("**/all/tour", lambda r: r.fulfill(status=200, content_type="application/json", body='{"status": "finished", "step": "", "steps": []}')
               if r.request.method == "GET" else r.continue_())


def _setup(browser, server, width, project, open_ca=True):
    ctx = browser.new_context(viewport={"width": width, "height": 900})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    ta = repo.create_task(project["id"], "Task Alpha", "body a", "must", "bug", [], {}, "owner")
    tb = repo.create_task(project["id"], "Task Beta", "body b", "must", "bug", [], {}, "owner")
    text = f"See task #{ta['id']} and /{project['slug']}/tasks/{tb['id']} and https://example.com/x."
    _mock_ca(page, text)
    if open_ca:
        page.add_init_script("try { if (!sessionStorage.getItem('seeded')) { sessionStorage.setItem('seeded', '1'); localStorage.setItem('crewboss-ca-open','1'); localStorage.setItem('crewboss-ca-conv','7'); } } catch (e) {}")
    return ctx, page, ta, tb, errors


def _launch(p):
    return p.chromium.launch(executable_path=CHROME)


def _msgs(page):
    return page.evaluate("document.getElementById('ca-msgs').children.length")


def test_desktop_links_swap_the_page_and_keep_the_assistant(server, project):
    with pw.sync_playwright() as p:
        b = _launch(p)
        ctx, page, ta, tb, errors = _setup(b, server, 1440, project)
        page.goto(f"{server}/{project['slug']}/board")
        page.wait_for_selector("#ca-msgs a[data-ca-link]")
        links = page.eval_on_selector_all("#ca-msgs a", "els => els.map(a => [a.getAttribute('href'), a.target, !!a.dataset.caLink])")
        assert [f"/all/tasks/{ta['id']}/page", "", True] in links and [f"/{project['slug']}/tasks/{tb['id']}", "", True] in links
        assert ["https://example.com/x", "_blank", False] in links                 # external: new tab, not ours
        page.evaluate("window.__keep = 1; document.getElementById('ca-drawer').__node = 1; document.getElementById('ca-msgs').__node = 1")
        assert page.evaluate("document.documentElement.classList.contains('ca-open')")
        page.click(f"#ca-msgs a[href='/all/tasks/{ta['id']}/page']")
        page.wait_for_url(f"**/{project['slug']}/tasks/{ta['id']}")
        page.wait_for_selector("[data-task-record]")
        assert page.evaluate("window.__keep") == 1                                  # no document load
        assert page.evaluate("document.getElementById('ca-drawer').__node === 1 && document.getElementById('ca-msgs').__node === 1")
        assert page.evaluate("document.documentElement.classList.contains('ca-open')")
        assert _msgs(page) == 1 and "Task Alpha" in page.inner_text("#page-inner")
        assert page.evaluate("document.querySelectorAll('#drawer-content').length") == 1
        # an htmx write inside the swapped page works (comment form posts and re-renders in place)
        page.fill("#drawer-content input[name=body]", "hello from the page")
        page.click("#drawer-content button:has-text('Post')")
        page.wait_for_selector("#drawer-content :text('hello from the page')")
        assert page.evaluate("document.querySelectorAll('[data-task-record]').length") == 1
        # a title saved on the page updates the heading and the tab title; nothing was re-injected by the swap
        page.fill("#drawer-content input[name=title]", "Alpha renamed")
        page.click("#drawer-content button:has-text('Save changes')")
        page.wait_for_function("document.querySelector('[data-ca-page] [data-record-title]').textContent.trim() === 'Alpha renamed' && document.title.indexOf('Alpha renamed') >= 0")
        assert page.evaluate("document.querySelectorAll('script[src*=\"tabs.js\"]').length") == 1
        # the second link, then Back to the first, still no document load
        page.click(f"#ca-msgs a[href='/{project['slug']}/tasks/{tb['id']}']")
        page.wait_for_url(f"**/tasks/{tb['id']}")
        page.wait_for_selector("text=Task Beta")
        page.go_back()
        page.wait_for_url(f"**/tasks/{ta['id']}")
        page.wait_for_selector("h1:has-text('Alpha renamed')")
        assert page.evaluate("window.__keep") == 1 and page.evaluate("document.getElementById('ca-drawer').__node") == 1
        # Back again reaches the board (not a swappable page: a real load); rule 1: the assistant is open on the same conversation
        page.go_back()
        page.wait_for_url(f"**/{project['slug']}/board")
        page.wait_for_selector("#ca-msgs a[data-ca-link]")
        assert page.evaluate("document.documentElement.classList.contains('ca-open')") and _msgs(page) == 1
        assert not errors, errors
        b.close()


def test_phone_sheet_steps_aside_and_returns_with_the_same_conversation(server, project):
    with pw.sync_playwright() as p:
        b = _launch(p)
        ctx, page, ta, tb, errors = _setup(b, server, 390, project)
        page.goto(f"{server}/{project['slug']}/board")
        page.wait_for_selector("#ca-msgs a[data-ca-link]")
        page.evaluate("window.__keep = 1; document.getElementById('ca-msgs').__node = 1; document.getElementById('ca-scroll').scrollTop = 0")
        page.click(f"#ca-msgs a[href='/all/tasks/{ta['id']}/page']")
        page.wait_for_url(f"**/tasks/{ta['id']}")
        page.wait_for_selector("[data-task-record]")
        assert page.evaluate("window.__keep") == 1
        assert not page.evaluate("document.documentElement.classList.contains('ca-open')")        # minimised
        page.click("#ca-btn")
        page.wait_for_function("document.documentElement.classList.contains('ca-open')")
        assert page.evaluate("document.getElementById('ca-msgs').__node") == 1 and _msgs(page) == 1   # same conversation, not a fresh one
        assert not errors, errors
        b.close()


def test_rule_closed_same_project_keeps_it_another_place_starts_fresh(server, project):
    with pw.sync_playwright() as p:
        b = _launch(p)
        ctx, page, ta, tb, errors = _setup(b, server, 1440, project)
        board = f"{server}/{project['slug']}/board"
        page.goto(board)
        page.wait_for_selector("#ca-msgs a[data-ca-link]")
        # 1. open + a full document load elsewhere: still open, same conversation
        page.goto(f"{server}/{project['slug']}/list")
        page.wait_for_selector("#ca-msgs a[data-ca-link]")
        assert page.evaluate("document.documentElement.classList.contains('ca-open')") and _msgs(page) == 1
        # 2. close, reopen on the same page: the same conversation
        page.click("#ca-close")
        page.click(".ca-hdr-btn[data-ca-open]")
        page.wait_for_function("document.documentElement.classList.contains('ca-open')")
        assert _msgs(page) == 1
        # 3. (owner 11-18: "created a new conversation under the project. Grrr") close, move to another page of the SAME project, open
        #    by hand: still the same conversation. The place is the project, not the exact page.
        page.click("#ca-close")
        page.goto(board)
        assert not page.evaluate("document.documentElement.classList.contains('ca-open')")
        page.click(".ca-hdr-btn[data-ca-open]")
        page.wait_for_function("document.documentElement.classList.contains('ca-open')")
        page.wait_for_selector("#ca-msgs a[data-ca-link]")
        assert _msgs(page) == 1
        # 4. close, move OUT of the project (the Now page), open by hand: a fresh conversation
        page.click("#ca-close")
        page.goto(f"{server}/")
        page.click(".ca-hdr-btn[data-ca-open]")
        page.wait_for_function("document.documentElement.classList.contains('ca-open')")
        page.wait_for_timeout(400)
        assert _msgs(page) == 0
        assert not errors, errors
        b.close()



def test_a_task_opens_as_a_full_window_over_the_page_and_back_leaves_the_page_as_it_was(server, project):
    """Owner 11-18: "Tasks should always open full page, with an exit to go back to previous page. So a full page window on top of the
    current window", and the assistant "loads over that and automatically links to the relevant Task under it". A board card opens the
    task's full-page layout in the task sheet over the board (no document load: the board stays underneath), the address bar shows the
    task's URL, Back and the exit button close it with the board untouched, ?open=<id> opens the sheet too, and the assistant is above it."""
    with pw.sync_playwright() as p:
        b = _launch(p)
        ctx, page, ta, tb, errors = _setup(b, server, 1440, project, open_ca=False)
        page.goto(f"{server}/{project['slug']}/board")
        page.wait_for_selector(f".task-card[data-task-id='{ta['id']}']")
        page.evaluate("window.__keep = 1")
        page.click(f".task-card[data-task-id='{ta['id']}'] >> text=Task Alpha")
        page.wait_for_url(f"**/{project['slug']}/tasks/{ta['id']}")
        page.wait_for_selector("[data-task-sheet].is-open [data-task-record]")              # the full-page layout, in the sheet
        assert page.evaluate("window.__keep") == 1                                            # no document load
        assert page.query_selector(f".task-card[data-task-id='{tb['id']}']")                  # the board is still underneath
        assert page.inner_text("[data-task-sheet-title]").strip() == "Task Alpha"
        assert page.query_selector(f"[data-task-sheet] #drawer-content [data-ca-entity='task:{ta['id']}']")   # what the assistant links to
        z = page.evaluate("[getComputedStyle(document.getElementById('ca-drawer')).zIndex, getComputedStyle(document.querySelector('[data-task-sheet]')).zIndex].map(Number)")
        assert z[0] > z[1], z                                                                 # the assistant is above the task window
        # a write inside the sheet keeps the full-page layout (the address bar is the task's)
        page.fill("#drawer-content input[name=body]", "from the sheet")
        page.click("#drawer-content button:has-text('Post')")
        page.wait_for_selector("#drawer-content :text('from the sheet')")
        assert page.query_selector("[data-task-sheet] [data-task-record]")
        # Back closes it: the board's URL, the board as it was, still no document load
        page.go_back()
        page.wait_for_url(f"**/{project['slug']}/board")
        page.wait_for_selector("[data-task-sheet]:not(.is-open)", state="attached")
        assert page.evaluate("window.__keep") == 1
        # the exit button does the same
        page.click(f".task-card[data-task-id='{tb['id']}'] >> text=Task Beta")
        page.wait_for_url(f"**/tasks/{tb['id']}")
        page.wait_for_selector("[data-task-sheet].is-open")
        page.click("[data-task-sheet-exit]")
        page.wait_for_url(f"**/{project['slug']}/board")
        assert page.evaluate("window.__keep") == 1 and not page.evaluate("Alpine.store('drawer').open")
        # a link that carries ?open=<id> opens the sheet
        page.goto(f"{server}/{project['slug']}/board?open={tb['id']}")
        page.wait_for_url(f"**/{project['slug']}/tasks/{tb['id']}")
        page.wait_for_selector("[data-task-sheet].is-open >> text=Task Beta")
        assert not errors, errors
        b.close()


def test_ca_on_a_global_page_is_not_bound_to_the_last_project(server, project):
    """Owner 11-18: Now, Dashboard and Recent are never locked to a project or task. Opened from the Now page, CA bound itself to the last project visited (the current_project cookie behind data-base).
    A new chat on a global page is unbound (the whole estate); on the project's own pages it offers the project."""
    with pw.sync_playwright() as p:
        b = _launch(p)
        ctx, page, ta, tb, errors = _setup(b, server, 1440, project, open_ca=False)
        ctx.add_cookies([{"name": "current_project", "value": project["slug"], "url": server}])
        state = {"online": True, "runners": [], "conversations": [], "first_name": "M", "speech": False, "unread": 0}
        page.route("**/ca/state", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(state)))
        page.goto(f"{server}/")
        assert page.get_attribute("body", "data-base") == f"/{project['slug']}"              # the cookie's project is still the base
        page.evaluate("document.querySelector('[data-ca-open]').click()")
        page.wait_for_function("document.documentElement.classList.contains('ca-open')")
        assert project["name"] not in page.inner_text("#ca-entity") and project["slug"] not in page.inner_text("#ca-entity")
        for glob in ("/dashboard", "/recent"):                                               # the other two above the divider
            page.goto(f"{server}{glob}")
            page.wait_for_function("document.documentElement.classList.contains('ca-open')")
            assert project["slug"] not in page.inner_text("#ca-entity") and project["name"] not in page.inner_text("#ca-entity"), glob
        page.goto(f"{server}/{project['slug']}/board")
        page.wait_for_function("document.documentElement.classList.contains('ca-open')")
        page.wait_for_function(f"document.getElementById('ca-entity').innerText.indexOf({json.dumps(project['slug'])}) >= 0 || document.getElementById('ca-entity').innerText.indexOf({json.dumps(project['name'])}) >= 0")
        assert not errors, errors
        b.close()
