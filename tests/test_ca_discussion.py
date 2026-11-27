"""A task's Discussion (task #830): the conversations bound to a task belong to the task. Everyone who can VIEW the task reads them,
only people who can EDIT it post or start one (a view-only person gets a 403), a person without the task gets a 404, and a turn
always runs as the person who typed, on THEIR runner. A global conversation stays its starter's own, but when one of its tools changes
a task it is linked into that task's history. Conversations follow a task to another project, and their access with it. A client
token never reaches any of it. The look tool and the planning outline are tests/test_ca_outline.py."""
import json

import pytest

from app import ca, ca_tools, runner_ws, web_auth
from app.db import get_cursor
from app.repo import BadRequest, NotFound

from test_ca import _ca_tables_clean, _rpc, _start, cw  # noqa: F401  (fixtures + helpers)
from test_ca_parity import people  # noqa: F401  (Bob = owner; Alice and Carol = members of acme-app)
from test_devices import _project, _user, est, signed_in  # noqa: F401
from test_jobs import _task
from test_runner_ws import Runner, _enrol, _next
from test_web_auth import auth_on  # noqa: F401

DAVE_OID = "dddddddd-0000-4000-8000-000000000004"


@pytest.fixture
def crew(people, monkeypatch):
    """people + Dave (a designer who is a member of NOTHING) + web sessions for everyone. Carol is made a VIEWER of acme-app."""
    cw = people
    cw["dave"] = _user(DAVE_OID, "Dave")
    app_id = cw["acme_app"]["id"]
    with get_cursor() as cur:
        cur.execute("UPDATE users SET role = 'designer', is_owner = false WHERE id = %s", (cw["dave"]["id"],))
        cur.execute("UPDATE project_members SET role = 'viewer' WHERE project_id = %s AND user_id = %s", (app_id, cw["carol"]["id"]))
    cw["carol_dev"], cw["carol_key"], _ = _enrol(cw, user=cw["carol"], name="carol-pc")
    sessions = {n: {"oid": cw[n]["oid"], "name": n.capitalize(), "username": f"{n}@x.test"} for n in ("bob", "alice", "carol", "dave")}
    monkeypatch.setattr(web_auth, "resolve_session", lambda tok: sessions.get(tok))
    for n in ("bob", "alice", "carol", "dave"):
        cw[n + "_h"] = {"cookie": f"{web_auth.SESSION_COOKIE}={n}"}
    return cw


def _start_task(cw, who, task, **kw):
    r = cw["client"].post("/ca/conversations", json={"path": "/acme-app/board", "project": "acme-app",
                                                      "entity": {"kind": "task", "id": task["id"]}, **kw}, headers=cw[who + "_h"])
    return r


def _online(cw, who, fn):
    """Run fn() with this person's runner counted as online (no socket needed)."""
    dev = cw[who + "_dev"]["device_id"]
    runner_ws._conns[dev] = type("C", (), {"verified": True, "post": lambda self, i: None})()
    try:
        return fn()
    finally:
        runner_ws._conns.pop(dev, None)


def _titles(res):
    return [i.get("title") for i in res.json()["items"]]


# --- the list ---------------------------------------------------------------------------------------------------------------

