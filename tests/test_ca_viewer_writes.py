"""CA cards and the answer / decision page routes need the right to CHANGE the project, not just see it (OSS review F1).

A person who can only VIEW a project (a viewer-role project member) must never get a write through their CA: not a comment, a
ticked step, an answered owner question or a decided change, neither when the card is PROPOSED nor when it is TAPPED (their
rights may have changed in between). Answering a question and deciding a change need the project's Admin or Owner (the same
level the page routes now need), and the row names the real person, never the owner. Demoting a person to the viewer role also
revokes their runners, so their CA turns stop."""
import pytest

from app import auth, ca, ca_tools, changes, repo, users
from app.db import get_cursor
from app.repo import BadRequest

from test_ca import _ca_tables_clean, _rpc, _start, cw  # noqa: F401
from test_ca_discussion import _online, crew  # noqa: F401  (people + Dave; Carol is a VIEWER of acme-app)
from test_ca_parity import people  # noqa: F401
from test_devices import est, signed_in, _user, _project  # noqa: F401
from test_web_auth import auth_on  # noqa: F401


def _turn(cw, who):
    """A running CA turn for this person: the `who` dict every ca_tools function takes."""
    conv = ca.create_conversation(cw[who]["id"], "/acme-app/board", "acme-app", None, account_id=cw["acme"]["id"])["id"]
    _online(cw, who, lambda: ca.send_message(cw[who]["id"], conv, "hi"))
    return auth.resolve_token(ca.claim_turn(cw[who + "_dev"]["device_id"])["turn_token"])


def _role(cw, who, role):
    with get_cursor() as cur:
        cur.execute("UPDATE project_members SET role = %s WHERE project_id = %s AND user_id = %s",
                    (role, cw["acme_app"]["id"], cw[who]["id"]))


def _cards(who):
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT id, state, result FROM ca_cards WHERE conversation_id = %s ORDER BY id", (who["conversation_id"],))
        return cur.fetchall()


def _things(cw):
    q = repo.ask_owner(cw["task"]["id"], "Ship it?", "We finished the work and need a go-ahead to ship.", ["Yes", "No"], "claude:features")
    ch = changes.recommend(cw["acme_app"]["id"], "Use sqlite", "because", "claude:features")
    with get_cursor() as cur:
        cur.execute("INSERT INTO subtasks (task_id, text, position, created_by) VALUES (%s, 'check it', 1, 'owner') RETURNING id", (cw["task"]["id"],))
        sub = cur.fetchone()
    return q, ch, sub


def _unchanged(cw, q, ch, sub):
    assert repo.get_question(q["id"])["answered_at"] is None
    assert changes.get(ch["id"])["stage"] == "recommended"
    assert not repo.list_subtasks(cw["task"]["id"])[0]["done"]
    assert not [c for c in repo.get_task(cw["task"]["id"])["comments"] if c["body"] == "sneaky"]


def test_a_viewer_cannot_even_propose_a_write(crew):
    cw = crew
    q, ch, sub = _things(cw)
    who = _turn(cw, "carol")                                   # Carol is a viewer of acme-app
    for call in (lambda: ca_tools.propose_answer(who, q["id"], "Yes"), lambda: ca_tools.show_question(who, q["id"]),
                 lambda: ca_tools.propose_decision(who, ch["id"], "approved"), lambda: ca_tools.comment(who, cw["task"]["id"], "sneaky"),
                 lambda: ca_tools.tick_subtask(who, sub["id"])):
        with pytest.raises(BadRequest):
            call()
    assert _cards(who) == []
    with pytest.raises(BadRequest):                            # the direct (bound-task) writers refuse too
        ca_tools._do_comment(who, cw["task"]["id"], "sneaky")
    with pytest.raises(BadRequest):
        ca_tools._do_tick(who, sub["id"], True)
    _unchanged(cw, q, ch, sub)


