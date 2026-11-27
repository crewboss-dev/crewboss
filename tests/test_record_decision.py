"""Task #581: the owner decides a change in the conversation and the session records it (record_decision). Verified
against the owner's own prompts (UserPromptSubmit hook -> /api/owner-prompt), seen and acknowledged on Now, and
overridable by the owner with the session's quote kept as evidence."""
import pytest

from app import changes, mcp_server, push, repo
from app.auth import issue_token, resolve_session_token
from app.db import get_cursor

OWN = {"Origin": "https://crewboss.example.com", "Sec-Fetch-Site": "same-origin"}


@pytest.fixture
def sess(project, mcp_caller, monkeypatch):
    """A session token on `project`, the MCP caller set to it, and push.deliver recorded instead of sent."""
    tok = issue_token("all", "a", project_id=project["id"])
    mcp_caller(tok)
    pushes = []
    monkeypatch.setattr(push, "deliver", lambda payload: pushes.append(payload) or {"sent": 1, "failed": 0, "pruned": 0})
    s = resolve_session_token(tok)
    s["pushes"] = pushes
    s["token"] = tok
    return s


def _events(change_id):
    return [o for o in repo.list_outbox("features", undelivered_only=True)
            if o["kind"] == "change_decided" and f"change #{change_id} " in o["message"]]


def _activity(change_id):
    with get_cursor() as cur:
        cur.execute("SELECT summary FROM activity WHERE entity = 'change' AND entity_id = %s ORDER BY id",
                    (change_id,))
        return [r["summary"] for r in cur.fetchall()]


def test_verified_when_quote_is_in_owner_prompt(sess, client, project):
    c = mcp_server.recommend_change("Pick blue", "why")
    changes.note_owner_prompt(sess["id"], "ok   so\nyes, go with <b>blue</b> please")
    d = mcp_server.record_decision(c["id"], "approved", "  yes, go with <b>blue</b>  ")
    assert d["stage"] == "decided" and d["decision"] == "approved" and d["words_verified"] is True
    assert d["decided_via"] == "session" and d["decided_by"] == "claude:all" and d["owner_words"] == "yes, go with <b>blue</b>"
    assert "notice" not in d
    assert len(_events(c["id"])) == 1
    assert len(sess["pushes"]) == 1 and "blue" not in sess["pushes"][0]["tag"]
    html = client.get(f"/{project['slug']}/changes").text
    assert "Decided in chat by" in html and "yes, go with &lt;b&gt;blue&lt;/b&gt;" in html and "unverified" not in html
    assert mcp_server.mark_change_done(c["id"])["stage"] == "done"


def test_unverified_is_stored_but_not_approved(sess, client, project):
    c = mcp_server.recommend_change("Pick red", "why")
    changes.note_owner_prompt(sess["id"], "something else entirely")
    d = mcp_server.record_decision(c["id"], "approved", "yes, red")
    assert d["words_verified"] is False and d["decided_via"] == "session" and "UNVERIFIED" in d["notice"]
    assert _events(c["id"]) == []                                       # the session isn't told it's approved
    assert len(sess["pushes"]) == 1                                     # the owner still hears of it
    with pytest.raises(ValueError):
        mcp_server.mark_change_done(c["id"])                            # not an approval yet
    assert "unverified" in client.get(f"/{project['slug']}/changes").text
    # the owner confirms it on Now: that is the first time the session hears it
    r = client.post(f"/{project['slug']}/changes/{c['id']}/acknowledge", headers=OWN, follow_redirects=False)
    assert r.status_code == 303 and changes.get(c["id"])["acknowledged_at"] is not None
    assert len(_events(c["id"])) == 1
    assert mcp_server.mark_change_done(c["id"])["stage"] == "done"


def test_channel_text_other_tokens_and_stale_prompts_never_qualify(sess, project):
    other_tok = resolve_session_token(issue_token("all", "b", project_id=project["id"]))
    changes.note_owner_prompt(other_tok["id"], "yes go ahead with green")      # another session's prompt
    c = mcp_server.recommend_change("Pick green", "why")
    # a message that reached the session over the channel never fired the hook: nothing was posted for it
    assert mcp_server.record_decision(c["id"], "approved", "yes go ahead with green")["words_verified"] is False
    c2 = mcp_server.recommend_change("Pick grey", "why")
    changes.note_owner_prompt(sess["id"], "yes go ahead with grey")
    with get_cursor() as cur:
        cur.execute("UPDATE owner_prompts SET created_at = now() - interval '31 minutes' WHERE token_id = %s",
                    (sess["id"],))
    assert mcp_server.record_decision(c2["id"], "approved", "yes go ahead with grey")["words_verified"] is False


