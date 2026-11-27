"""Integrations (#393, Dashboard side of acme-erp #391): the ERP's graded integration list arrives inside the catalog
import (scanner role token only, validated), belongs to the project whose scan carried it (another account's project
shows an empty state), drives the Integrations page (grades, diagrams, list, `i-<id>` anchors), the workflow canvas's
Integration block, and exports/catalog.json for the ERP's admin site."""
import copy
import json
import re
from pathlib import Path

import pytest

from app import catalog_export, catalog_import, config, db, repo, tree
from app import integrations as integrations_mod
from app.auth import issue_token

ROOT = Path(__file__).resolve().parent.parent
CATALOG = json.loads((ROOT / "fixtures/catalog-fixture.json").read_text())
EXPORT = json.loads((ROOT / "fixtures/integrations-fixture.json").read_text())


def _catalog(slug, integrations=EXPORT):
    c = copy.deepcopy(CATALOG)
    c["project"] = slug
    if integrations is not None:
        c["integrations"] = copy.deepcopy(integrations)
    return c


def _post(client, slug, body, token):
    return client.post(f"/api/catalog/import?project={slug}", json=body,
                       headers={"Authorization": f"Bearer {token}"} if token else {})


# --- the import: role token only, validated -------------------------------------------------------------------

def test_scanner_token_imports_integrations_with_the_catalog(client, project):
    r = _post(client, project["slug"], _catalog(project["slug"]), issue_token("scanner", "rescan"))
    assert r.status_code == 201, r.text
    doc = integrations_mod.latest(project["id"])
    assert [i["id"] for i in doc["integrations"]][:2] == ["db-erp", "pbx-sip"]     # stored as the ERP sorted it
    assert doc["systems"]["graph"] == ["ms-mail-send", "ms-sharepoint"]


def test_session_and_missing_tokens_cannot_import_integrations(client, project):
    assert _post(client, project["slug"], _catalog(project["slug"]), issue_token("bugs", "b")).status_code == 403
    assert _post(client, project["slug"], _catalog(project["slug"]), None).status_code == 401
    assert integrations_mod.latest(project["id"]) is None


@pytest.mark.parametrize("breakage, needle", [
    (lambda d: d.update(version=2), "version"),
    (lambda d: d.update(integrations="nope"), "must be a list"),
    (lambda d: d["integrations"][0].update(grade="E"), "grade"),
    (lambda d: d["integrations"][0].pop("peer"), "'peer'"),
    (lambda d: d["integrations"][1].update(id=d["integrations"][0]["id"]), "duplicate"),
    (lambda d: d["systems"].update(xero=["no-such-id"]), "unknown ids"),
    (lambda d: d["integrations"][0].update(keys="XERO_CLIENT_SECRET"), "key NAMES"),
])
def test_a_malformed_export_is_a_400_and_nothing_is_stored(client, project, breakage, needle):
    bad = copy.deepcopy(EXPORT)
    breakage(bad)
    r = _post(client, project["slug"], _catalog(project["slug"], bad), issue_token("scanner", "rescan"))
    assert r.status_code == 400 and needle in r.json()["detail"], r.text
    assert repo.latest_scan(project["id"]) is None


def test_a_catalog_without_integrations_still_imports(client, project):
    r = _post(client, project["slug"], _catalog(project["slug"], None), issue_token("scanner", "rescan"))
    assert r.status_code == 201
    assert integrations_mod.latest(project["id"]) is None


def test_features_token_of_another_project_cannot_place_integrations(client):
    a = repo.create_project("proj-a", "Project A", "internal", "owner", short_name="pa", session_roles=["all"])
    b = repo.create_project("proj-b", "Project B", "client", "owner", short_name="pb", session_roles=["all"])
    r = _post(client, "proj-b", _catalog("proj-b"), issue_token("features", "f", project_id=a["id"]))
    assert r.status_code == 404
    assert integrations_mod.latest(b["id"]) is None


# --- account partition: the list belongs to the project whose scan carried it -----------------------------------

