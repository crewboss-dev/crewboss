from scanner.lib import diff as difflib


def _wf(wid="platform.fixture.run", **overrides):
    wf = {
        "id": wid, "name": "Platform › Fixture › Run", "group": "Platform", "subproject": "ERP",
        "summary": "fixture", "trigger": {"type": "timer", "detail": "every 60s"},
        "steps": [
            {"id": "trigger", "kind": "trigger", "label": "Timer", "system": "internal",
             "code_ref": {"file": "x.py", "function": "main"}},
            {"id": "ai", "kind": "ai", "label": "AI", "system": "claude",
             "code_ref": {"file": "claude.py", "function": "f"},
             "ai": {"provider": "anthropic", "model_rule": "sonnet", "prompt_text": "v1",
                    "prompt_location": {"file": "claude.py"}, "max_tokens": 100, "output": "text"}},
        ],
        "confidence": "auto",
    }
    wf.update(overrides)
    return wf


def _catalog(workflows, commit="sha1"):
    return {"project": "mallen-erp", "commit": commit, "generated_at": "t", "workflows": workflows}


def test_added_and_removed_workflows():
    old = _catalog([_wf("a.b.c")])
    new = _catalog([_wf("d.e.f")])
    d = difflib.diff_catalogs(old, new)
    assert d["added"] == ["d.e.f"]
    assert d["removed"] == ["a.b.c"]
    assert d["changed"] == {}


def test_unchanged_workflow_produces_no_diff():
    wf = _wf()
    d = difflib.diff_catalogs(_catalog([wf]), _catalog([dict(wf)]))
    assert d["changed"] == {}


def test_prompt_text_change_is_reported():
    old_wf = _wf()
    new_wf = _wf()
    new_wf["steps"][1]["ai"]["prompt_text"] = "v2"
    d = difflib.diff_catalogs(_catalog([old_wf]), _catalog([new_wf]))
    reasons = d["changed"][old_wf["id"]]["steps_changed"]["ai"]
    assert any("prompt_text changed" in r for r in reasons)


def test_step_added_and_removed():
    old_wf = _wf()
    new_wf = _wf()
    new_wf["steps"].append({"id": "extra", "kind": "action", "label": "Extra", "system": "db",
                            "code_ref": {"file": "x.py", "function": "main"}})
    d = difflib.diff_catalogs(_catalog([old_wf]), _catalog([new_wf]))
    assert d["changed"][old_wf["id"]]["steps_added"] == ["extra"]

    d2 = difflib.diff_catalogs(_catalog([new_wf]), _catalog([old_wf]))
    assert d2["changed"][old_wf["id"]]["steps_removed"] == ["extra"]


def test_field_change_is_reported():
    old_wf = _wf()
    new_wf = _wf(group="Sales")
    d = difflib.diff_catalogs(_catalog([old_wf]), _catalog([new_wf]))
    assert any("group:" in f for f in d["changed"][old_wf["id"]]["fields"])


def test_render_report_is_readable_text():
    old = _catalog([_wf("a.b.c")])
    new = _catalog([_wf("d.e.f")])
    report = difflib.render_report(difflib.diff_catalogs(old, new), "sha1", "sha2")
    assert "+ d.e.f" in report
    assert "- a.b.c" in report


def test_decision_fields_and_edge_changes_are_reported():
    from scanner.lib import diff as difflib
    base = {"id": "billing.x.refresh", "name": "Billing › X › Refresh", "steps": [
        {"id": "check", "kind": "decision", "label": "Near?", "question": "Near expiry?"},
        {"id": "call", "kind": "action", "label": "Call", "parent": "check", "branch": "Yes"}],
        "edges": [{"from": "check", "to": "call", "label": "Yes"}]}
    import copy
    new = copy.deepcopy(base)
    new["steps"][0]["question"] = "Within 15 min of expiry?"
    new["steps"][1]["branch"] = "Refresh"
    new["edges"][0]["label"] = "Refresh"
    d = difflib.diff_workflow(base, new)
    assert any("question:" in r for r in d["steps_changed"]["check"])
    assert any("branch:" in r for r in d["steps_changed"]["call"])
    assert any(f.startswith("edges: +check->call [Refresh]") for f in d["fields"])