def test_owner_prompt_endpoint(sess, client):
    assert client.post("/api/owner-prompt", json={"prompt": "hello"}).status_code == 401
    h = {"Authorization": f"Bearer {sess['token']}"}
    assert client.post("/api/owner-prompt", json={"prompt": "  yes\n  do it  "}, headers=h).status_code == 200
    with get_cursor() as cur:
        cur.execute("UPDATE owner_prompts SET created_at = now() - interval '2 hours'")
    assert client.post("/api/owner-prompt", json={"prompt": "second"}, headers=h).status_code == 200
    with get_cursor() as cur:
        cur.execute("SELECT prompt FROM owner_prompts WHERE token_id = %s", (sess["id"],))
        assert [r["prompt"] for r in cur.fetchall()] == ["second"]        # old rows pruned


def test_refusals(sess, project):
    other = repo.create_project("other-q", "Other", "internal", "owner", short_name="otherq", session_roles=["all"])
    foreign = changes.recommend(other["id"], "x", "", "claude:all")
    c = mcp_server.recommend_change("Pick red", "why")
    with pytest.raises(Exception):
        mcp_server.record_decision(foreign["id"], "approved", "ok")           # wrong scope
    assert changes.get(foreign["id"])["stage"] == "recommended"
    with pytest.raises(repo.BadRequest):
        mcp_server.record_decision(c["id"], "approved", "   ")                # empty words
    with pytest.raises(repo.BadRequest):
        mcp_server.record_decision(c["id"], "maybe", "ok")                    # bad decision
    with pytest.raises(repo.BadRequest):
        mcp_server.record_decision(c["id"], "changed", "ok but")              # 'changed' needs a note
    changes.note_owner_prompt(sess["id"], "no")                                # a real owner prompt: verified, final
    mcp_server.record_decision(c["id"], "rejected", "no")
    with pytest.raises(ValueError):
        mcp_server.record_decision(c["id"], "approved", "yes")                # already decided
    d = mcp_server.recommend_change("Pick green", "why")
    changes.decide(d["id"], "approved", "owner")
    with pytest.raises(ValueError):
        mcp_server.record_decision(d["id"], "rejected", "no")                 # decided on the dashboard


def test_task_scope_comments_and_links(sess, project):
    other = repo.create_project("other-t", "Other", "internal", "owner", short_name="othert", session_roles=["all"])
    mine = repo.create_task(project["id"], "mine", "", None, "feature", [], {}, "owner")
    theirs = repo.create_task(other["id"], "theirs", "", None, "feature", [], {}, "owner")
    # (b) links across accounts can't be stored through the tools
    with pytest.raises(repo.NotFound):
        mcp_server.recommend_change("Leaky", "why", links={"tasks": [theirs["id"]]})
    ok = mcp_server.recommend_change("Fine", "why", links={"tasks": [mine["id"]]})
    changes.decide(ok["id"], "approved", "owner")
    with pytest.raises(repo.NotFound):
        mcp_server.mark_change_done(ok["id"], links={"tasks": [theirs["id"]]})
    # (a) an old/odd row that already names a foreign task: only the visible one is commented on
    c = changes.recommend(project["id"], "Old", "why", "claude:all", links={"tasks": [mine["id"], theirs["id"]]})
    changes.note_owner_prompt(sess["id"], "yes do it")
    mcp_server.record_decision(c["id"], "approved", "yes do it")
    assert "yes do it" in str(repo.get_task(mine["id"]))
    assert "yes do it" not in str(repo.get_task(theirs["id"]))


