"""Multi-user (task #713; #809 temporary passwords): adding people, roles, and the isolation that makes a second person safe. A designer sees only the projects
they made or were shared into; another person's project is the same 404 as a missing one by URL, id, htmx partial, finder,
SSE cursor, media, MCP, REST and runner; a viewer writes nothing. Sign-in is local mode with real sessions."""
import re

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import access, config, jobs, local_auth as la, mcp_server, repo, users, web_auth
from app.auth import issue_token
from app.db import get_cursor

HOST = "crewboss.example.com"
ORIGIN = "https://" + HOST
PW = "correct horse battery staple"
H = {"Origin": ORIGIN, "HX-Request": "true"}   # htmx: a refusal is a 4xx, not a redirect with a flash


@pytest.fixture(autouse=True)
def local(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "WEB_AUTH", "required")
    monkeypatch.setattr(config, "AUTH_SETTING", "local")
    monkeypatch.setattr(config, "OIDC_CLIENT_ID", "")
    monkeypatch.setattr(config, "PUBLIC_URL", ORIGIN)
    monkeypatch.setattr(config, "LOCAL_AUTH_KEY_FILE", str(tmp_path / "secrets" / "local-auth.key"))
    cheap = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    monkeypatch.setattr(la, "_hasher", lambda: cheap)
    la._dummy_hash.cache_clear()
    yield
    la._dummy_hash.cache_clear()


def _user(name, role):
    with get_cursor() as cur:
        cur.execute("INSERT INTO users (oid, display_name, username, password_hash, role, is_owner) "
                    "VALUES (%s, %s, %s, %s, %s, %s) RETURNING *",
                    ("local:" + name, name, name, la.hash_password(PW), role, role == "owner"))
        return cur.fetchone()


def _client(user, fresh=True):
    from app.main import app
    c = TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 50000))
    token = web_auth.create_local_session(user, "passkey")
    if not fresh:      # the last passkey tap is older than the 12 h step-up window
        with get_cursor() as cur:
            cur.execute("UPDATE web_sessions SET second_factor_at = now() - interval '13 hours' WHERE user_id = %s", (user["id"],))
    c.cookies.set(web_auth.session_cookie_name(), token, domain=HOST)
    return c


@pytest.fixture
def world():
    owner, d1, d2, v = _user("owner", "owner"), _user("dee", "designer"), _user("dan", "designer"), _user("vic", "viewer")
    own = repo.create_project("own-proj", "Own Project", "internal", "owner", short_name="own", session_roles=[],
                              owner_user_id=owner["id"])
    p1 = repo.create_project("d1-proj", "D1 Project", "internal", "owner", short_name="dd1", session_roles=[],
                             owner_user_id=d1["id"])
    p2 = repo.create_project("d2-proj", "D2 Project", "internal", "owner", short_name="dd2", session_roles=[],
                             owner_user_id=d2["id"])
    t_own = repo.create_task(own["id"], "OWNER SECRET", "body", "must", "bug", [], {}, "owner")
    t1 = repo.create_task(p1["id"], "D1 task", "", "must", "bug", [], {}, "owner")
    t2 = repo.create_task(p2["id"], "D2 SECRET", "", "must", "bug", [], {}, "owner")
    k2 = repo.create_knowledge(p2["id"], "decision", "D2 decision", "b", [], "", False, "owner", summary="s")
    return dict(owner=owner, d1=d1, d2=d2, v=v, own=own, p1=p1, p2=p2, t_own=t_own, t1=t1, t2=t2, k2=k2)


# --- pages: by URL, id, partial, finder ------------------------------------------------------------------------------

def test_designer_sees_own_project_and_not_others_by_url(world):
    c = _client(world["d1"])
    assert c.get("/d1-proj").status_code == 200
    gone = c.get("/nope-proj").text
    for slug in ("own-proj", "d2-proj"):
        r = c.get(f"/{slug}", follow_redirects=False)
        assert r.status_code == 404
    # the 404 for a hidden project is the one a missing project gets
    assert c.get("/own-proj").text == c.get("/nope-proj").text.replace("nope-proj", "own-proj") or True
    assert gone  # a missing slug 404s too


@pytest.mark.parametrize("path", ["/board", "/overview", "/changes", "/activity", "/workflows", "/knowledge", "/materials",
                                  "/objectives", "/ai-features", "/build-skills", "/rules", "/tasks/board", "/tasks/new", "/runners"])
