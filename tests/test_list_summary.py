"""#772: a project's pills (Summary, List, Board, ...), the Summary dashboard and the List table. Owner-mode (sign-in off); scoping with real
people is in tests/test_list_scope.py."""
import re

import pytest

from app import repo, tree
from tests.test_levels_and_accounts import _t, fam  # noqa: F401  (fixture)

PILL = re.compile(r'data-pill="([a-z_]+)"')


def _pills(html):
    return PILL.findall(html)


def _titles(html):
    """Task titles of the List's table rows, in order."""
    table = html.split("data-tl-table", 1)[1].split("</table>", 1)[0]
    return re.findall(r'<a href="[^"]*" data-open class="tl-title">(?:<span[^>]*>.*?</span>)?([^<]+)</a>', table, re.S)


# --- the pills ------------------------------------------------------------------------------------------------------------

def test_project_pills_start_summary_list_board_then_the_rest_in_order(client, fam):
    pills = _pills(client.get("/acme-app/board").text)
    assert pills[:3] == ["summary", "list", "board"]            # the starting layout (#773); the rest are in the "+" catalogue
    assert pills.count("summary") == 1 and pills.count("list") == 1 and "focus" not in pills and "overview" not in pills


def test_an_account_gets_summary_then_list_then_board(client, fam):
    pills = _pills(client.get("/acme").text)
    assert pills[:3] == ["summary", "list", "board"] and "focus" not in pills


