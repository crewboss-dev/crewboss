"""discover/state_routes.py + lib/coverage.py on a small fixture tree built per test: which state-
changing routes count (business-table writes, directly or 1-2 hops away), what is left out (GET, UI
state, log-only, non-schema tables, docstring prose, 3 hops), how routes group into one workflow per
(route file, entity), annotations replacing the routes they claim, and the coverage report."""
import json
import textwrap
from pathlib import Path

from scanner import build
from scanner.discover import state_routes
from scanner.lib import coverage, schema, sqlwrites

MIGRATIONS = '''
-- a comment naming CREATE TABLE not_a_table is ignored
CREATE TABLE widgets (id int);
CREATE TABLE IF NOT EXISTS widget_lines (id int);
CREATE TABLE purchase_orders (id int);
CREATE TABLE system_events (id int);
CREATE TABLE orphans (id int);
CREATE TABLE old_name (id int);
CREATE TABLE dropped (id int);
'''
MIGRATIONS_2 = '''
ALTER TABLE old_name RENAME TO gadgets;
DROP TABLE IF EXISTS dropped;
'''

ROUTER = '''
class _Router:
    def __getattr__(self, method):
        return lambda path, **kw: (lambda fn: fn)


router = _Router()
'''

WIDGETS_PY = ROUTER + '''
from app import widgetlib


def _close(cur, wid):
    cur.execute("UPDATE widgets SET status='closed' WHERE id=%s", (wid,))


def _via_two(cur, wid):
    widgetlib.write_line(cur, wid)


def _three_a(cur):
    _three_b(cur)


def _three_b(cur):
    widgetlib.three_c(cur)


@router.post("/widgets/new")
def create_widget(cur):
    cur.execute("INSERT INTO widgets (status) VALUES ('open')")


@router.post("/widgets/{wid:int}/cancel")
def cancel_widget(cur, wid: int):
    _close(cur, wid)


@router.post("/widgets/{wid:int}/line/{lid:int}/tick")
def tick_line(cur, wid: int, lid: int):
    _via_two(cur, wid)


@router.post("/widgets/{wid:int}/line")
def add_line(cur, wid: int):
    cur.execute("INSERT INTO widget_lines (widget_id) VALUES (%s)", (wid,))


@router.post("/widgets/{wid:int}/pdf/save")
def save_pdf(cur, wid: int):
    cur.execute("UPDATE widgets SET pdf=%s WHERE id=%s", ("x", wid))


@router.post("/widgets/{wid:int}/po/create")
def po_create(cur, wid: int):
    cur.execute("INSERT INTO purchase_orders (widget_id) VALUES (%s)", (wid,))


@router.put("/po/{po_id:int}/send")
def po_send(cur, po_id: int):
    cur.execute("UPDATE purchase_orders SET sent=true WHERE id=%s", (po_id,))


@router.delete("/po/{po_id:int}")
def po_delete(cur, po_id: int):
    cur.execute("DELETE FROM purchase_orders WHERE id=%s", (po_id,))


@router.get("/widgets/{wid:int}/touch")
def touch_on_get(cur, wid: int):
    cur.execute("UPDATE widgets SET seen=true WHERE id=%s", (wid,))


@router.post("/widgets/prefs")
def save_prefs(cur):
    cur.execute("UPDATE widgets SET pref=1")


@router.post("/widgets/{wid:int}/log")
def log_only(cur, wid: int):
    cur.execute("INSERT INTO system_events (source) VALUES ('w')")


@router.post("/widgets/{wid:int}/scratch")
def not_in_schema(cur, wid: int):
    cur.execute("UPDATE temp_scratch SET x=1")


@router.post("/widgets/{wid:int}/prose")
def prose_only(cur, wid: int):
    """The UPDATE widgets statement lives elsewhere — this docstring must not count as a write."""
    return None


@router.post("/widgets/{wid:int}/deep")
def too_deep(cur, wid: int):
    _three_a(cur)
'''

WEBHOOK_PY = ROUTER + '''

@router.post("/webhook/widget")
def widget_webhook(cur):
    cur.execute("UPDATE widgets SET hooked=true")
'''

WIDGETLIB_PY = '''
def write_line(cur, wid):
    cur.execute("UPDATE widget_lines SET ticked=true WHERE widget_id=%s", (wid,))


def three_c(cur):
    cur.execute("UPDATE gadgets SET deep=true")


def orphan_writer(cur):
    cur.execute("INSERT INTO orphans (x) VALUES (1)")
'''

ROUTES = "app/routes/widgets.py"


