"""Discovery-logic tests against small fixture modules under scanner/tests/fixtures/ — mirrors the
REAL repo's shapes closely enough to prove the AST walking works, without needing the real repos."""
from pathlib import Path

from scanner.discover import ai_erp, decisions, webhooks, workers

FIXTURES = Path(__file__).parent / "fixtures"


def test_workers_discover_finds_fixture_worker():
    found = workers.discover(FIXTURES)
    names = {w["name"] for w in found}
    assert "fixture_worker" in names
    w = next(x for x in found if x["name"] == "fixture_worker")
    assert w["trigger_detail"] == "every 600s (env FIXTURE_WORKER_SECONDS)"
    assert w["gate_env"] == "FIXTURE_ENABLED"
    assert "xero" in w["systems"]
    assert "fixture_table" in w["db_tables"]
    assert w["entry_function"] == "run_once"


def test_webhooks_discover_finds_fixture_route():
    found = webhooks.discover(FIXTURES)
    paths = {w["path"] for w in found}
    assert "/webhook/fixture" in paths
    w = next(x for x in found if x["path"] == "/webhook/fixture")
    assert w["func"] == "fixture_webhook"
    assert "stripeclient" in w["systems"] or "stripe" in w["systems"]
    assert "fixture_invoices" in w["db_tables"]


def test_decisions_discover_finds_executor_inline_and_producer():
    found = decisions.discover(FIXTURES)
    by_kind = {}
    for r in found:
        by_kind.setdefault(r["kind"], []).append(r)
    assert any(r["dtype"] == "fixture.thing.execute" for r in by_kind.get("executor", []))
    assert any(r["dtype"] == "fixture.inline.apply" for r in by_kind.get("inline", []))
    assert any(r["dtype"] == "produce_fixture_decisions" for r in by_kind.get("producer", []))
    executor = next(r for r in by_kind["executor"] if r["dtype"] == "fixture.thing.execute")
    assert "fixture_decisions" in executor["db_tables"]


def test_ai_erp_discover_direct_call():
    found = ai_erp.discover(FIXTURES)
    by_func = {r["func"]: r for r in found}
    assert "suggest" in by_func
    ai = by_func["suggest"]["ai"]
    assert ai["prompt_name"] == "_SUGGEST_SYSTEM"
    assert "fixture system prompt" in ai["prompt_text"]
    assert ai["max_tokens"] == 512
    assert ai["output"] == "json-parsed-text"
    assert "identity" in ai["context_injected"]


def test_ai_erp_discover_two_pass_escalation():
    found = ai_erp.discover(FIXTURES)
    by_func = {r["func"]: r for r in found}
    assert "triage_alert" in by_func
    rec = by_func["triage_alert"]
    assert rec["escalates"] is True
    assert "Two-pass" in rec["ai"]["model_rule"]
    assert "haiku" in rec["ai"]["model_rule"]
    assert "sonnet" in rec["ai"]["model_rule"]
    # context comes from the RESOLVED `user` assignment, not the bare identifier
    assert "user" not in rec["ai"]["context_injected"]
    assert "alert source" in rec["ai"]["context_injected"]
