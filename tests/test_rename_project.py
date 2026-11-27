"""#577: the owner's Rename project (tree.rename_project + POST /{slug}/rename). Name always, slug only when changed
and not locked; a changed slug leaves the old one as an alias (project_slug_aliases) so old page URLs redirect (GET)
or 404 (writes), and an API/MCP caller naming the old slug still resolves. The short name NEVER changes (session
tokens are filed under tokens/<short>/<role>), and the slug is locked while the project runs its own session."""
import pytest

from app import changes, config, drift, health_reports, repo, scope, tree
from app.auth import issue_token
from app.db import get_cursor
from app.repo import BadRequest, Forbidden

OWN = {"Origin": "https://crewboss.example.com", "Sec-Fetch-Site": "same-origin"}
EVIL = {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"}


def mk(slug, name, short=None, roles=()):
    return repo.create_project(slug, name, "internal", "owner", short_name=short or slug[:12],
                               session_roles=list(roles))


@pytest.fixture
def acme():
    """Acme (an account-like parent running the session) → acme-app, worked by Acme's session (no session of its own,
    like mallen-phone under mallen): its slug may change."""
    parent = mk("acme", "Acme", "acme", ["all"])
    child = mk("acme-app", "Acme App", "acmeapp")
    tree.set_place(child["id"], "owner", parent["id"])
    return repo.get_project(child["id"])


def _activity(project_id):
    with get_cursor() as cur:
        cur.execute("SELECT actor, verb, summary FROM activity WHERE project_id = %s AND verb = 'renamed' "
                    "ORDER BY id", (project_id,))
        return cur.fetchall()


def _aliases():
    with get_cursor() as cur:
        cur.execute("SELECT slug, project_id FROM project_slug_aliases ORDER BY slug")
        return [(r["slug"], r["project_id"]) for r in cur.fetchall()]


# --- tree.rename_project -----------------------------------------------------------------------------------------

def test_name_only_keeps_slug_and_short_name(acme):
    row = tree.rename_project(acme["id"], "owner", "  Acme   Portal ")
    assert (row["name"], row["slug"], row["short_name"]) == ("Acme Portal", "acme-app", "acmeapp")
    assert _aliases() == []
    [a] = _activity(acme["id"])
    assert a["actor"] == "owner" and "'Acme App' → 'Acme Portal'" in a["summary"]


def test_non_owner_is_refused(acme):
    for actor in ("claude:features", "claude:overseer", "system", ""):
        with pytest.raises(Forbidden):
            tree.rename_project(acme["id"], actor, "Hijacked", slug="hijacked")
    assert repo.get_project(acme["id"])["name"] == "Acme App" and _activity(acme["id"]) == []


@pytest.mark.parametrize("bad", ["Acme App", "-acme", "acme-", "acme_app", "api", "health", "a" * 51])
def test_slug_must_be_valid(acme, bad):
    with pytest.raises(BadRequest):
        tree.rename_project(acme["id"], "owner", "Acme App", slug=bad)
    assert repo.get_project(acme["id"])["slug"] == "acme-app"


def test_slug_must_be_unique_against_current_and_old_slugs(acme):
    other = mk("other", "Other")
    with pytest.raises(BadRequest, match="already exists"):
        tree.rename_project(acme["id"], "owner", "Acme App", slug="other")
    tree.rename_project(other["id"], "owner", "Other", slug="other-two")       # 'other' is now other's OLD slug
    with pytest.raises(BadRequest, match="old slug"):
        tree.rename_project(acme["id"], "owner", "Acme App", slug="other")
    with pytest.raises(BadRequest, match="old slug"):                          # nor can a new project take it
        repo.create_project("other", "New", "internal", "owner", short_name="newp")


def test_empty_name_is_refused(acme):
    with pytest.raises(BadRequest):
        tree.rename_project(acme["id"], "owner", "   ")


def test_slug_change_records_activity_and_an_alias(acme):
    row = tree.rename_project(acme["id"], "owner", "Acme Portal", slug="acme-portal")
    assert row["slug"] == "acme-portal" and row["short_name"] == "acmeapp"
    assert _aliases() == [("acme-app", acme["id"])]
    [a] = _activity(acme["id"])
    assert "slug acme-app → acme-portal" in a["summary"]


def test_renaming_back_drops_the_alias(acme):
    tree.rename_project(acme["id"], "owner", "Acme App", slug="acme-portal")
    tree.rename_project(acme["id"], "owner", "Acme App", slug="acme-app")
    assert _aliases() == [("acme-portal", acme["id"])]
    assert repo.get_project_by_slug("acme-app")["id"] == acme["id"]
    assert repo.get_project_by_slug("acme-portal")["slug"] == "acme-app"


# --- the short name never changes --------------------------------------------------------------------------------

def test_rename_has_no_way_to_change_the_short_name(client, acme):
    with pytest.raises(TypeError):
        tree.rename_project(acme["id"], "owner", "Acme App", short_name="other")     # noqa — no such parameter
    r = client.post("/acme-app/rename", data={"name": "Acme Portal", "slug": "acme-app", "short_name": "hijack"},
                    headers=OWN, follow_redirects=False)
    assert r.status_code == 303
    row = repo.get_project(acme["id"])
    assert row["name"] == "Acme Portal" and row["short_name"] == "acmeapp"
    t = client.get("/acme-app/settings").text
    assert 'name="short_name"' not in t.split("data-rename-project", 1)[1].split("</section>", 1)[0]


def test_a_used_short_name_is_never_given_out_again(acme):
    """Short names are immutable and projects are archived, never deleted, so every short name ever used stays taken:
    a new project can't pick up an old project's session tokens (tokens/<short>/<role>)."""
    old = mk("old-proj", "Old", "oldp", ["all"])
    repo.set_project_archived(old["id"], True, "owner")
    for short in ("oldp", "acmeapp", "acme"):
        with pytest.raises(BadRequest, match="already exists"):
            repo.create_project(f"new-{short}", "New", "internal", "owner", short_name=short)


# --- slug locks --------------------------------------------------------------------------------------------------

def test_a_project_running_its_own_session_keeps_its_slug(acme):
    parent = repo.get_project_by_slug("acme")
    with pytest.raises(BadRequest, match=r"runs its own Claude session \(acme\); change the slug when its sessions "
                                         r"are off, or rename only the name"):
        tree.rename_project(parent["id"], "owner", "Acme", slug="acme-co")
    assert tree.rename_project(parent["id"], "owner", "Acme Co")["name"] == "Acme Co"      # name only: fine
    assert repo.get_project(parent["id"])["slug"] == "acme"
    per_stream = mk("streams", "Streams", "strm", ["features", "reviewer"])
    assert "(strm-features, strm-review)" in tree.slug_locked(per_stream)
    # sessions off: the slug is free again
    with get_cursor() as cur:
        cur.execute("UPDATE projects SET session_roles = '{}' WHERE id = %s", (parent["id"],))
    assert tree.rename_project(parent["id"], "owner", "Acme Co", slug="acme-co")["slug"] == "acme-co"


def test_a_project_worked_by_an_ancestors_session_routes_by_id_after_a_slug_change(acme):
    """acme-app has no session of its own: its events go to Acme's session by project id, so a new slug changes
    nothing for that session."""
    parent = repo.get_project_by_slug("acme")
    assert tree.slug_locked(acme) == ""
    tree.rename_project(acme["id"], "owner", "Acme App", slug="acme-mobile")
    assert acme["id"] in tree.routed_project_ids(parent["id"])
    assert tree.session_holder(acme["id"])["id"] == parent["id"]
    task = repo.create_task(acme["id"], "after the rename", "", "should", "feature", [], {}, "owner", assignee="ai")
    with get_cursor() as cur:
        cur.execute("SELECT project_id FROM outbox WHERE task_id = %s", (task["id"],))
        assert {r["project_id"] for r in cur.fetchall()} == {acme["id"]}
    sql, params = repo._outbox_scope(repo.get_project(parent["id"]), "all")
    assert acme["id"] in params[0]


def test_a_slug_named_by_the_servers_settings_stays(monkeypatch, acme):
    monkeypatch.setattr(config, "HEALTH_PROJECT", "acme-app")
    with pytest.raises(BadRequest, match="System Health"):
        tree.rename_project(acme["id"], "owner", "Acme App", slug="acme-portal")
    assert tree.rename_project(acme["id"], "owner", "Acme Renamed")["name"] == "Acme Renamed"   # the name still can
    monkeypatch.setattr(config, "HEALTH_PROJECT", "acme-erp")
    monkeypatch.setattr(config, "PLATFORM_KEY_STORES", {"acme-app": {"url": "https://x.invalid"}})
    with pytest.raises(BadRequest, match="platform key store"):
        tree.rename_project(acme["id"], "owner", "Acme App", slug="acme-portal")


def test_health_feeds_drift_sites_and_the_launch_fallback_pin_slugs(monkeypatch, acme):
    project = dict(acme)
    assert tree.slug_locked(project) == ""
    health_reports.use_config({"acme-app": {"mode": "push", "expect_every_seconds": 120, "sources": [],
                                            "erp": False, "erp_source": None}})
    try:
        assert "health feed" in tree.slug_locked(project)
    finally:
        health_reports.use_config(None)
    monkeypatch.setattr(drift, "load_config", lambda path=None: {"admin_sites": {"acme-app": {}}, "os_timers": []})
    assert "admin site" in tree.slug_locked(project)
    monkeypatch.setattr(drift, "load_config", lambda path=None: {"admin_sites": {}, "os_timers": []})
    assert tree.slug_locked(project) == ""
    assert "boot fallback" in tree.slug_locked(dict(project, slug="boot-acct", session_roles=[]))
    monkeypatch.undo()                                               # the real files name acme-erp in both

    pins = tree._pinned_slugs()
    assert "boot-acct" in pins and "acme-erp" in pins
    assert "acme-erp" in health_reports.load_config() and "acme-erp" in drift.load_config()["admin_sites"]


# --- resolving an old slug ---------------------------------------------------------------------------------------

def test_api_and_mcp_callers_resolve_an_old_slug(client, acme):
    tree.rename_project(acme["id"], "owner", "Acme App", slug="acme-portal")
    assert scope.project_for(None, "acme-app")["id"] == acme["id"]
    parent = repo.get_project_by_slug("acme")
    tok = issue_token("all", "t-acme", project_id=parent["id"])          # the session that works acme-app
    repo.create_task(acme["id"], "kept", "", "should", "feature", [], {}, "owner")
    r = client.get("/api/tasks", params={"project": "acme-app"}, headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text
    assert "kept" in r.text


# --- the page ----------------------------------------------------------------------------------------------------

def test_overview_shows_the_rename_block(client, acme, monkeypatch):
    t = client.get("/acme-app/settings").text
    assert "data-rename-project" in t and 'action="/acme-app/rename"' in t
    assert 'value="Acme App"' in t and "data-rename-slug-locked" not in t
    monkeypatch.setattr(config, "HEALTH_PROJECT", "acme-app")
    t = client.get("/acme-app/settings").text
    assert "data-rename-slug-locked" in t and "System Health" in t
    t = client.get("/acme/settings").text                              # runs its own session
    assert "data-rename-slug-locked" in t and "runs its own Claude session (acme)" in t


def test_owner_renames_from_the_page_and_old_urls_redirect(client, acme):
    r = client.post("/acme-app/rename", data={"name": "Acme Portal", "slug": "acme-portal"},
                    headers={**OWN, "Referer": "https://crewboss.example.com/acme-app/settings"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/acme-portal/settings"
    assert "crewboss_flash" in r.headers.get("set-cookie", "")
    row = repo.get_project(acme["id"])
    assert (row["name"], row["slug"], row["short_name"]) == ("Acme Portal", "acme-portal", "acmeapp")
    done = changes.grouped(acme["id"])["done"]
    assert any(c["title"].startswith("Renamed Acme App to Acme Portal") for c in done)
    assert len(_activity(acme["id"])) == 1

    old = client.get("/acme-app/board?view=list", follow_redirects=False)
    assert old.status_code == 307 and old.headers["location"] == "/acme-portal/board?view=list"
    assert client.get("/acme-app", follow_redirects=False).headers["location"] == "/acme-portal"
    assert client.get("/acme-portal/board").status_code == 200

    stale = client.post("/acme-app/rename", data={"name": "Again", "slug": "again"}, headers=OWN,
                        follow_redirects=False)
    assert stale.status_code == 404 and "now /acme-portal" in stale.text
    assert repo.get_project(acme["id"])["name"] == "Acme Portal"


def test_a_refused_rename_is_a_flash_not_a_500(client, acme):
    mk("other", "Other")
    r = client.post("/acme-app/rename", data={"name": "Acme", "slug": "other"}, headers=OWN, follow_redirects=False)
    assert r.status_code == 303 and "crewboss_flash" in r.headers.get("set-cookie", "")
    assert repo.get_project(acme["id"])["slug"] == "acme-app"
    r = client.post("/acme/rename", data={"name": "Acme", "slug": "acme-co"}, headers=OWN, follow_redirects=False)
    assert r.status_code == 303 and "crewboss_flash" in r.headers.get("set-cookie", "")
    assert repo.get_project_by_slug("acme")["slug"] == "acme"


def test_a_cross_site_rename_is_refused(client, acme):
    r = client.post("/acme-app/rename", data={"name": "Evil", "slug": "evil"}, headers=EVIL, follow_redirects=False)
    assert r.status_code == 403
    row = repo.get_project(acme["id"])
    assert (row["name"], row["slug"]) == ("Acme App", "acme-app") and _aliases() == []
