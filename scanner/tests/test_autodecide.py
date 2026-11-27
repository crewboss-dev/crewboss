"""Auto-derived decisions (scanner/lib/autodecide.py) on small fixture modules: what becomes a
decision card, what deliberately does not, the depth cap, and the build wiring (flag on/off, annotated
workflows untouched, a bad derivation rolled back)."""
import ast
import textwrap
from pathlib import Path

from scanner import build
from scanner.lib import autodecide, schema


def _fn(src: str, name: str = "handler"):
    mod = ast.parse(textwrap.dedent(src))
    fns = {n.name: n for n in mod.body if isinstance(n, ast.FunctionDef)}
    return fns[name], fns


def _wf(step_ids, heartbeat=False):
    steps = [{"id": "trigger", "kind": "trigger", "label": "Approve", "detail": "", "system": "internal",
              "code_ref": {"file": "app/decisions.py", "function": "handler"}}]
    for sid in step_ids:
        steps.append({"id": sid, "kind": "ai" if sid == "ai" else "action", "label": sid, "detail": "",
                      "system": "db", "code_ref": {"file": "app/decisions.py", "function": "handler"},
                      **({"ai": {"provider": "anthropic", "model_rule": "x", "prompt_location": {"file": "x"},
                                 "output": "text"}} if sid == "ai" else {})})
    if heartbeat:
        steps.append({"id": "heartbeat", "kind": "logic", "label": "Heartbeat", "detail": "", "system": "db",
                      "code_ref": {"file": "app/decisions.py", "function": "handler"}})
    return {"id": "platform.fixture-thing.execute", "name": "Platform › Fixture thing › Execute",
            "group": "Platform", "subproject": "ERP", "summary": "fixture",
            "trigger": {"type": "button", "detail": "x"}, "has_ai": "ai" in step_ids, "steps": steps,
            "edges": [], "side_effects": [], "source_refs": [{"file": "app/decisions.py"}], "confidence": "auto"}


def _apply(src, step_ids, name="handler", heartbeat=False):
    fn, fns = _fn(src, name)
    wf = _wf(step_ids, heartbeat)
    n = autodecide.apply(wf, fn, file="app/decisions.py", func_name=name, module_functions=fns)
    return n, wf


def _problems(wf):
    return schema.validate_catalog({"project": "p", "commit": "c", "generated_at": "g", "workflows": [wf]})


def _decisions(wf):
    return [s for s in wf["steps"] if s["kind"] == "decision"]


def _out(wf, sid):
    return {e.get("label"): e["to"] for e in wf["edges"] if e["from"] == sid}


BILLING_MODE = '''
def handler(cur, account):
    cur.execute("SELECT billing_mode FROM accounts WHERE id=%s", (account,))
    billing_mode = cur.fetchone()[0]
    if billing_mode == 'suppress':
        cur.execute("INSERT INTO suppressed_log (account) VALUES (%s)", (account,))
    elif billing_mode == 'redirect':
        cur.execute("UPDATE payer_links SET used=true WHERE account=%s", (account,))
    else:
        cur.execute("INSERT INTO invoices (account) VALUES (%s)", (account,))
    cur.execute("INSERT INTO audit (account) VALUES (%s)", (account,))
'''


def test_if_elif_else_between_different_writes_becomes_one_decision():
    n, wf = _apply(BILLING_MODE, ["write-suppressed_log", "write-payer_links", "write-invoices", "write-audit"])
    assert n == 1
    [d] = _decisions(wf)
    assert d["question"] == "billing_mode == 'suppress'?"
    assert d["label"] == "Condition (line 5)"
    assert "auto-derived" in d["detail"]
    assert d["code_ref"] == {"file": "app/decisions.py", "lines": "5-10", "function": "handler"}
    assert _out(wf, d["id"]) == {"if billing_mode == 'suppress'": "write-suppressed_log",
                                 "elif billing_mode == 'redirect'": "write-payer_links",
                                 "else": "write-invoices"}
    steps = {s["id"]: s for s in wf["steps"]}
    assert steps["write-invoices"]["parent"] == d["id"] and steps["write-invoices"]["branch"] == "else"
    assert "parent" not in steps["write-audit"]                     # the rejoin
    for sid in ("write-suppressed_log", "write-payer_links", "write-invoices"):
        assert {"from": sid, "to": "write-audit"} in wf["edges"]
    assert {"from": "trigger", "to": d["id"]} in wf["edges"]
    assert _problems(wf) == []


def test_guard_clause_is_not_a_decision():
    n, wf = _apply('''
        def handler(cur, x):
            if not x:
                return None
            cur.execute("INSERT INTO a (x) VALUES (%s)", (x,))
            cur.execute("UPDATE b SET y=1")
    ''', ["write-a", "write-b"])
    assert n == 0 and wf["edges"] == [] and not _decisions(wf)


