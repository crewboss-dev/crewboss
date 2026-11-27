"""The three task views (Focus / Board / Plan) and the global home: what each shows (and never shows), the
Sydney-midnight "done today", the owner's WIP limit on the Focus buttons, the completion celebration, archived
projects excluded from every global tile and count, and every row linking into the right project page."""
import re

import pytest

from app import repo
from app.db import get_cursor

OWN = {"Origin": "https://crewboss.example.com", "Sec-Fetch-Site": "same-origin"}
EVIL = {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"}


def _t(project, title, status="ready", assignee="owner", priority="should", stream="feature"):
    row = repo.create_task(project["id"], title, "", priority, stream, [], {}, "claude:features",
                           assignee=assignee, status="ready" if status in ("doing", "done") else status)
    if status in ("doing", "done"):
        row = repo.update_task(row["id"], {"status": status}, "owner")
    return row


def _section(html, heading_id):
    """The HTML of one Focus section (from its heading to the next section)."""
    start = html.index(f'id="{heading_id}"')
    end = html.find("<section", start)
    return html[start:end if end > 0 else len(html)]


@pytest.fixture
def other():
    return repo.create_project("other-proj", "Other Project", "client", "owner")


# --- FOCUS ---------------------------------------------------------------------------------------------------

def test_focus_shows_only_now_review_up_next_done_and_ai(client, project):
    _t(project, "Now one", "doing")
    _t(project, "Check this", "review")
    _t(project, "Ready one", "ready", priority="must")
    _t(project, "Backlog thing", "backlog")
    _t(project, "Finished", "done")
    _t(project, "AI busy", "doing", assignee="ai")
    _t(project, "AI later", "ready", assignee="ai")
    html = client.get("/test-proj/tasks/board?layout=focus").text
    assert "Now one" in _section(html, "focus-now")
    assert "Check this" in _section(html, "focus-review")
    assert "Ready one" in _section(html, "focus-next")
    assert "Finished" in _section(html, "focus-done")
    assert "1</span> task in progress with AI" in html and "AI busy" in html
    assert "Backlog thing" not in html and "AI later" not in html  # the backlog is never on Focus
    assert html.index('id="focus-review"') < html.index('id="focus-now"') < html.index('id="focus-next"')
    # a row opens its task, but not when the click was on its Done / Park / Start button (or a project chip)
    assert html.count("hx-trigger=\"click[!event.target.closest('button, a')]\"") >= 4


def test_review_block_only_when_there_is_something(client, project):
    assert 'id="focus-review"' not in client.get("/test-proj").text


def test_up_next_is_three_ready_by_bucket_then_rank(client, project):
    _t(project, "could a", priority="could")
    _t(project, "should a", priority="should")
    _t(project, "must a", priority="must")
    _t(project, "must b", priority="must")
    _t(project, "wont a", priority="wont")
    up = repo.focus(project["id"])["up_next"]
    assert [t["title"] for t in up] == ["must a", "must b", "should a"]
    moved = repo.reorder_task(up[1]["id"], "must", None, "owner")  # "must b" to the top of Must
    assert [t["title"] for t in repo.focus(project["id"])["up_next"]][0] == moved["title"]


def test_done_today_resets_at_sydney_midnight_into_the_week(project):
    a = _t(project, "today", "done")
    b = _t(project, "earlier this week", "done")
    c = _t(project, "last week", "done")
    with get_cursor() as cur:
        # 1 minute before local midnight today = yesterday in Sydney
        cur.execute("UPDATE tasks SET done_at = (date_trunc('day', now() AT TIME ZONE 'Australia/Sydney') "
                    "AT TIME ZONE 'Australia/Sydney') - interval '1 minute' WHERE id = %s", (b["id"],))
        cur.execute("UPDATE tasks SET done_at = (date_trunc('week', now() AT TIME ZONE 'Australia/Sydney') "
                    "AT TIME ZONE 'Australia/Sydney') - interval '1 minute' WHERE id = %s", (c["id"],))
        cur.execute("SELECT extract(isodow FROM now() AT TIME ZONE 'Australia/Sydney') AS dow")
        monday = cur.fetchone()["dow"] == 1
    f = repo.focus(project["id"])
    assert [t["id"] for t in f["done_today"]] == [a["id"]]
    # "earlier this week" is yesterday; on a Monday yesterday was last week
    assert f["done_week"] == (1 if monday else 2)


def test_start_is_disabled_when_now_is_full_and_refused_anyway(client, project):
    for i in range(3):
        _t(project, f"now {i}", "doing")
    nxt = _t(project, "next")
    html = client.get("/test-proj/tasks/board?layout=focus").text
    nxt_html = _section(html, "focus-next")
    assert "disabled" in nxt_html and "Finish or park one" in nxt_html
    r = client.post(f"/test-proj/tasks/{nxt['id']}/status?layout=focus", data={"status": "doing"},
                    headers={**OWN, "HX-Request": "true"})
    assert r.status_code == 409 and "Finish or park one first" in r.headers["HX-Trigger"]
    assert '"tone": "info"' in r.headers["HX-Trigger"]  # a calm "not now", not an error


def test_start_park_finish_from_focus_and_celebrate(client, project):
    t = _t(project, "Do it")
    h = {**OWN, "HX-Request": "true"}
    r = client.post(f"/test-proj/tasks/{t['id']}/status?layout=focus", data={"status": "doing"}, headers=h)
    assert r.status_code == 200 and 'data-layout="focus"' in r.text and "Do it" in _section(r.text, "focus-now")
    r = client.post(f"/test-proj/tasks/{t['id']}/status?layout=focus", data={"status": "ready"}, headers=h)
    assert "Do it" in _section(r.text, "focus-next")  # parked
    client.post(f"/test-proj/tasks/{t['id']}/status?layout=focus", data={"status": "doing"}, headers=h)
    r = client.post(f"/test-proj/tasks/{t['id']}/status?layout=focus", data={"status": "done"}, headers=h)
    assert "celebrate-count" in r.text and "celebrate-row" in r.text
    assert re.search(r"data-done-today>1<", r.text)
    # a plain re-render doesn't celebrate again
    assert "celebrate" not in client.get("/test-proj/tasks/board?layout=focus").text.split("@keyframes")[0]


def test_focus_counts_go_down_for_todo_and_up_for_done(client, project):
    t = _t(project, "x", "doing")
    before = repo.focus(project["id"])
    repo.update_task(t["id"], {"status": "done"}, "owner")
    after = repo.focus(project["id"])
    assert len(after["now"]) == len(before["now"]) - 1 and len(after["done_today"]) == len(before["done_today"]) + 1


def test_focus_writes_refuse_cross_site(client, project):
    t = _t(project, "x")
    for q in ("layout=focus", "layout=home"):
        r = client.post(f"/test-proj/tasks/{t['id']}/status?{q}", data={"status": "doing"}, headers=EVIL)
        assert r.status_code == 403
    assert repo.get_task(t["id"])["status"] == "ready"


# --- BOARD ---------------------------------------------------------------------------------------------------

def test_board_has_status_columns_sorted_by_bucket_with_wip(client, project):
    _t(project, "b could", "backlog", priority="could")
    _t(project, "b must", "backlog", priority="must")
    _t(project, "mine doing", "doing")
    old = _t(project, "old done", "done")
    _t(project, "new done", "done")
    with get_cursor() as cur:
        cur.execute("UPDATE tasks SET done_at = now() - interval '9 days' WHERE id = %s", (old["id"],))
    html = client.get("/test-proj/board").text
    cols = re.findall(r'data-status-col="(\w+)"', html)
    assert cols == ["backlog", "ready", "doing", "review", "done"]
    backlog = html[html.index('data-status-col="backlog"'):html.index('data-status-col="ready"')]
    assert backlog.index("b must") < backlog.index("b could")
    assert "You 1/3" in html
    assert "new done" in html and "old done" not in html  # Done = this week
    assert "prio-stripe" in html and 'data-sort="0:' in html
    assert "Claude suggests" not in html  # triage banners live on Plan and in the drawer


def test_board_status_drop_moves_the_card(client, project):
    t = _t(project, "Drag me", "backlog", assignee="ai")
    r = client.post(f"/test-proj/tasks/{t['id']}/status?layout=board", data={"status": "ready"},
                    headers={**OWN, "HX-Request": "true"})
    assert r.status_code == 200 and 'data-layout="board"' in r.text
    ready = r.text[r.text.index('data-status-col="ready"'):r.text.index('data-status-col="doing"')]
    assert "Drag me" in ready


# --- the Board is the only task board (Plan retired 2026-10-01) ------------------------------------------------

def test_plan_redirects_to_the_board(client, project):
    r = client.get("/test-proj/plan?open=5&stream=bug", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/test-proj/board?open=5&stream=bug"
    html = client.get("/test-proj/board").text
    assert 'href="/test-proj/plan"' not in html and "data-priority-col" not in html


def _col(html, status):
    start = html.index(f'data-status-col="{status}"')
    nxt = html.find("data-status-col=", start + 10)
    return html[start:nxt if nxt != -1 else len(html)]


def test_board_drag_ranks_inside_the_priority_band(client, project):
    a = _t(project, "first", "backlog", assignee="ai", priority="must")
    b = _t(project, "second", "backlog", assignee="ai", priority="must")
    c = _t(project, "mover", "backlog", assignee="ai", priority="must")
    # same column: the drop line between first and second -> after_id=first (priority stays Must)
    r = client.post(f"/test-proj/tasks/{c['id']}/priority?layout=board", data={"priority": "must", "after_id": str(a["id"])},
                    headers={**OWN, "HX-Request": "true"})
    col = _col(r.text, "backlog")
    assert col.index("first") < col.index("mover") < col.index("second")
    # another column, at the top of its band: status + top=1
    d = _t(project, "ready one", "ready", assignee="ai", priority="must")
    r = client.post(f"/test-proj/tasks/{b['id']}/status?layout=board", data={"status": "ready", "top": "1"},
                    headers={**OWN, "HX-Request": "true"})
    col = _col(r.text, "ready")
    assert col.index("second") < col.index("ready one")
    assert repo.get_task(b["id"])["status"] == "ready" and repo.get_task(b["id"])["priority"] == "must"


def test_board_priority_filter(client, project):
    _t(project, "a must", "backlog", assignee="ai", priority="must")
    _t(project, "a could", "backlog", assignee="ai", priority="could")
    html = client.get("/test-proj/board?priority=must").text
    main = html.split("<main", 1)[1]
    assert "a must" in main and "a could" not in main
    assert 'aria-label="Remove filter Must have"' in main


def test_drop_indicator_is_wired():
    from pathlib import Path

    js = (Path(__file__).resolve().parent.parent / "app/static/js/app.js").read_text()
    for needle in ('"drop-line"', "insertionPoint", "bandCardAbove", "after_id", "statusCol"):
        assert needle in js, needle
    css = (Path(__file__).resolve().parent.parent / "frontend/src/input.css").read_text()
    assert ".drop-line" in css


# --- filters: common visible, the rest behind one button; no search box ------------------------------------------

@pytest.mark.parametrize("path", ["/test-proj/board"])
def test_filters_are_one_button_and_no_search_box(client, project, path):
    html = client.get(path + "?stream=bug&category=rule").text
    main = html.split("<main", 1)[1]
    assert "data-filters-button" in main and 'type="search"' not in main and 'name="q"' not in main
    assert 'aria-label="Remove filter Bug"' in main and 'aria-label="Remove filter Rule"' in main
    assert re.search(r'Filters <span class="chip[^"]*">2</span>', main)


def test_unknown_layout_or_filter_values_fall_back(client, project):
    assert client.get("/test-proj/tasks/board?layout=../../etc&stream=<x>").status_code == 200


# --- GLOBAL HOME ----------------------------------------------------------------------------------------------

@pytest.fixture
def estate(project, other):
    archived = repo.create_project("gone-proj", "Gone Project", "internal", "owner")
    _t(project, "P now", "doing")
    _t(other, "O review", "review")
    _t(other, "O next", "ready", priority="must")
    _t(project, "AI thing", "doing", assignee="ai")
    s = _t(other, "Suggested", "backlog", assignee="ai")
    repo.suggest_triage(s["id"], "claude:features", priority="must", rationale="r")
    # everything in the archived project, which must appear NOWHERE on the global home
    _t(archived, "ARCHIVED now", "doing")
    _t(archived, "ARCHIVED review", "review")
    _t(archived, "ARCHIVED ready", "ready")
    _t(archived, "ARCHIVED ai", "doing", assignee="ai")
    g = _t(archived, "ARCHIVED suggested", "backlog", assignee="ai")
    repo.suggest_triage(g["id"], "claude:features", priority="must", rationale="r")
    repo.set_project_archived(archived["id"], True, "owner")
    return {"archived": archived}


def test_global_home_excludes_archived_from_every_tile_and_count(client, estate):
    html = client.get("/").text
    main = html.split("<main", 1)[1]
    assert "ARCHIVED" not in main and "Gone Project" not in main and "gone-proj" not in main
    o = repo.global_overview()
    assert {r["project"]["slug"] for r in o["projects"]} == {"test-proj", "other-proj"}
    assert o["totals"]["triage"] == 1 and o["ai_work_count"] == 1
    f = repo.focus(None)
    assert [t["title"] for t in f["now"]] == ["P now"] and f["wip"] == 1
    assert [t["title"] for t in f["review"]] == ["O review"]
    assert [t["title"] for t in f["up_next"]] == ["O next"]
    assert [t["title"] for t in f["ai_doing"]] == ["AI thing"]


def test_global_home_rows_click_into_the_right_project(client, estate):
    html = client.get("/").text
    p_now = next(t for t in repo.focus(None)["now"] if t["title"] == "P now")
    o_rev = next(t for t in repo.focus(None)["review"])
    assert f'hx-get="/test-proj/tasks/{p_now["id"]}/panel"' in html
    assert f'hx-post="/other-proj/tasks/{o_rev["id"]}/status?layout=home"' in html
    assert "data-project-tree" not in html       # the project tree cards are the Projects list now (#733); Now is what needs you
    sug = next(t for t in repo.global_overview()["triage"])
    assert f'href="/other-proj/board?open={sug["id"]}"' in html
    ai = repo.focus(None)["ai_doing"][0]
    assert f'href="/test-proj/board?open={ai["id"]}"' in html  # the Sessions panel, per session


def test_global_home_panels_collapsed_by_default(client, estate):
    html = client.get("/").text
    for panel in ("triage", "sessions", "workflows", "health", "activity"):
        m = re.search(rf'<details class="[^"]*" data-panel="{panel}"([^>]*)>', html)
        assert m and "open" not in m.group(1), panel
    assert re.search(r'<details class="card group" data-ai-waiting>', html)


def test_global_focus_partial_and_actions(client, estate):
    r = client.get("/all/focus")
    assert r.status_code == 200 and 'data-layout="home"' in r.text and "ARCHIVED" not in r.text
    nxt = repo.focus(None)["up_next"][0]
    r = client.post(f"/other-proj/tasks/{nxt['id']}/status?layout=home", data={"status": "doing"},
                    headers={**OWN, "HX-Request": "true"})
    assert r.status_code == 200 and 'data-layout="home"' in r.text
    assert nxt["title"] in _section(r.text, "focus-now") and "P now" in _section(r.text, "focus-now")


def test_project_cards_show_open_counts(client, estate):
    row = {r["project"]["slug"]: r for r in repo.global_overview()["projects"]}
    assert row["other-proj"]["review"] == 1 and row["other-proj"]["must"] == 1 and row["other-proj"]["open"] == 3


# --- small drift fixes ------------------------------------------------------------------------------------------

def test_workflows_list_uses_the_filters_button_not_a_search_box(client, project):
    import json
    from pathlib import Path

    repo.import_catalog(project["slug"], json.loads((Path(__file__).resolve().parent.parent / "fixtures/catalog-fixture.json").read_text()))
    html = client.get("/test-proj/workflows?group=Sales").text
    main = html.split("<main", 1)[1]
    assert "data-filters-button" in main and 'type="search"' not in main and 'name="q"' not in main
    assert 'aria-label="Remove filter Sales"' in main and 'href="/test-proj/workflows?ai_only=1&amp;group=Sales"' not in main
    assert 'href="/test-proj/workflows?group=Sales&amp;ai_only=1"' in main


def test_list_view_refreshes_itself(client, project):
    html = client.get("/test-proj/board?view=list").text
    assert 'hx-get="/test-proj/tasks/board?layout=board&amp;view=list"' in html and 'mdd:board-stale from:body' in html
    frag = client.get("/test-proj/tasks/board?layout=board&view=list").text
    assert "<table" in frag and 'id="board"' in frag


def test_amber_text_is_never_amber_on_light():
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for f in [*root.joinpath("app/templates").rglob("*.html"), root / "app/config.py", root / "frontend/src/input.css"]:
        for m in re.finditer(r"(\S*)text-status-warn\b", f.read_text()):
            assert m.group(1).endswith("dark:"), f"{f.name}: light-mode amber text ({m.group(0)})"