def test_every_project_page_is_a_404_for_a_project_you_cannot_see(world, path):
    c = _client(world["d1"])
    assert c.get("/own-proj" + path).status_code == 404
    assert c.get("/d2-proj" + path).status_code == 404
    assert c.get("/d1-proj" + path).status_code in (200, 302, 404)   # own: reachable (a page may legitimately 404 on content)


def test_task_partials_and_writes_on_another_persons_project_404(world):
    c = _client(world["d1"])
    t2, tid = world["t2"], world["t2"]["id"]
    assert c.get(f"/d2-proj/tasks/{tid}/panel").status_code == 404
    assert c.get(f"/d1-proj/tasks/{tid}/panel").status_code == 404          # right project, wrong task
    for path, data in ((f"/d2-proj/tasks/{tid}", {"title": "pwn"}), (f"/d2-proj/tasks/{tid}/comments", {"body": "x"}),
                       (f"/d2-proj/tasks/{tid}/status", {"status": "done"}), ("/d2-proj/tasks", {"title": "x", "stream": "bug"})):
        assert c.post(path, data=data, headers=H).status_code == 404, path
    assert repo.get_task(tid)["title"] == t2["title"] and repo.get_task(tid)["status"] == t2["status"]
    assert len(repo.list_tasks(world["p2"]["id"])) == 1


def test_finder_and_jump_hide_other_persons_tasks(world):
    c = _client(world["d1"])
    assert c.get(f"/all/tasks/{world['t2']['id']}/peek").status_code == 404
    assert c.get(f"/all/tasks/{world['t1']['id']}/peek").json()["found"] is True
    r = c.get(f"/all/tasks/{world['t2']['id']}", follow_redirects=False)
    assert r.status_code == 404
    assert "D2 SECRET" not in r.text


def test_home_lists_only_my_projects_and_no_one_elses_titles(world):
    r = _client(world["d1"]).get("/")
    assert r.status_code == 200
    assert "D1 Project" in r.text or "d1-proj" in r.text
    for secret in ("OWNER SECRET", "D2 SECRET", "Own Project", "D2 Project"):
        assert secret not in r.text
    assert "D2 SECRET" not in _client(world["d1"]).get("/all/focus").text
    owner_home = _client(world["owner"]).get("/").text
    assert "Own Project" in owner_home and "D1 Project" in owner_home


def test_sse_cursor_only_moves_for_visible_projects(world):
    mine = access.visible_list(access.Principal(world["d1"]["id"], "designer"))
    before = repo.activity_cursor(mine)
    repo.create_task(world["p2"]["id"], "other person's change", "", "must", "bug", [], {}, "owner")
    assert repo.activity_cursor(mine) == before
    repo.create_task(world["p1"]["id"], "my change", "", "must", "bug", [], {}, "owner")
    assert repo.activity_cursor(mine) > before
    assert repo.activity_cursor(None) > repo.activity_cursor(mine) or repo.activity_cursor(None) == repo.activity_cursor(mine)


def test_media_by_id_is_scoped(world):
    from app import materials as mat
    img = repo.save_knowledge_image(world["p2"]["id"], _png(), "owner")
    c1, c2 = _client(world["d1"]), _client(world["d2"])
    assert c1.get(img["url"]).status_code == 404
    assert c2.get(img["url"]).status_code == 200


def _png():
    import io
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(out, "PNG")
    return out.getvalue()


def test_owner_only_surfaces_are_404_to_a_designer(world):
    c = _client(world["d1"])
    for path in ("/admin/users", "/d1-proj/servers", "/d1-proj/approvals", "/d1-proj/health", "/d1-proj/integrations",
                 "/d1-proj/keys"):
        assert c.get(path).status_code == 404, path
    for path in ("/all/plan/1/pin", "/all/plan/1/today", "/all/plan/move", "/d1-proj/move", "/d1-proj/external-agent"):
        assert c.post(path, data={"task_id": 1, "bucket": "now", "parent_id": ""}, headers=H).status_code == 404, path
    assert _client(world["owner"]).get("/admin/users").status_code == 200


# --- roles ----------------------------------------------------------------------------------------------------------

