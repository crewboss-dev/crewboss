"""Journey 9 (#938): the first-run how-to tour. It starts by itself on a person's first page, outlines the REAL element each step is about, keeps its place
on the server, can be skipped and reopened, and works by keyboard, at phone width, in dark mode and with reduced motion. Every step is walked."""
import re

import pytest
from playwright.sync_api import expect

GEOM = """(sel) => {
  const vis = e => e.getClientRects().length && e.getBoundingClientRect().width > 0;
  const t = sel ? [...document.querySelectorAll(sel)].find(vis) : null;
  const box = e => { const r = e.getBoundingClientRect(); return {l: r.left, t: r.top, r: r.right, b: r.bottom}; };
  const ring = document.querySelector('[data-tour-ring]');
  return {target: t ? box(t) : null, ring: ring.hidden ? null : box(ring), card: box(document.querySelector('[data-tour-card]')),
          vw: innerWidth, vh: innerHeight};
}"""


def _api(page):
    return page.evaluate("fetch('/all/tour', {headers: {'HX-Request': 'true'}}).then(r => r.json())")


def _saved(page, status, step=None):
    """The save is fire-and-forget (the tour never waits on it), so poll for it rather than race it."""
    for _ in range(40):
        d = _api(page)
        if d["status"] == status and (step is None or d["step"] == step):
            return d
        page.wait_for_timeout(100)
    raise AssertionError(f"never saved as {status}/{step}: {d['status']}/{d['step']}")


def _hits(a, b):
    return a["l"] < b["r"] and b["l"] < a["r"] and a["t"] < b["b"] and b["t"] < a["b"]


def _start(page, server, sign_in, path="/"):
    sign_in(page)
    page.goto(server + path)
    page.wait_for_selector("[data-tour-root]:not([hidden])")


def _title(page):
    return page.locator("[data-tour-title]")


def _check_step(page, step, n, total):
    """One step on screen: its text, its count, the card focused, and (when it points at something) an outline round the real element that the card
    does not cover."""
    expect(_title(page)).to_have_text(step["title"])
    expect(page.locator("[data-tour-text]")).to_have_text(step["text"])
    expect(page.locator("[data-tour-count]")).to_have_text(f"Step {n} of {total}")
    assert page.evaluate("document.querySelector('[data-tour-card]').contains(document.activeElement)"), "focus is in the step card"
    assert page.locator("[data-tour-live]").text_content().startswith(f"Step {n} of {total}: {step['title']}")
    page.wait_for_timeout(1000)                                              # let the entrance and the scroll settle before measuring
    g = page.evaluate(GEOM, step.get("target"))
    c, vw, vh = g["card"], g["vw"], g["vh"]
    assert c["l"] >= -1 and c["t"] >= -1 and c["r"] <= vw + 1 and c["b"] <= vh + 1, f"{step['id']}: the card is on screen {c}"
    if step.get("target") and step["met"]:
        assert g["target"] is not None, f"{step['id']}: {step['target']} is not visible on {page.url}"
        assert g["ring"] is not None, f"{step['id']}: no outline"
        r, t = g["ring"], g["target"]
        assert _hits(r, t), f"{step['id']}: the outline {r} misses the element {t}"
        if t["b"] - t["t"] < vh * .6 and t["t"] >= 0 and t["b"] <= vh:           # a small element is wholly inside its outline (which stays 4px inside the screen)
            assert r["l"] <= max(t["l"], 4) + 1 and r["t"] <= max(t["t"], 4) + 1 and r["r"] >= min(t["r"], vw - 4) - 1 and r["b"] >= min(t["b"], vh - 4) - 1, \
                f"{step['id']}: {r} does not enclose {t}"
            assert not _hits(c, t), f"{step['id']}: the card covers what it points at ({c} over {t})"
    else:
        assert g["ring"] is None


def _next(page):
    page.locator("[data-tour-act=next]").click()