def test_the_list_shows_only_that_tasks_conversations_newest_first(crew):
    cw, c = crew, crew["client"]
    t_a, t_b = cw["task"], _task(cw["acme_app"], title="Another task")
    a1 = _start_task(cw, "bob", t_a).json()["id"]
    a2 = _start_task(cw, "alice", t_a).json()["id"]
    b1 = _start_task(cw, "alice", t_b).json()["id"]
    glob = c.post("/ca/conversations", json={"path": "/", "project": None}, headers=cw["alice_h"]).json()["id"]
    gone = _start_task(cw, "bob", t_a).json()["id"]
    c.post(f"/ca/conversations/{gone}/archive", headers=cw["bob_h"])
    with get_cursor() as cur:   # titles + order
        cur.execute("UPDATE ca_conversations SET title = 'Bob on A', updated_at = now() - interval '2 hours' WHERE id = %s", (a1,))
        cur.execute("UPDATE ca_conversations SET title = 'Alice on A', updated_at = now() - interval '1 hour' WHERE id = %s", (a2,))
        cur.execute("UPDATE ca_conversations SET title = 'Alice on B' WHERE id = %s", (b1,))
        cur.execute("UPDATE ca_conversations SET title = 'Alice global secret' WHERE id = %s", (glob,))
    res = c.get(f"/ca/tasks/{t_a['id']}/discussion", headers=cw["bob_h"])
    assert res.status_code == 200
    data = res.json()
    assert [i["id"] for i in data["items"]] == [a2, a1] and _titles(res) == ["Alice on A", "Bob on A"]
    assert [(i["by"], i["mine"]) for i in data["items"]] == [("Alice", False), ("Bob", True)]
    assert data["can_post"] is True and data["task"]["id"] == t_a["id"]
    assert "Alice on B" not in res.text and "Alice global secret" not in res.text
    # the other task's list is its own
    assert [i["id"] for i in c.get(f"/ca/tasks/{t_b['id']}/discussion", headers=cw["bob_h"]).json()["items"]] == [b1]


def test_everyone_with_view_access_reads_a_task_conversation_with_who_wrote_each_message(crew):
    cw, c = crew, crew["client"]
    conv = _start_task(cw, "bob", cw["task"]).json()["id"]
    with get_cursor() as cur:
        for uid, role, text in ((cw["bob"]["id"], "user", "Bob asks"), (cw["bob"]["id"], "assistant", "CA answers"),
                                (cw["alice"]["id"], "user", "Alice adds")):
            cur.execute("INSERT INTO ca_messages (conversation_id, user_id, role, content) VALUES (%s, %s, %s, %s)", (conv, uid, role, text))
    for who in ("alice", "carol"):                       # a member and a viewer both read it
        got = c.get(f"/ca/conversations/{conv}", headers=cw[who + "_h"])
        assert got.status_code == 200
        msgs = [(m["author"], m["content"], m["mine"]) for m in got.json()["messages"] if m["role"] == "user"]
        assert msgs == [("Bob", "Bob asks", False), ("Alice", "Alice adds", who == "alice")]
        assert got.json()["shared"] is True


def test_global_conversations_stay_the_starters_own(crew):
    cw, c = crew, crew["client"]
    conv = c.post("/ca/conversations", json={"path": "/", "project": None}, headers=cw["alice_h"]).json()["id"]
    for who in ("bob", "carol", "dave"):
        assert c.get(f"/ca/conversations/{conv}", headers=cw[who + "_h"]).status_code == 404
        assert c.post(f"/ca/conversations/{conv}/messages", json={"text": "hi"}, headers=cw[who + "_h"]).status_code == 404
    assert c.get(f"/ca/conversations/{conv}", headers=cw["alice_h"]).status_code == 200


# --- access: none = 404, view = read only, edit = post ---------------------------------------------------------------------

def test_a_person_without_the_task_gets_404_everywhere(crew):
    cw, c = crew, crew["client"]
    conv = _start_task(cw, "bob", cw["task"]).json()["id"]
    tid = cw["task"]["id"]
    assert c.get(f"/ca/tasks/{tid}/discussion", headers=cw["dave_h"]).status_code == 404
    assert c.get(f"/ca/conversations/{conv}", headers=cw["dave_h"]).status_code == 404
    assert c.post(f"/ca/conversations/{conv}/messages", json={"text": "hi"}, headers=cw["dave_h"]).status_code == 404
    assert c.post(f"/ca/conversations/{conv}/stop", headers=cw["dave_h"]).status_code == 404
    assert c.post(f"/ca/conversations/{conv}/uploads?name=a.txt", content=b"x", headers={**cw["dave_h"], "content-type": "application/octet-stream"}).status_code == 404
    assert c.get(f"/ca/items/task/{tid}/discussions", headers=cw["dave_h"]).status_code == 404
    # a task of another account, and a missing one, read the same way
    assert c.get(f"/ca/tasks/{cw['btask']['id']}/discussion", headers=cw["alice_h"]).status_code == 404
    assert c.get("/ca/tasks/99999999999/discussion", headers=cw["alice_h"]).status_code == 404
    # no conversation of the task appears in the list a person without the task opens by another way
    assert [x["id"] for x in c.get("/ca/state", headers=cw["dave_h"]).json()["conversations"]] == []