@pytest.fixture
def accounts(client):
    with db.connect() as conn:
        conn.execute("TRUNCATE projects RESTART IDENTITY CASCADE")
        conn.commit()
    dash = repo.create_project("mallen-dashboard", "Dash", "internal", "owner", short_name="dash", session_roles=["all"])
    mallen = repo.create_project("mallen", "Mallen", "internal", "owner", short_name="mallen", session_roles=["all"])
    tree.set_place(mallen["id"], "owner", None, kind="account")
    erp = repo.create_project("acme-erp", "ERP", "internal", "owner", short_name="erp", session_roles=[])
    tree.set_place(erp["id"], "owner", mallen["id"], kind="project")
    acme = repo.create_project("acme", "Acme", "client", "owner", short_name="acme", session_roles=["all"])
    tree.set_place(acme["id"], "owner", None, kind="account")
    acme_app = repo.create_project("acme-app", "Acme app", "client", "owner", short_name="acmeapp", session_roles=[])
    tree.set_place(acme_app["id"], "owner", acme["id"], kind="project")
    catalog_import.import_document(_catalog("acme-erp"), "acme-erp")
    catalog_import.import_document(_catalog("acme-app", None), "acme-app")
    return {"erp": repo.get_project(erp["id"]), "acme_app": repo.get_project(acme_app["id"])}


def test_other_accounts_see_an_empty_state(client, accounts):
    html = client.get("/acme-app/integrations").text
    assert "data-empty-integrations" in html and "data-integration=" not in html
    assert client.get("/acme-app/integrations.json").json() == {"integrations": None}
    assert integrations_mod.sources(accounts["acme_app"]) == []
    erp = client.get("/acme-erp/integrations.json").json()["integrations"]
    assert {i["source"] for i in erp["integrations"]} == {"acme-erp"}


def test_integrations_sit_in_the_project_menu_after_workflows(client, accounts):
    html = client.get("/acme-erp/integrations").text
    # the project's tabs (#733, #773): Integrations is on the page you are on even when it is not on the bar, and sits after Workflows in the "+" catalogue
    assert 'aria-current="page" data-pill="integrations"' in html
    cat = re.findall(r'data-cat="([a-z]+)"', html)
    assert "integrations" not in cat or cat.index("integrations") > cat.index("workflows")
    # an ACCOUNT's own menu has no Integrations (workflows aren't there either)
    account_html = client.get("/mallen").text
    assert 'href="/mallen/integrations"' not in account_html


# --- the page ---------------------------------------------------------------------------------------------------

def test_page_shows_grades_every_grade_zero_included_diagrams_list_and_anchors(client, accounts):
    html = client.get("/acme-erp/integrations").text
    for g, n in {"A": 3, "B": 2, "C": 2, "D": 0}.items():
        tile = re.search(rf'data-grade-tile="{g}".*?text-title tabular leading-none">(\d+)<', html, re.S)
        assert tile and int(tile.group(1)) == n, g
    assert html.count('data-kind="integrations"') == 2              # core + pbx diagrams
    assert 'data-module="core"' in html and 'data-src="/acme-erp/integrations.json"' in html
    assert 'data-integration-link="#i-{id}"' in html
    for i in EXPORT["integrations"]:
        assert f'id="i-{i["id"]}"' in html
    # weakest first, reasons and key NAMES shown, never anything like a value
    order = re.findall(r'data-integration="([^"]+)"', html)
    grades = {i["id"]: i["grade"] for i in EXPORT["integrations"]}
    assert [grades[i] for i in order] == sorted(grades[i] for i in order)[::-1]
    assert "A superuser database login" in html and "XERO_CLIENT_SECRET" in html
    assert "Accepted:" in html and "The app owns its database." in html
    assert 'href="/static/canvas/mallen-canvas.js' in html or "/static/canvas/mallen-canvas.js?v=" in html


