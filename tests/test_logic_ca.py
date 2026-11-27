"""The CA's logic_map tool (task #715): read-only, scoped to the projects the person can see, and its text is returned as data."""
import pytest

from app import ca_mcp, ca_tools, logic
from app.db import get_cursor

from test_ca import _as_turn, _running_turn, cw, _ca_tables_clean  # noqa: F401  (fixtures/helpers)
from test_devices import est, signed_in  # noqa: F401
from test_web_auth import auth_on  # noqa: F401

SHA = "a" * 40


def _report():
    wf = {"id": "quote.won", "name": "Quote won", "summary": "Closing a quote as won.",
          "trigger": {"type": "button", "detail": "", "control": "Quote page: Won", "route": ""},
          "steps": [{"id": "save", "kind": "action", "label": "Mark won", "detail": "", "data": ["table:quotes"],
                     "effects": ["create:work_orders"]}], "edges": [], "data": [], "covers": []}
    return {"version": 1, "workflows": [wf], "errors": [], "stale": [], "uncovered": [{"key": "k", "kind": "route"}], "discovered": 1}


def _counts():
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT (SELECT count(*) FROM logic_reports) AS r, (SELECT count(*) FROM logic_overrides) AS o, "
                    "(SELECT count(*) FROM activity) AS a, (SELECT count(*) FROM ca_cards) AS c")
        return dict(cur.fetchone())


def test_logic_map_returns_the_slim_map_of_a_visible_project_and_writes_nothing(cw, monkeypatch):
    logic.record_report(cw["acme_app"]["id"], SHA, _report(), "ci")
    conv, turn = _running_turn(cw)
    _as_turn(monkeypatch, turn["turn_token"])
    before = _counts()
    out = ca_mcp.logic_map("acme-app")
    assert out["reported"] and out["untrusted"] is True and out["commit"] == SHA and out["uncovered"] == 1
    step = out["workflows"][0]["steps"][0]
    assert out["workflows"][0]["trigger"]["control"] == "Quote page: Won" and step["effects"] == ["create:work_orders"]
    assert "code_ref" not in step
    assert ca_mcp.logic_map("Acme App")["project"] == "acme-app"                          # by name too
    assert _counts() == before                                                            # read-only: no row anywhere changed


def test_logic_map_of_a_project_with_no_report_says_so(cw, monkeypatch):
    conv, turn = _running_turn(cw)
    _as_turn(monkeypatch, turn["turn_token"])
    out = ca_mcp.logic_map("acme-app")
    assert out["reported"] is False and "no logic map" in out["note"]


def test_logic_map_is_access_checked_another_accounts_project_is_not_found(cw, monkeypatch):
    logic.record_report(cw["beta_app"]["id"], SHA, _report(), "ci")
    conv, turn = _running_turn(cw)
    _as_turn(monkeypatch, turn["turn_token"])
    with pytest.raises(ValueError, match="not found"):
        ca_mcp.logic_map("beta-app")
    with pytest.raises(ValueError, match="not found"):
        ca_mcp.logic_map("")
    _as_turn(monkeypatch, None)
    with pytest.raises(PermissionError):                                                  # no turn token, no map
        ca_mcp.logic_map("acme-app")


def test_logic_map_is_a_registered_read_tool_and_not_a_propose_tool():
    assert "logic_map" in ca_mcp.TOOLS and callable(ca_tools.logic_map)
    assert not [t for t in ca_mcp.TOOLS if t == "logic_map" and t.startswith("propose_")]