def test_branches_sharing_the_same_steps_are_not_a_decision():
    n, wf = _apply('''
        def handler(cur, x):
            if x:
                cur.execute("INSERT INTO a (x) VALUES (1)")
            else:
                cur.execute("INSERT INTO a (x) VALUES (2)")
    ''', ["write-a"])
    assert n == 0


def test_step_in_both_branches_is_hoisted_to_the_rejoin():
    n, wf = _apply('''
        def handler(cur, x):
            if x:
                cur.execute("INSERT INTO a (x) VALUES (1)")
                cur.execute("INSERT INTO shared (x) VALUES (1)")
            else:
                cur.execute("INSERT INTO b (x) VALUES (2)")
                cur.execute("INSERT INTO shared (x) VALUES (2)")
    ''', ["write-a", "write-b", "write-shared"])
    assert n == 1
    steps = {s["id"]: s for s in wf["steps"]}
    assert "parent" not in steps["write-shared"]
    assert {"from": "write-a", "to": "write-shared"} in wf["edges"]
    assert {"from": "write-b", "to": "write-shared"} in wf["edges"]
    assert _problems(wf) == []


def test_one_empty_branch_goes_straight_to_the_rejoin():
    n, wf = _apply('''
        def handler(cur, kind):
            if kind == "a":
                cur.execute("INSERT INTO a VALUES (1)")
            elif kind == "b":
                cur.execute("INSERT INTO b VALUES (1)")
            else:
                pass
            cur.execute("UPDATE tail SET n=n+1")
    ''', ["write-a", "write-b", "write-tail"])
    assert n == 1
    [d] = _decisions(wf)
    assert _out(wf, d["id"])["else"] == "write-tail"
    assert _problems(wf) == []


def test_match_statement_becomes_a_decision():
    n, wf = _apply('''
        def handler(cur, mode):
            match mode:
                case "suppress":
                    cur.execute("INSERT INTO a VALUES (1)")
                case "redirect" | "forward":
                    cur.execute("INSERT INTO b VALUES (1)")
                case _:
                    cur.execute("INSERT INTO c VALUES (1)")
    ''', ["write-a", "write-b", "write-c"])
    assert n == 1
    [d] = _decisions(wf)
    assert d["question"] == "match mode?"
    assert set(_out(wf, d["id"])) == {"case 'suppress'", "case 'redirect' | 'forward'", "case _"}
    assert _problems(wf) == []


def test_client_calls_and_the_ai_call_count_as_side_effects():
    n, wf = _apply('''
        def handler(cur, x):
            if x.urgent:
                claude.triage_alert(x)
            else:
                xero.refresh("t")
    ''', ["ai", "call-xero"])
    assert n == 1
    [d] = _decisions(wf)
    assert _out(wf, d["id"]) == {"if x.urgent": "ai", "else": "call-xero"}
    assert _problems(wf) == []


def test_same_module_helper_is_followed_one_level():
    n, wf = _apply('''
        def _mark_paid(cur):
            cur.execute("UPDATE invoices SET status='paid'")

        def _record_payout(cur):
            cur.execute("INSERT INTO payouts VALUES (1)")

        def handler(cur, event):
            if event == "checkout.session.completed":
                _mark_paid(cur)
            elif event == "payout.paid":
                _record_payout(cur)
    ''', ["write-invoices", "write-payouts"])
    assert n == 1
    [d] = _decisions(wf)
    assert set(_out(wf, d["id"]).values()) == {"write-invoices", "write-payouts"}


def test_depth_is_capped_at_two_levels():
    n, wf = _apply('''
        def handler(cur, a, b, c):
            if a:
                if b:
                    if c:
                        cur.execute("INSERT INTO ta VALUES (1)")
                    else:
                        cur.execute("INSERT INTO tb VALUES (1)")
                else:
                    cur.execute("INSERT INTO tc VALUES (1)")
            else:
                cur.execute("INSERT INTO td VALUES (1)")
    ''', ["write-ta", "write-tb", "write-tc", "write-td"])
    assert n == 2
    steps = {s["id"]: s for s in wf["steps"]}
    outer, inner = _decisions(wf)
    assert "parent" not in outer and inner["parent"] == outer["id"]
    # the third level is flattened into a straight run inside the inner branch
    assert steps["write-ta"]["parent"] == inner["id"] and steps["write-tb"]["parent"] == inner["id"]
    assert {"from": "write-ta", "to": "write-tb"} in wf["edges"]
    assert _problems(wf) == []


