"""Per-project URLs: every page lives at /<slug>/…, the old unprefixed routes redirect into the current
project, unknown slugs 404, fixed top-level paths (/api, /mcp, /static, /healthz) are never shadowed by a
project, and a /<slug>/ route never touches another project's task."""
import json

import pytest

from app import repo

OWN = {"Origin": "https://crewboss.example.com", "Sec-Fetch-Site": "same-origin"}
EVIL = {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"}
PAGES = ["", "/board", "/tasks/new", "/workflows", "/ai-features", "/build-skills", "/rules", "/knowledge", "/objectives", "/activity",
         "/health"]


@pytest.fixture
def other():
    return repo.create_project("other-proj", "Other Project", "client", "owner")


@pytest.mark.parametrize("suffix", PAGES)
def test_every_project_page_renders_under_its_slug(client, project, suffix):
    r = client.get(f"/{project['slug']}{suffix}", follow_redirects=False)
    assert r.status_code == 200, (suffix, r.status_code)
    # links, forms and htmx calls on the page stay inside the project
    assert 'data-base="/test-proj"' in r.text


def test_project_links_are_scoped(client, project, other):
    # a page inside an account lists that account's projects only (task #368): put both in one
    from app import tree
    acct = repo.create_project("acct", "Acct", "internal", "owner")
    tree.set_place(acct["id"], "owner", None, "account")
    for p in (project, other):
        tree.set_place(p["id"], "owner", acct["id"])
    outsider = repo.create_project("outsider", "Outsider", "internal", "owner")
    assert 'href="/outsider/board"' not in client.get("/test-proj/board").text
    assert outsider
    html = client.get("/test-proj/board").text
    assert 'href="/test-proj/tasks/new"' in html and 'hx-get="/test-proj/tasks/board?layout=board' in html
    assert 'data-cat="workflows"' in html  # a view in the record's "+" catalogue (#773)
    assert 'href="/"' in html and 'data-nav="now"' in html  # the fixed menu's Now row
    assert 'data-record="test-proj"' in html          # the menu's Projects section lists the project (#771)
    # the old task page is the status Board now, filters and ?open= kept
    r = client.get("/test-proj/tasks?assignee=owner&open=3", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/test-proj/board?assignee=owner&open=3"


@pytest.mark.parametrize("slug", ["nope", "Test-Proj", "test_proj", "a" * 60, "%2e%2e"])
def test_unknown_or_malformed_slug_is_404(client, project, slug):
    assert client.get(f"/{slug}").status_code == 404
    assert client.get(f"/{slug}/tasks").status_code == 404


def test_archived_project_pages_still_open(client, project):
    repo.set_project_archived(project["id"], True, "owner")
    r = client.get("/test-proj/board")
    assert r.status_code == 200 and "archived" in r.text
    # bringing it back lives in its Danger zone (task #148), not the switcher
    assert "Unarchive" in client.get("/test-proj/settings").text


def test_visiting_a_project_remembers_it(client, project, other):
    r = client.get("/other-proj/workflows", follow_redirects=False)
    assert "current_project=other-proj" in r.headers.get("set-cookie", "")


# --- legacy unprefixed routes ---------------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["/tasks", "/tasks?assignee=owner&open=4", "/tasks/new", "/tasks/new?rule=x.y",
                                  "/workflows", "/workflows/billing.xero-token.refresh?failed_step=a&error=b",
                                  "/skills?open=x", "/rules", "/knowledge", "/objectives", "/activity", "/health",
                                  "/tasks/12/panel"])
def test_legacy_get_redirects_301_into_the_current_project(client, project, other, path):
    r = client.get(path, headers={"Cookie": "current_project=other-proj"}, follow_redirects=False)
    assert r.status_code == 301
    assert r.headers["location"] == "/other-proj" + path
    # the target depends on a cookie: never cached by the browser
    assert r.headers["cache-control"] == "no-store"


