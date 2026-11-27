"""The tree has no project root (change #76, task #713): accounts are top level, the overseer's project is an ordinary project
under the estate account with its own session. The account's session must not write the overseer's tasks, the overseer's own
writes are its own routed projects, and its cross-account READS and requests are unchanged."""
import importlib.util
from pathlib import Path

import pytest

from app import config, mcp_server, repo, scope, tree
from app.auth import issue_token
from app.db import connect, get_cursor

MIGRATION = Path(__file__).resolve().parent.parent / "db/migrations/0044_tree_root_is_not_a_project.py"


@pytest.fixture
def estate(monkeypatch):
    monkeypatch.setattr(config, "OVERSEER_PROJECT", "dash")
    monkeypatch.setattr(config, "HEALTH_PROJECT", "erp")
    acct = repo.create_project("mallen", "Mallen Services", "internal", "owner", short_name="mal", session_roles=["all"])
    other = repo.create_project("clientco", "Client Co", "internal", "owner", short_name="cli", session_roles=["all"])
    dash = repo.create_project("dash", "Crewboss", "internal", "owner", short_name="dash", session_roles=["all"])
    erp = repo.create_project("erp", "ERP", "internal", "owner", short_name="erpx", session_roles=[])
    tree.set_place(acct["id"], "owner", None, "account")
    tree.set_place(other["id"], "owner", None, "account")
    tree.set_place(dash["id"], "owner", acct["id"])
    tree.set_place(erp["id"], "owner", acct["id"])
    app = repo.create_project("clientapp", "Client App", "internal", "owner", short_name="capp", session_roles=[])
    tree.set_place(app["id"], "owner", other["id"])
    tasks = {n: repo.create_task(p["id"], f"{n} task", "", "must", "bug", [], {}, "owner")
             for n, p in (("dash", dash), ("erp", erp), ("other", app))}
    return dict(acct=acct, other=other, dash=dash, erp=erp, tasks=tasks)


def test_accounts_sit_at_the_top_and_the_overseer_is_an_ordinary_project(estate):
    assert repo.get_project(estate["acct"]["id"])["parent_id"] is None
    assert repo.get_project(estate["dash"]["id"])["parent_id"] == estate["acct"]["id"]
    with pytest.raises(repo.BadRequest):
        tree.set_place(estate["other"]["id"], "owner", estate["acct"]["id"], "account")      # an account has no parent
    with pytest.raises(repo.BadRequest):
        tree.set_place(estate["dash"]["id"], "owner", estate["acct"]["id"], "account")       # the overseer is a project
    assert tree.account_root_id(estate["dash"]["id"]) == estate["acct"]["id"]                 # inside the account partition


def test_a_new_project_is_top_level_not_under_the_dashboard(estate):
    p = repo.create_project("loose", "Loose", "internal", "owner", short_name="loo", session_roles=[])
    assert p["parent_id"] is None


def test_the_account_session_cannot_write_the_overseers_tasks(client, estate):
    tok = issue_token("all", "mallen-all", project_id=estate["acct"]["id"])
    h = {"Authorization": f"Bearer {tok}"}
    session = {"project_id": estate["acct"]["id"], "project": estate["acct"]}
    reach = scope.reach(session)
    assert estate["erp"]["id"] in reach and estate["dash"]["id"] not in reach
    dash_task = estate["tasks"]["dash"]["id"]
    assert client.get(f"/api/tasks/{dash_task}", headers=h).status_code == 404
    assert client.patch(f"/api/tasks/{dash_task}", json={"status": "doing"}, headers=h).status_code == 404
    assert client.post(f"/api/tasks/{dash_task}/comments", json={"body": "x"}, headers=h).status_code == 404
    assert client.post("/api/tasks", json={"title": "x", "stream": "bug", "project": "dash"}, headers=h).status_code == 404
    assert repo.get_task(dash_task)["status"] == estate["tasks"]["dash"]["status"]
    assert client.get(f"/api/tasks/{estate['tasks']['erp']['id']}", headers=h).status_code == 200