def test_the_tour_starts_by_itself_and_every_step_shows_its_real_element(new_page, server, seed, device, sign_in):
    page = new_page(device, tour=True)
    _start(page, server, sign_in)
    steps = _api(page)["steps"]
    assert [s["id"] for s in steps] == ["welcome", "project", "runner", "task", "run", "review", "assistant", "settings", "padlock", "done"]
    assert page.locator("[data-tour-act=back]").is_hidden()                                       # nothing before the first step
    for n, step in enumerate(steps, 1):
        _check_step(page, step, n, len(steps))
        page.shot(f"{n:02d}-{step['id']}")
        if n == 1:
            assert "Crewboss" in step["text"]
        if step["id"] == "runner":
            assert re.search(r"/machines(\?|$)", page.url), page.url                                    # it took us to the real page
            assert page.locator("[data-os-cards]").is_visible()
        if step["id"] == "run":
            assert re.search(r"/tasks/\d+$", page.url.split("?")[0]) and page.locator("[data-run]").is_visible()
        if step["id"] == "review":
            assert page.url.split("?")[0].endswith("/releases")
        if step["id"] == "padlock":                                                                     # the optional Front door step (#976): it goes to the card
            assert page.url.split("?")[0].endswith("/admin/front-door"), page.url
            assert page.locator("[data-tour-note]").count() == 0 or page.locator("[data-tour-note]").is_hidden()
        if step["id"] == "done":
            assert page.locator("[data-tour-act=next]").text_content().strip() == "Done"
            assert page.locator("[data-tour-act=skip]").is_hidden()
            break
        _next(page)
    # progress is on the server, at the step they reached
    _saved(page, "active", "done")
    page.locator("[data-tour-act=next]").click()
    page.wait_for_selector("[data-tour-root]", state="hidden")
    _saved(page, "finished", "done")
    page.reload()                                                                                 # finished: it does not come back by itself
    page.wait_for_selector("#page-inner")
    page.wait_for_timeout(500)
    assert page.locator("[data-tour-root]").is_hidden()


def test_back_goes_to_the_previous_step_and_across_pages(new_page, server, seed, device, sign_in):
    page = new_page(device, tour=True)
    _start(page, server, sign_in)
    for title in ("Make your first project", "Enrol a runner on your PC"):
        _next(page)
        expect(_title(page)).to_have_text(title)
    page.wait_for_url("**/machines**")
    page.locator("[data-tour-act=back]").click()
    expect(_title(page)).to_have_text("Make your first project")
    page.locator("[data-tour-act=back]").click()
    expect(_title(page)).to_have_text("Welcome to Crewboss")
    _saved(page, "active", "welcome")


def test_it_resumes_where_it_was_left_and_skip_ends_it_until_how_to(new_page, server, seed, device, sign_in):
    page = new_page(device, tour=True)
    _start(page, server, sign_in)
    _next(page)
    expect(_title(page)).to_have_text("Make your first project")
    _saved(page, "active", "project")
    page.reload()                                                                                 # a reload, a new page, another device: same place
    page.wait_for_selector("[data-tour-root]:not([hidden])")
    expect(_title(page)).to_have_text("Make your first project")
    page.locator("[data-tour-act=skip]").click()
    page.wait_for_selector("[data-tour-root]", state="hidden")
    page.reload()                                                                                 # straight away: the save must survive the reload
    _saved(page, "skipped", "project")
    page.reload()
    page.wait_for_selector("#page-inner")
    page.wait_for_timeout(500)
    assert page.locator("[data-tour-root]").is_hidden()
    # How-to, in the profile menu, begins again from the first step
    page.locator("#me-btn" if device == "phone" else "#user-btn").click()
    page.locator("[data-tour-restart]").click()
    page.wait_for_selector("[data-tour-root]:not([hidden])")
    expect(_title(page)).to_have_text("Welcome to Crewboss")
    _saved(page, "active", "welcome")
    page.shot("restarted-from-how-to")


def test_keyboard_tab_is_kept_in_the_card_enter_advances_and_esc_closes(new_page, server, seed, sign_in):
    page = new_page("desktop", tour=True)
    _start(page, server, sign_in)
    inside = "document.querySelector('[data-tour-card]').contains(document.activeElement)"
    for _ in range(8):
        page.keyboard.press("Tab")
        assert page.evaluate(inside), "Tab left the step card"
    for _ in range(8):
        page.keyboard.press("Shift+Tab")
        assert page.evaluate(inside), "Shift+Tab left the step card"
    page.locator("[data-tour-act=next]").focus()
    page.keyboard.press("Enter")
    expect(_title(page)).to_have_text("Make your first project")
    assert page.evaluate(inside)
    page.keyboard.press("Escape")
    page.wait_for_selector("[data-tour-root]", state="hidden")
    _saved(page, "skipped", "project")


def test_a_press_on_the_card_does_not_close_the_menu_a_step_opened(new_page, server, seed, device, sign_in):
    """The profile menu step opens the menu; Next / Back must not count as 'a click outside' and shut it before the next step points into it."""
    page = new_page(device, tour=True)
    _start(page, server, sign_in)
    steps = _api(page)["steps"]
    for _ in range(7):
        _next(page)
    expect(_title(page)).to_have_text("Where settings live")
    assert page.locator("#user-menu").is_visible()
    _next(page)
    expect(_title(page)).to_have_text("Give it your own web address")          # the padlock step (#976) sits between settings and the end
    _next(page)
    expect(_title(page)).to_have_text("You're set")
    assert page.locator("[data-tour-restart]").is_visible() and page.locator("#user-menu").is_visible()
    _check_step(page, steps[-1], 10, 10)
    page.locator("[data-tour-act=back]").click()
    expect(_title(page)).to_have_text("Give it your own web address")
    page.locator("[data-tour-act=back]").click()
    expect(_title(page)).to_have_text("Where settings live")
    assert page.locator("#user-menu").is_visible()