def test_legacy_redirect_defaults_without_a_cookie(client, project):
    # no cookie, no acme-erp: the first active project
    r = client.get("/tasks/new", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/test-proj/tasks/new"


def test_legacy_redirect_ignores_an_archived_or_unknown_cookie(client, project, other):
    repo.set_project_archived(other["id"], True, "owner")
    for cookie in ("current_project=other-proj", "current_project=nope", "current_project=%2F%2Fevil.example"):
        r = client.get("/tasks", headers={"Cookie": cookie}, follow_redirects=False)
        assert r.headers["location"] == "/test-proj/tasks", cookie


def test_legacy_redirect_with_no_projects_goes_home(client):
    with repo.get_cursor() as cur:
        cur.execute("DELETE FROM projects")
    r = client.get("/workflows", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/"


@pytest.mark.parametrize("path", ["/tasks//evil.example", "/tasks/%2F%2Fevil.example", "/tasks/..%2F..%2Fx",
                                  "/workflows/\\evil.example"])
def test_legacy_redirect_never_leaves_the_site(client, project, path):
    r = client.get(path, follow_redirects=False)
    loc = r.headers["location"]
    assert loc.startswith("/") and not loc.startswith("//") and "\\" not in loc
    assert loc == "/" or loc.startswith("/test-proj/")


def test_legacy_post_is_308_and_still_guarded(client, project):
    t = repo.create_task(project["id"], "Old tab", "", "should", "bug", [], {}, "owner")
    r = client.post(f"/tasks/{t['id']}/status", data={"status": "done"}, headers=OWN, follow_redirects=False)
    assert r.status_code == 308 and r.headers["location"] == f"/test-proj/tasks/{t['id']}/status"
    # a cross-site write is refused BEFORE any redirect
    r = client.post(f"/tasks/{t['id']}/status", data={"status": "done"}, headers=EVIL, follow_redirects=False)
    assert r.status_code == 403
    assert repo.get_task(t["id"])["status"] != "done"


# --- fixed paths are never shadowed by /{slug} ------------------------------------------------------------------

def test_fixed_paths_still_work(client, project):
    assert client.get("/healthz").json() == {"ok": True}
    assert client.get("/api/projects").status_code == 401  # the API route, not /{slug}: it asks for a token (#368)
    assert client.get("/api").status_code == 404
    fav = client.get("/favicon.ico")
    assert fav.status_code == 200 and fav.headers["content-type"] == "image/x-icon" and fav.content[:4] == b"\0\0\1\0"


def test_bare_mcp_path_still_reaches_mcp(client):
    """Clients are configured with …/mcp (no slash): it must still 307 to /mcp/, not hit a project route."""
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
    r = client.post("/mcp", content=json.dumps(body), follow_redirects=False,
                    headers={"Host": "127.0.0.1", "Content-Type": "application/json"})
    assert r.status_code == 307 and r.headers["location"] == "/mcp/"
    r = client.post("/mcp", content=json.dumps(body), headers={
        "Host": "127.0.0.1", "Accept": "application/json, text/event-stream", "Content-Type": "application/json"})
    assert r.status_code == 200 and "crewboss" in r.text


# --- a project URL never reaches into another project ------------------------------------------------------------

def test_task_routes_refuse_another_projects_task(client, project, other):
    theirs = repo.create_task(other["id"], "Theirs", "", "should", "bug", [], {}, "owner")
    sub = repo.add_subtasks(theirs["id"], ["step"], "owner")[0]
    h = {**OWN, "HX-Request": "true"}
    assert client.get(f"/test-proj/tasks/{theirs['id']}/panel").status_code == 404
    for path, data in [(f"/test-proj/tasks/{theirs['id']}", {"title": "hijack"}),
                       (f"/test-proj/tasks/{theirs['id']}/status", {"status": "done"}),
                       (f"/test-proj/tasks/{theirs['id']}/priority", {"priority": "must"}),
                       (f"/test-proj/tasks/{theirs['id']}/assignee", {"assignee": "owner"}),
                       (f"/test-proj/tasks/{theirs['id']}/comments", {"body": "x"}),
                       (f"/test-proj/tasks/{theirs['id']}/subtasks", {"text": "x"}),
                       (f"/test-proj/tasks/{theirs['id']}/triage/accept", {}),
                       (f"/test-proj/subtasks/{sub['id']}/toggle", {}),
                       (f"/test-proj/subtasks/{sub['id']}/delete", {})]:
        r = client.post(path, data=data, headers=h)
        assert r.status_code == 404, path
    t = repo.get_task(theirs["id"])
    assert t["title"] == "Theirs" and t["status"] == "backlog" and t["comments"] == [] and len(t["subtasks"]) == 1


def test_open_a_task_from_another_project_follows_it(client, project, other):
    theirs = repo.create_task(other["id"], "Theirs", "", "should", "bug", [], {}, "owner")
    for view in ("/board",):          # (/focus is a redirect to Summary now, #773)
        r = client.get(f"/test-proj{view}?open={theirs['id']}", follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == f"/other-proj{view}?open={theirs['id']}"


def test_new_request_lands_in_the_url_project_not_the_cookie(client, project, other):
    r = client.post("/other-proj/tasks/new", data={"title": "Here", "stream": "bug"},
                    headers={**OWN, "Cookie": "current_project=test-proj"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/other-proj/board?open=")
    assert [t["title"] for t in repo.list_tasks(other["id"])] == ["Here"]
    assert repo.list_tasks(project["id"]) == []


@pytest.mark.parametrize("path,data", [("/test-proj/tasks", {"title": "x", "stream": "bug"}),
                                       ("/test-proj/tasks/new", {"title": "x"}),
                                       ("/test-proj/knowledge", {"title": "x"}),
                                       ("/test-proj/objectives", {"title": "x"}),
                                       ("/projects", {"slug": "zz", "name": "x"})])
def test_new_route_writes_refuse_cross_site(client, project, path, data):
    before = (len(repo.list_tasks(project["id"])), len(repo.list_knowledge(project["id"])),
              len(repo.list_projects(include_archived=True)))
    for bad in (EVIL, {"Origin": "https://other.example.com", "Sec-Fetch-Site": "same-site"}, {"Origin": "null"}):
        assert client.post(path, data=data, headers=bad).status_code == 403, (path, bad)
    assert (len(repo.list_tasks(project["id"])), len(repo.list_knowledge(project["id"])),
            len(repo.list_projects(include_archived=True))) == before


# --- knowledge-v2 under /<slug>/ ----------------------------------------------------------------------------------

def test_knowledge_routes_live_under_the_project(client, project, other):
    mine = repo.create_knowledge(project["id"], "note", "Mine", "body", [], "", False, "owner", summary="s")
    theirs = repo.create_knowledge(other["id"], "note", "Theirs", "body", [], "", False, "owner", summary="s")
    assert client.get(f"/test-proj/knowledge/{mine['id']}/body").status_code == 200
    assert client.get(f"/test-proj/knowledge/{theirs['id']}/body").status_code == 404
    assert client.get(f"/test-proj/knowledge/{theirs['id']}/edit").status_code == 404
    r = client.post(f"/test-proj/knowledge/{theirs['id']}", data={"summary": "hijack"}, headers=OWN, follow_redirects=False)
    assert r.status_code in (303, 404) and repo.get_knowledge(theirs["id"])["summary"] == "s"
    html = client.get("/test-proj/knowledge/new").text
    assert 'data-upload-url="/test-proj/knowledge/images"' in html and "/static/js/knowledge-editor.js?v=" in html
    r = client.post("/test-proj/knowledge", data={"kind": "note", "title": "New", "summary": "s"}, headers=OWN,
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/test-proj/knowledge?open=")


@pytest.mark.parametrize("path", ["/knowledge", "/knowledge?open=3", "/knowledge/new?kind=objective", "/objectives",
                                  "/knowledge/4/edit"])
def test_legacy_knowledge_urls_redirect(client, project, path):
    r = client.get(path, follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "/test-proj" + path


def test_media_stays_global_and_is_reserved(client, project):
    with pytest.raises(repo.BadRequest, match="reserved"):
        repo.create_project("media", "Media", "internal", "owner")
    assert client.get("/media/knowledge/999999.png").status_code == 404