def test_a_viewer_reads_but_every_write_is_403(crew):
    cw, c = crew, crew["client"]
    conv = _start_task(cw, "bob", cw["task"]).json()["id"]
    got = c.get(f"/ca/conversations/{conv}", headers=cw["carol_h"])
    assert got.status_code == 200 and got.json()["can_post"] is False
    assert c.get(f"/ca/tasks/{cw['task']['id']}/discussion", headers=cw["carol_h"]).json()["can_post"] is False
    # the post is refused before anything is stored, runner or not
    r = c.post(f"/ca/conversations/{conv}/messages", json={"text": "let me in"}, headers=cw["carol_h"])
    assert r.status_code == 403 and "view-only" in r.json()["detail"]
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT count(*) AS n FROM ca_messages WHERE conversation_id = %s", (conv,))
        assert cur.fetchone()["n"] == 0
    assert c.post(f"/ca/conversations/{conv}/stop", headers=cw["carol_h"]).status_code == 403
    assert c.post(f"/ca/conversations/{conv}/uploads?name=a.txt", content=b"x", headers={**cw["carol_h"], "content-type": "application/octet-stream"}).status_code == 403
    # and she can't START one on the task either (she has no runner here either way: the rights come first)
    assert _start_task(cw, "carol", cw["task"]).status_code == 403
    # an editor can
    assert _start_task(cw, "alice", cw["task"]).status_code == 200


# --- a turn runs as the person who typed, on their runner -----------------------------------------------------------------

def test_a_second_person_posts_and_the_turn_runs_as_them_on_their_runner(crew):
    cw, c = crew, crew["client"]
    conv = _start_task(cw, "bob", cw["task"]).json()["id"]
    bob_r, alice_r = Runner(c, cw["bob_dev"], cw["bob_key"]), Runner(c, cw["alice_dev"], cw["alice_key"])
    bob_r.handshake()
    alice_r.handshake()
    try:
        assert c.post(f"/ca/conversations/{conv}/messages", json={"text": "Bob: what is left?"}, headers=cw["bob_h"]).status_code == 200
        assert _next(bob_r)["type"] == "ca_available"
        t1 = _rpc(bob_r, type="ca_claim", id=1)["turn"]
        assert t1["user_text"] == "Bob: what is left?" and t1["session_id"] is None      # one writer so far: plain, as before
        ca.finish_turn(cw["bob_dev"]["device_id"], t1["turn_id"], t1["fence"], True, session_id="sess-bob-1234")
        # Alice continues Bob's conversation
        r = c.post(f"/ca/conversations/{conv}/messages", json={"text": "and the tests?"}, headers=cw["alice_h"])
        assert r.status_code == 200
        assert _next(alice_r)["type"] == "ca_available"
        assert _rpc(bob_r, type="ca_claim", id=2)["turn"] is None                         # never Bob's runner
        t2 = _rpc(alice_r, type="ca_claim", id=1)["turn"]
        with get_cursor(commit=False) as cur:
            cur.execute("SELECT user_id, device_id FROM ca_turns WHERE id = %s", (t2["turn_id"],))
            row = cur.fetchone()
        assert row["user_id"] == cw["alice"]["id"] and row["device_id"] == cw["alice_dev"]["device_id"]
        assert t2["user_text"] == "[Alice] and the tests?" and t2["session_id"] is None   # several writers: labelled, replayed, never resumed
        assert 'Person ("Bob"): Bob: what is left?' in t2["replay"]
        assert "shared discussion" in t2["system"] and "Alice" in t2["system"]
        # her tools carry HER rights: the turn token names her
        from app import auth
        assert auth.resolve_token(t2["turn_token"])["user_id"] == cw["alice"]["id"]
    finally:
        bob_r.close()
        alice_r.close()