def test_the_overseers_writes_are_its_own_routed_projects_and_its_reads_still_cross_accounts(client, estate):
    tok = issue_token("all", "dash-all", project_id=estate["dash"]["id"])
    h = {"Authorization": f"Bearer {tok}"}
    reach = scope.reach({"project_id": estate["dash"]["id"], "project": estate["dash"]})
    assert reach == {estate["dash"]["id"]}
    for key in ("erp", "other"):                                   # reads cross accounts...
        assert client.get(f"/api/tasks/{estate['tasks'][key]['id']}", headers=h).status_code == 200
        assert client.patch(f"/api/tasks/{estate['tasks'][key]['id']}", json={"status": "doing"}, headers=h).status_code == 404  # ...writes do not
    assert client.patch(f"/api/tasks/{estate['tasks']['dash']['id']}", json={"title": "mine"}, headers=h).status_code == 200
    assert client.get("/api/plan", headers=h).status_code == 200


def test_the_overseer_still_gets_no_ssh_passes_after_moving_into_the_account(client, estate):
    session = {"project_id": estate["dash"]["id"], "project": estate["dash"], "stream": "all"}
    assert scope.server_account(session) is None
    assert scope.server_account({"project_id": estate["acct"]["id"], "project": estate["acct"], "stream": "all"}) is not None


def test_routing_keeps_the_overseers_branch_out_of_the_account_session(estate):
    assert tree.session_holder(estate["dash"]["id"])["id"] == estate["dash"]["id"]
    assert estate["dash"]["id"] not in tree.routed_project_ids(estate["acct"]["id"])
    assert estate["erp"]["id"] in tree.routed_project_ids(estate["acct"]["id"])


def _migrate():
    spec = importlib.util.spec_from_file_location("m0044", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with connect() as conn:
        mod.migrate(conn)
        conn.commit()
    return mod


def test_the_migration_moves_the_old_shape_and_rolls_back(monkeypatch):
    monkeypatch.setattr(config, "OVERSEER_PROJECT", "dash")
    monkeypatch.setattr(config, "HEALTH_PROJECT", "erp")
    dash = repo.create_project("dash", "Crewboss", "internal", "owner", short_name="dash", session_roles=["all"])
    acct = repo.create_project("mallen", "Mallen", "internal", "owner", short_name="mal", session_roles=["all"])
    erp = repo.create_project("erp", "ERP", "internal", "owner", short_name="erpx", session_roles=[])
    pa = repo.create_project("personal", "PA", "internal", "owner", short_name="pa", session_roles=["all"])
    with get_cursor() as cur:        # the OLD shape: dash is the root, accounts and personal projects hang under it
        cur.execute("UPDATE projects SET kind = 'account', parent_id = %s WHERE id = %s", (dash["id"], acct["id"]))
        cur.execute("UPDATE projects SET parent_id = %s WHERE id = %s", (acct["id"], erp["id"]))
        cur.execute("UPDATE projects SET parent_id = %s WHERE id = %s", (dash["id"], pa["id"]))
        cur.execute("UPDATE projects SET parent_id = NULL WHERE id = %s", (dash["id"],))
    # (dash first re-parented under nothing; put the children under it again so the old shape is real)
    with get_cursor() as cur:
        cur.execute("UPDATE projects SET parent_id = %s WHERE id IN (%s, %s)", (dash["id"], acct["id"], pa["id"]))
    mod = _migrate()
    parent = {p["slug"]: p["parent_id"] for p in repo.list_projects(include_archived=True)}
    assert parent["mallen"] is None and parent["personal"] is None          # accounts + personal projects are top level
    assert parent["dash"] == acct["id"] and parent["erp"] == acct["id"]       # the overseer sits under the estate account
    _migrate()                                                                 # idempotent
    assert {p["slug"]: p["parent_id"] for p in repo.list_projects(include_archived=True)} == parent
    with connect() as conn:
        mod.rollback(conn)
        conn.commit()
    parent = {p["slug"]: p["parent_id"] for p in repo.list_projects(include_archived=True)}
    assert parent["dash"] is None and parent["mallen"] == dash["id"] and parent["personal"] == dash["id"]


def test_a_fresh_database_is_left_alone(monkeypatch):
    monkeypatch.setattr(config, "OVERSEER_PROJECT", "")
    p = repo.create_project("solo", "Solo", "internal", "owner", short_name="sol", session_roles=[])
    _migrate()
    assert repo.get_project(p["id"])["parent_id"] is None
