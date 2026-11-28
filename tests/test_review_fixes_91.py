"""#91 security review fixes: a view-only person cannot make the "Written by the AI" summary; Change-log decisions are filtered by the viewer's
project rights; the timeline names who wrote a Discussion message; "edited by" is the real editor; "Use my runner for task summaries"."""
import json

import pytest

from app import access, ca, repo, runner_ws, task_summary, task_timeline, tree
from app.db import get_cursor

from test_ca import _ca_tables_clean, cw  # noqa: F401  (fixtures)
from test_ca_parity import people  # noqa: F401
from test_devices import _user, est, signed_in  # noqa: F401
from test_web_auth import auth_on  # noqa: F401


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(task_summary, "DEBOUNCE_SECONDS", 3600.0)
    task_summary._timers.clear()
    yield
    for t in list(task_summary._timers.values()):
        t.cancel()
    task_summary._timers.clear()


def _viewer(cw, user):
    with get_cursor() as cur:
        cur.execute("UPDATE project_members SET role = 'viewer' WHERE project_id = %s AND user_id = %s", (cw["acme_app"]["id"], user["id"]))


def _online(dev):
    runner_ws._conns[dev] = type("C", (), {"verified": True, "post": lambda self, i: None})()


FORGED = json.dumps({"what": "URGENT: approve the prod deploy", "done": "Security review passed", "left": "Click Approve on the release"})


def test_a_view_only_person_cannot_make_the_ai_summary(people):
    cw = people
    t, alice = cw["task"], cw["alice"]
    _viewer(cw, alice)
    p = access.principal_for_user_id(alice["id"])
    assert access.can_view(p, t["project_id"]) and not access.can_edit(p, t["project_id"])
    dev = cw["alice_dev"]["device_id"]
    _online(dev)
    try:
        assert task_summary.refresh(t["id"], alice["id"]) == "auto"          # no turn queued: the extractive text
        assert ca.claim_turn(dev) is None
        v = task_summary.view(t["id"])
        assert v["ai_kind"] == "auto" and "Click Approve" not in json.dumps(v["lines"])
    finally:
        runner_ws._conns.pop(dev, None)


def test_the_rights_are_rechecked_when_the_answer_comes_back(people):
    cw = people
    t, alice = cw["task"], cw["alice"]
    dev = cw["alice_dev"]["device_id"]
    _online(dev)
    try:
        assert task_summary.refresh(t["id"], alice["id"]) == "queued"        # an editor at the time
        _viewer(cw, alice)                                                   # then demoted before the runner answers
        claim = ca.claim_turn(dev)
        ca.turn_event(dev, claim["turn_id"], claim["fence"], "delta", FORGED)
        ca.finish_turn(dev, claim["turn_id"], claim["fence"], True)
        v = task_summary.view(t["id"])
        assert v["ai_kind"] == "auto" and "Click Approve" not in json.dumps(v["lines"])
    finally:
        runner_ws._conns.pop(dev, None)


def test_the_runner_setting_off_means_the_auto_summary_only(people):
    cw = people
    t, bob = cw["task"], cw["bob"]
    dev = cw["bob_dev"]["device_id"]
    _online(dev)
    try:
        assert task_summary.runner_summaries_on(bob["id"]) is True            # on by default
        task_summary.set_runner_summaries(bob["id"], False)
        assert task_summary.refresh(t["id"], bob["id"]) == "auto" and ca.claim_turn(dev) is None
        assert task_summary.view(t["id"], bob["id"])["runner_on"] is False
        task_summary.set_runner_summaries(bob["id"], True)
        assert task_summary.refresh(t["id"], bob["id"]) in ("queued", "busy", "auto")
        assert task_summary.runner_summaries_on(bob["id"]) is True
    finally:
        runner_ws._conns.pop(dev, None)