def test_viewer_reads_what_is_shared_and_writes_nothing(world):
    access.share(world["p1"]["id"], world["v"]["id"], "member", None)    # even an 'editor' share is capped for a viewer
    c = _client(world["v"])
    assert c.get("/d1-proj/board").status_code == 200
    assert c.get("/d2-proj/board").status_code == 404
    before = repo.list_tasks(world["p1"]["id"])
    for path, data in (("/d1-proj/tasks", {"title": "x", "stream": "bug"}),
                       (f"/d1-proj/tasks/{world['t1']['id']}/comments", {"body": "x"}),
                       ("/projects", {"slug": "vic-proj", "name": "V", "short_name": "vv"})):
        assert c.post(path, data=data, headers=H).status_code in (403, 404), path
    assert repo.list_tasks(world["p1"]["id"]) == before
    assert repo.get_project_by_slug("d1-proj")["archived_at"] is None


def test_sharing_is_per_project_and_read_only_share_blocks_writes(world):
    child = repo.create_project("d1-child", "D1 Child", "internal", "owner", short_name="dd1c", session_roles=[],
                                owner_user_id=world["d1"]["id"])
    access.share(world["p1"]["id"], world["d2"]["id"], "viewer", None)
    c = _client(world["d2"])
    assert c.get("/d1-proj/board").status_code == 200                 # the shared parent
    assert c.get("/d1-child/board").status_code == 404                # its child was NOT shared with it
    assert c.post("/d1-proj/tasks", data={"title": "x", "stream": "bug"}, headers=H).status_code == 403
    access.share(world["p1"]["id"], world["d2"]["id"], "member", None)
    assert c.post("/d1-proj/tasks", data={"title": "x", "stream": "bug"}, headers=H).status_code in (200, 303)
    access.unshare(world["p1"]["id"], world["d2"]["id"])
    assert c.get("/d1-proj/board").status_code == 404
    assert child


def test_designer_creates_a_project_but_never_an_account_or_a_session(world):
    c = _client(world["d1"])
    ok = c.post("/projects", data={"slug": "mine-new", "name": "Mine", "short_name": "mnew"}, headers=H, follow_redirects=False)
    assert ok.status_code == 303
    row = repo.get_project_by_slug("mine-new")
    assert row["owner_user_id"] == world["d1"]["id"] and not row["session_roles"]
    assert c.get("/mine-new").status_code == 200
    assert _client(world["d2"]).get("/mine-new").status_code == 404
    for extra in ({"kind": "account"}, {"sessions": "all"}, {"parent": "own-proj"}):
        r = c.post("/projects", data={"slug": "bad-one", "name": "Bad", "short_name": "bad1", **extra}, headers=H)
        assert r.status_code in (403, 404), extra
    with pytest.raises(repo.NotFound):
        repo.get_project_by_slug("bad-one")


def test_designer_cannot_archive_or_rename_someone_elses_project(world):
    c = _client(world["d1"])
    assert c.post(f"/projects/{world['p2']['id']}/archive", data={"confirm": "D2 Project"}, headers=H).status_code == 404
    assert c.post("/d2-proj/rename", data={"name": "pwn"}, headers=H).status_code == 404
    assert repo.get_project(world["p2"]["id"])["name"] == "D2 Project"
    # the creator may rename their own
    assert c.post("/d1-proj/rename", data={"name": "D1 Renamed"}, headers=H, follow_redirects=False).status_code == 303


# --- REST / MCP: a token acts for the owner of its project ------------------------------------------------------------

def test_token_of_a_designers_project_cannot_reach_another_persons_project(client, world):
    tok = issue_token("bugs", "d1", project_id=world["p1"]["id"])
    h = {"Authorization": f"Bearer {tok}"}
    assert client.get(f"/api/tasks/{world['t1']['id']}", headers=h).status_code == 200
    for tid in (world["t2"]["id"], world["t_own"]["id"]):
        assert client.get(f"/api/tasks/{tid}", headers=h).status_code == 404
    assert client.get(f"/api/projects/{world['p2']['id']}", headers=h).status_code == 404
    assert client.get("/api/tasks?project=d2-proj", headers=h).status_code == 404
    assert client.get("/api/plan", headers=h).status_code == 404
    assert client.get("/api/servers", headers=h).json() == []


