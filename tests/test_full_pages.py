"""#816: every record that opens in a drawer also has a FULL PAGE with its own URL, and the assistant's record links open it in the main area
without touching the assistant. Routes + markup here; the browser run (same node, Back, the three conversation rules) is
tests/test_full_pages_browser.py."""
import re
import subprocess

from app import repo

CA_JS = open("app/static/js/ca.js").read()
NAV_JS = open("app/static/js/pagenav.js").read()


def _task(project, title="Full page task"):
    t = repo.create_task(project["id"], title, "the body", "must", "bug", [], {}, "owner")
    repo.add_subtasks(t["id"], ["first step"], "owner")
    repo.comment_task(t["id"], "a comment", "owner")
    return t


def test_task_full_page_has_the_record_in_two_columns(client, project):
    t = _task(project)
    r = client.get(f"/{project['slug']}/tasks/{t['id']}")
    assert r.status_code == 200
    h = r.text
    assert "Full page task" in h and "data-task-record" in h and "data-swappable" in h
    assert 'id="drawer-content"' in h and 'id="page-inner"' in h
    assert "lg:grid-cols-" in h                                    # body left, thread right on a desktop
    left, right = h.split("data-task-record", 1)[1].split('lg:border-l', 1)
    assert "name=\"title\"" in left and "Comments" in right and "History" in right
    assert 'data-ca-entity="task:%d"' % t["id"] in h


def test_the_task_sheet_gets_the_full_page_layout_and_writes_follow_where_they_were_asked_from(client, project):
    """Owner 11-18: tasks always open full page, in a window over the current page. The sheet's fragment is the full-page layout; a write
    answers with it when asked from the task's URL (which the sheet puts in the address bar)."""
    t = _task(project)
    panel = client.get(f"/{project['slug']}/tasks/{t['id']}/panel").text
    assert "data-task-record" in panel and f'data-full-url="/{project["slug"]}/tasks/{t["id"]}"' in panel
    url = f"/{project['slug']}/tasks/{t['id']}/comments"
    hx = {"HX-Request": "true"}
    page = client.post(url, data={"body": "y"}, headers={**hx, "HX-Current-URL": f"http://testserver/{project['slug']}/tasks/{t['id']}"})
    assert "data-task-record" in page.text


def test_the_task_sheet_is_a_full_window_with_an_exit(client, project):
    h = client.get(f"/{project['slug']}/board").text
    assert "data-task-sheet" in h and "data-task-sheet-exit" in h and 'aria-label="Back to the page you were on"' in h
    assert 'id="drawer-full"' not in h                     # no "open full page" button: it IS the full page


def test_task_page_other_project_redirects_and_missing_is_404(client, project):
    other = repo.create_project("other-proj", "Other", "internal", "system")
    t = _task(project)
    r = client.get(f"/{other['slug']}/tasks/{t['id']}", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == f"/{project['slug']}/tasks/{t['id']}"
    assert client.get(f"/{project['slug']}/tasks/999999").status_code == 404
    r = client.get(f"/all/tasks/{t['id']}/page", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == f"/{project['slug']}/tasks/{t['id']}"
    assert client.get("/all/tasks/999999/page").status_code == 404


def test_knowledge_and_change_full_pages(client, project):
    k = repo.create_knowledge(project["id"], "decision", "A decision", "Why we did it", [], "", False, "owner", summary="short")
    r = client.get(f"/{project['slug']}/knowledge/{k['id']}")
    assert r.status_code == 200 and "A decision" in r.text and "Why we did it" in r.text and "data-swappable" in r.text
    assert client.get(f"/{project['slug']}/knowledge/999999").status_code == 404
    from app import changes
    c = changes.recommend(project["id"], "Ship it", "do the thing", "claude:x", options=["a", "b"])
    r = client.get(f"/{project['slug']}/changes/{c['id']}")
    assert r.status_code == 200 and "Ship it" in r.text and "data-swappable" in r.text
    assert client.get(f"/{project['slug']}/changes/999999").status_code == 404


def test_ca_marks_record_links_and_the_swap_code_exists():
    assert 'data-ca-link' in CA_JS and "window.crewbossNav(" in CA_JS and "renderText(" in CA_JS
    assert "history.pushState" in NAV_JS and 'addEventListener("popstate"' in NAV_JS and '"crewboss:page"' in NAV_JS
    assert "Alpine.initTree" in NAV_JS and "htmx.process" in NAV_JS
    assert "location.assign" in NAV_JS                               # the fallback
    assert "textContent = text" not in CA_JS.split("function renderText")[1].split("msgs.addEventListener")[0]      # never innerHTML / raw HTML from the model
    assert "innerHTML" not in CA_JS


def test_conversation_rules_are_in_the_code():
    # 1. open stays open and keeps its thread across navigation (a swap re-pins; a full load restores the saved conversation and scroll)
    assert 'document.addEventListener("crewboss:page"' in CA_JS and "restoreScroll" in CA_JS and 'addEventListener("pagehide"' in CA_JS
    # 2./3. closing pins the thread to the PLACE it was closed in (owner 11-18: the project, or "global" for Now / Dashboard / Recent and
    #       the other global pages); opening by hand in another place starts fresh
    assert "function here() {" in CA_JS and 'GLOBAL_FIRST[first] ? "global" : "/" + first' in CA_JS
    assert "if (!on && convId && !keepPin" in CA_JS and "pn.h !== here()" in CA_JS


def test_js_is_valid():
    for f in ("ca.js", "pagenav.js"):
        assert subprocess.run(["node", "--check", "app/static/js/" + f], capture_output=True).returncode == 0
    assert re.search(r'js/pagenav.js.*\n.*js/ca.js', open("app/templates/base.html").read())


def test_deciding_a_change_on_its_full_page_answers_with_the_decided_view(client, project):
    from app import changes
    c = changes.recommend(project["id"], "Ship it", "do the thing", "claude:x", options=["a"])
    slug = project["slug"]
    r = client.post(f"/{slug}/changes/{c['id']}/decide", data={"decision": "approved"},
                    headers={"HX-Request": "true", "HX-Current-URL": f"http://testserver/{slug}/changes/{c['id']}"})
    assert r.status_code == 200 and "data-change-decided" in r.text and "approved" in r.text and "Ship it" in r.text
    # from the Changes list it is still the empty fold
    c2 = changes.recommend(project["id"], "Other", "x", "claude:x")
    r = client.post(f"/{slug}/changes/{c2['id']}/decide", data={"decision": "approved"},
                    headers={"HX-Request": "true", "HX-Current-URL": f"http://testserver/{slug}/changes"})
    assert "data-change-decided" not in r.text


def test_page_scripts_rebind_by_event_not_by_reinjection():
    tabs, ro = open("app/static/js/tabs.js").read(), open("app/static/js/runs-on.js").read()
    for js in (tabs, ro):
        assert 'document.addEventListener("crewboss:page", init)' in js
    assert "_tabsBound" in tabs and "_roBound" in ro
    assert "data-rebind" not in NAV_JS and "data-rebind" not in open("app/templates/base.html").read()
    assert "replaceWith(nn)" in NAV_JS and "base !==" not in NAV_JS                    # the menu's active row refreshes on every swap


def test_task_title_survives_a_save_on_the_full_page(client, project):
    t = _task(project)
    panel = client.get(f"/{project['slug']}/tasks/{t['id']}/panel").text
    assert 'data-full-title="Full page task"' in panel and f'data-full-no="{t["id"]}"' in panel
    assert "data-full-title" in NAV_JS and "document.title" in NAV_JS