def test_page_filters_by_module_grade_and_kind(client, accounts):
    html = client.get("/acme-erp/integrations?module=pbx").text
    assert re.findall(r'data-integration="([^"]+)"', html) == ["pbx-sip"]
    assert html.count('data-kind="integrations"') == 1
    html = client.get("/acme-erp/integrations?grade=B").text
    assert set(re.findall(r'data-integration="([^"]+)"', html)) == {"zabbix-webhook", "anthropic-erp"}
    html = client.get("/acme-erp/integrations?kind=mail").text
    assert re.findall(r'data-integration="([^"]+)"', html) == ["ms-mail-send"]
    assert "No integrations match" in client.get("/acme-erp/integrations?grade=D").text
    # an unknown filter value is ignored, not an error
    assert len(re.findall(r'data-integration="', client.get("/acme-erp/integrations?grade=Z").text)) == 7


def test_page_lists_the_workflows_that_use_each_integration(client, accounts):
    html = client.get("/acme-erp/integrations?module=core").text
    row = html[html.index('id="i-xero"'):]
    row = row[:row.index("</details>")]
    assert 'href="/acme-erp/workflows/billing.xero-token.refresh"' in row


# --- the workflow canvas's Integration block ---------------------------------------------------------------------

def _canvas_data(html):
    m = re.search(r'<script type="application/json" data-canvas-for="wf-canvas">(.*?)</script>', html, re.S)
    return json.loads(m.group(1))


def test_workflow_page_carries_the_integrations_its_steps_use(client, accounts):
    html = client.get("/acme-erp/workflows/billing.xero-token.refresh").text
    assert 'class="mallen-canvas" id="wf-canvas" data-kind="workflow"' in html
    assert "data-integration-link" not in html                 # Dashboard: names are plain text
    data = _canvas_data(html)
    ints = data["integrations"]
    assert {i["id"] for i in ints["integrations"]} == {"xero", "db-erp"}   # the call-xero and db steps
    assert ints["systems"] == {"xero": ["xero"], "db": ["db-erp"]}
    assert ints["vocab"]["auths"]["oauth2"] == "OAuth 2"
    assert data["workflow"]["id"] == "billing.xero-token.refresh" and data["workflow"]["steps"]
    assert "tasks" not in data["workflow"] and "changes" not in data["workflow"]
    # the Dashboard's own extras ride separately, switched on explicitly
    assert data["extras"]["tasks"] is True and data["extras"]["base"] == "/acme-erp"
    # each step's Tasks section is host content, in a <template> per step
    assert '<template data-mc-step-extra="call-xero">' in html


def test_a_step_naming_its_integrations_narrows_its_system():
    doc = EXPORT
    assert integrations_mod.resolve_step({"system": "graph"}, doc) == ["ms-mail-send", "ms-sharepoint"]
    assert integrations_mod.resolve_step({"system": "graph", "integrations": ["ms-sharepoint"]}, doc) == ["ms-sharepoint"]
    assert integrations_mod.resolve_step({"system": "graph", "integrations": ["gone"]}, doc) == []
    assert integrations_mod.step_ids({"system": "graph", "integrations": ["gone"]}, doc) == ["gone"]
    assert integrations_mod.resolve_step({"system": "internal"}, doc) == []
    assert integrations_mod.resolve_step({"system": "graph"}, None) == []
    wf = {"steps": [{"id": "a", "system": "graph", "integrations": ["ms-sharepoint"]}, {"id": "b", "system": "zabbix"}]}
    sub = integrations_mod.subset_for_workflow(wf, doc)
    assert [i["id"] for i in sub["integrations"]] == ["ms-sharepoint", "zabbix-webhook"]
    assert sub["systems"] == {"graph": ["ms-sharepoint"], "sharepoint": ["ms-sharepoint"], "zabbix": ["zabbix-webhook"]}


def test_workflow_page_without_integrations_still_draws(client, project):
    catalog_import.import_document(_catalog(project["slug"], None), project["slug"])
    data = _canvas_data(client.get(f"/{project['slug']}/workflows/billing.xero-token.refresh").text)
    assert data["integrations"] is None and data["workflow"]["steps"]


