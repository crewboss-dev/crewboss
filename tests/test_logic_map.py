"""The logic map's pure half (task #715): parsing and validation, discovery, uncovered and stale detection, the diff, the picture, the
catalog importer and the export commands. No database."""
import json
import textwrap

import pytest

from app import logic_cli, logic_map as lm

GOOD = """
workflows:
  - id: quote.won
    name: Quote won
    summary: A person closes a quote as won.
    trigger: {type: button, control: "Quote page: Won", detail: closes the quote, route: "POST /quotes/won", code_ref: {file: app/quotes.py, function: mark_won}}
    data: ["table:quotes"]
    steps:
      - {id: save, kind: action, label: Mark won, code_ref: {file: app/quotes.py, function: mark_won}, effects: ["change:quotes", "create:work_orders"]}
      - {id: labour, kind: decision, question: "Is there labour?", label: "Labour?"}
      - id: note
        kind: ai
        label: Draft note
        ai: {model: sonnet, skill: handover-note, may: [write a draft]}
    edges:
      - {from: trigger, to: save}
      - {from: save, to: labour}
      - {from: labour, to: note, label: yes}
"""


def _write(root, name, text):
    d = root / ".crewboss" / "logic"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(textwrap.dedent(text), encoding="utf-8")


def _code(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(text), encoding="utf-8")


QUOTES = """
    from fastapi import APIRouter
    router = APIRouter()

    @router.post("/quotes/won")
    def mark_won(): ...

    @router.get("/quotes")
    def list_quotes(): ...

    @router.post("/hooks/xero")
    async def xero_webhook(): ...

    def helper():
        import anthropic
        c = anthropic.Anthropic()
        return c.messages.create(model="x", messages=[])

    async def poller():
        while True:
            await asyncio.sleep(60)
"""


# --- parsing and validation -----------------------------------------------------------------------------------------------------

def test_a_good_file_loads_and_normalises(tmp_path):
    _write(tmp_path, "quotes.yaml", GOOD)
    m = lm.load(tmp_path)
    assert m["errors"] == [] and [w["id"] for w in m["workflows"]] == ["quote.won"]
    wf = m["workflows"][0]
    assert wf["trigger"]["control"] == "Quote page: Won" and wf["steps"][0]["effects"] == ["change:quotes", "create:work_orders"]
    assert wf["steps"][2]["ai"]["skill"] == "handover-note" and wf["edges"][2]["label"] == "yes"


def test_no_directory_is_an_empty_map(tmp_path):
    assert lm.load(tmp_path) == {"workflows": [], "ignore": [], "errors": [], "files": []}


def test_bad_yaml_is_a_clear_error_naming_file_and_line_not_a_crash(tmp_path):
    _write(tmp_path, "broken.yaml", "workflows:\n  - id: x\n    name: [unclosed\n")
    m = lm.load(tmp_path)
    assert m["workflows"] == []
    assert len(m["errors"]) == 1 and m["errors"][0].startswith(".crewboss/logic/broken.yaml: not valid YAML at line")


@pytest.mark.parametrize("patch, expect", [
    ("summary: A person closes a quote as won.", "needs a 'summary'"),
    ("kind: decision", "a decision needs a 'question'"),
    ("kind: ai", "an ai step needs"),
    ("id: quote.won", "workflow needs an 'id'"),
    ("{type: button,", "trigger needs a 'type'"),
    ("to: note, label: yes", "'from' and 'to' must name steps"),
    ("file: app/quotes.py, function: mark_won}, effects", "must be a path inside the repo"),
])
def test_schema_problems_are_named(tmp_path, patch, expect):
    text = GOOD
    if expect == "needs a 'summary'":
        text = text.replace(patch, "")
    elif expect == "a decision needs a 'question'":
        text = text.replace('question: "Is there labour?", ', "")
    elif expect == "an ai step needs":
        text = text.replace("ai: {model: sonnet, skill: handover-note, may: [write a draft]}", "ai: {model: sonnet}")
    elif expect == "workflow needs an 'id'":
        text = text.replace("- id: quote.won", "- id: Quote Won!")
    elif expect == "trigger needs a 'type'":
        text = text.replace("{type: button,", "{type: pigeon,")
    elif expect == "'from' and 'to' must name steps":
        text = text.replace("to: note, label: yes", "to: ghost, label: yes")
    else:
        text = text.replace("file: app/quotes.py, function: mark_won}, effects", "file: ../etc/passwd, function: x}, effects")
    _write(tmp_path, "q.yaml", text)
    errs = lm.load(tmp_path)["errors"]
    assert any(expect in e for e in errs), errs


