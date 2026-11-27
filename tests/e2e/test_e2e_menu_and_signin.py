"""Journeys 1 and 2: sign-in, then the menu (Now, Dashboard, Recent, the project list, favourites, the app switcher, + Create)."""
import pytest


# --- 1. sign-in ----------------------------------------------------------------------------------------------------------

def test_signed_out_is_sent_to_sign_in_and_a_session_gets_the_page(new_page, server, seed, local_signin, sign_in):
    """A signed-out person is redirected to /auth/login (keeping where they were going); a local session (what a passkey sign-in ends with,
    made here the way tests/test_local_auth.py fakes it: the browser's passkey prompt cannot be driven headless) lands on the real page."""
    page = new_page("desktop")
    page.goto(f"{server}/test-proj/board")
    page.wait_for_url("**/auth/login**")
    assert "next=%2Ftest-proj%2Fboard" in page.url
    page.locator("input[name=identifier]").wait_for()
    page.shot("signed-out-sign-in")
    # a page request without a session is refused, not rendered
    assert "Task Alpha" not in page.content()

    sign_in(page)
    page.goto(f"{server}/test-proj/board")
    page.wait_for_selector(".task-card")
    assert "/auth/login" not in page.url
    page.shot("signed-in-board")
    # the sign-in page is only for the signed-out: the failed HTTP 3xx are redirects, not errors; nothing to allow here


# --- 2. the menu ---------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("path,title", [("/", "Now"), ("/dashboard", "Dashboard"), ("/recent", "Recent")])
def test_the_three_fixed_pages_load_and_are_current_in_the_menu(new_page, server, seed, device, path, title):
    page = new_page(device)
    page.goto(server + path)
    page.wait_for_selector("#page-inner")
    assert title in page.title()
    key = {"/": "now", "/dashboard": "dashboard", "/recent": "recent"}[path]
    row = page.locator(f"#nav-fixed a[data-nav='{key}'], #tabbar a.tab[data-key='{key}']").first
    assert row.get_attribute("aria-current") == "page"
    page.shot(f"{key}")


def test_the_menu_lists_the_projects_and_the_star_toggles_a_favourite(new_page, server, seed):
    page = new_page("desktop")
    page.goto(server + "/")
    page.locator("#nav-burger").click()                              # lock the drawer open
    page.wait_for_function("document.getElementById('nav-drawer').getAttribute('aria-hidden') === 'false'")
    names = page.eval_on_selector_all("#nav-sec-accounts a[data-record]", "els => els.map(e => e.dataset.record)")
    assert "test-proj" in names and "other-proj" in names
    assert page.locator("#nav-sec-favourites [data-fav-row]").count() == 0
    star = page.locator("#nav-sec-accounts button.nav-fav[data-fav='test-proj']")
    assert star.get_attribute("aria-pressed") == "false"
    star.click(force=True)                                           # the star shows on hover; the click is what matters
    page.wait_for_selector("#nav-sec-favourites [data-fav-row='test-proj']")
    assert star.get_attribute("aria-pressed") == "true"
    page.shot("favourite-on")
    page.reload()                                                    # it is stored for the person, not just painted
    assert page.locator("#nav-sec-favourites [data-fav-row='test-proj']").count() == 1
    page.locator("#nav-fav-sec button.nav-fav[data-fav='test-proj']").click(force=True)
    page.wait_for_selector("#nav-sec-favourites [data-fav-row='test-proj']", state="detached")
    assert page.locator("#nav-sec-accounts button.nav-fav[data-fav='test-proj']").get_attribute("aria-pressed") == "false"


def test_a_project_in_the_menu_opens(new_page, server, seed):
    page = new_page("desktop")
    page.goto(server + "/")
    page.locator("#nav-burger").click()
    page.locator("#nav-sec-accounts a[data-record='test-proj']").click()
    page.wait_for_url("**/test-proj**")
    page.wait_for_selector("[data-record-title]")
    assert page.inner_text("[data-record-title]").strip() == "Test Project"


def test_the_app_switcher_goes_between_build_assets_and_administration(new_page, server, seed):
    page = new_page("desktop")
    page.goto(server + "/")
    assert page.locator("[data-section-name]").inner_text().strip() == "Build"
    for app, url_part, label in [("assets", "/assets", "Assets"), ("admin", "/admin", "Administration"), ("build", "", "Build")]:
        page.locator("[data-switcher-btn]").click()
        page.wait_for_selector("[data-switcher-menu]", state="visible")
        assert page.locator("[data-switcher-menu] a[data-app]").count() == 3
        page.shot(f"switcher-open-before-{app}")
        with page.expect_navigation():
            page.locator(f"[data-switcher-menu] a[data-app='{app}']").click()
        page.wait_for_selector("[data-section-name]")
        assert page.locator("[data-section-name]").inner_text().strip() == label
        if url_part:
            assert url_part in page.url
    page.shot("switcher-back-in-build")


def test_header_create_makes_a_task_in_the_chosen_project(new_page, server, seed, device):
    from app import repo
    page = new_page(device)
    page.goto(server + "/")
    page.locator("[data-create-open]:visible").first.click()
    dlg = page.locator("[data-create-dialog] form[role=dialog]")
    dlg.wait_for(state="visible")
    page.locator("[data-create-picker]").wait_for(state="visible")     # on a page with no project the picker starts open
    page.locator("[data-create-option='all']", has_text="Other Project").click()
    assert "Other Project" in page.locator("[data-create-project]").inner_text()
    page.locator("[data-create-title]").fill("Made by the e2e suite")
    page.locator("[data-create-body]").fill("from the header")
    page.shot("create-dialog-filled")
    page.locator("[data-create-submit]").click()
    dlg.wait_for(state="hidden")
    tasks = [t for t in repo.list_tasks(seed["b"]["id"]) if t["title"] == "Made by the e2e suite"]
    assert len(tasks) == 1 and tasks[0]["project_id"] == seed["b"]["id"]
    page.goto(f"{server}/other-proj/list")
    page.locator("a:visible", has_text="Made by the e2e suite").first.wait_for()
    page.shot("created-task-in-list")


def test_a_tab_icon_becomes_its_menu_dots_only_when_the_pointer_is_on_the_icon(new_page, server, seed):
    """Owner 11-19: the "..." replaces a project tab's ICON when the mouse is on the icon, not when it is anywhere on the pill (its label)."""
    page = new_page("desktop")
    page.goto(server + "/test-proj")
    tab = page.locator("[data-record-pills] .rtab:has([data-tab-more])").first
    more, ico, label = tab.locator("[data-tab-more]"), tab.locator(".rtab-ico"), tab.locator("[data-tab-label]")
    opacity = lambda: float(more.evaluate("el => getComputedStyle(el).opacity"))
    hidden = lambda: ico.evaluate("el => getComputedStyle(el).visibility") == "hidden"
    label.hover()
    page.wait_for_timeout(150)
    assert opacity() == 0 and not hidden(), "hovering the label must leave the icon showing"
    page.shot("label-hover")
    ico.hover(force=True)
    page.wait_for_timeout(150)
    assert opacity() == 1 and hidden(), "hovering the icon shows the dots in its place"
    page.shot("icon-hover")
    more.click()
    assert page.locator("[data-tab-menu]").is_visible(), "the dots open the tab's menu"