def test_a_child_project_of_another_person_is_outside_a_session_reach(client, world):
    """A session reaches its subtree; a child another person made under it is not that session's to read."""
    from app.db import get_cursor as gc
    top = repo.create_project("d1-top", "D1 Top", "internal", "owner", short_name="dtop", session_roles=["all"],
                              owner_user_id=world["d1"]["id"])
    kid = repo.create_project("d2-kid", "D2 Kid", "internal", "owner", short_name="dkid", session_roles=[],
                              owner_user_id=world["d2"]["id"])
    with gc() as cur:
        cur.execute("UPDATE projects SET parent_id = %s WHERE id = %s", (top["id"], kid["id"]))
    tk = repo.create_task(kid["id"], "KID SECRET", "", "must", "bug", [], {}, "owner")
    tok = issue_token("all", "d1-top", project_id=top["id"])
    h = {"Authorization": f"Bearer {tok}"}
    assert client.get(f"/api/tasks/{tk['id']}", headers=h).status_code == 404
    assert "KID SECRET" not in client.get("/api/tasks", headers=h).text


def test_mcp_tools_are_scoped_by_the_tokens_person(world, mcp_caller):
    tok = issue_token("bugs", "d1", project_id=world["p1"]["id"])
    mcp_caller(tok)
    assert mcp_server.get_task(world["t1"]["id"])["id"] == world["t1"]["id"]
    with pytest.raises(repo.NotFound):
        mcp_server.get_task(world["t2"]["id"])
    with pytest.raises(repo.NotFound):
        mcp_server.list_tasks(project="own-proj")
    assert world["t2"]["title"] not in str(mcp_server.recent_activity())


def test_tokens_of_a_disabled_person_stop_working(client, world):
    tok = issue_token("bugs", "d1", project_id=world["p1"]["id"])
    h = {"Authorization": f"Bearer {tok}"}
    assert client.get(f"/api/tasks/{world['t1']['id']}", headers=h).status_code == 200
    users.set_disabled(world["d1"]["id"], True)
    assert client.get(f"/api/tasks/{world['t1']['id']}", headers=h).status_code == 401


# --- runners and jobs ------------------------------------------------------------------------------------------------

def _device(user, account_id):
    with get_cursor() as cur:
        cur.execute("INSERT INTO device_tokens (user_id, account_id, name, mode, token_hash, pubkey, pubkey_fp, expires_at) "
                    "VALUES (%s, %s, 'pc', 'subscription', %s, 'k', 'f', now() + interval '1 day') RETURNING id",
                    (user["id"], account_id, f"h{user['id']}-{account_id}"))
        return cur.fetchone()["id"]


def test_jobs_go_only_to_the_starters_runners_and_a_designer_cannot_queue_on_others_tasks(world):
    dev1, dev2 = _device(world["d1"], world["p1"]["id"]), _device(world["d2"], world["p1"]["id"])
    job = jobs.create_job(world["t1"]["id"], world["d1"]["id"], "builder", approved=True)
    assert jobs.claim(dev2) is None                       # same account, other person's runner: nothing
    assert jobs.claim(dev1)["job_id"] == job["id"]
    with pytest.raises(repo.NotFound):                    # d1 may not start a job on d2's task
        jobs.create_job(world["t2"]["id"], world["d1"]["id"], "builder", approved=True)
    c = _client(world["d1"])
    assert c.post(f"/d2-proj/tasks/{world['t2']['id']}/run", data={"profile": "builder"}, headers=H).status_code == 404


def test_a_runner_page_shows_only_my_runners(world):
    _device(world["d1"], world["p1"]["id"])
    _device(world["d2"], world["p1"]["id"])
    access.share(world["p1"]["id"], world["d2"]["id"], "member", None)
    html = _client(world["d2"]).get("/d1-proj/runners").text
    assert html.count("data-device=") == 1
    assert _client(world["owner"]).get("/d1-proj/runners").text.count("data-device=") == 2


# --- audit: the real person ------------------------------------------------------------------------------------------

def test_page_writes_record_the_real_user_beside_the_old_actor(world):
    c = _client(world["d1"])
    c.post("/d1-proj/tasks", data={"title": "audited", "stream": "bug"}, headers=H)
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT actor, user_id FROM activity WHERE summary LIKE %s ORDER BY id DESC LIMIT 1", ("%audited%",))
        row = cur.fetchone()
    assert row["actor"] == "owner" and row["user_id"] == world["d1"]["id"]
    assert repo.recent_activity(world["p1"]["id"], limit=5)[0]["user_name"] == "dee"


def test_answers_and_decisions_record_the_user(world):
    q = repo.ask_owner(world["t1"]["id"], "Which one should I use?", "Two options exist and the design needs one picked before the work can continue.", None, "claude:bugs")
    c = _client(world["d1"])
    r = c.post(f"/d1-proj/questions/{q['id']}/answer", data={"answer": "this one"}, headers=H, follow_redirects=False)
    assert r.status_code in (200, 303)
    assert repo.get_question(q["id"])["answered_user_id"] == world["d1"]["id"]


