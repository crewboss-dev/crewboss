"""AI Skills are derived from the catalog (grouped by the code that runs them); Rules & AI Instructions are a
read-only inventory from the catalog's `rules`, or — only when CREWBOSS_RULES_FIXTURE says so — the fixture."""
from pathlib import Path

import pytest

from app import repo, rules, skills

ROOT = Path(__file__).resolve().parent.parent


def _wf(wid, *steps, sub="ERP"):
    return {"id": wid, "name": wid.title(), "group": "Service", "subproject": sub, "summary": "", "trigger": {"type": "event"},
            "steps": list(steps), "side_effects": [], "source_refs": [], "has_ai": True, "confidence": "auto"}


def _ai(sid, fn, file="app/clients/claude.py", prompt="_P"):
    return {"id": sid, "kind": "ai", "label": f"AI: {fn}()", "code_ref": {"file": file, "function": fn},
            "ai": {"prompt_name": prompt, "model_rule": "sonnet", "output": "text", "max_tokens": 100}}


def test_one_skill_lists_every_workflow_that_uses_it():
    wfs = [_wf("service.case.draft", _ai("ai", "draft_email")), _wf("billing.invoice.send", _ai("draft", "draft_email")),
           _wf("sales.lead.reply", _ai("x", "draft_email"), sub="AiOS"), _wf("service.other", _ai("ai", "classify_intake"))]
    out = skills.derive_skills(wfs)
    assert [s["id"] for s in out] == ["draft-email", "classify-intake"]  # most-used first
    d = out[0]
    assert d["name"] == "Draft email" and d["subprojects"] == ["AiOS", "ERP"]
    assert [(u["workflow"], u["step"]) for u in d["used_by"]] == [
        ("service.case.draft", "ai"), ("billing.invoice.send", "draft"), ("sales.lead.reply", "x")]


def test_same_function_name_in_two_files_is_two_skills():
    out = skills.derive_skills([_wf("a.b.c", _ai("ai", "classify", file="app/x.py")),
                                _wf("d.e.f", _ai("ai", "classify", file="aios/y.py"))])
    assert sorted(s["id"] for s in out) == ["classify-x", "classify-y"]


def test_non_ai_steps_are_not_skills_and_prompt_name_is_the_fallback():
    out = skills.derive_skills([_wf("a.b.c", {"id": "s", "kind": "action", "label": "x"},
                                    {"id": "ai", "kind": "ai", "label": "AI", "ai": {"prompt_name": "_TRIAGE_SYSTEM"}})])
    assert [s["name"] for s in out] == ["Triage system"]


def test_rules_origin(project, monkeypatch):
    monkeypatch.delenv("CREWBOSS_RULES_FIXTURE", raising=False)
    assert rules.all_rules(project["id"]) == ([], "none")
    monkeypatch.setenv("CREWBOSS_RULES_FIXTURE", str(ROOT / "fixtures/rules-fixture.json"))
    got, origin = rules.all_rules(project["id"])
    assert origin == "fixture" and {r["kind"] for r in got} == {"rule", "instruction", "builtin"}
    # a catalog that reports rules wins over the fixture — and malformed rows are dropped
    repo.import_catalog(project["slug"], {"commit": "c", "generated_at": "2026-09-30T00:00:00Z", "workflows": [],
                                          "rules": [{"id": "x.y", "name": "Real", "kind": "rule"},
                                                    {"id": "BAD ID", "kind": "rule"}, {"id": "z", "kind": "skill"}]})
    got, origin = rules.all_rules(project["id"])
    assert origin == "catalog" and [r["name"] for r in got] == ["Real"] and got[0]["hits"] is None


def test_step_counts_enabled_only(project, monkeypatch):
    monkeypatch.setenv("CREWBOSS_RULES_FIXTURE", str(ROOT / "fixtures/rules-fixture.json"))
    assert rules.step_counts(project["id"], "email.triage.classify") == {
        "heuristic-gate": {"rules": 3, "instructions": 0}, "ai-classify": {"rules": 0, "instructions": 2}}
    assert rules.step_counts(project["id"], "documents.field-detect.classify") == {}  # its one is turned off
    with pytest.raises(repo.NotFound):
        rules.get_rule(project["id"], "nope")
