"""Journey 3 (owner 11-18): a task opens as a full-window sheet over the page. The address bar shows the task, Back / Esc / the exit button / the x all close
it, and the page underneath is exactly as it was (a marker on `window` survives = no reload; the scroll position is unchanged). Forward reopens it. A refresh on
the task's URL is the full task page. Moving from one task to the next inside the sheet adds no history entry."""
import pytest

from app import repo

OPENERS = {
    "board": lambda p, t: p.locator(f".task-card[data-task-id='{t['id']}']").locator("text=" + t["title"]),
    "list": lambda p, t: p.locator(f"[data-task='{t['id']}']:visible a.tl-title"),
    "now": lambda p, t: p.locator(f".focus-row:has-text('{t['title']}')"),
}
START = {"board": "/test-proj/board", "list": "/test-proj/list", "now": "/"}


@pytest.fixture
def busy_project(seed):
    """Enough backlog tasks that the list scrolls."""
    for i in range(30):
        repo.create_task(seed["a"]["id"], f"Filler {i:02d}", "x", "could", "tweak", [], {}, "owner", status="backlog")
    return seed


def _sheet_open(page):
    page.wait_for_selector("[data-task-sheet].is-open [data-task-record]")


def _closed(page):
    page.wait_for_selector("[data-task-sheet]:not(.is-open)", state="attached")


def _state(page):
    return page.evaluate("({keep: window.__keep, y: (document.getElementById('page') || {}).scrollTop, len: history.length, path: location.pathname + location.search})")


@pytest.mark.parametrize("where", ["board", "list", "now"])
def test_a_task_opens_over_the_page_and_every_exit_leaves_the_page_untouched(new_page, server, busy_project, device, where):
    s = busy_project
    page = new_page(device)
    task = s["t1"]
    page.goto(server + START[where])
    OPENERS[where](page, task).first.wait_for()
    page.evaluate("window.__keep = 1; var p = document.getElementById('page'); p.scrollTop = 120;")
    before = _state(page)
    page.shot(f"{where}-before")
    url = f"/test-proj/tasks/{task['id']}"
    exits = {
        "back": lambda: page.go_back(),
        "escape": lambda: page.keyboard.press("Escape"),
        "exit-button": lambda: page.locator("[data-task-sheet-exit]").click(),
        "x": lambda: page.locator("[data-task-sheet] .task-sheet-x").click(),
    }
    for name, leave in exits.items():
        OPENERS[where](page, task).first.click()
        page.wait_for_url(f"**{url}")
        _sheet_open(page)
        open_state = _state(page)
        assert open_state["keep"] == 1, "opening the sheet must not reload the page"
        if name == "back":                                         # the first open: one new entry (later opens reuse the slot Back left)
            assert open_state["len"] == before["len"] + 1, open_state
        assert page.inner_text("[data-task-sheet-title]").strip() == task["title"]
        assert page.locator("[data-task-sheet-no]").text_content().strip() == f"Task #{task['id']}"
        page.shot(f"{where}-sheet-open-then-{name}")
        leave()
        _closed(page)
        page.wait_for_url(f"**{before['path']}")
        after = _state(page)
        assert after["keep"] == 1, f"{name}: the page underneath was reloaded"
        assert after["path"] == before["path"], f"{name}: the address bar did not return to the page"
        assert abs(after["y"] - before["y"]) <= 1, f"{name}: the page's scroll moved ({before['y']} -> {after['y']})"


def test_forward_reopens_the_sheet_and_a_refresh_is_the_full_task_page(new_page, server, seed, device):
    page = new_page(device)
    t = seed["t1"]
    page.goto(server + "/test-proj/board")
    page.evaluate("window.__keep = 1")
    page.locator(f".task-card[data-task-id='{t['id']}']").locator(f"text={t['title']}").click()
    page.wait_for_url(f"**/test-proj/tasks/{t['id']}")
    _sheet_open(page)
    page.go_back()
    _closed(page)
    page.go_forward()
    page.wait_for_url(f"**/test-proj/tasks/{t['id']}")
    _sheet_open(page)
    assert page.evaluate("window.__keep") == 1
    assert page.inner_text("[data-task-sheet-title]").strip() == t["title"]
    page.shot("forward-reopened")
    page.reload()                                                     # a refresh on the task URL: the full page, not a sheet
    page.wait_for_selector("[data-ca-page] [data-task-record]")
    assert page.evaluate("window.__keep") is None                     # a real reload this time
    assert page.locator("[data-task-sheet].is-open").count() == 0
    assert page.locator("[data-ca-page] [data-record-title]").inner_text().strip() == t["title"]
    page.shot("refresh-full-page")


def test_moving_from_one_task_to_the_next_inside_the_sheet_adds_no_history(new_page, server, seed, device):
    """The sheet's content is replaced when another task is loaded into it (a link in the CA, a jump result, a card behind a dialog all do exactly this
    through htmx): the address bar follows, and it is ONE history entry, so a single Back leaves the sheet."""
    page = new_page(device)
    a, b = seed["t1"], seed["t2"]
    page.goto(server + "/test-proj/board")
    page.evaluate("window.__keep = 1")
    page.locator(f".task-card[data-task-id='{a['id']}']").locator(f"text={a['title']}").click()
    page.wait_for_url(f"**/tasks/{a['id']}")
    _sheet_open(page)
    n = page.evaluate("history.length")
    page.evaluate(f"htmx.ajax('GET', '/test-proj/tasks/{b['id']}/panel', {{target: '#drawer-content', swap: 'innerHTML'}})")
    page.wait_for_url(f"**/tasks/{b['id']}")
    page.wait_for_function(f"document.querySelector('[data-task-sheet-title]').textContent.trim() === {b['title']!r}")
    assert page.evaluate("history.length") == n, "moving to the next task must replace the entry, not add one"
    page.shot("second-task-in-the-sheet")
    page.go_back()                                                    # ONE back leaves the sheet for the board
    _closed(page)
    page.wait_for_url("**/test-proj/board")
    assert page.evaluate("window.__keep") == 1


def test_an_open_param_link_opens_the_sheet(new_page, server, seed):
    page = new_page("desktop")
    t = seed["t2"]
    page.goto(f"{server}/test-proj/board?open={t['id']}")
    page.wait_for_url(f"**/test-proj/tasks/{t['id']}")
    _sheet_open(page)
    assert page.inner_text("[data-task-sheet-title]").strip() == t["title"]
