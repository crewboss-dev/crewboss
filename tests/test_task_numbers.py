"""Task #197: sessions talk about "#123", so every task card and row shows its number (small, muted, the shared
.task-no), and typing 123 or #123 into the topbar's finder jumps to that task's drawer in whatever project it is in,
with a friendly "No task #N" when there is none."""
import json
import re
from pathlib import Path

import pytest

from app import config, plan, repo, tree

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = json.loads((ROOT / "fixtures/catalog-fixture.json").read_text())
WF = "sales.lead.qualify-from-email-reply"


def _t(project, title, status="backlog", assignee="owner", **kw):
    t = repo.create_task(project["id"], title, "", kw.pop("priority", "should"), "feature", [], kw.pop("links", {}),
                         "owner", assignee=assignee)
    if status != "backlog":
        t = repo.update_task(t["id"], {"status": status}, "owner")
    return t


def no(t) -> str:
    """The number as every surface renders it."""
    return f'data-task-no>#{t["id"]}<'


def _section(html: str, start_marker: str, end_marker: str | None = None) -> str:
    i = html.index(start_marker)
    j = html.index(end_marker, i) if end_marker else len(html)
    return html[i:j]


@pytest.fixture
def cookie(project):
    return {"Cookie": f"current_project={project['slug']}"}


# --- 1. the number on every surface --------------------------------------------------------------------------------

def test_board_card_list_row_and_drawer_show_the_number(client, cookie, project):
    t = _t(project, "Fix the invoice export", status="ready")
    board = client.get("/test-proj/board", headers=cookie).text
    card = _section(board, f'data-task-id="{t["id"]}"')
    assert no(t) in card
    # first on the card's meta line, before the priority word
    meta = card[card.index('mt-2 flex items-center'):]
    assert meta.index(no(t)) < meta.index("Should")
    assert 'class="task-no"' in card
    listed = client.get("/test-proj/board?view=list", headers=cookie).text
    assert no(t) in listed and "Fix the invoice export" in listed
    assert "text-charcoal/30 font-normal" not in listed  # the old too-faint #id is gone
    panel = client.get(f"/test-proj/tasks/{t['id']}/panel", headers=cookie).text
    assert f'data-task-no>Task #{t["id"]}:<' in panel      # the task window's header (#91) prints it as "Task #N:" before the title


def test_board_fragment_refetch_keeps_the_number(client, cookie, project):
    """Live updates re-render the board fragment: the number survives the refetch."""
    t = _t(project, "Refetched card", status="doing")
    frag = client.get("/test-proj/tasks/board?layout=board", headers={**cookie, "HX-Request": "true"}).text
    assert no(t) in frag


def test_focus_shows_the_number_on_review_now_next_done_and_ai_rows(client, cookie, project):
    review = _t(project, "Check the lead flow", status="review")          # the owner's review handoff
    now = _t(project, "Write the release notes", status="doing")
    nxt = _t(project, "Tidy the backlog", status="ready")
    done = _t(project, "Shipped thing", status="done")
    ai = _t(project, "AI builds the importer", status="doing", assignee="ai")
    html = client.get("/test-proj/tasks/board?layout=focus", headers=cookie).text
    assert no(review) in _section(html, 'id="focus-review"', 'id="focus-now"')
    assert no(now) in _section(html, 'id="focus-now"', 'id="focus-next"')
    assert no(nxt) in _section(html, 'id="focus-next"', 'id="focus-done"')
    assert no(done) in _section(html, 'id="focus-done"', "data-ai-waiting")
    assert no(ai) in _section(html, "data-ai-waiting")


