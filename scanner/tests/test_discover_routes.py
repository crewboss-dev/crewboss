"""Tests discover/routes.py's 2-hop call-graph BFS against small fixtures: a route (fixture_ai_route.py)
calls a shared helper (helper.py) which calls the AI function (claude.fixture_ai_func) — proving the
"route -> app.kb.draft() -> claude.draft_kb_article()" shape works, not just a direct call."""
from pathlib import Path

from scanner.discover import routes

FIXTURES = Path(__file__).parent / "fixtures"


def test_resolves_ai_function_two_hops_from_route():
    found = routes.find_routes_for({"fixture_ai_func"}, FIXTURES)
    assert "fixture_ai_func" in found
    r = found["fixture_ai_func"]
    assert r["method"] == "GET"
    assert r["path"] == "/fixture/ai-thing"
    assert r["route_func"] == "fixture_ai_page"
    assert r["route_file"] == "app/routes/fixture_ai_route.py"
    # the AI call itself lives in the HELPER, not the route — hop info should say so.
    assert r["hop_func"] == "do_the_ai_thing"


def test_unresolvable_function_is_absent():
    found = routes.find_routes_for({"no_such_ai_function"}, FIXTURES)
    assert found == {}
