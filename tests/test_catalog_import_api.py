"""POST /api/catalog/import (the auto-rescan's way in), the `scanner` role token, broken workflows
(scanner `problems`), and stale carry-forward across consecutive scans."""
import copy
import json
from pathlib import Path

from app import catalog_import, repo, tokens
from app.auth import issue_token, resolve_session_token, resolve_token

FIXTURE = json.loads((Path(__file__).parent.parent / "fixtures" / "catalog-fixture.json").read_text())
WF = "billing.xero-token.refresh"


def _catalog(project, commit="a" * 40, **wf_overrides):
    c = copy.deepcopy(FIXTURE)
    c["project"] = project["slug"]
    c["commit"] = commit
    c["workflows"][0].update(wf_overrides)
    return c


def _annotated(project, commit, hash_, **extra):
    return _catalog(project, commit, confidence="annotated",
                    source_refs=[{"file": "app/xero_sync.py", "lines": "40-101", "hash": hash_}], **extra)


def _post(client, project, catalog, token):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post(f"/api/catalog/import?project={project['slug']}", json=catalog, headers=headers)


def _tweaks(project, title_prefix):
    return [t for t in repo.tasks_for_workflow(project["id"], WF)
            if t["stream"] == "tweak" and t["title"].startswith(title_prefix)]


# --- auth -----------------------------------------------------------------------------------------

def test_scanner_token_imports(client, project):
    token = issue_token("scanner", "auto-rescan")
    resp = _post(client, project, _catalog(project), token)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["workflow_count"] == 3 and body["broken_count"] == 0
    assert repo.latest_scan(project["id"])["commit"] == "a" * 40


def test_features_session_token_imports(client, project):
    resp = _post(client, project, _catalog(project), issue_token("features", "f"))
    assert resp.status_code == 201, resp.text


def test_other_streams_and_missing_tokens_are_refused(client, project):
    assert _post(client, project, _catalog(project), issue_token("bugs", "b")).status_code == 403
    assert _post(client, project, _catalog(project), None).status_code == 401
    assert _post(client, project, _catalog(project), "mdd_not-a-real-token").status_code == 401
    assert repo.latest_scan(project["id"]) is None