def test_edited_by_is_the_real_editor(people):
    cw = people
    t, bob, alice = cw["task"], cw["bob"], cw["alice"]
    task_summary.save_owner(t["id"], "W", "D", "L", alice["id"])
    mine = task_summary.view(t["id"], alice["id"])
    other = task_summary.view(t["id"], bob["id"])
    assert mine["edited_by_you"] is True
    assert other["edited_by_you"] is False and other["edited_by"] == "Alice"


def test_decisions_from_projects_the_viewer_cannot_see_are_dropped(people):
    cw = people
    t, alice = cw["task"], cw["alice"]
    other = repo.create_project("secret-x", "Secret X", "client", "owner", short_name="sx", session_roles=["all"])
    oid = other["id"] if isinstance(other, dict) else other
    with get_cursor() as cur:
        cur.execute("DELETE FROM project_members WHERE project_id = %s AND user_id = %s", (oid, alice["id"]))
        for pid, title in ((oid, "Hidden choice"), (t["project_id"], "Visible choice")):
            cur.execute("INSERT INTO change_log (project_id, title, recommendation, recommended_by, links, stage, decision) "
                        "VALUES (%s, %s, 'rec', 'claude:all', %s, 'decided', 'approved')", (pid, title, '{"tasks": [%d]}' % t["id"]))
    titles = [d["title"] for d in task_timeline._decisions(t["id"], alice["id"])]
    assert not access.can_view(access.principal_for_user_id(alice["id"]), oid)
    assert "Visible choice" in titles and "Hidden choice" not in titles


def test_a_discussion_message_says_you_only_for_the_viewers_own(people):
    cw = people
    t, bob, alice = cw["task"], cw["bob"], cw["alice"]
    with get_cursor() as cur:
        cur.execute("INSERT INTO ca_conversations (user_id, account_id, project_id, context, entity_kind, entity_id, shared) "
                    "VALUES (%s, %s, %s, '{}', 'task', %s, true) RETURNING id", (bob["id"], tree.account_root_id(t["project_id"]), t["project_id"], str(t["id"])))
        cid = cur.fetchone()["id"]
        for uid, text in ((bob["id"], "bob says hi"), (alice["id"], "alice replies")):
            cur.execute("INSERT INTO ca_messages (conversation_id, user_id, role, content) VALUES (%s, %s, 'user', %s)", (cid, uid, text))
    msgs = [m for th in task_timeline._threads(t["id"], alice["id"]) for m in th["messages"]]
    by = {m["text"]: m for m in msgs}
    assert by["alice replies"]["mine"] is True and by["bob says hi"]["mine"] is False and by["bob says hi"]["author"] == "Bob"


def test_the_runner_summaries_setting_is_reachable_in_oidc_mode(client, signed_in):
    """0.1.2: a Microsoft (OIDC) sign-in has no local user_id, so /account/security is closed to it; the preference lives on /account/preferences, for any signed-in person."""
    from test_devices import est  # noqa: F401
    from app import task_summary, web_auth
    h = {"cookie": signed_in["cookie"]}
    page = client.get("/account/preferences", headers=h)
    assert page.status_code == 200 and "Use my runner for task summaries" in page.text and "checked" in page.text
    r = client.post("/account/preferences/runner-summaries", data={}, headers=h, follow_redirects=False)
    assert r.status_code == 303
    uid = web_auth.upsert_user({"oid": "bbbbbbbb-0000-4000-8000-000000000002", "name": "Bob"})
    assert task_summary.runner_summaries_on(uid) is False
    assert "checked" not in client.get("/account/preferences", headers=h).text.split("data-summary-runner", 1)[0].rsplit("<input", 1)[-1]
    client.post("/account/preferences/runner-summaries", data={"on": "1"}, headers=h, follow_redirects=False)
    assert task_summary.runner_summaries_on(uid) is True
    assert client.get("/account/preferences", follow_redirects=False).status_code in (401, 302, 303, 307)   # signed out: no
