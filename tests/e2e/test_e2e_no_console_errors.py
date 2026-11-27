"""Journey 8, the cheap broad net: visit the pages a person lands on and fail on any console error, uncaught exception, failed same-origin request or
response >= 400. (Every other test in this suite carries the same watcher: tests/e2e/conftest.py new_page.) Expected ones go in conftest ALLOWED, each with a reason."""
import pytest

PAGES = [
    "/", "/dashboard", "/recent",
    "/test-proj", "/test-proj/list", "/test-proj/board", "/test-proj/changes", "/test-proj/workflows", "/test-proj/knowledge",
    "/test-proj/discussions", "/test-proj/integrations", "/test-proj/skills", "/test-proj/rules", "/test-proj/materials",
    "/test-proj/objectives", "/test-proj/activity", "/test-proj/health", "/test-proj/releases", "/test-proj/environment",
    "/test-proj/team", "/test-proj/settings", "/test-proj/machines?tab=runners", "/test-proj/machines?tab=servers",
    "/test-proj/tasks/{t1}",
    "/assets", "/assets/recent", "/assets/skills", "/assets/rules", "/assets/materials",
    "/admin", "/admin/users",
    "/ca/history",
]


def test_no_console_errors_or_failed_requests_on_any_page(new_page, server, seed, device, sign_in):
    page = new_page(device)
    sign_in(page)
    found = []
    for path in PAGES:
        path = path.format(t1=seed["t1"]["id"])
        page.watch.problems.clear()
        resp = page.goto(server + path)
        page.wait_for_load_state("load")
        page.wait_for_selector("#page-inner, main")
        if resp is None or resp.status >= 400:
            found.append(f"{path}: the page itself answered {resp.status if resp else None}")
        found += [f"{path}: {p}" for p in page.watch.problems]
        page.watch.problems.clear()
    assert not found, "pages with problems:\n" + "\n".join(found)


def test_the_watcher_itself_catches_an_error_a_failed_request_and_an_exception(new_page, server, seed):
    """A net that cannot catch anything is worse than none: prove the three kinds are seen (then mute the page so its teardown check passes)."""
    page = new_page("desktop")
    page.goto(server + "/")
    with page.expect_event("pageerror"), page.expect_response("**/no-such-page-anywhere"):
        page.evaluate("fetch('/no-such-page-anywhere'); console.error('boom from the test'); setTimeout(() => { throw new Error('late failure') }, 0)")
    text = "\n".join(page.watch.problems)
    assert "console error: boom from the test" in text and "HTTP 404" in text and "uncaught: " in text, text
    page.watch.muted = True                                                          # provoked on purpose: not for the teardown check
