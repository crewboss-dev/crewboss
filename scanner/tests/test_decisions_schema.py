"""The control-flow rules in docs/catalog-schema.md ("Decisions, loops and scopes") as enforced by
scanner/lib/schema.py::validate_flow — every rule proven both ways (a clean flow passes, each breakage
is named)."""
import copy

from scanner.lib import schema


def _step(sid, kind="action", **kw):
    d = {"id": sid, "kind": kind, "label": sid, "system": "internal",
         "code_ref": {"file": "app/workers/fixture.py", "function": "run_once"}}
    d.update(kw)
    return d


def _wf(steps, edges, **overrides):
    wf = {
        "id": "billing.fixture-token.refresh", "name": "Billing › Fixture token › Refresh",
        "group": "Billing", "subproject": "ERP", "summary": "fixture",
        "trigger": {"type": "timer", "detail": "every 600s"}, "has_ai": False,
        "steps": steps, "edges": edges, "side_effects": [], "confidence": "annotated",
        "source_refs": [{"file": "app/workers/fixture.py"}],
    }
    wf.update(overrides)
    return wf


def _problems(wf):
    return schema.validate_catalog({"project": "p", "commit": "c", "generated_at": "g", "workflows": [wf]})


def _refresh_flow():
    """The billing.xero-token.refresh shape: a guard decision whose 'No' branch is empty, a nested
    three-way decision, and a shared rejoin (heartbeat)."""
    steps = [
        _step("timer", "trigger"),
        _step("load-token"),
        _step("check-expiry", "decision", question="Within 15 min of expiry?"),
        _step("call-xero", parent="check-expiry", branch="Yes"),
        _step("refresh-ok", "decision", question="Token back?", parent="check-expiry", branch="Yes"),
        _step("save-token", parent="refresh-ok", branch="OK"),
        _step("record-failure", parent="refresh-ok", branch="Error"),
        _step("heartbeat", "logic"),
    ]
    edges = [
        {"from": "timer", "to": "load-token"},
        {"from": "load-token", "to": "check-expiry"},
        {"from": "check-expiry", "to": "call-xero", "label": "Yes"},
        {"from": "check-expiry", "to": "heartbeat", "label": "No"},
        {"from": "call-xero", "to": "refresh-ok"},
        {"from": "refresh-ok", "to": "save-token", "label": "OK"},
        {"from": "refresh-ok", "to": "heartbeat", "label": "No token"},
        {"from": "refresh-ok", "to": "record-failure", "label": "Error"},
        {"from": "save-token", "to": "heartbeat"},
        {"from": "record-failure", "to": "heartbeat"},
    ]
    return _wf(steps, edges)


def _loop_flow():
    steps = [
        _step("timer", "trigger"),
        _step("each-account", "loop", detail="every dirty account"),
        _step("push", parent="each-account"),
        _step("ok", "decision", question="Accepted?", parent="each-account"),
        _step("stamp", parent="ok", branch="OK"),
        _step("leave", parent="ok", branch="Failed"),
        _step("heartbeat", "logic"),
    ]
    edges = [
        {"from": "timer", "to": "each-account"},
        {"from": "each-account", "to": "push"},
        {"from": "each-account", "to": "heartbeat"},
        {"from": "push", "to": "ok"},
        {"from": "ok", "to": "stamp", "label": "OK"},
        {"from": "ok", "to": "leave", "label": "Failed"},
    ]
    return _wf(steps, edges)


def _step_by_id(wf, sid):
    return next(s for s in wf["steps"] if s["id"] == sid)


# ---- clean flows ---------------------------------------------------------------------------------

def test_decision_flow_with_empty_branch_and_rejoin_is_clean():
    assert _problems(_refresh_flow()) == []


def test_loop_flow_with_nested_decision_is_clean():
    assert _problems(_loop_flow()) == []


def test_straight_run_without_edges_still_valid():
    wf = _wf([_step("timer", "trigger"), _step("a"), _step("b")], [])
    assert _problems(wf) == []


def test_old_partial_edges_without_a_decision_still_valid():
    """Back-compat: the pre-decision annotations only describe the branching part of their flow."""
    wf = _wf([_step("timer", "trigger"), _step("a"), _step("b"), _step("c")],
             [{"from": "b", "to": "c"}])
    assert _problems(wf) == []


def test_mid_flow_trigger_step_counts_as_an_entry():
    wf = _refresh_flow()
    wf["steps"].append(_step("approve", "trigger"))
    wf["steps"].append(_step("after-approve"))
    wf["edges"].append({"from": "approve", "to": "after-approve"})
    assert _problems(wf) == []


def test_first_step_is_the_entry_when_no_trigger_kind_step():
    wf = _refresh_flow()
    wf["steps"][0]["kind"] = "action"
    assert _problems(wf) == []


# ---- decisions -----------------------------------------------------------------------------------

def test_decision_needs_a_question():
    wf = _refresh_flow()
    del _step_by_id(wf, "check-expiry")["question"]
    assert any("needs a plain-words 'question'" in p for p in _problems(wf))