def test_duplicate_ids_across_files_and_steps_are_refused(tmp_path):
    _write(tmp_path, "a.yaml", GOOD)
    _write(tmp_path, "b.yaml", GOOD)
    errs = lm.load(tmp_path)["errors"]
    assert any("already defined in a.yaml" in e for e in errs)
    _write(tmp_path, "b.yaml", GOOD.replace("id: quote.won", "id: other").replace("id: labour", "id: save"))
    assert any("used twice" in e for e in lm.load(tmp_path)["errors"])


def test_ignore_needs_a_reason(tmp_path):
    _write(tmp_path, "i.yaml", "ignore:\n  - {file: 'app/x/*'}\n")
    assert any("ignore[0] needs a 'file'" in e for e in lm.load(tmp_path)["errors"])


# --- discovery ------------------------------------------------------------------------------------------------------------------

def test_discovers_python_routes_webhooks_ai_and_loops_but_not_gets_or_tests(tmp_path):
    _code(tmp_path, "app/quotes.py", QUOTES)
    _code(tmp_path, "tests/test_q.py", QUOTES)
    _code(tmp_path, "app/test_inline.py", QUOTES)
    found = {(e["kind"], e["function"], e["name"]) for e in lm.discover(tmp_path)}
    assert found == {("route", "mark_won", "POST /quotes/won"), ("webhook", "xero_webhook", "POST /hooks/xero"),
                     ("ai", "helper", "AI SDK call"), ("timer", "poller", "background loop")}


def test_discovers_js_and_go(tmp_path):
    _code(tmp_path, "src/api.ts", """
        import express from 'express';
        const app = express();
        function createOrder() {
          app.post('/orders', (req, res) => {});
          const r = await client.messages.create({});
        }
        app.get('/orders', list);
        cron.schedule('* * * * *', () => {});
    """)
    _code(tmp_path, "svc/main.go", """
        package main
        func handleOrder() {}
        func main() {
            mux.HandleFunc("POST /orders", handleOrder)
            r.Delete("/orders/{id}", del)
            c.AddFunc("@hourly", tick)
        }
    """)
    kinds = {(e["kind"], e["name"]) for e in lm.discover(tmp_path)}
    assert ("route", "POST /orders") in kinds and ("ai", "AI SDK call") in kinds and ("timer", "cron.schedule") in kinds
    assert ("route", "DELETE /orders/{id}") in kinds and ("timer", "timer") in kinds
    assert not any(n == "GET /orders" for _, n in kinds)


def test_unparseable_files_and_skipped_dirs_do_not_break_discovery(tmp_path):
    _code(tmp_path, "app/bad.py", "def (:\n")
    _code(tmp_path, "node_modules/x/y.js", "app.post('/hidden', h)")
    assert lm.discover(tmp_path) == []


# --- uncovered and stale ---------------------------------------------------------------------------------------------------------

def test_uncovered_lists_only_what_no_workflow_names(tmp_path):
    _code(tmp_path, "app/quotes.py", QUOTES)
    _write(tmp_path, "quotes.yaml", GOOD)
    c = lm.check(tmp_path)
    names = sorted(u["name"] for u in c["uncovered"])
    assert names == ["AI SDK call", "POST /hooks/xero", "background loop"]      # mark_won is covered by code_ref