def test_events_reach_only_people_who_can_view_the_task(crew):
    cw = crew
    conv = _start_task(cw, "bob", cw["task"]).json()["id"]
    ca._subs.clear()
    ca._viewer_cache.clear()
    for n in ("alice", "carol", "dave"):
        ca._subs[cw[n]["id"]] = {object()}
    assert ca._readers(conv, cw["bob"]["id"]) == {cw["bob"]["id"], cw["alice"]["id"], cw["carol"]["id"]}   # Dave has no access
    glob = cw["client"].post("/ca/conversations", json={"path": "/", "project": None}, headers=cw["alice_h"]).json()["id"]
    assert ca._readers(glob, cw["alice"]["id"]) == {cw["alice"]["id"]}                                      # a global chat is private


# --- a global conversation that changes a task is linked into the task's history -------------------------------------------

def test_a_global_conversation_that_changes_a_task_is_linked_into_its_history(crew):
    cw, c = crew, crew["client"]
    tid = cw["task"]["id"]
    conv = c.post("/ca/conversations", json={"path": "/", "project": None}, headers=cw["alice_h"]).json()["id"]
    with get_cursor() as cur:
        cur.execute("UPDATE ca_conversations SET title = 'Alice private idea' WHERE id = %s", (conv,))
        cur.execute("INSERT INTO ca_cards (conversation_id, user_id, kind, payload) VALUES (%s, %s, 'confirm', %s) RETURNING id",
                    (conv, cw["alice"]["id"], json.dumps({"action": "comment", "summary": "Comment", "params": {"task_id": tid, "body": "from chat"}})))
        card = cur.fetchone()["id"]
    assert [i for i in c.get(f"/ca/tasks/{tid}/discussion", headers=cw["bob_h"]).json()["items"]] == []   # nothing yet
    assert c.post(f"/ca/cards/{card}", json={"action": "confirm"}, headers=cw["alice_h"]).status_code == 200
    bob = c.get(f"/ca/tasks/{tid}/discussion", headers=cw["bob_h"])
    # Bob sees THAT it happened, who and when: never the title, and he cannot open it
    assert bob.json()["items"] == [{"linked": True, "open": False, "mine": False, "by": "Alice", "updated_at": bob.json()["items"][0]["updated_at"]}]
    assert "Alice private idea" not in bob.text
    assert c.get(f"/ca/conversations/{conv}", headers=cw["bob_h"]).status_code == 404
    alice = c.get(f"/ca/tasks/{tid}/discussion", headers=cw["alice_h"]).json()["items"]
    assert alice[0]["id"] == conv and alice[0]["title"] == "Alice private idea" and alice[0]["linked"] is True
    # linking twice is one row; a person without the task sees nothing of it
    ca.link_to_task(conv, tid)
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT count(*) AS n FROM ca_conversation_links WHERE conversation_id = %s", (conv,))
        assert cur.fetchone()["n"] == 1
    assert c.get(f"/ca/tasks/{tid}/discussion", headers=cw["dave_h"]).status_code == 404


def test_a_conversation_bound_to_the_task_needs_no_link(crew):
    cw = crew
    conv = _start_task(cw, "alice", cw["task"]).json()["id"]
    ca.link_to_task(conv, cw["task"]["id"])
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT count(*) AS n FROM ca_conversation_links")
        assert cur.fetchone()["n"] == 0