def test_now_section_acknowledge_and_owner_only(sess, client, project):
    c = mcp_server.recommend_change("Pick blue", "why")
    mcp_server.record_decision(c["id"], "approved", "yes blue")
    html = client.get(f"/{project['slug']}/tasks/board?layout=focus").text
    assert "data-focus-chat-decisions" in html and "Decided in chat" in html and "data-acknowledge" in html
    with pytest.raises(repo.Forbidden):
        changes.acknowledge(c["id"], "claude:all")
    r = client.post(f"/{project['slug']}/changes/{c['id']}/acknowledge", headers={"Origin": "https://evil.example"},
                    follow_redirects=False)
    assert r.status_code in (400, 403) and changes.get(c["id"])["acknowledged_at"] is None   # same-origin only
    client.post(f"/{project['slug']}/changes/{c['id']}/acknowledge", headers=OWN, follow_redirects=False)
    assert "data-focus-chat-decisions" not in client.get(f"/{project['slug']}").text
    assert changes.get(c["id"])["decision"] == "approved"


def test_override_keeps_evidence_and_same_decision_only_acknowledges(sess, project):
    c = mcp_server.recommend_change("Pick blue", "why")
    changes.note_owner_prompt(sess["id"], "yes blue")
    mcp_server.record_decision(c["id"], "approved", "yes blue")
    assert len(_events(c["id"])) == 1
    same = changes.decide(c["id"], "approved", "owner")                         # same decision: just acknowledges
    assert same["acknowledged_at"] is not None and same["decided_via"] == "session"
    assert len(_events(c["id"])) == 1                                           # no second change_decided
    new = changes.decide(c["id"], "rejected", "owner")                          # a real override
    assert new["decision"] == "rejected" and new["decided_via"] == "dashboard" and new["decided_by"] == "owner"
    assert new["superseded_owner_words"] == "yes blue" and new["superseded_by"] == "claude:all"
    assert any("yes blue" in a and "claude:all" in a for a in _activity(c["id"]))
    assert len(_events(c["id"])) == 2
    with pytest.raises(repo.Conflict):
        changes.decide(c["id"], "approved", "owner")                            # the owner's own call stands


def test_unverified_same_decision_confirms_and_tells_the_session(sess, project):
    c = mcp_server.recommend_change("Pick blue", "why")
    mcp_server.record_decision(c["id"], "approved", "yes blue")
    assert _events(c["id"]) == []
    changes.decide(c["id"], "approved", "owner")
    assert len(_events(c["id"])) == 1


def test_override_after_done_while_unacknowledged(sess, project):
    c = mcp_server.recommend_change("Pick blue", "why")
    changes.note_owner_prompt(sess["id"], "yes blue")
    mcp_server.record_decision(c["id"], "approved", "yes blue")
    mcp_server.mark_change_done(c["id"])
    new = changes.decide(c["id"], "rejected", "owner")
    assert new["stage"] == "done" and new["decision"] == "rejected"             # the work shipped: stage is kept
    assert "does not undo" in new["override_note"]
    # acknowledged + done = final
    d = mcp_server.recommend_change("Pick pink", "why")
    changes.note_owner_prompt(sess["id"], "yes pink")
    mcp_server.record_decision(d["id"], "approved", "yes pink")
    mcp_server.mark_change_done(d["id"])
    changes.acknowledge(d["id"], "owner")
    with pytest.raises(repo.Conflict):
        changes.decide(d["id"], "rejected", "owner")





# --- re-review: short quotes, re-record, overseer links, retention -----------------------------------------------

def test_short_yes_never_matches_an_unrelated_earlier_prompt(sess):
    c = mcp_server.recommend_change("Pick blue", "why")
    changes.note_owner_prompt(sess["id"], "yes that sounds good for later")
    changes.note_owner_prompt(sess["id"], "totally different thing now")
    for w in ("yes", "e"):
        assert mcp_server.record_decision(c["id"], "approved", w)["words_verified"] is False
        with get_cursor() as cur:
            cur.execute("UPDATE change_log SET stage='recommended', decision=NULL, decided_via='dashboard' "
                        "WHERE id=%s", (c["id"],))
    assert _events(c["id"]) == []


def test_long_quote_anywhere_in_window_and_case_fold_verify(sess):
    c = mcp_server.recommend_change("Pick blue", "why")
    changes.note_owner_prompt(sess["id"], "Please GO with   the Blue option, thanks")
    changes.note_owner_prompt(sess["id"], "and one more unrelated message")
    d = mcp_server.record_decision(c["id"], "approved", "go with the blue OPTION")   # 15+ chars, not the latest
    assert d["words_verified"] is True and len(_events(c["id"])) == 1