def test_covers_star_route_and_ignore_each_remove_an_entry(tmp_path):
    _code(tmp_path, "app/quotes.py", QUOTES)
    _write(tmp_path, "quotes.yaml", GOOD + "    covers: [{file: app/quotes.py, function: '*'}]\n")
    assert lm.check(tmp_path)["uncovered"] == []
    _write(tmp_path, "quotes.yaml", GOOD.replace("code_ref: {file: app/quotes.py, function: mark_won}}", "code_ref: {file: app/quotes.py, function: mark_won}}")
           + "ignore:\n  - {file: 'app/*', reason: demo}\n")
    c = lm.check(tmp_path)
    assert c["uncovered"] == [] and c["ignored"] == 3
    # a route named on the trigger covers by its text alone
    _write(tmp_path, "quotes.yaml", GOOD.replace("code_ref: {file: app/quotes.py, function: mark_won}}\n    data", "code_ref: {file: app/other.py}}\n    data"))
    assert "POST /quotes/won" not in [u["name"] for u in lm.check(tmp_path)["uncovered"]]


def test_stale_when_the_file_or_function_is_gone(tmp_path):
    _code(tmp_path, "app/quotes.py", QUOTES)
    _write(tmp_path, "quotes.yaml", GOOD)
    assert lm.check(tmp_path)["stale"] == []
    _code(tmp_path, "app/quotes.py", "def something_else(): ...\n")
    stale = lm.check(tmp_path)["stale"]
    assert {s["why"] for s in stale} == {"mark_won is no longer in app/quotes.py"}
    (tmp_path / "app" / "quotes.py").unlink()
    assert {s["why"] for s in lm.check(tmp_path)["stale"]} == {"the file no longer exists"}


def test_report_bundles_the_map_and_the_findings(tmp_path):
    _code(tmp_path, "app/quotes.py", QUOTES)
    _write(tmp_path, "quotes.yaml", GOOD)
    rep = lm.build_report(tmp_path, "a" * 40)
    assert rep["version"] == lm.REPORT_VERSION and rep["sha"] == "a" * 40 and len(rep["workflows"]) == 1
    assert rep["errors"] == [] and rep["stale"] == [] and len(rep["uncovered"]) == 3 and rep["discovered"] == 4
    json.dumps(rep)


# --- diff, slim and the picture -------------------------------------------------------------------------------------------------

def _wf(tmp_path, text=GOOD):
    _write(tmp_path, "q.yaml", text)
    return lm.load(tmp_path)["workflows"]


def test_diff_names_added_removed_and_changed_steps(tmp_path):
    old = _wf(tmp_path)
    new_text = GOOD.replace('      - {id: labour, kind: decision, question: "Is there labour?", label: "Labour?"}\n', "")
    new_text = new_text.replace("    edges:", "      - {id: email, kind: action, label: Email the customer, effects: [email:customer]}\n    edges:")
    new_text = new_text.replace("      - {from: save, to: labour}\n      - {from: labour, to: note, label: yes}\n", "      - {from: save, to: note}\n")
    new_text = new_text.replace('effects: ["change:quotes", "create:work_orders"]', 'effects: ["change:quotes"]')
    new = _wf(tmp_path, new_text)
    d = lm.diff(old, new)
    ch = {(c["what"], c["step"]) for w in d["changed"] for c in w["changes"]}
    assert ("step added", "email") in ch and ("step removed", "labour") in ch and ("step changed", "save") in ch
    assert d["summary"] == {"added": 0, "removed": 0, "changed": 1, "steps": 3}
    other = [dict(new[0], id="quote.lost", name="Quote lost")]
    d2 = lm.diff(old, other)
    assert [w["id"] for w in d2["added"]] == ["quote.lost"] and [w["id"] for w in d2["removed"]] == ["quote.won"]
    assert lm.diff(old, old)["summary"] == {"added": 0, "removed": 0, "changed": 0, "steps": 0}