# --- moving a task: the conversations follow, and so does the access ---------------------------------------------------------

def test_moving_a_task_takes_its_conversations_and_their_access_along(crew):
    cw, c = crew, crew["client"]
    tid = cw["task"]["id"]
    conv = _start_task(cw, "bob", cw["task"]).json()["id"]
    assert c.get(f"/ca/conversations/{conv}", headers=cw["alice_h"]).status_code == 200
    with get_cursor() as cur:                       # the task moves to beta-app: Alice is not on it, Bob (owner) is
        cur.execute("UPDATE tasks SET project_id = %s WHERE id = %s", (cw["beta_app"]["id"], tid))
    assert c.get(f"/ca/conversations/{conv}", headers=cw["alice_h"]).status_code == 404
    assert c.get(f"/ca/tasks/{tid}/discussion", headers=cw["alice_h"]).status_code == 404
    assert c.get(f"/ca/conversations/{conv}", headers=cw["bob_h"]).status_code == 200
    assert [i["id"] for i in c.get(f"/ca/tasks/{tid}/discussion", headers=cw["bob_h"]).json()["items"]] == [conv]
    # and the conversation now runs in the task's new account
    with get_cursor() as cur:
        cur.execute("SELECT * FROM ca_conversations WHERE id = %s", (conv,))
        row = cur.fetchone()
        ca._sync_task_home(cur, row)
        cur.execute("SELECT account_id, project_id, context FROM ca_conversations WHERE id = %s", (conv,))
        moved = cur.fetchone()
    assert moved["account_id"] == cw["beta"]["id"] and moved["project_id"] == cw["beta_app"]["id"] and moved["context"]["project"] == "beta-app"
    # a member added to the new project reads it
    with get_cursor() as cur:
        cur.execute("INSERT INTO project_members (project_id, user_id, role) VALUES (%s, %s, 'member')", (cw["beta_app"]["id"], cw["dave"]["id"]))
    assert c.get(f"/ca/conversations/{conv}", headers=cw["dave_h"]).status_code == 200


# --- a client never sees any of it ---------------------------------------------------------------------------------------------

def test_a_client_token_never_sees_a_discussion(crew):
    from app.auth import issue_token
    from app import repo
    cw, c = crew, crew["client"]
    tid = cw["task"]["id"]
    conv = _start_task(cw, "bob", cw["task"]).json()["id"]
    with get_cursor() as cur:
        cur.execute("UPDATE ca_conversations SET title = 'INTERNAL PLAN' WHERE id = %s", (conv,))
        cur.execute("UPDATE tasks SET client_visible = true WHERE id = %s", (tid,))
    repo.update_project_satellite(cw["acme"]["id"], "owner", True, host="acme-app01", lan_ip="192.168.1.20", mesh_name="acme-app01.example.internal")
    sat = {"Authorization": f"Bearer {issue_token('satellite', 'acme-satellite', project_id=cw['acme']['id'])}"}
    for url in (f"/ca/tasks/{tid}/discussion", f"/ca/conversations/{conv}", "/ca/state", f"/ca/items/task/{tid}/discussions"):
        assert c.get(url, headers=sat, follow_redirects=False).status_code in (302, 401, 403), url
    assert c.post(f"/ca/conversations/{conv}/messages", json={"text": "hi"}, headers=sat, follow_redirects=False).status_code in (302, 401, 403)
    snap = c.get("/api/satellite/snapshot", headers=sat)
    assert snap.status_code == 200 and "INTERNAL PLAN" not in snap.text and "conversation" not in snap.text.lower()
    # the page code a client can reach carries no discussion hooks: they are in the staff pages only
    from pathlib import Path
    for name in ("client_dashboard.html", "client.html"):
        f = Path("app/templates/pages") / name
        assert not f.exists() or "ca-disc" not in f.read_text()