def _tree(tmp_path) -> Path:
    (tmp_path / "app" / "routes").mkdir(parents=True)
    (tmp_path / "db" / "migrations").mkdir(parents=True)
    (tmp_path / "db" / "migrations" / "001_base.sql").write_text(MIGRATIONS)
    (tmp_path / "db" / "migrations" / "002_more.sql").write_text(MIGRATIONS_2)
    (tmp_path / "app" / "routes" / "widgets.py").write_text(textwrap.dedent(WIDGETS_PY))
    (tmp_path / "app" / "widgetlib.py").write_text(textwrap.dedent(WIDGETLIB_PY))
    (tmp_path / "app" / "routes" / "webhook.py").write_text(textwrap.dedent(WEBHOOK_PY))
    return tmp_path


def _by_func(records):
    return {r["func"]: r for r in records}


def test_schema_tables_replays_create_drop_and_rename(tmp_path):
    tables = sqlwrites.schema_tables(_tree(tmp_path))
    assert {"widgets", "widget_lines", "purchase_orders", "gadgets", "orphans"} <= tables
    assert "old_name" not in tables and "dropped" not in tables and "not_a_table" not in tables
    assert sqlwrites.schema_tables(tmp_path / "nowhere") is None
    assert sqlwrites.is_business_table("anything", None)            # no migrations: accept, don't filter
    assert not sqlwrites.is_business_table("system_events", None)   # bookkeeping never counts


def test_literal_writes_skips_docstrings():
    import ast
    fn = ast.parse(textwrap.dedent('''
        def f(cur):
            """UPDATE widgets is described here."""
            cur.execute(f"UPDATE widget_lines SET x={1}")
    ''')).body[0]
    assert sqlwrites.literal_writes(fn) == ["widget_lines"]


def test_discover_finds_state_changing_routes_directly_and_through_hops(tmp_path):
    found = _by_func(state_routes.discover(_tree(tmp_path)))
    assert found["create_widget"]["method"] == "POST"
    assert found["create_widget"]["tables"] == [{"table": "widgets", "via": None}]
    # 1 hop (same module) and 2 hops (same module -> another app/ module) both resolve, with the hop named
    assert found["cancel_widget"]["tables"] == [{"table": "widgets", "via": "routes.widgets._close"}]
    assert found["tick_line"]["tables"] == [{"table": "widget_lines", "via": "widgetlib.write_line"}]
    assert found["po_send"]["method"] == "PUT" and found["po_delete"]["method"] == "DELETE"


def test_discover_leaves_out_non_business_routes(tmp_path):
    found = _by_func(state_routes.discover(_tree(tmp_path)))
    for func in ("touch_on_get",    # GET is never a state-changing route here
                 "save_prefs",      # UI state (path)
                 "log_only",        # writes only a bookkeeping table
                 "not_in_schema",   # table the migrations never create
                 "prose_only",      # SQL only in a docstring
                 "too_deep"):       # the write is 3 hops away
        assert func not in found, func


def test_entities_from_paths(tmp_path):
    found = _by_func(state_routes.discover(_tree(tmp_path)))
    assert found["cancel_widget"]["entity_name"] == "Widget"
    assert found["tick_line"]["entity_name"] == "Widget line"
    assert found["add_line"]["entity_name"] == "Widget line"       # bare final segment the file uses as a sub
    assert found["save_pdf"]["entity_name"] == "Widget PDF"
    assert found["po_create"]["entity_name"] == "Purchase order"   # alias
    assert found["po_send"]["entity_name"] == "Purchase order"


def test_group_records_one_group_per_entity_folding_singletons(tmp_path):
    groups = state_routes.group_records(state_routes.discover(_tree(tmp_path)))
    by_name = {g["entity_name"]: sorted(r["func"] for r in g["routes"]) for g in groups}
    assert by_name["Widget"] == ["cancel_widget", "create_widget", "save_pdf"]   # 'Widget PDF' folded in
    assert by_name["Widget line"] == ["add_line", "tick_line"]
    assert by_name["Purchase order"] == ["po_create", "po_delete", "po_send"]   # /widgets/{id}/po + /po/…
    assert "Widget PDF" not in by_name


def _catalog(tree, annotations=()):
    return build.build_catalog(tree, None, list(annotations), "sha", None, "g")


