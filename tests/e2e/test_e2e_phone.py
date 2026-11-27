"""Journey 7: the phone. The bottom menu sheet opens and closes; the task sheet ends above the tab bar and its head is a drag zone.
Touch gestures are real touch events sent through the browser's debugging protocol (the sheets' drag code ignores a mouse by design)."""


def swipe_down(page, selector, dy=320, steps=8):
    """A finger put down on the middle of `selector` and pulled `dy` pixels down, then lifted."""
    page.evaluate("Promise.all(document.getAnimations().map(a => a.finished.catch(() => null)))")      # a sheet still sliding in is not where it will rest
    box = page.locator(selector).first.bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    cdp = page.context.new_cdp_session(page)

    def touch(kind, yy):
        cdp.send("Input.dispatchTouchEvent", {"type": kind, "touchPoints": [] if kind == "touchEnd" else [{"x": x, "y": yy}]})
    touch("touchStart", y)
    for i in range(1, steps + 1):
        touch("touchMove", y + dy * i / steps)
    touch("touchEnd", y + dy)
    cdp.detach()


def rects(page, *selectors):
    page.evaluate("Promise.all(document.getAnimations().map(a => a.finished.catch(() => null)))")      # let the slide-in finish: measure the resting place
    return page.evaluate("sels => sels.map(s => { var r = document.querySelector(s).getBoundingClientRect(); return {top: r.top, bottom: r.bottom, left: r.left, right: r.right}; })", list(selectors))


def test_the_phone_menu_is_a_bottom_sheet_that_opens_and_closes(new_page, server, seed):
    page = new_page("phone")
    page.goto(server + "/")
    page.wait_for_selector("#tabbar")
    assert page.locator("#nav-drawer").get_attribute("aria-hidden") == "true"
    page.shot("closed")
    page.locator("#tabbar [data-nav-toggle]").tap()
    page.wait_for_function("document.documentElement.classList.contains('nav-open')")
    page.wait_for_function("document.getElementById('nav-drawer').getAttribute('aria-hidden') === 'false'")
    drawer, bar = rects(page, "#nav-drawer", "#tabbar")
    assert abs(drawer["bottom"] - bar["top"]) <= 1, f"the menu sheet must end at the tab bar: {drawer} / {bar}"
    assert drawer["left"] <= 0.5 and drawer["right"] >= 389.5, "a bottom sheet spans the width"
    assert page.locator("#nav-sec-accounts a[data-record='test-proj']").is_visible()
    page.shot("open")
    page.locator("#tabbar [data-nav-toggle]").tap()                    # the Menu tab again closes it
    page.wait_for_function("!document.documentElement.classList.contains('nav-open')")
    # and a swipe down on its grab line closes it too
    page.locator("#tabbar [data-nav-toggle]").tap()
    page.wait_for_function("document.documentElement.classList.contains('nav-open')")
    swipe_down(page, "#nav-drawer .nav-grab")
    page.wait_for_function("!document.documentElement.classList.contains('nav-open')")
    page.shot("closed-by-swipe")


def test_the_task_sheet_ends_above_the_tab_bar_and_its_head_drags_it_away(new_page, server, seed):
    page = new_page("phone")
    t = seed["t1"]
    page.goto(f"{server}/test-proj/board")
    page.evaluate("window.__keep = 1")
    page.locator(f".task-card[data-task-id='{t['id']}']").locator(f"text={t['title']}").tap()
    page.wait_for_url(f"**/tasks/{t['id']}")
    page.wait_for_selector("[data-task-sheet].is-open [data-task-record]")
    sheet, bar = rects(page, "[data-task-sheet]", "#tabbar")
    assert sheet["bottom"] <= bar["top"] + 1, f"the task sheet runs under the tab bar: {sheet} / {bar}"
    assert bar["bottom"] > bar["top"] and page.locator("#tabbar").is_visible()
    # the head is the drag zone: marked as a grab area, with the browser's own touch scrolling off so the drag reaches the script
    head = page.locator("[data-task-sheet] .task-sheet-head")
    assert head.get_attribute("data-sheet-grab") is not None
    assert page.evaluate("getComputedStyle(document.querySelector('.task-sheet-head')).touchAction") == "none"
    page.shot("task-sheet-above-the-bar")
    swipe_down(page, "[data-task-sheet] .task-sheet-no")                # pulled down from the head's middle (the number, not a button)
    page.wait_for_selector("[data-task-sheet]:not(.is-open)", state="attached")
    page.wait_for_url("**/test-proj/board")
    assert page.evaluate("window.__keep") == 1
    page.shot("after-the-swipe")
