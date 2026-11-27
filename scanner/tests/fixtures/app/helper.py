"""Fixture shared-helper module (mimics app/kb.py etc.): a route calls into this, and THIS calls the
AI function — the 2-hop chain discover/routes.py has to follow."""
from app.clients import claude


def do_the_ai_thing(payload):
    return claude.fixture_ai_func(payload)