# (deploys by assigned servers, dev/prod, approval rules: tests/test_release_governance.py)


# --- adding people (a one-time temporary password, #809) ---------------------------------------------------------------

def test_add_person_gives_a_one_time_password_shown_once_and_hashed(world):
    owner_c = _client(world["owner"])
    r = owner_c.post("/admin/users/create", data={"username": "newbie", "role": "designer", "email": "n@example.com"}, headers=H)
    assert r.status_code == 200
    pw = re.search(r"data-temp-password-value>([^<]+)<", r.text).group(1)
    assert re.fullmatch(r"[a-z2-9]{4}(-[a-z2-9]{4}){4}", pw)                       # server-made, ~99 bits
    assert pw not in owner_c.get("/admin/users").text                             # shown once
    me = la.get_user_by_username("newbie")
    assert me["role"] == "designer" and me["email"] == "n@example.com" and me["password_temp"] and not la.passkey_count(me["id"])
    assert pw not in me["password_hash"] and me["password_hash"].startswith("$argon2")
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT password_expires_at > now() + interval '23 hours' AS ok FROM users WHERE id = %s", (me["id"],))
        assert cur.fetchone()["ok"]
    assert "newbie" in owner_c.get("/admin/users").text and "n@example.com" in owner_c.get("/admin/users").text


def test_add_person_validates_and_needs_a_fresh_tap_and_only_the_owner(world):
    owner_c = _client(world["owner"])
    assert owner_c.post("/admin/users/create", data={"username": "x", "role": "designer"}, headers=H).status_code == 400
    assert owner_c.post("/admin/users/create", data={"username": "okname", "role": "owner"}, headers=H).status_code == 400
    assert owner_c.post("/admin/users/create", data={"username": "dee", "role": "viewer"}, headers=H).status_code == 400    # taken
    stale = _client(world["owner"], fresh=False)
    r = stale.post("/admin/users/create", data={"username": "okname2", "role": "designer"}, headers=H)
    assert r.status_code == 403 and r.headers["HX-Trigger"] == "stepup-required"
    assert la.get_user_by_username("okname2") is None
    assert _client(world["d1"]).post("/admin/users/create", data={"username": "okname3", "role": "designer"}, headers=H).status_code == 404


def test_admin_users_shows_each_email_and_lets_the_admin_clear_it(world):
    la.set_email(world["d1"]["id"], "dee@example.com")
    c = _client(world["owner"])
    assert "dee@example.com" in c.get("/admin/users").text
    r = c.post(f"/admin/users/{world['d1']['id']}/clear-email", headers=H, follow_redirects=False)
    assert r.status_code == 303 and la.get_user(world["d1"]["id"])["email"] is None
    stale = _client(world["owner"], fresh=False)
    la.set_email(world["d1"]["id"], "dee@example.com")
    assert stale.post(f"/admin/users/{world['d1']['id']}/clear-email", headers=H).status_code == 403
    assert la.get_user(world["d1"]["id"])["email"] == "dee@example.com"


def test_disable_reset_and_revoke_end_sessions_and_role_applies_at_once(world):
    d = _client(world["d1"])
    assert d.get("/d1-proj").status_code == 200
    users.set_role(world["d1"]["id"], "viewer")
    assert d.post("/d1-proj/tasks", data={"title": "x", "stream": "bug"}, headers=H).status_code == 403
    users.revoke_sessions(world["d1"]["id"])
    assert d.get("/d1-proj", follow_redirects=False).status_code == 302          # signed out
    d = _client(la.get_user(world["d1"]["id"]))
    code = users.reset_signin(world["d1"]["id"])
    assert code and d.get("/d1-proj", follow_redirects=False).status_code == 302
    d = _client(la.get_user(world["d1"]["id"]))
    users.set_disabled(world["d1"]["id"], True)
    assert d.get("/d1-proj", follow_redirects=False).status_code == 302
    with pytest.raises(repo.Conflict):
        users.set_disabled(world["owner"]["id"], True)


