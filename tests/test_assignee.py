"""Tasks v2 — ASSIGNEE: owner | ai (the AI = the session that owns the task's stream)."""
import pytest

from app import repo
from app.repo import BadRequest, Forbidden


def _outbox(stream):
    return [(r["kind"], r["message"]) for r in repo.list_outbox(stream)]


def test_owner_created_task_defaults_to_ai_and_says_assigned(project):
    t = repo.create_task(project["id"], "Build it", "", "must", "bug", [], {}, "owner")
    assert t["assignee"] == "ai"
    assert repo.assignee_label(t) == "AI · bugs"
    assert _outbox("bugs") == [("task_assigned", f"Owner assigned task #{t['id']} to AI (must, bug)")]


def test_owner_created_task_for_themselves_is_task_created(project):
    t = repo.create_task(project["id"], "My job", "", "should", "tweak", [], {}, "owner", assignee="owner")
    assert repo.assignee_label(t) == "You"
    assert _outbox("tweaks") == [("task_created", f"Owner created task #{t['id']} (should, tweak)")]


def test_reassign_to_ai_is_task_assigned_and_back_to_owner_is_took_over(project):
    t = repo.create_task(project["id"], "x", "", "should", "feature", [], {}, "system", assignee="owner")
    repo.update_task(t["id"], {"assignee": "ai"}, "owner")
    repo.update_task(t["id"], {"assignee": "owner"}, "owner")
    assert _outbox("features") == [
        ("task_assigned", f"Owner assigned task #{t['id']} to AI (should, feature)"),
        ("task_update", f"Owner took over task #{t['id']} (should, feature)"),
    ]
    hist = repo.get_task(t["id"])["history"]
    assert hist[0]["verb"] == "assigned" and "assigned to You" in hist[0]["summary"]


def test_moving_an_ai_task_to_another_stream_assigns_that_session(project):
    t = repo.create_task(project["id"], "x", "", "should", "feature", [], {}, "system")
    repo.update_task(t["id"], {"stream": "bug"}, "owner")
    assert _outbox("bugs") == [("task_assigned", f"Owner assigned task #{t['id']} to AI (should, bug)")]


def test_other_owner_changes_keep_owner_verb_task(project):
    t = repo.create_task(project["id"], "x", "", "should", "review", [], {}, "system")
    repo.update_task(t["id"], {"status": "doing"}, "owner")
    assert _outbox("reviewer") == [("task_update", f"Owner started task #{t['id']} (should, review)")]
    repo.update_task(t["id"], {"title": "y", "status": "done"}, "owner")  # more than a status: "updated"
    assert _outbox("reviewer")[-1] == ("task_update", f"Owner updated task #{t['id']} (should, review)")


def test_session_actions_never_enqueue(project):
    t = repo.create_task(project["id"], "x", "", "should", "bug", [], {}, "claude:bugs", assignee="owner")
    repo.assign_task(t["id"], "ai", "claude:bugs")
    assert repo.list_outbox("bugs") == []


def test_session_may_assign_to_owner_anything_but_to_ai_only_its_own_stream(project):
    bug = repo.create_task(project["id"], "b", "", "should", "bug", [], {}, "system", assignee="owner")
    feat = repo.create_task(project["id"], "f", "", "should", "feature", [], {}, "system")
    assert repo.assign_task(feat["id"], "owner", "claude:bugs")["assignee"] == "owner"
    with pytest.raises(Forbidden):
        repo.assign_task(feat["id"], "ai", "claude:bugs")
    assert repo.assign_task(bug["id"], "ai", "claude:bugs")["assignee"] == "ai"


def test_invalid_assignee_rejected(project):
    with pytest.raises(BadRequest):
        repo.create_task(project["id"], "x", "", "should", "bug", [], {}, "owner", assignee="bob")
    t = repo.create_task(project["id"], "x", "", "should", "bug", [], {}, "owner")
    with pytest.raises(BadRequest):
        repo.update_task(t["id"], {"assignee": "claude:bugs"}, "owner")


def test_list_filter_by_assignee(project):
    repo.create_task(project["id"], "mine", "", "should", "bug", [], {}, "owner", assignee="owner")
    repo.create_task(project["id"], "theirs", "", "should", "bug", [], {}, "owner")
    assert [t["title"] for t in repo.list_tasks(project["id"], assignee="owner")] == ["mine"]
    assert [t["title"] for t in repo.list_tasks(project["id"], assignee="ai")] == ["theirs"]


def test_testing_handoff_lands_in_assigned_to_me_with_steps(project):
    t = repo.create_task(project["id"], "Test the Emails pill", "", "should", "review", [],
                         {"workflows": [{"id": "email.triage.classify", "step": "ai-classify"}]},
                         "claude:features", assignee="owner", status="testing",
                         subtasks=["Open a case", "Send a reply", "Check the thread"])
    mine = repo.list_tasks(project["id"], assignee="owner", with_progress=True)
    assert [m["id"] for m in mine] == [t["id"]]
    assert mine[0]["progress"] == {"done": 0, "total": 3, "proposals": 0}
    assert mine[0]["status"] == "review"  # the old word still works: testing -> review
    assert [s["created_by"] for s in repo.list_subtasks(t["id"])] == ["claude:features"] * 3
    assert repo.tasks_for_workflow(project["id"], "email.triage.classify", "ai-classify")[0]["id"] == t["id"]


def test_ground_lists_ai_tasks_first_then_awaiting_owner(project):
    o = repo.create_task(project["id"], "Owner job", "", "must", "bug", [], {}, "system", assignee="owner",
                         subtasks=["a", "b"])
    a = repo.create_task(project["id"], "AI job", "", "could", "bug", [], {}, "system", subtasks=["x"])
    g = repo.build_ground("bugs", project["slug"])
    assert [t["id"] for t in g["open_tasks"]] == [a["id"], o["id"]]
    assert [t["id"] for t in g["assigned_to_ai"]] == [a["id"]]
    assert g["assigned_to_ai"][0]["progress"]["total"] == 1
    text = repo.render_ground_text(g)
    assert text.index("ASSIGNED TO YOU (AI)") < text.index("AI job") < text.index("Waiting on the owner") \
        < text.index("Owner job")
    assert "0/2 subtasks" in text