def test_tasks_a_viewer_cannot_edit_cannot_be_linked_to_a_conversation(crew):
    cw = crew
    conv = cw["client"].post("/ca/conversations", json={"path": "/", "project": None}, headers=cw["carol_h"]).json()["id"]
    r = cw["client"].patch(f"/ca/conversations/{conv}", json={"entity": {"kind": "task", "id": cw["task"]["id"]}}, headers=cw["carol_h"])
    assert r.status_code == 403


# --- a shared discussion reaches only the task's project (review P1) ---------------------------------------------------------------

@pytest.fixture
def wide(crew, monkeypatch):
    """Alice can see acme-app (P) AND acme-q (Q); Bob and Carol only P. A task in each; both projects have a repo set up."""
    cw = crew
    cw["q"] = _project("acme-q", "acmeq", cw["acme"])
    cw["q_task"] = _task(cw["q"], title="Q SECRET LAUNCH PLAN", body="Q SECRET BODY")
    with get_cursor() as cur:
        cur.execute("INSERT INTO project_members (project_id, user_id, role) VALUES (%s, %s, 'member')", (cw["q"]["id"], cw["alice"]["id"]))
        cur.execute("UPDATE projects SET git_remote = 'https://git.example.test/p.git' WHERE id IN (%s, %s)", (cw["acme_app"]["id"], cw["q"]["id"]))
        cur.execute("UPDATE users SET role = 'designer', is_owner = false WHERE id = %s", (cw["bob"]["id"],))
        cur.execute("INSERT INTO project_members (project_id, user_id, role) VALUES (%s, %s, 'member')", (cw["acme_app"]["id"], cw["bob"]["id"]))
    return cw


def _alice_turn(cw, conv, text="what's next?"):
    _online(cw, "alice", lambda: ca.send_message(cw["alice"]["id"], conv, text))
    turn = ca.claim_turn(cw["alice_dev"]["device_id"])
    from app import auth
    return turn, auth.resolve_token(turn["turn_token"])


def test_tools_in_a_task_discussion_reach_only_that_tasks_project(wide, monkeypatch):
    from app import ca_mcp
    cw = wide
    conv = _start_task(cw, "alice", cw["task"]).json()["id"]          # Alice's shared discussion on a P task (Bob reads it)
    turn, who = _alice_turn(cw, conv)
    q_id, p_id = cw["q_task"]["id"], cw["task"]["id"]
    # the starting context: only P
    assert "Q SECRET" not in turn["system"] and "acme-q" not in turn["system"] and "shared with everyone who can see this task" in turn["system"]
    out = ca_tools.planning_outline(who)
    text = json.dumps(out, default=str)
    assert "Q SECRET" not in text and {r["project"] for r in out["open_tasks"]} == {"acme-app"} and "Global" in out["scope"]
    with pytest.raises(NotFound):
        ca_tools.get_task(who, q_id)
    assert "Q SECRET" not in json.dumps(ca_tools.review_tasks(who), default=str)
    assert [p["slug"] for p in ca_tools.list_projects(who)] == ["acme-app"]
    assert all(j["task_id"] != q_id for j in ca_tools.list_jobs(who))
    assert ca_tools.get_task(who, p_id)["id"] == p_id
    for call in (lambda: ca_tools.propose_plan_change(who, q_id, "now"), lambda: ca_tools.comment(who, q_id, "x"),
                 lambda: ca_tools.propose_start_job(who, q_id), lambda: ca_tools.tick_subtask(who, 999999)):
        with pytest.raises(NotFound):
            call()
    # the same Alice in a GLOBAL conversation still sees both: the narrowing is for shared discussions only
    gconv = cw["client"].post("/ca/conversations", json={"path": "/", "project": None}, headers=cw["alice_h"]).json()["id"]
    gturn, gwho = _alice_turn(cw, gconv)
    assert "acme-q" in json.dumps(ca_tools.planning_outline(gwho), default=str) and ca_tools.get_task(gwho, q_id)["id"] == q_id
    # look: only this project's repo
    with pytest.raises(NotFound):
        _online(cw, "alice", lambda: ca_tools.look(who, "what is in Q?", "acme-q"))
    assert _online(cw, "alice", lambda: ca_tools.look(who, "what is in P?"))["project"] == "acme-app"