def test_a_card_proposed_by_an_editor_is_refused_when_tapped_after_a_demotion(crew):
    cw = crew
    q, ch, sub = _things(cw)
    _role(cw, "alice", "admin")
    who = _turn(cw, "alice")
    ca_tools.propose_answer(who, q["id"], "Yes")
    ca_tools.propose_decision(who, ch["id"], "approved")
    ca_tools.comment(who, cw["task"]["id"], "sneaky")          # not the bound task: a card
    ca_tools.tick_subtask(who, sub["id"])
    assert len(_cards(who)) == 4
    _role(cw, "alice", "viewer")                               # ... then Alice is made a viewer
    for card in _cards(who):
        out = ca_tools.resolve_card(cw["alice"]["id"], card["id"], "confirm")
        assert out["state"] == "failed", out
    _unchanged(cw, q, ch, sub)


def test_a_member_cannot_answer_or_decide_but_an_admin_can_and_is_recorded(crew):
    cw = crew
    q, ch, sub = _things(cw)
    who = _turn(cw, "alice")                                   # Alice is a plain member: may comment and tick, may not answer / decide
    with pytest.raises(BadRequest):
        ca_tools.propose_answer(who, q["id"], "Yes")
    with pytest.raises(BadRequest):
        ca_tools.propose_decision(who, ch["id"], "approved")
    ca_tools.comment(who, cw["task"]["id"], "from a member")
    ca_tools.tick_subtask(who, sub["id"])
    for card in _cards(who):
        assert ca_tools.resolve_card(cw["alice"]["id"], card["id"], "confirm")["state"] == "done"
    assert repo.list_subtasks(cw["task"]["id"])[0]["done_by"] == f"ca:{cw['alice']['id']}"          # the person, not 'owner'
    assert [c["author"] for c in repo.get_task(cw["task"]["id"])["comments"] if c["body"] == "from a member"] == [f"ca:{cw['alice']['id']}"]
    _role(cw, "alice", "admin")
    before = {c["id"] for c in _cards(who)}
    ca_tools.propose_answer(who, q["id"], "Yes")
    ca_tools.propose_decision(who, ch["id"], "approved")
    for card in _cards(who):
        if card["id"] not in before:
            assert ca_tools.resolve_card(cw["alice"]["id"], card["id"], "confirm")["state"] == "done"
    got = repo.get_question(q["id"])
    assert got["answer"] == "Yes" and got["answered_user_id"] == cw["alice"]["id"]
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT author FROM task_comments WHERE body LIKE 'Answer: Yes%'")
        assert cur.fetchone()["author"] == f"ca:{cw['alice']['id']}"     # model-written text stays ca: even when tapped (#702)
        cur.execute("SELECT decided_by, decided_user_id FROM change_log WHERE id = %s", (ch["id"],))
        row = cur.fetchone()
        assert row["decided_by"] == f"ca:{cw['alice']['id']}" and row["decided_user_id"] == cw["alice"]["id"]
        cur.execute("SELECT actor FROM activity WHERE verb IN ('answered', 'decided') AND user_id = %s", (cw["alice"]["id"],))
        assert {r["actor"] for r in cur.fetchall()} == {f"ca:{cw['alice']['id']}"}


def test_r4_only_text_the_person_chose_is_theirs_a_tapped_propose_answer_stays_ca(crew):
    cw = crew
    _role(cw, "alice", "admin")
    who, aid = _turn(cw, "alice"), cw["alice"]["id"]

    def author(text):
        with get_cursor(commit=False) as cur:
            cur.execute("SELECT author FROM task_comments WHERE body LIKE %s", (f"Answer: {text}%",))
            return cur.fetchone()["author"]
    q, _, _ = _things(cw)
    ca_tools.propose_answer(who, q["id"], "Yes")                       # the model wrote "Yes": tapping confirm does not make it hers
    out = ca_tools.resolve_card(aid, _cards(who)[-1]["id"], "confirm")
    assert out["state"] == "done", out
    assert author("Yes") == f"ca:{aid}"
    q2 = repo.ask_owner(cw["task"]["id"], "Which?", "We need a choice to go on with the work.", ["A", "B"], "claude:features")
    ca_tools.show_question(who, q2["id"])                              # she picks the option herself
    ca_tools.resolve_card(aid, _cards(who)[-1]["id"], "answer", "A")
    assert author("A") == f"user:{aid}"


