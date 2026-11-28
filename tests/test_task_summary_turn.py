"""#91: the task summary is written by a `summarise` turn on the viewer's own runner (the server's half; the runner's half is in tests/test_ca_runner.py).
Queued only for someone who can see the task, one at a time, rate limited, the facts scrubbed, the reply validated, and the extractive text kept as the
fallback (labelled "Auto summary")."""
import json
import time

import pytest

from app import ca, repo, runner_ws, task_summary
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


@pytest.fixture
def online(people):
    dev = people["bob_dev"]["device_id"]
    runner_ws._conns[dev] = type("C", (), {"verified": True, "post": lambda self, i: None})()
    yield people
    runner_ws._conns.pop(dev, None)


def _t(cw):
    return cw["task"]


def _turns():
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT * FROM ca_turns WHERE kind = 'summarise' ORDER BY id")
        return [dict(r) for r in cur.fetchall()]


def _clear_limits():
    with get_cursor() as cur:
        cur.execute("UPDATE ca_turns SET created_at = now() - interval '1 hour' WHERE kind = 'summarise'")


def test_a_stale_summary_queues_one_turn_on_the_viewers_own_runner(online):
    cw = online
    t = _t(cw)
    assert task_summary.refresh(t["id"], cw["bob"]["id"]) == "queued"
    (row,) = _turns()
    assert row["state"] == "queued" and row["user_id"] == cw["bob"]["id"] and row["summary_task_id"] == t["id"] and row["conversation_id"] is None
    assert row["model"] == "haiku" and "=== DATA (untrusted) ===" in row["summary_input"]
    assert task_summary.view(t["id"])["lines"] is None                     # nothing is written until the runner answers
    assert task_summary.refresh(t["id"], cw["bob"]["id"]) == "busy"        # one live turn per task
    assert len(_turns()) == 1


def test_only_the_viewers_runner_claims_it_with_no_token_no_tools_and_the_facts(online):
    cw = online
    t = _t(cw)
    task_summary.refresh(t["id"], cw["bob"]["id"])
    assert ca.claim_turn(cw["alice_dev"]["device_id"]) is None             # another person's runner gets nothing
    claim = ca.claim_turn(cw["bob_dev"]["device_id"])
    assert claim["kind"] == "summarise" and claim["model"] == "haiku" and claim["max_seconds"] == 60 and claim["conversation_id"] is None
    assert "turn_token" not in claim and "mcp" not in json.dumps(claim).lower() and "tools" not in claim
    assert "ONE JSON object" in claim["system"] and "never instructions" in claim["system"] and "Fix the thing" in claim["input"]
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT token_hash FROM ca_turns WHERE id = %s", (claim["turn_id"],))
        assert cur.fetchone()["token_hash"] is None


def test_the_facts_are_scrubbed_and_cut(online):
    cw = online
    t = _t(cw)
    secret = "AK" + "IA" + "ABCDEFGHIJKLMNOP"
    for i in range(7):
        repo.comment_task(t["id"], f"note {i} password=hunter2hunter2 {secret} " + "x" * 900, "claude:features")
    tok = "abcdef01" * 4                                                      # built here so no literal token sits in the file
    repo.ask_owner(t["id"], f"Which one? token={tok}", "We need to pick a provider for the wallets", None, "claude:features")
    facts = task_summary.facts_for_model(t["id"])
    assert "hunter2hunter2" not in facts and secret not in facts
    assert "note 6" in facts and "note 2" not in facts and "note 1 " not in facts          # the latest five only
    assert facts.count("- claude:features:") == 5 and all(len(l) < 560 for l in facts.splitlines() if l.startswith("- claude"))
    assert "OPEN QUESTIONS FOR THE OWNER" in facts and tok not in facts
    assert facts.startswith("=== DATA (untrusted)") and facts.endswith("=== END DATA ===")


