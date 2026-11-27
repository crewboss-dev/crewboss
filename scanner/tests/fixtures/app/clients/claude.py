"""Fixture mimicking the real app/clients/claude.py shape: a module-level system-prompt constant, a
model-resolution helper, a direct AI call, and a two-pass (Haiku->Sonnet) escalation through a private
`_once` helper — the two patterns discover/ai_erp.py has to handle."""
import os

from app import models as _models


def _model():
    return os.environ.get("ANTHROPIC_MODEL", "").strip() or _models.sonnet()


_SUGGEST_SYSTEM = (
    "You are a fixture system prompt used only by scanner tests. Respond with ONLY a JSON object: "
    '{"matches": []}.'
)


def suggest(identity: str, candidates: list) -> list:
    user = f"Identity: {identity}\nCandidates: {candidates}"
    import anthropic
    client = anthropic.Anthropic()
    resp = client.with_options(timeout=20.0).messages.create(
        model=_model(), max_tokens=512, system=_SUGGEST_SYSTEM,
        messages=[{"role": "user", "content": user}],
    )
    text = resp.content[0].text
    return _json_obj(text).get("matches", [])


def _json_obj(text):
    import json
    return json.loads(text)


def _triage_model():
    return os.environ.get("TRIAGE_MODEL", "").strip() or _models.haiku()


_TRIAGE_SYSTEM = "Fixture triage system prompt. Respond with ONLY JSON: {\"confidence\": <0..1>}."


def _triage_once(model, user):
    import anthropic
    client = anthropic.Anthropic()
    resp = client.with_options(timeout=20.0).messages.create(
        model=model, max_tokens=400, system=_TRIAGE_SYSTEM,
        messages=[{"role": "user", "content": user}],
    )
    text = resp.content[0].text
    return _json_obj(text)


def _escalate_model():
    return os.environ.get("ESCALATE_MODEL", "").strip() or _models.sonnet()


def _escalate_threshold():
    return float(os.environ.get("ESCALATE_CONFIDENCE", "0.7"))


def triage_alert(alert: dict, weather: dict | None = None) -> dict | None:
    user = (
        f"source: {alert.get('source')}\n"
        f"severity: {alert.get('severity')}\n"
        f"weather: {weather}"
    )
    first = _triage_once(_triage_model(), user)
    if first is not None and first.get("confidence", 0) >= _escalate_threshold():
        return first
    second = _triage_once(_escalate_model(), user)
    return second if second is not None else first


def fixture_ai_func(payload):
    """Fixture AI function reached only via app/helper.py, 1 hop from a route."""
    return None