def _real_sessions(cw, monkeypatch):
    """Page sessions that carry the person's users.id and role, as a real sign-in does (the `crew` stubs carry neither, so they
    act as the owner)."""
    from app import web_auth

    def resolve(tok):
        if tok not in ("bob", "alice", "carol", "dave"):
            return None
        with get_cursor(commit=False) as cur:
            cur.execute("SELECT id, role, is_owner, display_name FROM users WHERE id = %s", (cw[tok]["id"],))
            u = cur.fetchone()
        return {"oid": cw[tok]["oid"], "name": tok, "username": f"{tok}@x.test", "user_id": u["id"], "role": u["role"],
                "is_owner": u["is_owner"]}
    monkeypatch.setattr(web_auth, "resolve_session", resolve)


def test_the_page_routes_for_answer_and_decide_need_admin_and_name_the_person(crew, monkeypatch):
    cw, c = crew, crew["client"]
    _real_sessions(cw, monkeypatch)
    q, ch, sub = _things(cw)
    h = {**cw["alice_h"], "hx-request": "true"}
    assert c.post(f"/acme-app/questions/{q['id']}/answer", data={"answer": "Yes"}, headers=h).status_code == 403
    assert c.post(f"/acme-app/changes/{ch['id']}/decide", data={"decision": "approved"}, headers=h).status_code == 403
    assert repo.get_question(q["id"])["answered_at"] is None and changes.get(ch["id"])["stage"] == "recommended"
    carol = {**cw["carol_h"], "hx-request": "true"}                                     # a viewer: refused before the handler
    assert c.post(f"/acme-app/questions/{q['id']}/answer", data={"answer": "Yes"}, headers=carol).status_code == 403
    _role(cw, "alice", "admin")
    assert c.post(f"/acme-app/questions/{q['id']}/answer", data={"answer": "Yes"}, headers=h).status_code == 200
    assert c.post(f"/acme-app/changes/{ch['id']}/decide", data={"decision": "approved"}, headers=h).status_code == 200
    assert repo.get_question(q["id"])["answered_user_id"] == cw["alice"]["id"]
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT decided_by FROM change_log WHERE id = %s", (ch["id"],))
        assert cur.fetchone()["decided_by"] == f"user:{cw['alice']['id']}"
        cur.execute("SELECT author FROM task_comments WHERE body LIKE 'Answer: Yes%'")
        assert cur.fetchone()["author"] == f"user:{cw['alice']['id']}"
    # the instance owner is unchanged: still the 'owner' actor
    q2 = repo.ask_owner(cw["task"]["id"], "Again?", "A second question that needs the owner to say something.", None, "claude:features")
    bob = {**cw["bob_h"], "hx-request": "true"}
    assert c.post(f"/acme-app/questions/{q2['id']}/answer", data={"answer": "ok"}, headers=bob).status_code == 200
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT author FROM task_comments WHERE body LIKE 'Answer: ok%'")
        assert cur.fetchone()["author"] == "owner"


def test_demoting_to_viewer_revokes_the_persons_runners(crew):
    cw = crew
    with get_cursor() as cur:                                   # users.set_role works on local accounts (a username)
        cur.execute("UPDATE users SET username = display_name || '-local' WHERE id = ANY(%s)", ([cw["alice"]["id"], cw["carol"]["id"]],))
    users.set_role(cw["alice"]["id"], "viewer")
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT revoked_at FROM device_tokens WHERE id = %s", (cw["alice_dev"]["device_id"],))
        assert cur.fetchone()["revoked_at"] is not None
        cur.execute("SELECT revoked_at FROM device_tokens WHERE id = %s", (cw["carol_dev"]["device_id"],))
        assert cur.fetchone()["revoked_at"] is None            # someone else's runner is untouched
    users.set_role(cw["carol"]["id"], "designer")              # a change to designer revokes nothing
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT revoked_at FROM device_tokens WHERE id = %s", (cw["carol_dev"]["device_id"],))
        assert cur.fetchone()["revoked_at"] is None