def test_phone_card_is_a_bottom_sheet_that_swipes_away_and_respects_reduced_motion(new_page, server, seed, sign_in):
    page = new_page("phone", tour=True)
    page.emulate_media(reduced_motion="reduce")
    _start(page, server, sign_in)
    g = page.evaluate(GEOM, None)
    c = g["card"]
    assert abs(c["b"] - g["vh"]) <= 1 and c["l"] <= 0 and c["r"] >= g["vw"] - 1, f"a bottom sheet across the screen: {c}"
    assert page.evaluate("getComputedStyle(document.querySelector('[data-tour-card]')).animationName") == "tour-fade"      # a cross-fade, not a slide
    assert page.evaluate("document.querySelector('[data-tour-card] [data-sheet-grab]').offsetHeight") > 0
    for b in page.locator("[data-tour-card] button:visible").all():                                                      # 44px touch targets
        assert b.bounding_box()["height"] >= 43, b.text_content()
    page.shot("bottom-sheet-reduced-motion")
    # swipe it down by its grab handle: the tour closes (the same as Skip tour)
    box = page.locator("[data-tour-card] [data-sheet-grab]").bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    client = page.context.new_cdp_session(page)
    for kind, yy in (("touchStart", y), ("touchMove", y + 60), ("touchMove", y + 160), ("touchMove", y + 260), ("touchEnd", y + 260)):
        client.send("Input.dispatchTouchEvent", {"type": kind, "touchPoints": [] if kind == "touchEnd" else [{"x": x, "y": yy}]})
        page.wait_for_timeout(30)
    page.wait_for_selector("[data-tour-root]", state="hidden")
    _saved(page, "skipped")


def test_dark_mode_card_and_outline(new_page, server, seed, device, sign_in):
    page = new_page(device, tour=True)
    page.add_init_script("try { localStorage.setItem('crewboss-theme', 'dark'); } catch (e) {}")
    _start(page, server, sign_in)
    assert page.evaluate("document.documentElement.classList.contains('dark')")
    expect(_title(page)).to_have_text("Welcome to Crewboss")
    bg, fg = page.evaluate("(() => { const s = getComputedStyle(document.querySelector('[data-tour-card]')); return [s.backgroundColor, s.color]; })()")
    lum = lambda c: sum(int(v) for v in re.findall(r"\d+", c)[:3]) / 3                                                    # noqa: E731
    assert lum(bg) < 60 and lum(fg) > 200, (bg, fg)                                                                      # dark card, light text
    _next(page)
    _next(page)
    page.wait_for_url("**/machines**")
    expect(_title(page)).to_have_text("Enrol a runner on your PC")
    page.wait_for_timeout(600)                                                                                           # past the entrance, for a clean picture
    page.shot("dark-runner")


# --- an EMPTY instance: a fresh owner with no projects and no tasks (the fixtures above always have some, which hid this) ----------------------------------

def _make_project(page, name="First Project", slug="first-project"):
    form = page.locator("form[action='/projects']")
    form.wait_for(state="visible")
    form.locator("input[name=name]").fill(name)
    form.locator("input[name=slug]").fill(slug)
    sn = form.locator("input[name=short_name]")
    if sn.count() and sn.is_visible():
        sn.fill("fp")
    form.locator("button[type=submit]").click()


def _no_off_site(page, server, seen):
    assert all(u.startswith(server + "/") or u == server for u in seen), f"navigated off the app: {seen}"


