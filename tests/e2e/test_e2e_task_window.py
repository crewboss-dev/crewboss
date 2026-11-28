"""#91: the redesigned task window, driven in a real browser: one column in order (header, code line, NOW, summary, checklist, timeline), the menu by
keyboard, edit in place with a Saved tick, the summary edit, the timeline opening on what is waiting on you, nothing wider than the screen on a
phone (390 px). With CREWBOSS_91_SHOTS=<dir> set it also writes the demo screenshots (desktop + phone, light + dark)."""
import os

import pytest

from app import repo, task_summary
from app.db import get_cursor

SHOTS_91 = os.environ.get("CREWBOSS_91_SHOTS", "")


def _demo(seed):
    """Task Alpha in a busy state: a finished run on a branch that is reviewed but not merged, AI comments, an open question, a summary."""
    from app import web_auth
    t, a = seed["t1"], seed["a"]
    uid = web_auth.upsert_user({"oid": "cccccccc-0000-4000-8000-000000000091", "name": "Demo Builder"})
    with get_cursor() as cur:
        cur.execute("UPDATE projects SET git_remote = 'https://github.com/example/checkout.git' WHERE id = %s", (a["id"],))
        cur.execute("SELECT id FROM agent_profiles ORDER BY id LIMIT 1")
        prof = cur.fetchone()["id"]
        cur.execute("INSERT INTO jobs (task_id, account_id, user_id, profile_id, state, branch, summary, finished_at) "
                    "VALUES (%s, %s, %s, %s, 'done', 'ai/checkout-wallets', 'Wallet buttons added on cart and checkout; unit tests green.', now()) RETURNING id",
                    (t["id"], a["id"], uid, prof))
        jid = cur.fetchone()["id"]
        cur.execute("INSERT INTO merge_requests (project_id, job_id, branch, sha, state, audit_passed_at) VALUES (%s, %s, 'ai/checkout-wallets', %s, 'requested', now())",
                    (a["id"], jid, "b" * 40))
    repo.add_subtasks(t["id"], ["Add the wallet payment sheet", "Hide the buttons on unsupported browsers"], "owner")
    repo.comment_task(t["id"], "Please keep it behind the existing payment provider.", "owner")
    for i in range(3):
        repo.comment_task(t["id"], f"Progress {i + 1}: wired the wallet sheet into the cart.\n<details>\n**Files:** `cart.py`, `checkout.py`\n\n- tests added\n</details>", "claude:features")
    repo.comment_task(t["id"], "Long note. " + "It covers the whole flow in detail. " * 20, "claude:features")
    repo.ask_owner(t["id"], "Which wallets should the buttons offer?", "The provider supports three wallets and each needs its own review.", ["Apple Pay only", "Apple Pay and Google Pay"], "claude:features")
    task_summary.refresh_now(t["id"])
    return t


def _open(page, server, t):
    page.goto(f"{server}/test-proj/tasks/{t['id']}")
    page.wait_for_selector("[data-timeline]")
    return page


def test_one_column_in_order_and_nothing_wider_than_the_screen(new_page, server, seed, device):
    t = _demo(seed)
    page = _open(new_page(device), server, t)
    ys = [page.locator(sel).first.bounding_box()["y"] for sel in ("[data-task-header]", "[data-code-line]", "[data-now-line]", "[data-summary]", "#task-checklist", "[data-timeline]")]
    assert ys == sorted(ys), ys
    xs = page.evaluate("[document.documentElement.scrollWidth, window.innerWidth]")
    assert xs[0] <= xs[1], f"horizontal scroll: {xs}"
    assert page.locator("[data-task-tab]").count() == 0 and page.locator("text=Save changes").count() == 0
    assert page.locator("[data-code-chip='reviewed'][data-on='yes']").count() == 1 and page.locator("[data-code-chip='merged'][data-on='no']").count() == 1
    assert page.locator("[data-code-view]").get_attribute("href").startswith("https://github.com/example/checkout/compare/ai/checkout-wallets")
    page.shot(f"window-{device}")


def test_it_opens_on_what_is_waiting_on_you(new_page, server, seed, device):
    t = _demo(seed)
    page = _open(new_page(device), server, t)
    page.wait_for_function("(() => { var w = document.querySelector('[data-waiting]'); if (!w) return false; var r = w.getBoundingClientRect(); return r.top >= 0 && r.bottom <= window.innerHeight; })()")
    assert page.locator("[data-waiting]").count() == 1
    page.shot(f"waiting-{device}")


