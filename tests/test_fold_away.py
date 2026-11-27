"""Task #582: an action that removes or moves an item updates IN PLACE (app/fold.py, static/js/fold.js, css/fold.css).

Server half, per action: an htmx request (HX-Request) gets an EMPTY 200 (or just the rows that land in another list) plus
an HX-Trigger toast; a plain form post still gets the 303 + flash cookie. The same-origin guard and owner-only checks are
exactly what they were. Plus the markup contract the JS relies on, and the JS's pure logic run under node."""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app import changes, fold, repo, server_access
from app.config import HEALTH_PROJECT  # noqa: F401  (the health route itself is covered in test_erp_health.py)
from test_server_access import est  # noqa: F401  (fixture: two accounts + a throwaway SSH CA)

ROOT = Path(__file__).resolve().parent.parent
OWN = {"Origin": "https://crewboss.example.com", "Sec-Fetch-Site": "same-origin"}
HX = {**OWN, "HX-Request": "true"}


# --- app/fold.py ---------------------------------------------------------------------------------------------------------

def test_fold_response_is_empty_with_a_toast_and_carries_insert_blocks():
    r = fold.fold_response("Change #4 approved.")
    assert r.status_code == 200 and r.body == b""
    assert json.loads(r.headers["HX-Trigger"]) == {"mdd:flash": {"message": "Change #4 approved.", "tone": "info"}}
    block = fold.insert_block("#changes-decided-list", "<div>row</div>", "afterbegin")
    assert block == '<template data-fold-into="#changes-decided-list" data-fold-pos="afterbegin"><div>row</div></template>'
    assert fold.fold_response(inserts=[block]).body.decode() == block
    assert "HX-Trigger" not in fold.fold_response().headers                      # no message, no toast
    with pytest.raises(ValueError):
        fold.insert_block('#x"><script>', "<b/>")                                  # a selector can't carry markup
    with pytest.raises(ValueError):
        fold.insert_block("#x", "<b/>", "inside")


def test_toast_header_stays_latin1_safe_for_any_message():
    r = fold.fold_response("Removed “web” — done")
    r.headers["HX-Trigger"].encode("latin-1")                                       # json.dumps escapes non-ASCII


# --- the Change log: Focus, and the Changes page ------------------------------------------------------------------------

def _change(project, title="Add a thing"):
    return changes.recommend(project["id"], title, "Because", "claude:features")


def test_decide_from_focus_folds_the_card_away_with_nothing_to_insert(client, project):
    c = _change(project)
    page = client.get(f"/{project['slug']}/tasks/board?layout=focus").text
    # the card is a fold item, its form posts via htmx at itself, and the Focus section is a fold section
    assert f'data-fold="chg:{c["id"]}"' in page and 'hx-target="closest [data-fold]"' in page
    assert "data-focus-changes" in page and "data-fold-hide-empty" in page and "data-fold-root" in page
    r = client.post(f"/{project['slug']}/changes/{c['id']}/decide", data={"decision": "approved"},
                    headers={**HX, "HX-Current-URL": f"https://crewboss.example.com/{project['slug']}"})
    assert r.status_code == 200 and r.text == ""                                  # no row to insert: it only leaves
    assert f"Change #{c['id']} approved." in r.headers["HX-Trigger"]
    assert changes.get(c["id"])["decision"] == "approved"