def test_home_plan_cards_focus_and_triage_rows_show_the_number(client, project):
    now = _t(project, "Plan now card", status="ready")
    nxt = _t(project, "Plan next card", status="ready", assignee="ai")
    later = _t(project, "Plan later card")
    plan.owner_move(now["id"], "now", None, "owner")
    plan.owner_move(nxt["id"], "next", None, "owner")
    plan.owner_move(later["id"], "later", None, "owner")
    doing = _t(project, "Home focus row", status="doing")
    sug = _t(project, "Needs a triage decision")
    repo.suggest_triage(sug["id"], "claude:features", priority="must", rationale="urgent")
    html = client.get("/").text
    gp = _section(html, 'id="global-plan"', "</section>")
    for t in (now, nxt, later):
        card = _section(gp, f'data-plan-item="{t["id"]}"', "</li>")
        assert no(t) in card
        meta = card[card.index("mt-1 text-micro"):]
        assert meta.index(no(t)) < meta.index("Test Project")  # first on the meta line, before the project
    assert no(doing) in _section(html, 'id="focus-now"', 'id="focus-next"')
    assert no(sug) in _section(html, 'data-panel="triage"', "</details>")
    # the plan's own fragment (what it refetches after a change) too
    assert no(now) in client.get("/all/plan").text


@pytest.fixture
def acct(monkeypatch):
    """dash root -> Acme (account) -> acme-app, acme-site: an account's rolled-up board and focus."""
    monkeypatch.setattr(config, "OVERSEER_PROJECT", "dash-root")
    mk = lambda slug, name: repo.create_project(slug, name, "internal", "owner", short_name=slug.replace("-", ""))  # noqa: E731
    root = mk("dash-root", "Dash")
    a = mk("acme", "Acme")
    tree.set_place(a["id"], "owner", None, kind="account")
    app_, site = mk("acme-app", "Acme App"), mk("acme-site", "Acme Site")
    tree.set_place(app_["id"], "owner", a["id"])
    tree.set_place(site["id"], "owner", a["id"])
    return {"acct": a, "app": app_, "site": site}


def test_account_rollup_board_list_and_focus_show_the_number(client, acct):
    at = _t(acct["app"], "App task", status="ready")
    st = _t(acct["site"], "Site task", status="doing")
    board = client.get("/acme/board").text
    assert no(at) in board and no(st) in board and "data-rollup-note" in board
    listed = client.get("/acme/board?view=list").text
    assert no(at) in listed and no(st) in listed
    focus = client.get("/acme/tasks/board?layout=focus&rollup=acme").text
    assert no(st) in _section(focus, 'id="focus-now"', 'id="focus-next"')
    assert no(at) in _section(focus, 'id="focus-next"', 'id="focus-done"')


def test_workflow_detail_task_rows_show_the_number(client, cookie, project):
    repo.import_catalog(project["slug"], FIXTURE)
    t = _t(project, "Qualify replies better", links={"workflows": [{"id": WF, "step": "classify"}]})
    html = client.get(f"/test-proj/workflows/{WF}", headers=cookie).text
    assert no(t) in html


def test_task_number_style_is_small_muted_and_shared():
    css = (ROOT / "frontend/src/input.css").read_text()
    rule = css[css.index(".task-no {"):]
    rule = rule[:rule.index("}")]
    assert "text-micro" in rule and "tabular-nums" in rule and "whitespace-nowrap" in rule
    assert "text-charcoal/65" in rule and "dark:" not in rule  # muted, still AA at 11px; the token swaps (#479)
    for tpl in ("partials/task_card.html", "partials/focus.html", "partials/global_plan.html",
                "partials/tasklist.html", "pages/home.html", "pages/workflow_detail.html"):
        assert 'class="task-no' in (ROOT / "app/templates" / tpl).read_text(), tpl
    # the task window's header (#91) prints the number large, before the title, in its own style
    assert 'class="tw-no" data-task-no' in (ROOT / "app/templates/partials/task_header.html").read_text()


# --- 2. jump to a number -------------------------------------------------------------------------------------------

def test_task_number_parsing():
    assert [repo.task_number(x) for x in ("123", "#123", " # 45 ", "#0", "0", "12a", "#", "", None, "1234567890")] \
        == [123, 123, 45, None, None, None, None, None, None, None]