def test_slim_keeps_what_teaches_and_drops_the_rest(tmp_path):
    s = lm.slim(_wf(tmp_path))[0]
    assert s["trigger"]["control"] == "Quote page: Won" and [x["id"] for x in s["steps"]] == ["save", "labour", "note"]
    assert s["steps"][0]["effects"] == ["change:quotes", "create:work_orders"] and "code_ref" not in s["steps"][0]
    assert s["steps"][2]["ai"]["skill"] == "handover-note" and s["steps"][1]["question"] == "Is there labour?"


def test_layout_places_a_branch_below_its_decision_and_handles_cycles(tmp_path):
    wf = _wf(tmp_path)[0]
    pic = lm.layout(wf)
    ys = {n["id"]: n["y"] for n in pic["nodes"]}
    assert ys["trigger"] < ys["save"] < ys["labour"] < ys["note"] and len(pic["edges"]) == 3
    wf["edges"].append({"from": "note", "to": "save", "label": "again"})
    assert lm.layout(wf)["nodes"]                           # a loop back never hangs or loses nodes
    wf["edges"] = []
    assert len(lm.layout(wf)["edges"]) == 3                 # no edges: straight on, in order


# --- the catalog importer and the commands --------------------------------------------------------------------------------------

def test_importer_converts_a_catalog_into_a_valid_map(tmp_path):
    catalog = json.loads((__import__("pathlib").Path(__file__).parent.parent / "fixtures" / "catalog-fixture.json").read_text())
    wfs = lm.from_catalog(catalog)
    assert wfs and all(w["steps"] for w in wfs)
    _write(tmp_path, "catalog.yaml", lm.dump_workflows(wfs))
    m = lm.load(tmp_path)
    assert m["errors"] == [] and len(m["workflows"]) == len(wfs)


def test_cli_check_report_export_and_import(tmp_path, capsys):
    _code(tmp_path, "app/quotes.py", QUOTES)
    _write(tmp_path, "quotes.yaml", GOOD)
    assert logic_cli.main(["check", "--dir", str(tmp_path)]) == 0
    assert "OK" in capsys.readouterr().out
    assert logic_cli.main(["check", "--dir", str(tmp_path), "--strict"]) == 1      # uncovered fails only with --strict
    assert "UNCOVERED" in capsys.readouterr().out
    assert logic_cli.main(["report", "--dir", str(tmp_path), "--sha", "c" * 40]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["sha"] == "c" * 40 and len(rep["workflows"]) == 1
    out = tmp_path / "map.json"
    assert logic_cli.main(["export", "--dir", str(tmp_path), "-o", str(out)]) == 0
    data = json.loads(out.read_text())
    assert data["version"] == 1 and data["workflows"][0]["steps"][0]["effects"][1] == "create:work_orders"
    page = tmp_path / "map.html"
    assert logic_cli.main(["export", "--dir", str(tmp_path), "--format", "html", "-o", str(page)]) == 0
    assert "Quote won" in page.read_text() and "<script" not in page.read_text()
    _code(tmp_path, "app/quotes.py", "x = 1\n")
    assert logic_cli.main(["check", "--dir", str(tmp_path)]) == 1                  # stale fails
    _write(tmp_path, "quotes.yaml", "workflows: [oops")
    with pytest.raises(SystemExit):
        logic_cli.main(["export", "--dir", str(tmp_path)])                          # a map with errors does not export


def test_the_crewboss_map_itself_is_valid_and_not_stale():
    """Dogfood: this repo's own .crewboss/logic stays valid and every code_ref still resolves."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    m = lm.load(root)
    assert m["errors"] == [] and len(m["workflows"]) >= 4
    assert lm.stale_refs(root, m["workflows"]) == []