def test_decide_on_the_changes_page_moves_the_row_to_its_next_list(client, project):
    approved, rejected = _change(project, "Approve me"), _change(project, "Reject me")
    page = client.get(f"/{project['slug']}/changes").text
    for needle in ('id="changes-decided-list"', 'id="changes-done-list"', "data-fold-placeholder", "data-fold-count"):
        assert needle in page
    here = {"HX-Current-URL": f"https://crewboss.example.com/{project['slug']}/changes"}
    r = client.post(f"/{project['slug']}/changes/{approved['id']}/decide", data={"decision": "approved"},
                    headers={**HX, **here})
    assert r.status_code == 200 and "mdd:flash" in r.headers["HX-Trigger"]
    assert r.text.startswith('<template data-fold-into="#changes-decided-list" data-fold-pos="afterbegin">')
    assert f'data-fold="dec:{approved["id"]}"' in r.text and "Approve me" in r.text and "approved" in r.text
    r = client.post(f"/{project['slug']}/changes/{rejected['id']}/decide", data={"decision": "rejected"},
                    headers={**HX, **here})
    assert r.text.startswith('<template data-fold-into="#changes-done-list"')    # a rejection closes: it lands in Done
    assert f'data-fold="done:{rejected["id"]}"' in r.text and "rejected" in r.text