def test_the_answer_streamed_to_readers_cannot_carry_what_they_may_not_see(wide):
    """Bob (P only) reads Alice's P discussion; nothing a tool could return in it comes from Q, and a card tap is narrowed too."""
    cw = wide
    conv = _start_task(cw, "alice", cw["task"]).json()["id"]
    turn, who = _alice_turn(cw, conv)
    assert ca.visible_project_ids(who["user_id"], who["account_id"], who["conversation_id"]) == {cw["acme_app"]["id"]}
    assert cw["q"]["id"] in ca.visible_project_ids(who["user_id"], who["account_id"])
    with pytest.raises(NotFound):                                      # a card tapped later is narrowed the same way
        ca_tools._do_comment(who, cw["q_task"]["id"], "hello")


# --- pre-#830 conversations are not shared retroactively (review P2a) ----------------------------------------------------------------

def test_an_older_task_conversation_stays_its_starters_until_shared(crew):
    from test_ca_parity import _png, _up
    cw, c = crew, crew["client"]
    tid = cw["task"]["id"]
    conv = _start_task(cw, "alice", cw["task"]).json()["id"]
    with get_cursor() as cur:   # what a conversation made before #830 looks like
        cur.execute("UPDATE ca_conversations SET shared = false, title = 'Alice old private chat' WHERE id = %s", (conv,))
    meta = _up(cw, conv, "shot.png", _png(), who="alice").json()
    for who in ("bob", "carol"):
        assert c.get(f"/ca/conversations/{conv}", headers=cw[who + "_h"]).status_code == 404
        assert c.post(f"/ca/conversations/{conv}/messages", json={"text": "hi"}, headers=cw[who + "_h"]).status_code == 404
        assert c.get(f"/ca/uploads/{meta['id']}", headers=cw[who + "_h"]).status_code == 404
    bob = c.get(f"/ca/tasks/{tid}/discussion", headers=cw["bob_h"])
    assert [i for i in bob.json()["items"] if i.get("by") == "Alice"] == [
        {"linked": False, "open": False, "mine": False, "by": "Alice", "updated_at": bob.json()["items"][0]["updated_at"]}]
    assert "Alice old private chat" not in bob.text
    assert c.get(f"/ca/conversations/{conv}", headers=cw["alice_h"]).json()["shareable"] is True
    # a viewer cannot share one onto the task; an editor can, and from then on it is the task's
    assert c.post(f"/ca/conversations/{conv}/share", headers=cw["bob_h"]).status_code == 404
    assert c.post(f"/ca/conversations/{conv}/share", headers=cw["alice_h"]).status_code == 200
    assert c.get(f"/ca/conversations/{conv}", headers=cw["bob_h"]).status_code == 200
    assert "Alice old private chat" in c.get(f"/ca/tasks/{tid}/discussion", headers=cw["bob_h"]).text