def test_build_emits_one_auto_workflow_per_entity(tmp_path):
    cat = _catalog(_tree(tmp_path))
    assert schema.validate_catalog(cat) == []
    wfs = {w["id"]: w for w in cat["workflows"] if w["id"].endswith(".actions")}
    po = wfs["platform.purchase-order.actions"]   # route file not in ROUTE_FILE_GROUP -> Platform
    assert po["name"] == "Platform › Purchase order › Actions" and po["confidence"] == "auto"
    assert po["trigger"]["type"] == "button" and po["steps"][0]["kind"] == "trigger"
    assert [s["label"] for s in po["steps"][1:]] == ["PO create", "PO send", "PO delete"]
    send = po["steps"][2]
    assert send["code_ref"] == {"file": ROUTES, "lines": send["code_ref"]["lines"], "function": "po_send"}
    assert send["detail"] == "PUT /po/{po_id:int}/send — writes db:purchase_orders"
    assert po["side_effects"] == ["db:purchase_orders"]
    widget = wfs["platform.widget.actions"]
    cancel = next(s for s in widget["steps"] if s["id"] == "cancel-widget")
    assert "db:widgets (via routes.widgets._close())" in cancel["detail"]
    # the webhook handler is a discovered webhook workflow already (its own trigger) -> claimed, not repeated
    assert any(w["trigger"]["type"] == "webhook" for w in cat["workflows"])
    assert not any(s["code_ref"].get("function") == "widget_webhook"
                   for w in wfs.values() for s in w["steps"])


def test_annotation_replaces_the_routes_it_claims(tmp_path):
    ann = {"id": "sales.widget.cancel", "name": "Sales › Widget › Cancel", "group": "Sales",
           "subproject": "ERP", "summary": "hand-written", "trigger": {"type": "button", "detail": "x"},
           "steps": [{"id": "click", "kind": "trigger", "label": "Click",
                      "code_ref": {"file": ROUTES, "function": "cancel_widget"}},
                     {"id": "add", "kind": "action", "label": "Add line",
                      "code_ref": {"file": ROUTES, "function": "add_line"}},
                     {"id": "tick", "kind": "action", "label": "Tick line",
                      "code_ref": {"file": ROUTES, "function": "tick_line"}}],
           "source_refs": [{"file": ROUTES}]}
    cat = _catalog(_tree(tmp_path), [ann])
    assert schema.validate_catalog(cat) == []
    ids = {w["id"]: w for w in cat["workflows"]}
    assert ids["sales.widget.cancel"]["confidence"] == "annotated"
    widget_steps = {s["code_ref"]["function"] for s in ids["platform.widget.actions"]["steps"][1:]}
    assert "cancel_widget" not in widget_steps and "create_widget" in widget_steps
    assert "platform.widget-line.actions" not in ids     # every route it had is claimed -> gone


def test_coverage_lists_tables_no_workflow_covers(tmp_path):
    tree = _tree(tmp_path)
    writers = coverage.table_writers(tree)
    assert writers["orphans"] == [{"file": "app/widgetlib.py", "function": "orphan_writer"}]
    assert "system_events" not in writers and "temp_scratch" not in writers
    cat = _catalog(tree)
    cov = cat["coverage"]
    uncovered = {e["table"]: e["writers"] for e in cov["uncovered"]}
    assert uncovered["orphans"] == [{"file": "app/widgetlib.py", "function": "orphan_writer"}]
    assert uncovered["gadgets"] == [{"file": "app/widgetlib.py", "function": "three_c"}]
    assert "widgets" not in uncovered and "purchase_orders" not in uncovered   # the auto workflows cover them
    assert cov["covered"] == cov["business_tables"] - len(cov["uncovered"])
    assert schema.privacy_sweep(json.dumps(cat)) == []


def test_coverage_counts_side_effects_and_step_refs():
    writers = {"a": [{"file": "app/x.py", "function": "f"}], "b": [{"file": "app/y.py", "function": "g"}],
               "c": [{"file": "app/z.py", "function": "h"}]}
    wfs = [{"side_effects": ["db:a"], "steps": []},
           {"side_effects": [], "steps": [{"code_ref": {"file": "app/y.py", "function": "g"}}]}]
    cov = coverage.report(wfs, writers)
    assert cov == {"scope": "app/", "business_tables": 3, "covered": 2,
                   "uncovered": [{"table": "c", "writers": [{"file": "app/z.py", "function": "h"}]}]}


def test_validator_checks_the_coverage_object():
    base = {"project": "p", "commit": "c", "generated_at": "g", "workflows": []}
    ok = dict(base, coverage={"scope": "app/", "business_tables": 1, "covered": 0,
                              "uncovered": [{"table": "t", "writers": [{"file": "f", "function": "g"}]}]})
    assert schema.validate_catalog(ok) == []
    bad = dict(base, coverage={"business_tables": "1", "covered": 0, "uncovered": [{"table": "t", "writers": []}]})
    problems = schema.validate_catalog(bad)
    assert any("business_tables" in p for p in problems) and any("writers" in p for p in problems)
