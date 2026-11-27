"""AI triage decides WHAT a request is: category (Rule / AI Skill / Workflow / …) + needed-now, alongside
bucket / rank / stream / assignee. The owner accepts (category stored on the task) or keeps theirs."""
import pytest

from app import repo
from app.repo import BadRequest


@pytest.fixture
def task(project):
    return repo.create_task(project["id"], "Ignore out-of-office replies", "", "should", "feature", [], {}, "owner",
                            assignee="owner")


def test_suggestion_carries_category_and_needed_now(task):
    row = repo.suggest_triage(task["id"], "claude:features", priority="could", category="rule",
                              needed_now="later", needed_reason="not until the Emails pill ships",
                              assignee="ai", rationale="it's an if-this-then-that on the triage step")
    s = row["suggestion"]
    assert (s["category"], s["needed_now"], s["assignee"]) == ("rule", "later", "ai")
    assert s["needed_reason"] == "not until the Emails pill ships"
    assert row["category"] is None  # nothing applied until the owner accepts


def test_accept_stores_category_and_assignee(task):
    repo.suggest_triage(task["id"], "claude:features", category="ai_skill", assignee="ai", rationale="r")
    before = len(repo.list_outbox("features"))
    row = repo.accept_triage(task["id"], "owner")
    assert row["category"] == "ai_skill" and row["assignee"] == "ai" and row["triage_state"] == "accepted"
    new = repo.list_outbox("features")[before:]
    assert [r["kind"] for r in new] == ["task_assigned"]


def test_keep_mine_stores_nothing(task):
    repo.suggest_triage(task["id"], "claude:features", category="ai_instruction", rationale="r")
    row = repo.override_triage(task["id"], "owner")
    assert row["category"] is None and row["triage_state"] == "overridden"


@pytest.mark.parametrize("kw", [{"category": "business_rule"}, {"category": "skill"}, {"category": "instruction"}, {"needed_now": "maybe"},
                                {"assignee": "bob"}, {"needed_reason": "x" * 501}])
def test_invalid_values_rejected(task, kw):
    with pytest.raises(BadRequest):
        repo.suggest_triage(task["id"], "claude:features", rationale="r", **kw)


def test_category_filter_and_owner_can_set_it(project, task):
    other = repo.create_task(project["id"], "untriaged one", "", "should", "bug", [], {}, "owner")
    repo.update_task(task["id"], {"category": "rule"}, "owner")
    assert [t["id"] for t in repo.list_tasks(project["id"], category="rule")] == [task["id"]]
    assert [t["id"] for t in repo.list_tasks(project["id"], category="untriaged")] == [other["id"]]
    with pytest.raises(BadRequest):
        repo.list_tasks(project["id"], category="nope")
    with pytest.raises(BadRequest):
        repo.update_task(task["id"], {"category": "business_rule"}, "owner")
    assert repo.update_task(task["id"], {"category": ""}, "owner")["category"] is None