@pytest.mark.parametrize("how", ["press-the-real-button", "do-it-now"])
def test_empty_instance_walk_has_no_dead_end_no_404_and_no_off_site_navigation(new_page, server, device, sign_in, how):
    page = new_page(device, tour=True)                                    # NO seed: no projects, no tasks
    seen = []
    page.on("framenavigated", lambda f: seen.append(f.url) if f == page.main_frame else None)
    _start(page, server, sign_in)
    steps = _api(page)["steps"]
    by = {s["id"]: s for s in steps}
    for sid in ("runner", "run", "review"):                               # nothing to go to yet: no navigation, no outline, a sentence saying what to do
        assert by[sid]["met"] is False and "go" not in by[sid] and by[sid]["else_text"], sid
    expect(_title(page)).to_have_text("Welcome to Crewboss")
    _next(page)
    expect(_title(page)).to_have_text("Make your first project")
    _check_step(page, steps[1], 2, len(steps))
    assert page.locator("[data-tour-act=do]").is_visible()
    page.shot(f"empty-{how}-step2")
    if how == "do-it-now":
        # backing out of the dialog brings the tour back to this step, and the person is not stranded
        page.locator("[data-tour-act=do]").click()
        page.locator("form[action='/projects']").wait_for(state="visible")
        assert page.locator("[data-tour-root]").is_hidden()
        page.locator("form[action='/projects'] button:has-text('Cancel')").click()      # backing out of the dialog
        page.wait_for_selector("[data-tour-root]:not([hidden])")
        expect(_title(page)).to_have_text("Make your first project")
        page.locator("[data-tour-act=do]").click()
    else:
        # the outlined + is clickable THROUGH the dim: press the real thing
        box = page.locator("[data-tour='new-project']").bounding_box()
        # ...but ONLY the + itself: the hole is the control's own rect, so a click a few px beside it (inside the padded outline) lands on the
        # shield and never reaches the neighbouring control.
        cy = box["y"] + box["height"] / 2
        for bx, by in ((box["x"] - 3, cy), (box["x"] + box["width"] + 3, cy), (box["x"] + box["width"] / 2, box["y"] - 3),
                       (box["x"] + box["width"] / 2, box["y"] + box["height"] + 3)):
            top = page.evaluate("([x, y]) => { const e = document.elementFromPoint(x, y); return !!(e && e.closest('[data-tour-root]')); }", [bx, by])
            assert top, f"a point just beside the + ({bx:.0f},{by:.0f}) reaches the page under the shield"
            page.mouse.click(bx, by)
        page.wait_for_timeout(300)
        assert page.locator("form[action='/projects']").is_hidden() and page.locator("[data-tour-root]").is_visible()
        assert page.evaluate("document.elementFromPoint(%f, %f).closest('[data-tour=new-project]') !== null" % (box["x"] + box["width"] / 2, cy))
        page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.locator("form[action='/projects']").wait_for(state="visible")
    assert page.locator("[data-tour-root]").is_hidden()                   # it stepped aside while the person uses the real form
    _make_project(page)
    page.wait_for_url("**/first-project**")
    page.wait_for_selector("[data-tour-root]:not([hidden])")
    expect(_title(page)).to_have_text("Enrol a runner on your PC")        # the project exists now: the tour went on to the next step
    steps = _api(page)["steps"]
    by = {s["id"]: s for s in steps}
    assert by["runner"]["met"] is True and by["runner"]["go"] == "/runners" and by["project"]["met"] is True
    assert by["run"]["met"] is False and by["review"]["met"] is True and by["review"]["go"] == "/first-project/releases"
    n = 3
    while True:
        step = steps[n - 1]
        _check_step(page, step, n, len(steps))
        if step["id"] == "runner":
            assert re.search(r"/machines(\?|$)", page.url) and page.locator("[data-os-cards]").is_visible()
        if step["id"] == "run":                                           # no task yet: it says so, stays where it is, outlines nothing
            assert "/tasks/" not in page.url and page.locator("[data-tour-note]").is_visible()
            assert "task" in page.locator("[data-tour-note]").text_content().lower()
        if step["id"] == "review":
            assert page.url.split("?")[0].endswith("/first-project/releases")
        page.shot(f"empty-{how}-{n:02d}-{step['id']}")
        if step["id"] == "done":
            break
        _next(page)
        n += 1
        expect(_title(page)).to_have_text(steps[n - 1]["title"])
        assert page.locator("body").text_content().strip() != "", "a dead end: an empty page"
    page.locator("[data-tour-act=next]").click()
    page.wait_for_selector("[data-tour-root]", state="hidden")
    _saved(page, "finished", "done")
    _no_off_site(page, server, seen)
    assert not [u for u in seen if "//releases" in u or u.rstrip("/").endswith("//")], seen


def test_the_tour_swallows_slash_and_ctrl_k_so_search_stays_shut(new_page, server, seed, sign_in):
    page = new_page("desktop", tour=True)
    _start(page, server, sign_in)
    page.keyboard.press("/")
    page.keyboard.press("Control+k")
    page.wait_for_timeout(300)
    assert page.locator("[data-jump]").is_hidden()
    assert page.evaluate("document.activeElement.closest('[data-tour-card]') !== null")
    page.keyboard.press("Escape")
    page.wait_for_selector("[data-tour-root]", state="hidden")
    page.keyboard.press("/")                                              # with the tour closed, "/" is the search again
    page.wait_for_selector("[data-jump]", state="visible")
