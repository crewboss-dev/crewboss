"""The group tables are instance configuration: empty by default, filled from $CREWBOSS_HOME/scanner/naming.yaml."""
import pytest

from scanner.lib import naming as N


@pytest.fixture(autouse=True)
def _reset():
    yield
    N.load_overrides("/nonexistent/naming.yaml")


def test_defaults_are_empty():
    N.load_overrides("/nonexistent/naming.yaml")
    assert not (N.WORKER_GROUP or N.WEBHOOK_GROUP or N.DECISION_GROUP or N.ROUTE_FILE_GROUP or N.ROUTE_ENTITY_GROUP)


def test_overrides_load_from_the_instance_home(tmp_path, monkeypatch):
    (tmp_path / "scanner").mkdir()
    (tmp_path / "scanner" / "naming.yaml").write_text("worker_group:\n  invoice_sender: Billing\nwebhook_group:\n  /webhook/payments: Billing\n")
    monkeypatch.setenv("CREWBOSS_HOME", str(tmp_path))
    monkeypatch.delenv("CREWBOSS_NAMING", raising=False)
    N.load_overrides()
    assert N.WORKER_GROUP == {"invoice_sender": "Billing"}
    assert N.WEBHOOK_GROUP == {"/webhook/payments": "Billing"}
    N.load_overrides("/nonexistent/naming.yaml")
    assert not N.WORKER_GROUP


def test_unknown_table_and_bad_values_are_errors(tmp_path):
    p = tmp_path / "n.yaml"
    p.write_text("worker_grup: {a: Billing}\n")
    with pytest.raises(ValueError, match="unknown table"):
        N.load_overrides(p)
    p.write_text("worker_group: {a: 3}\n")
    with pytest.raises(ValueError, match="strings"):
        N.load_overrides(p)


def test_example_file_loads():
    N.load_overrides("scanner/naming.example.yaml")
    assert N.WEBHOOK_GROUP["/webhook/payments"] == "Billing"