def test_merged_tags_every_row_with_its_source_for_more_sources_later():
    other = {"version": 1, "vocab": {"auths": {"oauth2": "OAuth two", "x": "X"}},
             "integrations": [dict(EXPORT["integrations"][0], id="zabbix-ro", module="dashboard", grade="D")]}
    m = integrations_mod.merged([("acme-erp", EXPORT), ("mallen-dashboard", other)])
    assert m["sources"] == ["acme-erp", "mallen-dashboard"]
    assert m["integrations"][0]["id"] == "zabbix-ro" and m["integrations"][0]["source"] == "mallen-dashboard"
    assert m["vocab"]["auths"]["oauth2"] == "OAuth 2" and m["vocab"]["auths"]["x"] == "X"   # first source wins
    assert m["summary"] == {"A": 3, "B": 2, "C": 2, "D": 1}
    assert integrations_mod.merged([]) is None


# --- exports/catalog.json for the ERP ----------------------------------------------------------------------------

def test_every_import_of_the_erp_writes_the_catalog_export(project, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EXPORTS_DIR", str(tmp_path))
    monkeypatch.setattr(config, "EXPORT_PROJECT", project["slug"])
    c = _catalog(project["slug"])
    c["workflows"][1]["steps"][0]["integrations"] = ["ms-sharepoint"]      # fetch-thread narrows graph
    catalog_import.import_document(c, project["slug"])
    out = json.loads((tmp_path / "catalog.json").read_text())
    assert out["version"] == 1 and out["commit"] == c["commit"] and out["integrations_version"] == 1
    steps = {(w["id"], s["id"]): s for w in out["workflows"] for s in w["steps"]}
    assert steps[("billing.xero-token.refresh", "call-xero")]["integrations"] == ["xero"]
    assert steps[("billing.xero-token.refresh", "load-token")]["integrations"] == ["db-erp"]
    assert steps[("sales.lead.qualify-from-email-reply", "fetch-thread")]["integrations"] == ["ms-sharepoint"]
    assert steps[("sales.lead.qualify-from-email-reply", "notify")]["integrations"] == []
    assert [s["id"] for s in out["skills"]] == ["qualify-reply"]
    assert out["skills"][0]["used_by"][0]["step"] == "classify"
    assert "integrations" not in out                       # the list itself isn't re-exported: the ERP has it
    # the stored catalog is left as the scanner wrote it (no resolved ids → no spurious workflow diffs)
    stored = repo.latest_scan(project["id"])["catalog"]
    assert "integrations" not in stored["workflows"][0]["steps"][1]
    assert not list(tmp_path.glob("*.tmp"))


def test_export_without_integrations_has_empty_step_lists(project, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EXPORTS_DIR", str(tmp_path))
    monkeypatch.setattr(config, "EXPORT_PROJECT", project["slug"])
    catalog_import.import_document(_catalog(project["slug"], None), project["slug"])
    out = json.loads((tmp_path / "catalog.json").read_text())
    assert out["integrations_version"] is None
    assert all(s["integrations"] == [] for w in out["workflows"] for s in w["steps"])


def test_other_projects_and_an_unset_dir_write_nothing(project, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EXPORTS_DIR", str(tmp_path))
    monkeypatch.setattr(config, "EXPORT_PROJECT", "acme-erp")
    catalog_import.import_document(_catalog(project["slug"]), project["slug"])
    assert not (tmp_path / "catalog.json").exists()
    monkeypatch.setattr(config, "EXPORTS_DIR", "")
    monkeypatch.setattr(config, "EXPORT_PROJECT", project["slug"])
    assert catalog_export.write(_catalog(project["slug"]), project["slug"]) is None


def test_a_failed_export_write_never_fails_the_import(project, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "EXPORTS_DIR", str(tmp_path / "missing"))
    monkeypatch.setattr(config, "EXPORT_PROJECT", project["slug"])
    result = catalog_import.import_document(_catalog(project["slug"]), project["slug"])
    assert result["scan_id"]


def test_merged_keeps_module_display_names_first_source_wins():
    erp = dict(EXPORT, modules={"core": "Mallen ERP (core)"})
    other = {"version": 1, "modules": {"core": "ignored", "dashboard": "Crewboss"}, "integrations": []}
    m = integrations_mod.merged([("acme-erp", erp), ("mallen-dashboard", other)])
    assert m["modules"] == {"core": "Mallen ERP (core)", "dashboard": "Crewboss"}
    assert integrations_mod.merged([("acme-erp", EXPORT)])["modules"] == {}   # optional: absent = empty map