def test_decide_without_htmx_is_the_same_303_with_a_flash(client, project):
    c = _change(project)
    r = client.post(f"/{project['slug']}/changes/{c['id']}/decide", data={"decision": "rejected"}, headers=OWN,
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/{project['slug']}/changes"
    assert f"Change #{c['id']} rejected." in r.headers["set-cookie"] and "HX-Trigger" not in r.headers


def test_decide_guards_are_unchanged(client, project):
    c = _change(project)
    url = f"/{project['slug']}/changes/{c['id']}/decide"
    r = client.post(url, data={"decision": "approved"}, headers={"Origin": "https://evil.example", "HX-Request": "true"})
    assert r.status_code == 403 and changes.get(c["id"])["stage"] == "recommended"   # the same-origin guard
    other = repo.create_project("other-p", "Other", "internal", "owner", short_name="otherp")
    c2 = changes.recommend(other["id"], "x", "", "claude:all")
    r = client.post(f"/{project['slug']}/changes/{c2['id']}/decide", data={"decision": "approved"}, headers=HX)
    assert r.status_code == 404 and changes.get(c2["id"])["stage"] == "recommended"  # another project's change: refused
    r = client.post(url, data={"decision": "changed"}, headers=HX)                   # a note is required: a toast, no fold
    assert r.status_code == 400 and "mdd:flash" in r.headers["HX-Trigger"] and r.headers["HX-Reswap"] == "none"
    r = client.post(url, data={"decision": "approved"}, headers=HX)
    r = client.post(url, data={"decision": "approved"}, headers=HX)                  # decided twice: calm toast, no swap
    assert r.status_code == 409 and r.headers["HX-Reswap"] == "none" and '"tone": "info"' in r.headers["HX-Trigger"]


def test_decided_in_chat_buttons_fold_in_place_and_post_plain_without_htmx(client, project):
    """#581 buttons (Acknowledge/Confirm/Override): htmx = empty 200 + toast; plain = the 303 as before."""
    from app import mcp_server
    from app.auth import issue_token, resolve_session_token
    mcp_server.__dict__  # noqa: B018
    c1, c2 = _change(project, "Chat one"), _change(project, "Chat two")
    for c in (c1, c2):
        changes.decide(c["id"], "approved", "owner")                   # stage decided
    with __import__("app.db", fromlist=["get_cursor"]).get_cursor() as cur:
        cur.execute("UPDATE change_log SET decided_via = 'session', decided_by = 'claude:all', owner_words = 'yes', "
                    "words_verified = true WHERE id = ANY(%s)", ([c1["id"], c2["id"]],))
    focus = client.get(f"/{project['slug']}/tasks/board?layout=focus").text
    assert "data-focus-chat-decisions" in focus and f'data-fold="chat:{c1["id"]}"' in focus
    assert f'hx-post="/{project["slug"]}/changes/{c1["id"]}/acknowledge"' in focus
    page = client.get(f"/{project['slug']}/changes").text
    assert f'data-fold="cd:{c1["id"]}"' in page                          # on the Changes page only the buttons fold
    r = client.post(f"/{project['slug']}/changes/{c1['id']}/acknowledge", headers=HX)
    assert r.status_code == 200 and r.text == "" and "acknowledged" in r.headers["HX-Trigger"]
    assert changes.get(c1["id"])["acknowledged_at"] is not None
    r = client.post(f"/{project['slug']}/changes/{c2['id']}/acknowledge", headers=OWN, follow_redirects=False)
    assert r.status_code == 303 and "acknowledged" in r.headers["set-cookie"]
    r = client.post(f"/{project['slug']}/changes/{c1['id']}/acknowledge",
                    headers={"Origin": "https://evil.example", "HX-Request": "true"})
    assert r.status_code == 403


def _chat_decided(change_id, decision="approved"):
    from app.db import get_cursor
    changes.decide(change_id, decision, "owner")
    with get_cursor() as cur:
        cur.execute("UPDATE change_log SET decided_via = 'session', decided_by = 'claude:all', owner_words = 'yes', "
                    "words_verified = true WHERE id = %s", (change_id,))


def test_override_that_changes_the_call_replaces_the_row_on_the_changes_page(client, project):
    c = _change(project, "Chat override")
    _chat_decided(c["id"], "approved")
    here = {"HX-Current-URL": f"https://crewboss.example.com/{project['slug']}/changes"}
    # the same call again: only the buttons fold, no row is replaced
    r = client.post(f"/{project['slug']}/changes/{c['id']}/decide", data={"decision": "approved"}, headers={**HX, **here})
    assert r.status_code == 200 and r.text == ""
    c2 = _change(project, "Chat override two")
    _chat_decided(c2["id"], "approved")
    # approve -> reject: the Decided row folds away and the re-rendered row (new chip) folds into Done
    r = client.post(f"/{project['slug']}/changes/{c2['id']}/decide", data={"decision": "rejected"}, headers={**HX, **here})
    assert r.status_code == 200
    assert r.text.startswith('<template data-fold-into="#changes-done-list" data-fold-pos="afterbegin">')
    assert f'data-fold="done:{c2["id"]}"' in r.text and "rejected" in r.text
    assert f'data-fold-remove="[data-fold=&quot;dec:{c2["id"]}&quot;]"' in r.text
    assert f'data-fold-remove="[data-fold=&quot;done:{c2["id"]}&quot;]"' in r.text
    # approve -> "changed" (with a note): the row is replaced in Decided
    c3 = _change(project, "Chat override three")
    _chat_decided(c3["id"], "approved")
    r = client.post(f"/{project['slug']}/changes/{c3['id']}/decide", data={"decision": "changed", "note": "use green"},
                    headers={**HX, **here})
    assert r.text.startswith('<template data-fold-into="#changes-decided-list"') and "use green" in r.text
    assert f'data-fold-remove="[data-fold=&quot;dec:{c3["id"]}&quot;]"' in r.text


def test_buttons_are_disabled_together_while_a_decision_is_in_flight(client, project):
    _change(project)
    page = client.get(f"/{project['slug']}/changes").text
    assert 'hx-disabled-elt="find fieldset"' in page and "<fieldset" in page and "find button" not in page
    d = _change(project, "Chat card")
    _chat_decided(d["id"], "approved")
    page = client.get(f"/{project['slug']}/changes").text
    assert page.count('hx-disabled-elt="find fieldset"') == 2 and "find button" not in page


# --- servers ---------------------------------------------------------------------------------------------------------------

def test_server_remove_folds_away_for_htmx_and_still_redirects_without_it(client, est):  # noqa: F811
    acct = est["acme"]
    row = server_access.add(acct, "web03", "203.0.113.11", "22", "mallen", "owner", "")
    html = client.get("/acme/servers").text
    assert f'data-fold="srv:{row["id"]}"' in html and "hx-confirm=" in html and "data-fold-section" in html
    r = client.post(f"/acme/servers/{row['id']}/remove", headers={"Origin": "https://evil.example", "HX-Request": "true"})
    assert r.status_code == 403 and server_access.list_for([acct["id"]])           # the same-origin guard is unchanged
    r = client.post(f"/acme/servers/{row['id']}/remove", headers=HX, follow_redirects=False)
    assert r.status_code == 200 and r.text == ""
    assert "authorized_keys" in r.headers["HX-Trigger"] and "mdd:flash" in r.headers["HX-Trigger"]
    assert server_access.list_for([acct["id"]]) == []
    other = server_access.add(acct, "web04", "203.0.113.12", "22", "mallen", "owner", "")
    r = client.post(f"/acme/servers/{other['id']}/remove", headers=OWN, follow_redirects=False)
    assert r.status_code == 303 and "authorized_keys" in r.headers.get("set-cookie", "")


# --- the markup contract: every re-rendered view is a fold root, every row a keyed item ----------------------------------

def test_task_views_are_fold_roots_with_keyed_items(client, project):
    t = repo.create_task(project["id"], "Fold me", "", "should", "feature", [], {}, "owner", assignee="owner")
    repo.update_task(t["id"], {"status": "doing"}, "owner")
    focus = client.get(f"/{project['slug']}/tasks/board?layout=focus").text
    assert 'data-fold-root' in focus and f'data-fold="now:{t["id"]}"' in focus
    board = client.get(f"/{project['slug']}/board").text
    assert 'data-fold-root' in board and f'data-fold="doing:{t["id"]}"' in board
    listing = client.get(f"/{project['slug']}/board?view=list").text
    assert 'data-fold-root' in listing and f'data-fold="row:{t["id"]}"' in listing
    drawer = client.get(f"/{project['slug']}/tasks/{t['id']}/panel", headers=HX).text
    repo.add_subtasks(t["id"], ["one"], "owner")
    drawer = client.get(f"/{project['slug']}/tasks/{t['id']}/panel", headers=HX).text
    assert 'id="task-checklist"' in drawer and "data-fold-root" in drawer and 'data-fold="sub:' in drawer


def test_the_shell_loads_the_fold_assets(client):
    html = client.get("/").text
    assert re.search(r'/static/css/fold\.css\?v=\w+', html) and re.search(r'/static/js/fold\.js\?v=\w+', html)
    assert client.get("/static/js/fold.js").status_code == 200 and client.get("/static/css/fold.css").status_code == 200
    css = (ROOT / "app/static/css/fold.css").read_text()
    assert "prefers-reduced-motion" in css and "transform" not in css                  # height + opacity only, none if reduced


def test_plain_board_actions_still_answer_a_whole_fragment_for_htmx(client, project):
    """Status / assign / tick answer the full view (the JS reconciles it by key); nothing here becomes an empty body."""
    t = repo.create_task(project["id"], "Move me", "", "should", "feature", [], {}, "owner", assignee="owner")
    r = client.post(f"/{project['slug']}/tasks/{t['id']}/status?layout=focus", data={"status": "doing"}, headers=HX)
    assert r.status_code == 200 and 'id="board"' in r.text and f'data-fold="now:{t["id"]}"' in r.text
    r = client.post(f"/{project['slug']}/tasks/{t['id']}/status?layout=focus", data={"status": "done"}, headers=HX)
    assert f'data-fold="done:{t["id"]}"' in r.text and f'data-fold="now:{t["id"]}"' not in r.text   # moved: left Now, joined Done
    assert repo.get_task(t["id"])["status"] == "done"


# --- fold.js's pure logic, under node ------------------------------------------------------------------------------------

@pytest.mark.skipif(not shutil.which("node"), reason="needs node")
def test_fold_js_logic():
    out = subprocess.run([shutil.which("node"), "--test", str(ROOT / "tests/fold.test.mjs")], capture_output=True,
                         text=True, cwd=ROOT)
    assert out.returncode == 0, out.stdout + out.stderr
