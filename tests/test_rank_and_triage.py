from app import repo
from app.auth import issue_token


def _mk(project, title, priority="should", stream="bug"):
    return repo.create_task(project["id"], title, "", priority, stream, [], {}, "owner")


def test_new_tasks_append_to_bottom_of_column(project):
    a = _mk(project, "First", priority="must")
    b = _mk(project, "Second", priority="must")
    ordered = [t["title"] for t in repo.list_tasks(project["id"], priority="must")]
    assert ordered == ["First", "Second"]
    assert b["rank"] > a["rank"]


def test_reorder_within_column(project):
    a = _mk(project, "A", priority="should")
    b = _mk(project, "B", priority="should")
    c = _mk(project, "C", priority="should")
    # move C to the top
    repo.reorder_task(c["id"], "should", None, "owner")
    ordered = [t["title"] for t in repo.list_tasks(project["id"], priority="should")]
    assert ordered == ["C", "A", "B"]
    # move A between C and B (after C)
    repo.reorder_task(a["id"], "should", c["id"], "owner")
    ordered = [t["title"] for t in repo.list_tasks(project["id"], priority="should")]
    assert ordered == ["C", "A", "B"]


def test_reorder_across_columns_changes_priority(project):
    a = _mk(project, "Move me", priority="could")
    repo.reorder_task(a["id"], "must", None, "owner")
    got = repo.get_task(a["id"])
    assert got["priority"] == "must"


def test_drag_reorder_enqueues_outbox_only_on_priority_change(project):
    a = _mk(project, "Same column reorder", priority="should")
    b = _mk(project, "Sibling", priority="should")
    # creating both tasks already enqueued their own "task_created" messages — clear those first so
    # this test only observes what the REORDER itself does.
    for item in repo.list_outbox("bugs", undelivered_only=True):
        repo.mark_outbox_delivered(item["id"])

    repo.reorder_task(b["id"], "should", None, "owner")  # same column — no priority change
    assert repo.list_outbox("bugs", undelivered_only=True) == []
    repo.reorder_task(a["id"], "must", None, "owner")  # crosses columns — priority changed
    assert len(repo.list_outbox("bugs", undelivered_only=True)) == 1


def test_suggest_triage_and_accept(project, mcp_caller):
    task = _mk(project, "Bug to re-prioritise", priority="could", stream="bug")
    token = issue_token("bugs", "test-bugs")
    from app import mcp_server

    mcp_caller(token)
    mcp_server.suggest_triage(task["id"], rationale="This blocks billing.", priority="must")

    got = repo.get_task(task["id"])
    assert got["triage_state"] == "suggested"
    assert got["suggestion"]["priority"] == "must"
    assert got["suggestion"]["rationale"] == "This blocks billing."

    accepted = repo.accept_triage(task["id"], "owner")
    assert accepted["priority"] == "must"
    assert accepted["triage_state"] == "accepted"


def test_override_triage_keeps_owners_values(project):
    task = _mk(project, "Keep my priority", priority="should")
    repo.suggest_triage(task["id"], "claude:tweaks", rationale="try must", priority="must")
    overridden = repo.override_triage(task["id"], "owner")
    assert overridden["priority"] == "should"  # unchanged
    assert overridden["triage_state"] == "overridden"


def test_discuss_triage_and_comment_notifies_session(project):
    task = _mk(project, "Let's talk", priority="should", stream="tweak")
    repo.suggest_triage(task["id"], "claude:tweaks", rationale="try could")
    discussed = repo.discuss_triage(task["id"], "owner")
    assert discussed["triage_state"] == "discussing"
    repo.comment_task(task["id"], "why do you think that?", "owner")
    pending = repo.list_outbox("tweaks", undelivered_only=True)
    assert pending[-1]["message"] == f"Owner commented on task #{task['id']} (should, tweak)"


def test_rank_hint_above_below_resolves_position(project):
    a = _mk(project, "A", priority="must")
    b = _mk(project, "B", priority="must")
    c = _mk(project, "C", priority="must")  # order: A, B, C
    task = _mk(project, "New one", priority="should")
    repo.suggest_triage(task["id"], "claude:bugs", rationale="urgent", priority="must",
                         rank_hint=f"above #{c['id']}")
    repo.accept_triage(task["id"], "owner")
    ordered = [t["title"] for t in repo.list_tasks(project["id"], priority="must")]
    assert ordered == ["A", "B", "New one", "C"]


# --- P3-10: accepting a priority-only suggestion lands at the BOTTOM of the new bucket ----------------

def _order(project, priority):
    return [t["title"] for t in repo.list_tasks(project["id"], priority=priority)]


def test_accept_priority_only_goes_to_bottom_of_new_bucket(project):
    _mk(project, "M1", priority="must")
    _mk(project, "M2", priority="must")
    task = _mk(project, "Promoted", priority="could")
    repo.suggest_triage(task["id"], "claude:bugs", rationale="r", priority="must")
    repo.accept_triage(task["id"], "owner")
    assert _order(project, "must") == ["M1", "M2", "Promoted"]


def test_accept_priority_with_unresolvable_hint_goes_to_bottom(project):
    _mk(project, "M1", priority="must")
    task = _mk(project, "Promoted", priority="could")
    repo.suggest_triage(task["id"], "claude:bugs", rationale="r", priority="must", rank_hint="above #99999")
    repo.accept_triage(task["id"], "owner")
    assert _order(project, "must") == ["M1", "Promoted"]


def test_accept_rank_hint_above_first_goes_to_top(project):
    m1 = _mk(project, "M1", priority="must")
    _mk(project, "M2", priority="must")
    task = _mk(project, "Promoted", priority="could")
    repo.suggest_triage(task["id"], "claude:bugs", rationale="r", priority="must", rank_hint=f"above #{m1['id']}")
    repo.accept_triage(task["id"], "owner")
    assert _order(project, "must") == ["Promoted", "M1", "M2"]


def test_accept_rank_hint_below_within_same_bucket(project):
    a = _mk(project, "A", priority="should")
    _mk(project, "B", priority="should")
    c = _mk(project, "C", priority="should")
    repo.suggest_triage(c["id"], "claude:bugs", rationale="r", rank_hint=f"below #{a['id']}")
    repo.accept_triage(c["id"], "owner")
    assert _order(project, "should") == ["A", "C", "B"]


def test_accept_stream_only_keeps_position(project):
    _mk(project, "A", priority="should")
    b = _mk(project, "B", priority="should")
    _mk(project, "C", priority="should")
    repo.suggest_triage(b["id"], "claude:bugs", rationale="r", stream="feature")
    got = repo.accept_triage(b["id"], "owner")
    assert got["stream"] == "feature" and got["owner_stream"] == "features"
    assert _order(project, "should") == ["A", "B", "C"]
