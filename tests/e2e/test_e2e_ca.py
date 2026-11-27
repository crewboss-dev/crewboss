"""Journey 4 (owner 11-18): the CA drawer sits above the task sheet and is bound to that task; on Now / Dashboard / Recent it is bound to no project; inside a
project it offers that project; a sent message shows once. The runner side is stubbed in the browser (tests/e2e/conftest.py stub_ca) and the
conversation endpoints record what the page asked for."""
import json

import pytest


def stub_conversations(page, sent):
    """POST /ca/conversations answers with the entity it was asked to link (what the server would check and keep); /messages answers with an id."""
    def create(route):
        body = route.request.post_data_json or {}
        sent["create"] = body
        ent = body.get("entity")
        ctx = {"entity": {**ent, "title": ""}} if ent else {}
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"id": 7, "title": "t", "context": ctx}))

    def message(route):
        sent["message"] = route.request.post_data_json
        if sent.get("before_answer"):
            sent["before_answer"]()
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"message_id": 55}))
        if sent.get("after_answer"):
            sent["after_answer"]()

    page.route("**/ca/conversations", lambda r: create(r) if r.request.method == "POST" else r.fallback())
    page.route("**/ca/conversations/7/messages", message)
    page.route("**/ca/conversations/7", lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(
        {"id": 7, "title": "t", "context": {}, "messages": [], "cards": [], "live_turn": None})))


def open_ca(page, device):
    page.locator("#ca-btn" if device == "phone" else ".ca-hdr-btn").click()
    page.wait_for_function("document.documentElement.classList.contains('ca-open')")


def chip_text(page):
    page.wait_for_timeout(0)                      # (the chip is repainted on a short debounce: wait for it, never sleep)
    return page.evaluate("(() => { var e = document.getElementById('ca-entity'); return e.hidden ? '' : e.textContent; })()")


DESKTOP_SHEET_THEN_CA = pytest.param("desktop", "sheet-then-ca")


@pytest.mark.parametrize("device,order", [DESKTOP_SHEET_THEN_CA, ("desktop", "ca-then-sheet"), ("phone", "sheet-then-ca"), ("phone", "ca-then-sheet")])
def test_ca_opens_above_the_task_sheet_and_is_bound_to_that_task(new_page, server, seed, device, order):
    page = new_page(device)
    t = seed["t1"]
    page.goto(f"{server}/test-proj/board")
    if order == "ca-then-sheet":
        open_ca(page, device)
        if device == "phone":                      # on a phone the assistant fills the screen: it steps aside for a card, as a page link does
            page.locator("#ca-close").click()
            page.wait_for_function("!document.documentElement.classList.contains('ca-open')")
    page.locator(f".task-card[data-task-id='{t['id']}']").locator(f"text={t['title']}").click()
    page.wait_for_url(f"**/tasks/{t['id']}")
    page.wait_for_selector("[data-task-sheet].is-open [data-task-record]")
    # the way a person gets to the assistant with the sheet open: a REAL click (Playwright refuses one that lands on something else)
    if order == "sheet-then-ca" or device == "phone":
        open_ca(page, device)
    page.wait_for_function(f"document.getElementById('ca-entity').textContent.indexOf('Task #{t['id']}') >= 0")
    page.shot("ca-over-the-task-sheet")
    z = page.evaluate("[getComputedStyle(document.getElementById('ca-drawer')).zIndex, getComputedStyle(document.querySelector('[data-task-sheet]')).zIndex].map(Number)")
    assert z[0] > z[1], z
    # what is under the middle of the assistant's panel is the assistant, not the sheet
    # (polled: the panel slides in and re-lays itself out when the task's discussion arrives, so the first instant may catch it mid-move;
    # a sheet that really covers the assistant stays covering, and this still fails)
    try:
        page.wait_for_function("""(() => { var r = document.getElementById('ca-panel').getBoundingClientRect();
            var e = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2); return !!(e && e.closest('#ca-drawer')); })()""", timeout=3000)
        hit = True
    except Exception:
        hit = False
    assert hit, "the task sheet covers the assistant"
    assert "Task" in chip_text(page) and f"#{t['id']}" in chip_text(page)
    # the first message starts a conversation linked to THIS task
    sent = {}
    stub_conversations(page, sent)
    page.locator("#ca-input").fill("what is the state of this?")
    page.locator("#ca-input").press("Enter")
    page.wait_for_selector("#ca-msgs .ca-msg.user")
    assert sent["create"]["entity"] == {"kind": "task", "id": str(t["id"])}, sent["create"]
    page.shot("ca-message-sent-on-the-task")


@pytest.mark.parametrize("path", ["/", "/dashboard", "/recent"])
def test_ca_on_now_dashboard_and_recent_is_not_bound_to_a_project(new_page, server, seed, device, path):
    """Even with a last-visited project in the cookie (what feeds the shared partials' data-base), the three global pages never lock the assistant."""
    page = new_page(device)
    page.context.add_cookies([{"name": "current_project", "value": "test-proj", "url": server}])
    page.goto(server + path)
    if path == "/":
        assert page.get_attribute("body", "data-base") == "/test-proj"    # on Now the cookie IS in play: this is the case the rule exists for
    open_ca(page, device)
    assert chip_text(page) == ""
    sent = {}
    stub_conversations(page, sent)
    page.locator("#ca-input").fill("hello")
    page.locator("#ca-input").press("Enter")
    page.wait_for_selector("#ca-msgs .ca-msg.user")
    assert not sent["create"].get("entity"), sent["create"]
    page.shot(f"ca-unbound-on-{path.strip('/') or 'now'}")


def test_ca_inside_a_project_offers_that_project(new_page, server, seed, device):
    page = new_page(device)
    page.goto(f"{server}/test-proj/board")
    open_ca(page, device)
    page.wait_for_function("document.getElementById('ca-entity').textContent.toLowerCase().indexOf('project') >= 0")
    assert "test-proj" in chip_text(page) or "Test Project" in chip_text(page)
    sent = {}
    stub_conversations(page, sent)
    page.locator("#ca-input").fill("hello")
    page.locator("#ca-input").press("Enter")
    page.wait_for_selector("#ca-msgs .ca-msg.user")
    assert sent["create"]["entity"] == {"kind": "project", "id": "test-proj"}, sent["create"]
    page.shot("ca-offers-the-project")


@pytest.mark.parametrize("order", ["live-event-first", "post-answer-first"])
def test_a_sent_message_shows_once(new_page, server, seed, order):
    """Owner 11-18: "doubled". The live `message` event from the stream can beat the POST's answer (or follow it): either way ONE bubble."""
    page = new_page("desktop")
    page.goto(f"{server}/test-proj/board")
    open_ca(page, "desktop")
    sent = {}
    stub_conversations(page, sent)
    event = {"conversation_id": 7, "message": {"id": 55, "role": "user", "content": "say it once", "attachments": []}}
    emit = lambda: page.evaluate("d => window.__ca_emit('message', d)", event)   # noqa: E731
    sent["before_answer" if order == "live-event-first" else "after_answer"] = emit
    page.locator("#ca-input").fill("say it once")
    page.locator("#ca-input").press("Enter")
    page.wait_for_selector("#ca-msgs .ca-msg.user[data-mid='55']")
    assert page.locator("#ca-msgs .ca-msg.user").count() == 1
    assert page.locator("#ca-msgs .ca-msg.user").inner_text().strip() == "say it once"
    page.shot(f"sent-once-{order}")