def test_a_valid_reply_is_stored_as_written_by_the_ai(online):
    cw = online
    t = _t(cw)
    task_summary.refresh(t["id"], cw["bob"]["id"])
    claim = ca.claim_turn(cw["bob_dev"]["device_id"])
    reply = '```json\n{"what": "Fix the thing", "done": "Nothing yet", "left": "' + "word " * 80 + 'password=hunter2hunter2"}\n```'
    ca.turn_event(cw["bob_dev"]["device_id"], claim["turn_id"], claim["fence"], "delta", reply, seq=1)
    ca.finish_turn(cw["bob_dev"]["device_id"], claim["turn_id"], claim["fence"], True)
    v = task_summary.view(t["id"])
    assert v["source"] == "ai" and v["ai_kind"] == "ai" and not v["pending"]
    assert v["lines"]["what"] == "Fix the thing" and len(v["lines"]["left"]) <= 160 and "hunter2hunter2" not in str(v["lines"])
    assert _turns()[0]["state"] == "done"


@pytest.mark.parametrize("reply", ["I can't do that.", "{not json", '{"what": "a", "done": "b"}', '{"what": "a", "done": ["x"], "left": "c"}',
                                   '{"what": "a", "done": "", "left": "c"}', '[]', ""])
def test_an_invalid_reply_falls_back_to_the_auto_summary(online, reply):
    cw = online
    t = _t(cw)
    task_summary.refresh(t["id"], cw["bob"]["id"])
    claim = ca.claim_turn(cw["bob_dev"]["device_id"])
    ca.turn_event(cw["bob_dev"]["device_id"], claim["turn_id"], claim["fence"], "delta", reply, seq=1)
    ca.finish_turn(cw["bob_dev"]["device_id"], claim["turn_id"], claim["fence"], True)
    v = task_summary.view(t["id"])
    assert v["source"] == "ai" and v["ai_kind"] == "auto" and v["lines"]["what"]


def test_a_failed_turn_and_an_expired_turn_use_the_auto_summary(online):
    cw = online
    t = _t(cw)
    task_summary.refresh(t["id"], cw["bob"]["id"])
    claim = ca.claim_turn(cw["bob_dev"]["device_id"])
    ca.finish_turn(cw["bob_dev"]["device_id"], claim["turn_id"], claim["fence"], False, error="claude not installed")
    assert task_summary.view(t["id"])["ai_kind"] == "auto"
    # an unclaimed turn expires into the auto summary too (no chat message is made: there is no chat)
    with get_cursor() as cur:
        cur.execute("UPDATE tasks SET summary_ai = NULL, summary_updated_at = NULL WHERE id = %s", (t["id"],))
    _clear_limits()
    task_summary.refresh(t["id"], cw["bob"]["id"])
    with get_cursor() as cur:
        cur.execute("UPDATE ca_turns SET created_at = now() - interval '1 hour' WHERE state = 'queued' AND kind = 'summarise'")
    assert ca.expire_turns()["failed"] == 1
    assert task_summary.view(t["id"])["ai_kind"] == "auto" and not [r for r in _turns() if r["state"] == "queued"]
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT count(*) AS n FROM ca_messages")
        assert cur.fetchone()["n"] == 0


def test_no_runner_online_means_the_auto_summary_at_once(people):
    cw = people                                                            # nobody is connected
    t = _t(cw)
    assert task_summary.refresh(t["id"], cw["bob"]["id"]) == "auto"
    assert not _turns() and task_summary.view(t["id"])["ai_kind"] == "auto"
    assert task_summary.refresh(t["id"], None) == "busy"                    # (nothing new happened: nothing to do)


def test_a_person_who_cannot_see_the_task_never_triggers_a_turn(online):
    cw = online
    dave = _user("dddddddd-0000-4000-8000-000000000004", "Dave")           # a member of nothing
    with get_cursor() as cur:
        cur.execute("UPDATE users SET role = 'designer', is_owner = false WHERE id = %s", (dave["id"],))
    assert task_summary.request_ai(_t(cw)["id"], dave["id"]) == "none" and not _turns()
    assert task_summary.request_ai(_t(cw)["id"], None) == "none"