def test_decision_needs_two_labelled_edges():
    wf = _refresh_flow()
    wf["edges"] = [e for e in wf["edges"] if not (e["from"] == "check-expiry" and e["to"] == "heartbeat")]
    probs = _problems(wf)
    assert any("check-expiry' needs >= 2 labelled outgoing edges (has 1)" in p for p in probs)


def test_decision_edge_without_a_label_is_flagged():
    wf = _refresh_flow()
    for e in wf["edges"]:
        if e["from"] == "refresh-ok" and e["to"] == "heartbeat":
            del e["label"]
    assert any("refresh-ok' has an outgoing edge with no label" in p for p in _problems(wf))


def test_duplicate_branch_labels_are_flagged():
    wf = _refresh_flow()
    for e in wf["edges"]:
        if e["from"] == "check-expiry" and e["to"] == "heartbeat":
            e["label"] = "Yes"
    assert any("duplicate branch labels" in p for p in _problems(wf))


def test_edge_endpoint_must_exist():
    wf = _refresh_flow()
    wf["edges"].append({"from": "save-token", "to": "nowhere"})
    assert any("unknown step id" in p for p in _problems(wf))


def test_every_step_reachable_from_the_trigger_once_a_decision_exists():
    wf = _refresh_flow()
    wf["steps"].append(_step("orphan"))
    assert any("step 'orphan' is not reachable from the trigger" in p for p in _problems(wf))


def test_decision_without_any_edges_is_flagged():
    wf = _refresh_flow()
    wf["edges"] = []
    assert any("has a decision/loop but no 'edges'" in p for p in _problems(wf))


# ---- scopes --------------------------------------------------------------------------------------

def test_parent_must_exist():
    wf = _refresh_flow()
    _step_by_id(wf, "save-token")["parent"] = "ghost"
    assert any("parent 'ghost' which is not a step" in p for p in _problems(wf))


def test_parent_must_be_a_decision_or_loop():
    wf = _refresh_flow()
    _step_by_id(wf, "save-token")["parent"] = "load-token"
    assert any("a parent must be a decision or a loop" in p for p in _problems(wf))


def test_branch_required_inside_a_decision():
    wf = _refresh_flow()
    del _step_by_id(wf, "save-token")["branch"]
    assert any("is inside decision 'refresh-ok' but has no 'branch'" in p for p in _problems(wf))


def test_branch_must_match_an_edge_label():
    wf = _refresh_flow()
    _step_by_id(wf, "save-token")["branch"] = "Maybe"
    assert any("has branch 'Maybe' but decision 'refresh-ok' has no outgoing edge labelled that" in p
               for p in _problems(wf))


def test_branch_not_allowed_inside_a_loop():
    wf = _loop_flow()
    _step_by_id(wf, "push")["branch"] = "OK"
    assert any("is inside loop 'each-account' and must not carry a 'branch'" in p for p in _problems(wf))


def test_branch_without_parent_is_flagged():
    wf = _refresh_flow()
    _step_by_id(wf, "heartbeat")["branch"] = "OK"
    assert any("'branch' without a 'parent'" in p for p in _problems(wf))


def test_branch_step_must_be_reached_from_its_own_branch_edge():
    wf = _refresh_flow()
    # save-token claims the Error branch but only the OK edge leads to it
    _step_by_id(wf, "save-token")["branch"] = "Error"
    probs = _problems(wf)
    assert any("edge labelled 'OK' enters step 'save-token' which is in branch 'Error'" in p for p in probs)
    assert any("step 'save-token' (branch 'Error' of 'refresh-ok') is not reachable" in p for p in probs)


def test_parent_cycle_is_flagged():
    wf = _loop_flow()
    _step_by_id(wf, "each-account")["parent"] = "ok"
    _step_by_id(wf, "each-account")["branch"] = "OK"
    assert any("is a cycle" in p for p in _problems(wf))


def test_step_cannot_be_its_own_parent():
    wf = _loop_flow()
    _step_by_id(wf, "push")["parent"] = "push"
    assert any("is its own parent" in p for p in _problems(wf))


# ---- loops ---------------------------------------------------------------------------------------

def test_loop_needs_a_detail_naming_the_collection():
    wf = _loop_flow()
    _step_by_id(wf, "each-account")["detail"] = ""
    assert any("kind=loop needs a 'detail'" in p for p in _problems(wf))


def test_loop_needs_a_body():
    wf = _loop_flow()
    for s in wf["steps"]:
        if s.get("parent") == "each-account":
            del s["parent"]
    assert any("loop 'each-account' has no body" in p for p in _problems(wf))


def test_loop_needs_an_edge_into_its_body():
    wf = _loop_flow()
    wf["edges"] = [e for e in wf["edges"] if not (e["from"] == "each-account" and e["to"] == "push")]
    wf["edges"].append({"from": "timer", "to": "push"})
    probs = _problems(wf)
    assert any("loop 'each-account' has no edge into its body" in p for p in probs)
    assert any("(body of loop 'each-account') is not reachable from the loop" in p for p in probs)


def test_validation_does_not_mutate_the_workflow():
    wf = _refresh_flow()
    before = copy.deepcopy(wf)
    _problems(wf)
    assert wf == before