def test_jump_opens_the_task_in_its_own_project(client, project):
    other = repo.create_project("other-proj", "Other Project", "internal", "system")
    t = _t(other, "Over in another project", status="ready")
    # the owner is in test-proj; the task lives in other-proj
    for ref in (str(t["id"]), f"%23{t['id']}"):
        r = client.get(f"/all/tasks/{ref}", headers={"Cookie": "current_project=test-proj"}, follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == f"/other-proj/board?open={t['id']}"
    page = client.get(f"/all/tasks/{t['id']}").text  # followed: the board opens that drawer
    assert f'/other-proj/tasks/{t["id"]}/panel' in page


def test_jump_reaches_a_task_in_an_archived_project(client, project):
    gone = repo.create_project("old-proj", "Old Project", "internal", "system")
    t = _t(gone, "Old work")
    repo.set_project_archived(gone["id"], True, "owner")
    r = client.get(f"/all/tasks/{t['id']}", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == f"/old-proj/board?open={t['id']}"
    assert client.get(r.headers["location"]).status_code == 200


def test_jump_to_a_missing_number_is_a_friendly_page(client, project):
    r = client.get("/all/tasks/98765")
    assert r.status_code == 404
    assert "No task #98765" in r.text and "data-task-missing" in r.text and 'href="/"' in r.text
    assert "<title>" in r.text  # a whole page, never a bare error string
    r = client.get("/all/tasks/%3Cb%3Ehi")
    assert r.status_code == 404 and "isn" in r.text and "a task number" in r.text
    assert "<b>hi" not in r.text and "&lt;b&gt;hi" in r.text  # what was typed comes back escaped


def test_peek_says_what_the_number_is(client, project):
    t = _t(project, "Peek at me", status="review")
    r = client.get(f"/all/tasks/{t['id']}/peek")
    assert r.status_code == 200
    assert r.json() == {"found": True, "id": t["id"], "title": "Peek at me", "status": "Review",
                        "project": "Test Project", "archived": False, "url": f"/test-proj/board?open={t['id']}",
                        "panel": f"/test-proj/tasks/{t['id']}/panel"}
    assert client.get(f"/all/tasks/%23{t['id']}/peek").json()["id"] == t["id"]
    r = client.get("/all/tasks/98765/peek")
    assert r.status_code == 404 and r.json() == {"found": False, "id": 98765, "message": "No task #98765"}
    r = client.get("/all/tasks/nope/peek")
    assert r.status_code == 404 and r.json()["found"] is False and r.json()["message"] == "Not a task number"


def test_topbar_finder_offers_the_number_jump_on_every_page(client, cookie, project):
    for path in ("/", "/test-proj", "/test-proj/board", "/test-proj/knowledge"):
        html = client.get(path, headers=cookie).text
        assert "data-jump-input" in html and 'placeholder="Jump to…"' in html, path       # #771: the jump box
    # still no per-page search box on the Board (house rule): the finder lives in the topbar's jump box
    board = client.get("/test-proj/board", headers=cookie).text
    assert 'type="search"' not in board and not re.search(r'<input(?![^>]*type="hidden")[^>]*name="q"', board)


def test_board_q_matches_a_task_number(client, cookie, project):
    """?q= (a link landing a search on the Board) matches a task by its number as well as its words."""
    t = _t(project, "Nothing numeric in this title")
    _t(project, "Some other task")
    for q in (str(t["id"]), f"%23{t['id']}"):
        html = client.get(f"/test-proj/board?view=list&q={q}", headers=cookie).text
        assert "Nothing numeric in this title" in html and "Some other task" not in html, q
    assert [x["id"] for x in repo.list_tasks(project["id"], q=f"#{t['id']}")] == [t["id"]]
    assert repo.list_tasks(project["id"], q="Some other")[0]["title"] == "Some other task"  # words still match