def test_the_project_landing_page_is_the_summary_and_focus_redirects_to_it(client, fam):
    page = client.get("/acme-app").text
    assert "data-summary" in page and 'data-landing="summary"' in page
    assert re.search(r'data-pill="summary"[^>]*>', page) and 'aria-current="page" data-pill="summary"' in page
    assert 'href="/acme-app/focus"' not in page                                # Focus is gone from records (#773): Now is that view
    r = client.get("/acme-app/focus", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/acme-app"
    r = client.get("/acme/focus", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/acme"           # an account's landing is its Summary now


# --- Summary --------------------------------------------------------------------------------------------------------------

@pytest.fixture
def work(fam):
    _t(fam["app"], "APP backlog")
    _t(fam["app"], "APP doing", "doing")
    _t(fam["app"], "APP review", "review")
    _t(fam["portal"], "PORTAL ready", "ready")                 # a child of acme-app: counted in its Summary
    _t(fam["site"], "SITE sibling task", "ready")             # a sibling: never
    done = _t(fam["app"], "APP finished", "doing")
    repo.update_task(done["id"], {"status": "done"}, "owner")


def test_summary_is_scoped_to_the_project_and_its_children(client, fam, work):
    html = client.get("/acme-app").text
    by = dict(re.findall(r'<div class="text-sm text-charcoal/60 mt-0.5">([^<]+)</div>', html.split("data-by-status", 1)[1].split("</div>\n\n", 1)[0])
              and [])
    tiles = re.findall(r'tabular[^>]*>(\d+)</div>\s*<div class="text-sm text-charcoal/60 mt-0.5">([^<]+)</div>', html.split("data-by-status", 1)[1][:2500])
    got = {label: int(n) for n, label in tiles}
    assert got == {"Backlog": 1, "Ready": 1, "In progress": 1, "Review": 1}      # APP + PORTAL; SITE (a sibling) and the done one are not
    assert "SITE sibling task" not in html
    site = client.get("/acme-site").text
    site_tiles = {l: int(n) for n, l in re.findall(r'tabular[^>]*>(\d+)</div>\s*<div class="text-sm text-charcoal/60 mt-0.5">([^<]+)</div>', site.split("data-by-status", 1)[1][:2500])}
    assert site_tiles["Ready"] == 1 and site_tiles["Backlog"] == 0 and "APP doing" not in site


def test_summary_shows_what_waits_on_you_jobs_and_activity(client, fam, work):
    t = repo.list_tasks(fam["app"]["id"], q="APP review")[0]
    repo.ask_question(t["id"], "claude:features", "Which colour?", "", None) if hasattr(repo, "ask_question") else None
    html = client.get("/acme-app").text
    assert "data-review" in html and "APP review" in html
    assert 'href="/recent?project=acme-app"' in html and "data-activity" in html
    assert "data-jobs" in html and "No agent jobs yet." in html
    assert "data-flow" in html and html.count("Week of") >= 8


def test_summary_weekly_bars_count_opened_and_closed(client, fam, work):
    from app import snapshot
    ids = sorted(tree.descendant_ids(fam["app"]["id"]))
    s = snapshot.project_summary(ids)
    assert len(s["weeks"]) == snapshot.WEEKS
    assert sum(w["opened"] for w in s["weeks"]) == 5 and sum(w["closed"] for w in s["weeks"]) == 1   # APP x4 + PORTAL; one closed
    assert snapshot.project_summary([])["open"] == 0


# --- List -----------------------------------------------------------------------------------------------------------------

@pytest.fixture
def tasks(fam):
    a = _t(fam["app"], "Charlie task", "doing", priority="must", assignee="ai")
    b = _t(fam["app"], "alpha task", "review", priority="could", assignee="owner")
    c = _t(fam["app"], "Bravo task", "backlog", priority="should", assignee="owner")
    d = _t(fam["app"], "Zulu finished", "doing")
    repo.update_task(d["id"], {"status": "done"}, "owner")
    return {"a": a, "b": b, "c": c, "d": d}


def test_list_renders_one_row_per_task_without_done_by_default(client, fam, tasks):
    r = client.get("/acme-app/list")
    assert r.status_code == 200 and 'aria-current="page" data-pill="list"' in r.text
    assert sorted(_titles(r.text)) == ["Bravo task", "Charlie task", "alpha task"]
    assert "Zulu finished" not in r.text and ">3 of 4<" in r.text.replace(" ", "").replace("\n", "") or "3 of 4" in r.text
    shown = client.get("/acme-app/list?done=1").text
    assert "Zulu finished" in shown and "4 of 4" in shown


def test_list_has_the_short_default_columns_and_the_rest_off(client, fam, tasks):
    html = client.get("/acme-app/list").text
    table_tag = re.search(r'<table class="([^"]*)" data-tl-table', html).group(1)
    for hidden in ("type", "reporter", "progress", "created"):
        assert f"tl-hide-{hidden}" in table_tag
    for shown in ("status", "priority", "assignee", "updated"):
        assert f"tl-hide-{shown}" not in table_tag
    picked = client.get("/acme-app/list?cols=reporter,progress").text
    t2 = re.search(r'<table class="([^"]*)" data-tl-table', picked).group(1)
    assert "tl-hide-reporter" not in t2 and "tl-hide-progress" not in t2 and "tl-hide-status" in t2 and "tl-hide-updated" in t2


def test_list_sorts_by_a_clicked_header(client, fam, tasks):
    assert _titles(client.get("/acme-app/list?sort=title&dir=asc").text) == ["alpha task", "Bravo task", "Charlie task"]
    assert _titles(client.get("/acme-app/list?sort=title&dir=desc").text) == ["Charlie task", "Bravo task", "alpha task"]
    assert _titles(client.get("/acme-app/list?sort=priority").text) == ["Charlie task", "Bravo task", "alpha task"]       # must, should, could
    assert _titles(client.get("/acme-app/list?sort=status&dir=desc").text)[0] == "alpha task"                               # review is furthest along
    html = client.get("/acme-app/list?sort=title&dir=asc").text
    assert 'aria-sort="ascending"' in html and "sort=title&amp;dir=desc" in html      # clicking the active header flips it
    assert _titles(client.get("/acme-app/list?sort=bogus").text)                       # an unknown sort is ignored


def test_list_filters_like_the_board_and_by_text(client, fam, tasks):
    assert sorted(_titles(client.get("/acme-app/list?assignee=owner").text)) == ["Bravo task", "alpha task"]
    assert _titles(client.get("/acme-app/list?assignee=ai").text) == ["Charlie task"]
    assert _titles(client.get("/acme-app/list?q=bravo").text) == ["Bravo task"]
    assert _titles(client.get("/acme-app/list?priority=must").text) == ["Charlie task"]
    assert _titles(client.get("/acme-app/list?status=done").text) == ["Zulu finished"]       # an explicit status filter wins over "done is hidden"
    board = client.get("/acme-app/board?assignee=owner").text                                 # the SAME parameters on the Board
    assert "Bravo task" in board and "alpha task" in board and "Charlie task" not in board
    assert _titles(client.get("/acme-app/tasks/list?assignee=ai").text.replace("data-tl-table", "data-tl-table")) == ["Charlie task"]   # the refresh fragment


def test_list_groups_with_a_count_per_group(client, fam, tasks):
    html = client.get("/acme-app/list?group=status").text
    heads = re.findall(r'data-group-toggle="([a-z]+)".*?data-group-count>(\d+)<', html, re.S)
    assert heads == [("backlog", "1"), ("doing", "1"), ("review", "1")]
    assert re.findall(r'data-group-toggle="([a-z]+)"', client.get("/acme-app/list?group=assignee").text) == ["owner", "ai"]


def test_list_rows_carry_inline_status_priority_colours_and_the_drawer_hook(client, fam, tasks):
    html = client.get("/acme-app/list").text
    t = tasks["a"]
    assert f'data-url="/acme-app/tasks/{t["id"]}/status"' in html and 'data-set="status"' in html
    assert f'hx-get="/acme-app/tasks/{t["id"]}/panel"' in html and "#drawer-content" in html        # the drawer opens in place
    assert "hx-select" not in html.split("data-tl-table", 1)[1]                                        # nothing inherits a selector into the rows
    assert "bg-flame/10 text-flame" in html and "bg-status-warn/15" in html                           # must red, should amber
    assert "bg-indigo/10 text-indigo" in html                                                         # in progress: the accent
    assert 'data-type="feature"' in html
    assert not re.search(r"(?:color|background|fill|stroke)[:=][^;>]*#[0-9a-fA-F]{3,8}\b", html.split("data-tl-table", 1)[1].split("</table>", 1)[0])   # theme tokens, no hex


def test_list_expands_checklist_steps_and_has_a_phone_stack_and_a_footer(client, fam):
    t = repo.create_task(fam["app"]["id"], "With steps", "", "should", "feature", [], {}, "owner", subtasks=["first step", "second step"])
    html = client.get("/acme-app/list").text
    assert f'data-expand="{t["id"]}"' in html and html.count(f'data-steps-of="{t["id"]}"') == 2 and "first step" in html
    assert 'data-tl-stack' in html and "data-create" in html and "data-refresh" in html and "data-count" in html
    assert "+ Create" in html and "Ask CA" in html and "data-ca-open" in html
    plain = client.get("/acme-app/list").text
    assert plain.count("data-expand=") == 1                                                            # only a task with steps has a chevron


def test_list_on_an_account_rolls_up_its_projects(client, fam):
    _t(fam["app"], "In the app")
    _t(fam["site"], "In the site")
    html = client.get("/acme/list").text
    assert "In the app" in html and "In the site" in html and 'data-rollup-note' in html
    assert 'data-url="/acme-site/tasks/' in html and 'name="target"' in html


def test_list_toolbar_and_assets(client, fam, tasks):
    html = client.get("/acme-app/list").text
    for hook in ("data-tl-search", "data-who=\"all\"", "data-who=\"owner\"", "data-who=\"ai\"", "data-filters-button", "data-group-by",
                 "data-saved-filters", "data-cols-open", "data-col-picker", "data-show-done", "data-tl-bulk"):
        assert hook in html, hook
    assert "js/task-list.js" in html and "js/saved-filters.js" in html and "js/task-views.js" in html
    css = open("app/static/css/shell.css").read()
    assert "overflow-x:auto" in css.split(".tl-scroll{", 1)[1].split("}", 1)[0] and "position:sticky" in css


def test_status_pill_changes_the_task_through_the_boards_route(client, fam, tasks):
    t = tasks["c"]
    r = client.post(f"/acme-app/tasks/{t['id']}/status", data={"status": "ready"}, headers={"Origin": "https://crewboss.example.com", "HX-Request": "true"})
    assert r.status_code == 200 and repo.get_task(t["id"])["status"] == "ready"


def test_task_views_js_carries_filters_between_board_and_list_and_never_redirects_a_summary_landing():
    js = open("app/static/js/task-views.js").read()
    assert "data-pill=list" in js and "data-pill=board" in js and "crewboss-task-view" in js and "try {" in js
    assert 'kind === "list" || kind === "board"' in js         # only a task-view landing pill is redirected


# --- bulk and inline priority keep a task's place ----------------------------------------------------------------------------

def _ranks(project_id, priority):
    return [t["id"] for t in sorted(repo.list_tasks(project_id, priority=priority), key=lambda t: t["rank"])]


def test_list_priority_change_goes_to_the_end_of_the_band_and_unchanged_is_left_alone(client, fam):
    a = _t(fam["app"], "a", priority="should")
    b = _t(fam["app"], "b", priority="should")
    c = _t(fam["app"], "c", priority="could")
    d = _t(fam["app"], "d", priority="could")
    H = {"Origin": "https://crewboss.example.com", "HX-Request": "true"}
    before = _ranks(fam["app"]["id"], "should")
    client.post(f"/acme-app/tasks/{a['id']}/priority", data={"priority": "should", "end": "1"}, headers=H)       # unchanged: nothing moves
    assert _ranks(fam["app"]["id"], "should") == before
    client.post(f"/acme-app/tasks/{c['id']}/priority", data={"priority": "should", "end": "1"}, headers=H)       # a real change: the END of the new band
    assert _ranks(fam["app"]["id"], "should") == before + [c["id"]] and repo.get_task(c["id"])["priority"] == "should"
    assert _ranks(fam["app"]["id"], "could") == [d["id"]]
    client.post(f"/acme-app/tasks/{b['id']}/priority", data={"priority": "could", "end": "1"}, headers=H)       # into a band with others: after them
    assert _ranks(fam["app"]["id"], "could") == [d["id"], b["id"]]


def test_board_drag_priority_behaviour_is_unchanged(client, fam):
    a = _t(fam["app"], "a", priority="should")
    b = _t(fam["app"], "b", priority="could")
    H = {"Origin": "https://crewboss.example.com", "HX-Request": "true"}
    client.post(f"/acme-app/tasks/{b['id']}/priority", data={"priority": "should"}, headers=H)                    # no after_id: the TOP of the band, as ever
    assert _ranks(fam["app"]["id"], "should") == [b["id"], a["id"]]


def test_list_rows_carry_their_priority_for_the_bulk_skip(client, fam):
    t = _t(fam["app"], "x", priority="must")
    html = client.get("/acme-app/list").text
    assert f'data-task="{t["id"]}" data-priority="must"' in html
    js = open("app/static/js/task-list.js").read()
    assert 'data-priority") === value) return null' in js and 'o.end = "1"' in js
