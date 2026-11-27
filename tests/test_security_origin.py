"""P0(b): browser writes outside /api and /mcp must come from the dashboard's own origin."""
import pytest

from app import repo
from app.auth import issue_token
from app.origin_guard import origin_host, rejection_reason

OWN = "https://crewboss.example.com"


def _form_post(client, project, headers, title="From the browser"):
    h = {"Cookie": f"current_project={project['slug']}", **headers}
    return client.post(f"/test-proj/tasks", data={"title": title, "stream": "bug", "priority": "should"}, headers=h)


def test_cross_site_post_is_refused(client, project):
    # compare before/after: when this runs first, the app's startup seed has already queued its own outbox row (#420)
    outbox_before = repo.list_outbox("bugs")
    resp = _form_post(client, project, {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"})
    assert resp.status_code == 403
    assert repo.list_tasks(project["id"]) == []
    assert repo.list_outbox("bugs") == outbox_before


def test_sibling_dev_site_is_refused(client, project):
    # *.example.com siblings are "same-site" to the browser but must not be able to write here.
    resp = _form_post(client, project, {"Origin": "https://other.example.com", "Sec-Fetch-Site": "same-site"})
    assert resp.status_code == 403
    assert repo.list_tasks(project["id"]) == []


def test_foreign_origin_without_fetch_metadata_is_refused(client, project):
    resp = _form_post(client, project, {"Origin": "https://evil.example"})
    assert resp.status_code == 403


def test_null_origin_is_refused(client, project):
    resp = _form_post(client, project, {"Origin": "null"})
    assert resp.status_code == 403


def test_same_origin_post_passes(client, project):
    resp = _form_post(client, project, {"Origin": OWN, "Sec-Fetch-Site": "same-origin"})
    assert resp.status_code == 200
    assert [t["title"] for t in repo.list_tasks(project["id"])] == ["From the browser"]


def test_htmx_request_passes(client, project):
    task = repo.create_task(project["id"], "Drag me", "", "should", "bug", [], {}, "system")
    resp = client.post(f"/test-proj/tasks/{task['id']}/status", data={"status": "doing"}, headers={
        "Cookie": f"current_project={project['slug']}", "HX-Request": "true", "HX-Current-URL": OWN + "/tasks",
        "Origin": OWN, "Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "cors",
    })
    assert resp.status_code == 200
    assert repo.get_task(task["id"])["status"] == "doing"


def test_mesh_and_local_origins_pass(client, project):
    for origin in ("https://crewboss.example.net", "https://crewboss.internal.example.com",
                   "http://127.0.0.1:8931", "http://localhost:8931"):
        resp = _form_post(client, project, {"Origin": origin, "Sec-Fetch-Site": "same-origin"}, title=origin)
        assert resp.status_code == 200, origin


def test_non_browser_post_without_headers_passes(client, project):
    assert _form_post(client, project, {}).status_code == 200


def test_get_is_never_blocked(client, project):
    resp = client.get(f"/test-proj/tasks", headers={"Cookie": f"current_project={project['slug']}",
                                         "Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example"})
    assert resp.status_code == 200


def test_api_is_exempt_bearer_token_is_its_gate(client, project):
    token = issue_token("bugs", "t")
    resp = client.post("/api/tasks", json={"project": project["slug"], "title": "via api", "stream": "bug"},
                       headers={"Authorization": f"Bearer {token}", "Origin": "https://evil.example",
                                "Sec-Fetch-Site": "cross-site"})
    assert resp.status_code == 201


def test_no_cors_headers_are_emitted(client):
    resp = client.get("/api/sessions", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in {k.lower() for k in resp.headers}
    pre = client.options("/api/tasks", headers={"Origin": "https://evil.example",
                                                "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in {k.lower() for k in pre.headers}


@pytest.mark.parametrize("origin,expected", [
    ("https://crewboss.example.com", "crewboss.example.com"),
    ("https://crewboss.example.com:443", "crewboss.example.com"),
    ("http://127.0.0.1:8931", "127.0.0.1:8931"),
    ("HTTPS://CREWBOSS.EXAMPLE.COM", "crewboss.example.com"),
    ("null", None),
    ("", None),
])
def test_origin_host_parsing(origin, expected):
    assert origin_host(origin) == expected


def test_allowed_hosts_are_configurable():
    hdrs = {"origin": "https://other.example", "sec-fetch-site": "same-origin"}
    assert rejection_reason("POST", "/tasks", hdrs, allowed_hosts=("crewboss.example.com",))
    assert rejection_reason("POST", "/tasks", hdrs, allowed_hosts=("other.example",)) is None
    # /apiary is not /api
    assert rejection_reason("POST", "/apiary", {"origin": "https://x.example"}, ("crewboss.example.com",))


def test_retired_vpn_origin_is_refused(client, project):
    resp = _form_post(client, project, {"Origin": "http://dash.dev-box.example.internal", "Sec-Fetch-Site": "same-origin"})
    assert resp.status_code == 403
