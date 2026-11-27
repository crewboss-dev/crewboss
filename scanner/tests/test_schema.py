from scanner.lib import schema


def _minimal_workflow(**overrides):
    wf = {
        "id": "platform.fixture-thing.run",
        "name": "Platform › Fixture thing › Run",
        "group": "Platform",
        "subproject": "ERP",
        "summary": "A fixture workflow.",
        "trigger": {"type": "timer", "detail": "every 60s"},
        "has_ai": False,
        "steps": [
            {"id": "trigger", "kind": "trigger", "label": "Timer", "system": "internal",
             "code_ref": {"file": "app/workers/fixture.py", "function": "main"}},
        ],
        "edges": [],
        "side_effects": [],
        "source_refs": [{"file": "app/workers/fixture.py", "lines": "1-10"}],
        "confidence": "auto",
    }
    wf.update(overrides)
    return wf


def _minimal_catalog(workflows):
    return {"project": "mallen-erp", "commit": "abc123", "generated_at": "2026-09-30T00:00:00Z",
            "workflows": workflows}


def test_valid_catalog_has_no_problems():
    problems = schema.validate_catalog(_minimal_catalog([_minimal_workflow()]))
    assert problems == []


def test_bad_id_is_flagged():
    wf = _minimal_workflow(id="NotLowercase")
    problems = schema.validate_catalog(_minimal_catalog([wf]))
    assert any("not lower-case group.thing.action" in p for p in problems)


def test_bad_group_is_flagged():
    wf = _minimal_workflow(group="Support")
    problems = schema.validate_catalog(_minimal_catalog([wf]))
    assert any("group" in p for p in problems)


def test_has_ai_without_ai_step_is_flagged():
    wf = _minimal_workflow(has_ai=True)
    problems = schema.validate_catalog(_minimal_catalog([wf]))
    assert any("has_ai=true but no step has kind='ai'" in p for p in problems)


def test_ai_step_without_has_ai_is_flagged():
    wf = _minimal_workflow()
    wf["steps"].append({"id": "ai", "kind": "ai", "label": "AI", "system": "claude",
                        "code_ref": {"file": "x.py", "function": "f"},
                        "ai": {"provider": "anthropic", "model_rule": "x",
                               "prompt_location": {"file": "x.py"}, "output": "text"}})
    problems = schema.validate_catalog(_minimal_catalog([wf]))
    assert any("an 'ai' step exists but has_ai is not true" in p for p in problems)


def test_duplicate_workflow_id_is_flagged():
    problems = schema.validate_catalog(_minimal_catalog([_minimal_workflow(), _minimal_workflow()]))
    assert any("duplicate workflow id" in p for p in problems)


def test_edge_referencing_unknown_step_is_flagged():
    wf = _minimal_workflow(edges=[{"from": "trigger", "to": "nowhere"}])
    problems = schema.validate_catalog(_minimal_catalog([wf]))
    assert any("unknown step id" in p for p in problems)


def test_privacy_sweep_flags_a_real_email():
    findings = schema.privacy_sweep('{"note": "contact jane.doe@example.com about this"}')
    assert any("jane.doe@example.com" in f for f in findings)


def test_privacy_sweep_allows_shared_mailbox_roles():
    findings = schema.privacy_sweep('{"note": "support@ handles this, accounts@ handles billing"}')
    assert findings == []


def test_privacy_sweep_flags_anthropic_key_shape():
    findings = schema.privacy_sweep('{"key": "sk-ant-api03-abcdefghijklmnop"}')
    assert any("Anthropic API key" in f for f in findings)