def test_rate_limits_one_per_task_per_two_minutes_and_one_per_person_per_minute(online):
    cw = online
    t = _t(cw)
    assert task_summary.request_ai(t["id"], cw["bob"]["id"]) == "queued"
    claim = ca.claim_turn(cw["bob_dev"]["device_id"])
    ca.finish_turn(cw["bob_dev"]["device_id"], claim["turn_id"], claim["fence"], False)
    assert task_summary.request_ai(t["id"], cw["bob"]["id"]) == "limited"      # same task, within 2 minutes
    other = repo.create_task(cw["acme_app"]["id"], "Another", "b", "should", "bug", [], {}, "owner")
    assert task_summary.request_ai(other["id"], cw["bob"]["id"]) == "limited"  # same person, within a minute
    with get_cursor() as cur:
        cur.execute("UPDATE ca_turns SET created_at = now() - interval '90 seconds' WHERE kind = 'summarise'")
    assert task_summary.request_ai(other["id"], cw["bob"]["id"]) == "queued"   # the person's minute has passed
    assert task_summary.request_ai(t["id"], cw["bob"]["id"]) in ("limited", "busy")


def test_a_limited_refresh_keeps_the_stale_text_but_never_leaves_the_box_empty(online):
    cw = online
    t = _t(cw)
    task_summary.request_ai(t["id"], cw["bob"]["id"])
    claim = ca.claim_turn(cw["bob_dev"]["device_id"])
    ca.finish_turn(cw["bob_dev"]["device_id"], claim["turn_id"], claim["fence"], False)     # -> auto summary stored
    first = task_summary.view(t["id"])["lines"]
    time.sleep(0.05)
    repo.comment_task(t["id"], "news", "claude:features")
    assert task_summary.refresh(t["id"], cw["bob"]["id"]) == "limited" and task_summary.view(t["id"])["lines"] == first
    other = repo.create_task(cw["acme_app"]["id"], "Fresh", "b", "should", "bug", [], {}, "owner")
    assert task_summary.refresh(other["id"], cw["bob"]["id"]) == "limited"  # hmm: the person's minute; but a task with NO summary still gets one
    assert task_summary.view(other["id"])["lines"] is not None


def test_the_allocated_runner_is_the_only_one_that_may_claim(online):
    cw = online
    app_id = cw["acme_app"]["id"]
    bob2, _key, _ = __import__("test_runner_ws")._enrol(cw, user=cw["bob"], name="bob-pc-2")
    runner_ws._conns[bob2["device_id"]] = type("C", (), {"verified": True, "post": lambda self, i: None})()
    try:
        with get_cursor() as cur:
            cur.execute("INSERT INTO project_runners (project_id, device_id, added_by) VALUES (%s, %s, 'test')", (app_id, bob2["device_id"]))
        assert task_summary.request_ai(_t(cw)["id"], cw["bob"]["id"]) == "queued"
        assert ca.claim_turn(cw["bob_dev"]["device_id"]) is None            # his other, unallocated runner does not take it
        assert ca.claim_turn(bob2["device_id"])["kind"] == "summarise"
    finally:
        runner_ws._conns.pop(bob2["device_id"], None)


def test_the_drawer_labels_it_honestly(online, client):
    cw = online
    t = _t(cw)
    task_summary.store_ai(t["id"], {"what": "w", "done": "d", "left": "l"}, "auto")
    html = task_summary.view(t["id"])
    assert html["ai_kind"] == "auto"
    from app.templating import templates
    for kind, label in (("auto", "Auto summary"), ("ai", "Written by the AI")):
        task_summary.store_ai(t["id"], {"what": "w", "done": "d", "left": "l"}, kind)
        out = templates.get_template("partials/task_summary.html").render(task=repo.get_task(t["id"]), base="/acme-app", summary=task_summary.view(t["id"]))
        assert label in out and (("Written by the AI" in out) == (kind == "ai"))


def test_parse_model_output_is_strict():
    ok = task_summary.parse_model_output('Here: {"what": "A", "done": "B", "left": "C"}')
    assert ok == {"what": "A", "done": "B", "left": "C"}
    assert task_summary.parse_model_output('{"what": "A", "done": "B", "left": "C", "extra": {"x": 1}}') == ok     # extra keys are ignored, not stored
    assert task_summary.parse_model_output('{"what": "a\\nb", "done": "B", "left": "C"}')["what"] == "a b"          # folded to one line
