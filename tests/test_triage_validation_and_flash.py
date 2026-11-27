"""P2-8: suggest_triage validates priority/stream; templates survive unknown values; page writes that
fail validation flash instead of 500. Plus the /switch-project `next` nit."""
import json

import pytest
from psycopg.types.json import Jsonb

from app import mcp_server, repo
from app.auth import issue_token
from app.db import get_cursor
from app.routers.pages import safe_local_path


def _cookie(project):
    return {"Cookie": f"current_project={project['slug']}"}


@pytest.mark.parametrize("kw", [{"priority": "urgent"}, {"stream": "ops"}, {"priority": "MUST"}])
def test_repo_rejects_bad_suggestion(project, kw):
    t = repo.create_task(project["id"], "T", "", "should", "bug", [], {}, "owner")
    with pytest.raises(repo.BadRequest):
        repo.suggest_triage(t["id"], "claude:bugs", rationale="r", **kw)
    assert repo.get_task(t["id"])["triage_state"] == "new"


def test_api_suggest_triage_bad_values_are_400(client, project):
    t = repo.create_task(project["id"], "T", "", "should", "bug", [], {}, "owner")
    tok = issue_token("bugs", "s")
    resp = client.post(f"/api/tasks/{t['id']}/suggest-triage", json={"rationale": "r", "priority": "urgent"},
                       headers={"Authorization": f"Bearer {tok}"})
    assert resp.status_code == 400 and "priority must be one of" in resp.json()["detail"]
    ok = client.post(f"/api/tasks/{t['id']}/suggest-triage", json={"rationale": "r", "priority": "must"},
                     headers={"Authorization": f"Bearer {tok}"})
    assert ok.status_code == 200


def test_mcp_suggest_triage_bad_stream_raises_cleanly(project, mcp_caller):
    t = repo.create_task(project["id"], "T", "", "should", "bug", [], {}, "owner")
    mcp_caller(issue_token("bugs", "s"))
    with pytest.raises(repo.BadRequest, match="stream must be one of"):
        mcp_server.suggest_triage(t["id"], rationale="r", stream="ops")


def test_board_and_drawer_render_with_a_legacy_unknown_suggestion(client, project):
    t = repo.create_task(project["id"], "Legacy", "", "should", "bug", [], {}, "owner")
    with get_cursor() as cur:  # a suggestion stored before validation existed
        cur.execute("UPDATE tasks SET suggestion = %s, triage_state = 'suggested' WHERE id = %s",
                    (Jsonb({"priority": "urgent", "stream": "ops", "rationale": "old"}), t["id"]))
    assert client.get(f"/test-proj/tasks", headers=_cookie(project)).status_code == 200
    drawer = client.get(f"/test-proj/tasks/{t['id']}/panel", headers=_cookie(project))
    assert drawer.status_code == 200 and "Unknown" in drawer.text


def test_htmx_page_write_with_bad_value_flashes_not_500(client, project):
    resp = client.post(f"/test-proj/tasks", data={"title": "x", "stream": "ops", "priority": "should"},
                       headers={**_cookie(project), "HX-Request": "true"})
    assert resp.status_code == 400
    assert resp.headers["HX-Reswap"] == "none"
    flash = json.loads(resp.headers["HX-Trigger"])["mdd:flash"]
    assert "stream must be one of" in flash["message"] and flash["tone"] == "error"
    assert repo.list_tasks(project["id"]) == []


def test_htmx_drag_to_bad_bucket_flashes(client, project):
    t = repo.create_task(project["id"], "T", "", "should", "bug", [], {}, "owner")
    resp = client.post(f"/test-proj/tasks/{t['id']}/priority", data={"priority": "urgent"},
                       headers={**_cookie(project), "HX-Request": "true"})
    assert resp.status_code == 400 and "mdd:flash" in resp.headers["HX-Trigger"]


def test_full_page_form_with_bad_value_redirects_back_with_flash(client, project):
    resp = client.post(f"/test-proj/objectives", data={"title": "x", "target_value": "lots"},
                       headers={**_cookie(project), "Referer": "https://crewboss.example.com/objectives"},
                       follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/objectives"
    assert "crewboss_flash" in resp.headers["set-cookie"]
    page = client.get(f"/test-proj/objectives", headers={**_cookie(project), "Cookie": f"current_project={project['slug']}; "
                                              "crewboss_flash=\"target value must be a number\""})
    assert "target value must be a number" in page.text
    assert 'crewboss_flash=""' in page.headers.get("set-cookie", "")  # shown once, then cleared


def test_duplicate_project_slug_is_a_flash_not_500(client, project):
    resp = client.post("/projects", data={"slug": project["slug"], "name": "Dup", "short_name": "dup"},
                       headers={"Referer": "https://crewboss.example.com/tasks"}, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/tasks"


def test_unknown_task_page_write_is_404_flash(client, project):
    resp = client.post(f"/test-proj/tasks/999999/status", data={"status": "done"},
                       headers={**_cookie(project), "HX-Request": "true"})
    assert resp.status_code == 404 and "mdd:flash" in resp.headers["HX-Trigger"]


# --- nit: /switch-project only follows relative same-site paths -------------------------------------

@pytest.mark.parametrize("nxt,expected", [
    ("/tasks?view=list", "/tasks?view=list"),
    ("/", "/"),
    ("https://evil.example/", "/"),
    ("//evil.example/x", "/"),
    ("/\\evil.example", "/"),
    ("\\\\evil.example", "/"),
    ("javascript:alert(1)", "/"),
    ("tasks", "/"),
    ("/x\r\nSet-Cookie: a=b", "/"),
    ("", "/"),
])
def test_safe_local_path(nxt, expected):
    assert safe_local_path(nxt) == expected


def test_switch_project_route_is_gone_links_switch_instead(client, project):
    """The project dropdown is plain links to /<slug>/<section>; the old cookie-setting POST is gone."""
    resp = client.post("/switch-project", data={"slug": project["slug"], "next": "https://evil.example/"},
                       follow_redirects=False)
    assert resp.status_code in (404, 405)
    html = client.get(f"/{project['slug']}/workflows").text
    assert f'href="/{project["slug"]}/workflows"' in html and "data-project-link" not in html      # the pills are plain links (#771: no switcher)