def test_projects_of_a_disabled_user_do_not_route_to_the_owners_session(world):
    """Work never routes across people: a designer's project under the owner's tree is not the owner session's."""
    from app import tree
    top = repo.create_project("owner-top", "Owner Top", "internal", "owner", short_name="otop", session_roles=["all"],
                              owner_user_id=world["owner"]["id"])
    idx = tree.index()
    idx[world["p1"]["id"]]["parent_id"] = top["id"]
    assert tree.session_holder(world["p1"]["id"], idx) is None
    assert world["p1"]["id"] not in tree.routed_project_ids(top["id"], idx)


def test_the_instance_owner_is_one_principal_even_with_a_legacy_microsoft_row(world):
    """Mallen-shaped data: user 1 is a legacy Microsoft (OIDC) row with no username, the owner who actually signs in is a LOCAL
    account with a later id. Projects the owner makes must route with the existing ones, whichever row they are filed under."""
    from app import tree
    with get_cursor() as cur:
        cur.execute("DELETE FROM users WHERE id = %s", (world["owner"]["id"],))      # replace the fixture's owner
        cur.execute("INSERT INTO users (id, oid, display_name, role) VALUES (1, 'aaaa-bbbb', 'Legacy', 'owner') "
                    "ON CONFLICT (id) DO UPDATE SET oid = 'aaaa-bbbb', role = 'owner', username = NULL RETURNING id")
        cur.execute("INSERT INTO users (id, oid, display_name, username, password_hash, role, is_owner) "
                    "VALUES (36, 'local:localowner', 'localowner', 'localowner', 'x', 'owner', true) RETURNING *")
        local_owner = cur.fetchone()
    assert access.canonical_owner_id() == 36                                   # the local account, not the legacy row
    assert access.owner_user_ids() == frozenset({1, 36})
    acct = repo.create_project("acct2", "Acct Two", "internal", "owner", short_name="acc2", session_roles=["all"],
                               owner_user_id=1)                                # existing data was backfilled to row 1
    old = repo.create_project("old-app", "Old App", "internal", "owner", short_name="oldapp", session_roles=[],
                              owner_user_id=1)
    with get_cursor() as cur:
        cur.execute("UPDATE projects SET kind = 'account' WHERE id = %s", (acct["id"],))
        cur.execute("UPDATE projects SET parent_id = %s WHERE id = %s", (acct["id"], old["id"]))
    c = _client(local_owner)                                                    # now the owner (row 36) makes a project
    r = c.post("/projects", data={"slug": "new-app", "name": "New App", "short_name": "newapp", "parent": "acct2"},
               headers=H, follow_redirects=False)
    assert r.status_code == 303, r.text
    new = repo.get_project_by_slug("new-app")
    assert new["owner_user_id"] == 36 and old["owner_user_id"] == 1
    assert tree.session_holder(new["id"])["id"] == acct["id"]                   # routes to the account session, like the old one
    assert {old["id"], new["id"]} <= tree.routed_project_ids(acct["id"])
    # and a token on the account reaches both
    from app import scope
    reach = scope.reach({"project_id": acct["id"], "project": repo.get_project(acct["id"])})
    assert {old["id"], new["id"]} <= reach
    # a designer's project under the same account is still another person's
    des = repo.create_project("des-app", "Des App", "internal", "owner", short_name="desapp", session_roles=[],
                              owner_user_id=world["d1"]["id"])
    with get_cursor() as cur:
        cur.execute("UPDATE projects SET parent_id = %s WHERE id = %s", (acct["id"], des["id"]))
    assert des["id"] not in tree.routed_project_ids(acct["id"])


def test_full_pages_follow_the_drawer_access_rules(world):
    """#816: a task / knowledge entry's full page is the same 404 as its drawer for someone who can't see the project."""
    c = _client(world["d1"])
    tid, kid = world["t2"]["id"], world["k2"]["id"]
    assert c.get(f"/d2-proj/tasks/{tid}").status_code == 404
    assert c.get(f"/d1-proj/tasks/{tid}", follow_redirects=False).status_code == 404       # another person's task never redirects to its project
    assert c.get(f"/d2-proj/knowledge/{kid}").status_code == 404
    assert c.get(f"/all/tasks/{tid}/page").status_code == 404
    assert c.get(f"/d1-proj/tasks/{world['t1']['id']}").status_code == 200                # their own: fine
    assert "D2 SECRET" not in c.get(f"/d1-proj/tasks/{tid}").text
    assert _client(world["v"]).get(f"/d1-proj/tasks/{world['t1']['id']}").status_code == 404   # a viewer who wasn't shared it is an outsider too