def test_unlocated_steps_lead_and_heartbeat_trails():
    n, wf = _apply(BILLING_MODE, ["call-graph", "write-suppressed_log", "write-payer_links",
                                  "write-invoices", "write-audit"], heartbeat=True)
    assert n == 1
    ids = [s["id"] for s in wf["steps"]]
    assert ids[:2] == ["trigger", "call-graph"] and ids[-1] == "heartbeat"
    assert {"from": "trigger", "to": "call-graph"} in wf["edges"]
    assert {"from": "write-audit", "to": "heartbeat"} in wf["edges"]
    assert _problems(wf) == []


def test_labels_are_shortened_source_text():
    n, wf = _apply('''
        def handler(cur, account):
            if account.billing_mode == 'suppress' and account.bill_to_account_id is not None:
                cur.execute("INSERT INTO a VALUES (1)")
            else:
                cur.execute("INSERT INTO b VALUES (1)")
    ''', ["write-a", "write-b"])
    [d] = _decisions(wf)
    label = next(lbl for lbl in _out(wf, d["id"]) if lbl.startswith("if "))
    assert label.endswith("…") and len(label) <= autodecide.LABEL_MAX + 3


# ---- build wiring --------------------------------------------------------------------------------

_DECISIONS_PY = '''
EXECUTORS = {}


def executor(dtype):
    def deco(fn):
        EXECUTORS[dtype] = fn
        return fn
    return deco


@executor("fixture.route.execute")
def _execute_route(cur, decision_id, action):
    """Routes a fixture decision."""
    if action.get("kind") == "case":
        cur.execute("INSERT INTO cases (id) VALUES (%s)", (decision_id,))
    else:
        cur.execute("INSERT INTO leads (id) VALUES (%s)", (decision_id,))
    return {}


@executor("fixture.plain.execute")
def _execute_plain(cur, decision_id, action):
    """No branching."""
    cur.execute("UPDATE plain SET done=true WHERE id=%s", (decision_id,))
    return {}
'''


def _tree(tmp_path) -> Path:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "decisions.py").write_text(textwrap.dedent(_DECISIONS_PY))
    return tmp_path


def _by_func(catalog):
    return {w["steps"][0]["code_ref"]["function"]: w for w in catalog["workflows"]}


def test_build_derives_decisions_for_auto_workflows(tmp_path):
    cat = build.build_catalog(_tree(tmp_path), None, [], "sha", None, "g", auto_decisions=True)
    assert schema.validate_catalog(cat) == []
    wfs = _by_func(cat)
    [d] = _decisions(wfs["_execute_route"])
    assert set(_out(wfs["_execute_route"], d["id"])) == {"if action.get('kind') == 'case'", "else"}
    assert not _decisions(wfs["_execute_plain"]) and wfs["_execute_plain"]["edges"] == []


def test_build_flag_off_leaves_straight_runs(tmp_path):
    cat = build.build_catalog(_tree(tmp_path), None, [], "sha", None, "g", auto_decisions=False)
    assert not any(_decisions(w) for w in cat["workflows"])


def test_annotated_workflow_is_never_rewritten(tmp_path):
    ann = {"id": "sales.fixture-route.execute", "name": "Sales › Fixture route › Execute", "group": "Sales",
           "subproject": "ERP", "summary": "hand-written", "trigger": {"type": "button", "detail": "x"},
           "steps": [{"id": "approve", "kind": "trigger", "label": "Approve",
                      "code_ref": {"file": "app/decisions.py", "function": "_execute_route"}}],
           "source_refs": [{"file": "app/decisions.py"}]}
    cat = build.build_catalog(_tree(tmp_path), None, [ann], "sha", None, "g", auto_decisions=True)
    wf = next(w for w in cat["workflows"] if w["id"] == "sales.fixture-route.execute")
    assert [s["id"] for s in wf["steps"]] == ["approve"] and wf["edges"] == []


def test_a_derivation_that_fails_validation_is_rolled_back(tmp_path, monkeypatch):
    def broken(wf, fn, **kw):
        wf["steps"].append({"id": "decide-1", "kind": "decision", "label": "x", "system": "internal",
                            "code_ref": {"file": "f", "function": "g"}})    # no question, no edges
        return 1
    monkeypatch.setattr(build.autodecide, "apply", broken)
    cat = build.build_catalog(_tree(tmp_path), None, [], "sha", None, "g", auto_decisions=True)
    assert schema.validate_catalog(cat) == []
    assert not any(_decisions(w) for w in cat["workflows"])


def test_a_derivation_that_raises_is_rolled_back(tmp_path, monkeypatch):
    def boom(wf, fn, **kw):
        wf["steps"] = []
        raise RuntimeError("bug")
    monkeypatch.setattr(build.autodecide, "apply", boom)
    cat = build.build_catalog(_tree(tmp_path), None, [], "sha", None, "g", auto_decisions=True)
    assert schema.validate_catalog(cat) == []
    assert all(w["steps"] for w in cat["workflows"])
