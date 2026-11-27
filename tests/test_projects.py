from app import repo


def test_create_and_get_project():
    p = repo.create_project("acme", "Acme Co", "client", "owner")
    assert p["slug"] == "acme"
    assert repo.get_project(p["id"])["name"] == "Acme Co"
    assert repo.get_project_by_slug("acme")["id"] == p["id"]


def test_archive_filter_excludes_by_default():
    active = repo.create_project("active-one", "Active One", "internal", "owner")
    archived = repo.create_project("archived-one", "Archived One", "internal", "owner")
    repo.set_project_archived(archived["id"], True, "owner")

    visible = {p["id"] for p in repo.list_projects(include_archived=False)}
    assert active["id"] in visible
    assert archived["id"] not in visible

    everything = {p["id"] for p in repo.list_projects(include_archived=True)}
    assert active["id"] in everything
    assert archived["id"] in everything


def test_unarchive_restores_visibility():
    p = repo.create_project("temp", "Temp", "internal", "owner")
    repo.set_project_archived(p["id"], True, "owner")
    assert p["id"] not in {x["id"] for x in repo.list_projects()}
    repo.set_project_archived(p["id"], False, "owner")
    assert p["id"] in {x["id"] for x in repo.list_projects()}


def test_project_type_validation():
    import pytest

    with pytest.raises(repo.BadRequest):
        repo.create_project("bad", "Bad", "not-a-type", "owner")


# --- slugs are URL path segments (/<slug>/tasks): validated, reserved words refused ---------------------

import pytest  # noqa: E402


@pytest.mark.parametrize("slug", ["acme", "acme-retail", "a", "x9", "acme-erp", "a" * 50])
def test_valid_slugs_are_accepted(slug):
    assert repo.create_project(slug, "Name", "internal", "owner")["slug"] == slug


@pytest.mark.parametrize("slug", ["", "Acme", "acme_retail", "-acme", "acme-", "acme retail", "acme/x", "a" * 51,
                                  "..", "acme.io", "%2e", "ácme", None])
def test_malformed_slugs_are_refused(slug):
    with pytest.raises(repo.BadRequest):
        repo.create_project(slug, "Name", "internal", "owner")


@pytest.mark.parametrize("slug", ["api", "mcp", "static", "healthz", "tasks", "workflows", "skills", "rules",
                                  "knowledge", "objectives", "activity", "health", "subtasks", "projects"])
def test_reserved_slugs_are_refused(slug):
    with pytest.raises(repo.BadRequest, match="reserved"):
        repo.create_project(slug, "Name", "internal", "owner")
    assert slug not in {p["slug"] for p in repo.list_projects(include_archived=True)}


def test_reserved_and_bad_slugs_refused_over_api_and_form(client, project, monkeypatch):
    from app import config
    from app.auth import issue_token

    # POST /api/projects is the overseer's only (task #368): any other session gets a 404
    assert client.post("/api/projects", json={"slug": "fine-slug", "name": "X", "short_name": "xx"},
                       headers={"Authorization": f"Bearer {issue_token('features', 'f')}"}).status_code == 404
    monkeypatch.setattr(config, "OVERSEER_PROJECT", project["slug"])
    token = issue_token("all", "t", project_id=project["id"])
    r = client.post("/api/projects", json={"slug": "tasks", "name": "X", "short_name": "xx"}, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 400 and "reserved" in r.json()["detail"]
    r = client.post("/api/projects", json={"slug": "Bad Slug", "name": "X", "short_name": "xx"}, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 400
    r = client.post("/projects", data={"slug": "api", "name": "X", "short_name": "xx"}, follow_redirects=False,
                    headers={"Origin": "https://crewboss.example.com", "Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 303 and "crewboss_flash" in r.headers.get("set-cookie", "")
    assert not {"tasks", "Bad Slug", "api"} & {p["slug"] for p in repo.list_projects(include_archived=True)}


def test_blank_project_name_is_refused():
    with pytest.raises(repo.BadRequest):
        repo.create_project("fine", "   ", "internal", "owner")