def test_scanner_token_is_not_a_session(client, project):
    token = issue_token("scanner", "auto-rescan")
    assert resolve_token(token)["stream"] == "scanner"
    assert resolve_session_token(token) is None            # → no MCP write access
    resp = client.post("/api/tasks", json={"project": project["slug"], "title": "nope", "stream": "bug"},
                       headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403
    resp = client.post("/api/sessions/heartbeat", json={"state": "online"},
                       headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


def test_non_catalog_body_is_a_400_from_the_shared_import_path(client, project):
    token = issue_token("scanner", "auto-rescan")
    resp = _post(client, project, {"commit": "x", "generated_at": "y", "workflows": "nope"}, token)
    assert resp.status_code == 400
    assert "workflows" in resp.json()["detail"]
    assert _post(client, {"slug": "no-such-project"}, _catalog(project), token).status_code == 404


def test_endpoint_and_cli_share_one_code_path(client, project, tmp_path, monkeypatch):
    calls = []
    real = catalog_import.import_document

    def spy(catalog, project_slug="acme-erp"):
        calls.append(project_slug)
        return real(catalog, project_slug)

    monkeypatch.setattr(catalog_import, "import_document", spy)
    assert _post(client, project, _catalog(project), issue_token("scanner", "s")).status_code == 201
    f = tmp_path / "scan.json"
    f.write_text(json.dumps(_catalog(project, commit="b" * 40)))
    assert catalog_import.main([str(f), "--project", project["slug"]]) == 0
    assert calls == [project["slug"], project["slug"]]


def test_tokens_cli_scanner_kind_quiet_prints_only_the_token(capsys):
    assert tokens.main(["create", "--kind", "scanner", "--name", "auto-rescan", "--quiet"]) == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1 and out[0].startswith("cb_")
    assert resolve_token(out[0])["stream"] == "scanner"


# --- broken ---------------------------------------------------------------------------------------

PROBLEMS = [{"code": "missing_function", "where": "steps[call-xero].code_ref", "step": "call-xero",
             "file": "app/xero_sync.py", "function": "refresh",
             "message": "steps[call-xero].code_ref: refresh() in app/xero_sync.py no longer exists"}]


def test_broken_workflow_is_flagged_and_raises_one_tweak_task(project):
    repo.import_catalog(project["slug"], _annotated(project, "a" * 40, "sha256:aaa"))
    result = repo.import_catalog(project["slug"], _annotated(project, "b" * 40, "sha256:bbb", problems=PROBLEMS))
    assert result["broken_count"] == 1
    assert result["stale_count"] == 0                     # broken supersedes stale

    listed = {w["id"]: w for w in repo.list_workflows(project["id"])}
    assert listed[WF]["broken"] is True and listed[WF]["stale"] is False
    assert [w["id"] for w in repo.list_workflows(project["id"], broken_only=True)] == [WF]
    assert repo.get_workflow(project["id"], WF)["broken"] is True

    broken_tasks = _tweaks(project, "Fix broken workflow annotation")
    assert len(broken_tasks) == 1
    assert "refresh() in app/xero_sync.py no longer exists" in broken_tasks[0]["body"]
    assert not _tweaks(project, "Update workflow layout")
    changes = [d for d in repo.recent_workflow_changes(project["id"], limit=20) if d["change_type"] == "broken"]
    assert len(changes) == 1

    # still broken next scan: no second task, no second 'broken' diff row
    repo.import_catalog(project["slug"], _annotated(project, "c" * 40, "sha256:bbb", problems=PROBLEMS))
    assert len(_tweaks(project, "Fix broken workflow annotation")) == 1
    changes = [d for d in repo.recent_workflow_changes(project["id"], limit=20) if d["change_type"] == "broken"]
    assert len(changes) == 1

    # closing the task WITHOUT fixing the annotation: the next scan raises it again
    repo.update_task(broken_tasks[0]["id"], {"status": "done"}, "owner")
    repo.import_catalog(project["slug"], _annotated(project, "d" * 40, "sha256:bbb", problems=PROBLEMS))
    assert len([t for t in _tweaks(project, "Fix broken workflow annotation") if t["status"] != "done"]) == 1

    # fixed: no longer broken
    repo.import_catalog(project["slug"], _annotated(project, "e" * 40, "sha256:ccc"))
    assert {w["id"]: w for w in repo.list_workflows(project["id"])}[WF]["broken"] is False


# --- stale carry-forward --------------------------------------------------------------------------

def test_stale_survives_later_unrelated_scans_until_its_task_is_closed(project):
    repo.import_catalog(project["slug"], _annotated(project, "a" * 40, "sha256:aaa"))
    assert repo.import_catalog(project["slug"], _annotated(project, "b" * 40, "sha256:bbb"))["stale_count"] == 1

    # a later scan (some other commit) with the same hash: still stale, no second task
    third = _annotated(project, "c" * 40, "sha256:bbb")
    third["workflows"][1]["summary"] = "an unrelated workflow changed"
    result = repo.import_catalog(project["slug"], third)
    assert result["stale_count"] == 0 and result["stale_carried"] == 1
    assert {w["id"]: w for w in repo.list_workflows(project["id"])}[WF]["stale"] is True
    assert len(_tweaks(project, "Update workflow layout")) == 1

    # the annotation was checked and the task closed: the flag drops on the next scan
    repo.update_task(_tweaks(project, "Update workflow layout")[0]["id"], {"status": "done"}, "owner")
    repo.import_catalog(project["slug"], _annotated(project, "d" * 40, "sha256:bbb"))
    assert {w["id"]: w for w in repo.list_workflows(project["id"])}[WF]["stale"] is False


def test_stale_clears_when_the_annotation_is_revisited(project):
    repo.import_catalog(project["slug"], _annotated(project, "a" * 40, "sha256:aaa"))
    repo.import_catalog(project["slug"], _annotated(project, "b" * 40, "sha256:bbb"))
    revisited = _annotated(project, "c" * 40, "sha256:bbb", summary="Re-checked against the new code.")
    result = repo.import_catalog(project["slug"], revisited)
    assert result["stale_carried"] == 0
    assert {w["id"]: w for w in repo.list_workflows(project["id"])}[WF]["stale"] is False


def test_pages_render_broken_workflow(client, project):
    repo.import_catalog(project["slug"], _annotated(project, "a" * 40, "sha256:aaa", problems=PROBLEMS))
    client.cookies.set("current_project", project["slug"])
    try:
        listing = client.get(f"/test-proj/workflows")
        assert listing.status_code == 200 and ">broken<" in listing.text
        detail = client.get(f"/test-proj/workflows/{WF}")
        assert detail.status_code == 200
        assert "layout broken" in detail.text and "no longer exists" in detail.text
        home = client.get("/")
        assert home.status_code == 200
    finally:
        client.cookies.clear()