def test_short_quote_needs_the_latest_prompt_after_the_change(sess):
    early = changes.recommend(sess["project_id"], "Early", "", "claude:all")
    changes.note_owner_prompt(sess["id"], "Yes")                                    # BEFORE the next change exists
    c = mcp_server.recommend_change("Pick blue", "why")
    assert mcp_server.record_decision(c["id"], "approved", "yes")["words_verified"] is False
    d = mcp_server.recommend_change("Pick red", "why")
    changes.note_owner_prompt(sess["id"], "Yes.")                                   # the latest, after the change
    assert mcp_server.record_decision(d["id"], "approved", "YES")["words_verified"] is True
    assert early["stage"] == "recommended"


def test_rerecord_only_while_unverified_and_unacknowledged(sess):
    c = mcp_server.recommend_change("Pick blue", "why")
    changes.note_owner_prompt(sess["id"], "Yes")
    first = mcp_server.record_decision(c["id"], "approved", "yess")                 # a typo: unverified
    assert first["words_verified"] is False and _events(c["id"]) == []
    second = mcp_server.record_decision(c["id"], "approved", "yes")                 # corrected: replaces it
    assert second["words_verified"] is True and second["owner_words"] == "yes"
    assert second["superseded_owner_words"] == "yess" and len(_events(c["id"])) == 1
    with pytest.raises(ValueError):
        mcp_server.record_decision(c["id"], "rejected", "yes")                      # verified: final for the session
    d = mcp_server.recommend_change("Pick red", "why")
    mcp_server.record_decision(d["id"], "approved", "nope")
    changes.acknowledge(d["id"], "owner")
    with pytest.raises(ValueError):
        mcp_server.record_decision(d["id"], "approved", "yes")                      # acknowledged: final
    e = mcp_server.recommend_change("Pick pink", "why")
    mcp_server.record_decision(e["id"], "approved", "nope")
    mcp_server.record_decision(e["id"], "rejected", "nope")                         # may even change its mind
    other = resolve_session_token(issue_token("all", "z", project_id=sess["project_id"]))
    with pytest.raises(Exception):
        changes.record_session_decision(e["id"], "approved", "yes", "claude:other", token_id=other["id"])


def test_overseer_may_link_a_readable_task_in_another_project(project, mcp_caller, monkeypatch):
    from app import config, scope
    monkeypatch.setattr(config, "OVERSEER_PROJECT", "dash-ov")
    dash = repo.create_project("dash-ov", "Dash", "internal", "owner", short_name="dashx",
                               session_roles=["all"])
    other = repo.create_project("other-ov", "Other", "internal", "owner", short_name="otherov", session_roles=["all"])
    theirs = repo.create_task(other["id"], "theirs", "", None, "feature", [], {}, "owner")
    tok = issue_token("all", "dash", project_id=dash["id"])
    mcp_caller(tok)
    scope.check_links(resolve_session_token(tok), {"tasks": [theirs["id"]]})        # no raise
    c = mcp_server.recommend_change("Depends on theirs", "why", links={"tasks": [theirs["id"]]})
    assert c["links"]["tasks"] == [theirs["id"]]
    # a non-overseer session still can't
    mcp_caller(issue_token("all", "p", project_id=project["id"]))
    with pytest.raises(repo.NotFound):
        mcp_server.recommend_change("Leaky", "why", links={"tasks": [theirs["id"]]})


def test_prompt_retention_limits(sess, client):
    changes.note_owner_prompt(sess["id"], "x" * 2001)                                # never a decision quote
    with get_cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM owner_prompts")
        assert cur.fetchone()["n"] == 0
    h = {"Authorization": f"Bearer {sess['token']}"}
    assert client.post("/api/owner-prompt", json={"prompt": "y" * 2001}, headers=h).status_code == 422
    for i in range(205):
        changes.note_owner_prompt(sess["id"], f"prompt {i}")
    with get_cursor() as cur:
        cur.execute("SELECT count(*) AS n, min(prompt) AS lo FROM owner_prompts WHERE token_id = %s", (sess["id"],))
        r = cur.fetchone()
    assert r["n"] == 200
    with get_cursor() as cur:
        cur.execute("UPDATE owner_prompts SET created_at = now() - interval '2 hours'")
    changes.prune_owner_prompts()                                                    # the periodic sweep
    with get_cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM owner_prompts")
        assert cur.fetchone()["n"] == 0