def test_status_menu_by_keyboard_and_edit_in_place(new_page, server, seed):
    t = seed["t2"]
    page = _open(new_page("desktop"), server, t)
    btn = page.locator("[data-task-menu-button]")
    btn.focus()
    page.keyboard.press("Enter")
    page.wait_for_selector("[aria-label='Task actions']", state="visible")
    assert page.evaluate("document.activeElement.getAttribute('role')").startswith("menuitem")     # focus moved into the menu
    page.keyboard.press("Escape")
    page.wait_for_selector("[aria-label='Task actions']", state="hidden")
    assert page.evaluate("document.activeElement === document.querySelector('[data-task-menu-button]')")   # and back to the button
    btn.click()
    page.locator("[aria-label='Task actions'] >> text=Review").first.click()
    page.wait_for_selector("[data-badge-status]:has-text('Review')")
    assert repo.get_task(t["id"])["status"] == "review"
    title = page.locator("[data-task-title]")
    title.fill("Beta retitled")
    title.dispatch_event("change")
    page.wait_for_selector("[data-save-tick]:has-text('Saved')")
    assert repo.get_task(t["id"])["title"] == "Beta retitled"
    page.shot("menu-and-saved")


def test_summary_edit_then_a_new_comment_brings_the_ai_text_back_with_restore(new_page, server, seed):
    t = seed["t1"]
    task_summary.refresh_now(t["id"])
    page = _open(new_page("desktop"), server, t)
    page.click("[data-summary-edit]")
    page.fill("#task-summary input[name=what]", "My own words")
    page.click("#task-summary button:has-text('Save my version')")
    page.wait_for_selector("[data-summary-edited]")
    assert page.inner_text("[data-summary-what]").strip() == "My own words"
    import time
    time.sleep(0.05)
    repo.comment_task(t["id"], "News from the agent.", "claude:features")
    task_summary.refresh_now(t["id"])
    page.reload()
    page.wait_for_selector("[data-summary-lines]")
    assert page.inner_text("[data-summary-what]").strip() != "My own words"
    page.click("[data-summary-restore]")
    page.wait_for_selector("[data-summary-edited]")
    assert page.inner_text("[data-summary-what]").strip() == "My own words"


def test_original_request_collapsed_and_editable_and_show_more(new_page, server, seed):
    t = _demo(seed)
    page = _open(new_page("desktop"), server, t)
    assert page.locator("[data-original-request]").get_attribute("open") is None
    page.click("[data-original-request] summary")
    page.click("[data-request-edit]")
    page.fill("[data-task-body]", "Edited request")
    page.dispatch_event("[data-task-body]", "change")
    page.wait_for_selector("[data-save-tick]:has-text('Saved')")
    assert repo.get_task(t["id"])["body"] == "Edited request"
    group = page.locator("[data-tl=ai-group] summary").first
    assert "updates from Claude" in group.inner_text()
    group.click()                                                         # the folded AI row opens
    more = page.locator("[data-show-more]").first
    more.scroll_into_view_if_needed()
    more.click()
    assert more.inner_text().strip() == "Show less"


def test_composer_posts_a_comment_and_scrolls_to_it(new_page, server, seed):
    t = seed["t1"]
    page = _open(new_page("desktop"), server, t)
    page.fill("[data-composer] textarea", "A fresh comment")
    page.keyboard.press("Control+Enter")
    page.wait_for_selector("[data-timeline] :text('A fresh comment')")
    assert page.locator("[data-composer]").count() == 1


@pytest.mark.skipif(not SHOTS_91, reason="set CREWBOSS_91_SHOTS=<dir> to write the demo screenshots")
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_demo_screenshots(new_page, server, seed, device, theme):
    t = _demo(seed)
    page = _open(new_page(device), server, t)
    if theme == "dark":
        page.evaluate("document.documentElement.classList.add('dark')")
    os.makedirs(SHOTS_91, exist_ok=True)
    page.wait_for_timeout(300)
    page.screenshot(path=os.path.join(SHOTS_91, f"{device}-{theme}-opened-on-waiting.png"))
    page.evaluate("document.querySelector('#page, .task-sheet-body') && (document.querySelector('#page').scrollTop = 0); window.scrollTo(0, 0)")
    page.screenshot(path=os.path.join(SHOTS_91, f"{device}-{theme}-top.png"), full_page=True)
    page.click("[data-original-request] summary")
    page.click("[data-tl=ai-group] summary")
    page.screenshot(path=os.path.join(SHOTS_91, f"{device}-{theme}-full-expanded.png"), full_page=True)