def test_a_viewer_cannot_share_and_a_used_conversation_linked_later_stays_private(crew):
    cw, c = crew, crew["client"]
    mine = c.post("/ca/conversations", json={"path": "/", "project": None}, headers=cw["alice_h"]).json()["id"]
    with get_cursor() as cur:
        cur.execute("INSERT INTO ca_messages (conversation_id, user_id, role, content) VALUES (%s, %s, 'user', 'earlier private words')", (mine, cw["alice"]["id"]))
    assert c.patch(f"/ca/conversations/{mine}", json={"entity": {"kind": "task", "id": cw["task"]["id"]}}, headers=cw["alice_h"]).status_code == 200
    assert c.get(f"/ca/conversations/{mine}", headers=cw["bob_h"]).status_code == 404            # it had history: not shared by linking
    empty = c.post("/ca/conversations", json={"path": "/", "project": None}, headers=cw["alice_h"]).json()["id"]
    assert c.patch(f"/ca/conversations/{empty}", json={"entity": {"kind": "task", "id": cw["task"]["id"]}}, headers=cw["alice_h"]).status_code == 200
    assert c.get(f"/ca/conversations/{empty}", headers=cw["bob_h"]).status_code == 200            # an empty one is the task's from the start
    assert c.post(f"/ca/conversations/{mine}/share", headers=cw["alice_h"]).status_code == 200
    assert c.get(f"/ca/conversations/{mine}", headers=cw["bob_h"]).status_code == 200
    with get_cursor() as cur:   # a viewer's own old conversation: sharing it onto a task she may not edit is refused
        cur.execute("UPDATE ca_conversations SET user_id = %s, shared = false WHERE id = %s", (cw["carol"]["id"], mine))
    assert c.post(f"/ca/conversations/{mine}/share", headers=cw["carol_h"]).status_code == 403


# --- cards on the live feed are not "mine" for everyone (review P2b) -----------------------------------------------------------------

def test_a_card_on_the_live_feed_carries_whose_it_is_not_mine(crew, monkeypatch):
    cw = crew
    conv = _start_task(cw, "alice", cw["task"]).json()["id"]
    turn, who = _alice_turn(cw, conv)
    seen = []
    monkeypatch.setattr(ca, "publish_conv", lambda cid, uid, event, data: seen.append((event, data)))
    ca.create_card(who, "confirm", {"action": "stop_job", "summary": "x", "params": {"job_id": 1}})
    (event, data), = seen
    assert event == "card" and data["card"]["user_id"] == cw["alice"]["id"] and "mine" not in data["card"]
    js = open("app/static/js/ca.js").read()
    assert "d.card.mine = d.card.user_id === myId" in js                 # each browser decides; no Confirm / Dismiss for anyone else
    # and the read path: Bob loading the conversation gets her card as not his
    cards = cw["client"].get(f"/ca/conversations/{conv}", headers=cw["bob_h"]).json()["cards"]
    assert cards and all(k["mine"] is False and k["by"] == "Alice" for k in cards)
    assert cw["client"].post(f"/ca/cards/{cards[0]['id']}", json={"action": "confirm"}, headers=cw["bob_h"]).status_code == 404


# --- another person's unsent upload is not served (review P3a) ------------------------------------------------------------------------

def test_an_unsent_upload_is_served_to_its_owner_only(crew):
    from test_ca_parity import _png, _up
    cw, c = crew, crew["client"]
    conv = _start_task(cw, "alice", cw["task"]).json()["id"]
    meta = _up(cw, conv, "draft.png", _png(), who="alice").json()
    assert c.get(f"/ca/uploads/{meta['id']}", headers=cw["alice_h"]).status_code == 200
    assert c.get(f"/ca/uploads/{meta['id']}", headers=cw["bob_h"]).status_code == 404            # attached, not sent
    with get_cursor() as cur:
        cur.execute("INSERT INTO ca_messages (conversation_id, user_id, role, content) VALUES (%s, %s, 'user', 'see this') RETURNING id", (conv, cw["alice"]["id"]))
        mid = cur.fetchone()["id"]
        cur.execute("UPDATE ca_attachments SET message_id = %s WHERE id = %s", (mid, meta["id"]))
    assert c.get(f"/ca/uploads/{meta['id']}", headers=cw["bob_h"]).status_code == 200            # sent into a conversation Bob may read
    assert c.get(f"/ca/uploads/{meta['id']}", headers=cw["dave_h"]).status_code == 404
