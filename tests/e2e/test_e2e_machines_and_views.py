"""Journeys 5 and 6: the Machines page (Runners | Servers tabs) and a project's Summary / List / Board pills with saved filters."""
import re

import pytest
from playwright.sync_api import expect


# --- 5. machines -------------------------------------------------------------------------------------------------------------

def _tab(page):
    return page.evaluate("document.querySelector('[data-machines-page]').dataset.tab")


def test_machines_has_runners_and_servers_tabs_and_the_tab_survives_a_refresh(new_page, server, seed, device, sign_in):
    page = new_page(device)
    sign_in(page)
    page.goto(f"{server}/test-proj/machines")
    page.wait_for_selector("[data-machine-tabs]")
    tabs = page.eval_on_selector_all("[data-machine-tabs] [role=tab]", "els => els.map(e => e.dataset.tabLink)")
    assert tabs == ["runners", "servers"]
    assert _tab(page) == "runners"                                         # the default
    page.shot("runners-tab")
    page.locator("[data-machine-tabs] [data-tab-link=servers]").click()
    page.wait_for_url("**/machines?tab=servers")
    assert _tab(page) == "servers"
    assert page.locator("[data-tab-link=servers]").get_attribute("aria-selected") == "true"
    page.reload()                                                          # ?tab= persists on refresh
    page.wait_for_selector("[data-machine-tabs]")
    assert page.url.endswith("/machines?tab=servers") and _tab(page) == "servers"
    page.shot("servers-tab-after-refresh")
    page.locator("[data-machine-tabs] [data-tab-link=runners]").click()
    page.wait_for_url("**/machines?tab=runners")
    assert _tab(page) == "runners"


@pytest.mark.parametrize("entry,tab", [("/runners", "runners"), ("/servers", "servers")])
def test_runners_and_servers_entry_points_land_on_the_right_tab(new_page, server, seed, entry, tab, sign_in):
    page = new_page("desktop")
    sign_in(page)
    page.goto(server + entry)
    page.wait_for_selector("[data-machines-page]")
    assert re.search(r"/(test|other)-proj/machines", page.url), page.url
    assert _tab(page) == tab, f"{entry} landed on the {_tab(page)} tab (url {page.url})"
    page.shot(f"entry{entry.replace('/', '-')}")


def test_the_deploy_flow_diagram_shows_and_the_two_add_actions_are_separate(new_page, server, seed, device, sign_in):
    page = new_page(device)
    sign_in(page)
    page.goto(f"{server}/test-proj/machines?tab=runners")
    page.wait_for_selector("[data-machines-page]")
    assert page.locator(".df").is_visible()                                 # the picture of how a change reaches a server
    assert page.locator(".df-server").count() >= 1 and page.locator(".df-node").count() >= 3
    page.shot("deploy-flow-runners")
    # the Runners tab offers "Add a runner" and NOT "Add a server"; the Servers tab the reverse: two separate actions, never two steps of one
    assert page.locator("[data-add-btn=runner]").count() == 1 and page.locator("[data-add-btn=server]").count() == 0
    page.goto(f"{server}/test-proj/machines?tab=servers")
    page.wait_for_selector("[data-machines-page]")
    assert page.locator("[data-add-btn=server]").count() == 1 and page.locator("[data-add-btn=runner]").count() == 0
    assert page.locator(".df").is_visible()
    page.shot("deploy-flow-servers")


# --- 6. project pills and saved filters ---------------------------------------------------------------------------------------

def test_a_project_has_summary_list_and_board_pills_and_each_opens(new_page, server, seed, device):
    page = new_page(device)
    page.goto(f"{server}/test-proj")
    page.wait_for_selector("[data-record-pills]")
    keys = page.eval_on_selector_all("[data-record-pills] [data-pill]", "els => els.map(e => e.dataset.pill)")
    assert keys[:3] == ["summary", "list", "board"], keys
    assert page.locator("[data-pill=summary]").get_attribute("aria-current") == "page"
    page.shot("summary")
    page.locator("[data-pill=list]").click()
    page.wait_for_url("**/test-proj/list")
    assert page.locator("[data-pill=list]").get_attribute("aria-current") == "page"
    page.wait_for_selector("a.tl-title:visible")
    page.shot("list")
    page.locator("[data-pill=board]").click()
    page.wait_for_url("**/test-proj/board")
    assert page.locator("[data-pill=board]").get_attribute("aria-current") == "page"
    page.wait_for_selector(".task-card")
    page.shot("board")
    page.locator("[data-pill=summary]").click()
    page.wait_for_url(re.compile(r".*/test-proj/?(summary)?$"))
    assert page.locator("[data-pill=summary]").get_attribute("aria-current") == "page"


def test_switching_between_list_and_board_keeps_the_filter(new_page, server, seed, device):
    page = new_page(device)
    page.goto(f"{server}/test-proj/list?priority=must")
    page.wait_for_selector("[data-record-pills]")
    page.locator("[data-pill=board]").click()
    page.wait_for_url("**/test-proj/board?priority=must")
    page.wait_for_selector(".task-card")
    assert page.locator(".task-card", has_text="Task Alpha").count() == 1      # must
    assert page.locator(".task-card", has_text="Task Beta").count() == 0       # should: filtered out


@pytest.mark.parametrize("view", ["list", "board"])
def test_a_saved_filter_is_saved_listed_applied_and_deleted(new_page, server, seed, device, view):
    page = new_page(device)
    dialogs = []

    def on_dialog(d):
        dialogs.append((d.type, d.message))
        d.accept("Must haves" if d.type == "prompt" else None)
    page.on("dialog", on_dialog)
    page.goto(f"{server}/test-proj/{view}?priority=must")
    page.wait_for_selector("[data-saved-filters]")
    box = page.locator("[data-saved-filters]:visible").first
    box.locator("[data-sf-open]").click()
    box.locator("[data-sf-menu]").wait_for(state="visible")
    expect(box.locator("[data-sf-empty]")).to_be_visible()
    box.locator("[data-sf-save]").click()
    box.locator("[data-sf-apply]", has_text="Must haves").wait_for()
    page.shot(f"{view}-saved")
    page.goto(f"{server}/test-proj/{view}")                                   # a bare page, then apply the saved set
    page.locator("[data-saved-filters]:visible [data-sf-open]").click()
    page.locator("[data-sf-apply]", has_text="Must haves").click()
    page.wait_for_url(re.compile(rf".*/test-proj/{view}\?(.*&)?priority=must"))        # (the List also saves its columns)
    # delete it again (the confirm dialog is accepted above)
    page.locator("[data-saved-filters]:visible [data-sf-open]").click()
    page.locator("li.tl-sf-row:has([data-sf-apply])").first.locator("[data-sf-delete]").click()
    expect(page.locator("[data-saved-filters]:visible [data-sf-empty]")).to_be_visible()
    assert [d[0] for d in dialogs] == ["prompt", "confirm"]
    page.shot(f"{view}-saved-deleted")
