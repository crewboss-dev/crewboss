"""Release governance (change #77, task #713): dev first, production by promotion only, an Admin approves; project roles Owner /
Admin / Member / Viewer; hotfix, rollback, the production-only policy, merge gates, and GitHub branch protection (a fake client)."""
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import access, config, deploys, jobs, local_auth as la, release_gates, repo, web_auth
from app.db import get_cursor

HOST = "crewboss.example.com"
ORIGIN = "https://" + HOST
H = {"Origin": ORIGIN, "HX-Request": "true"}
SHA1, SHA2, SHA3 = "1" * 40, "2" * 40, "3" * 40


@pytest.fixture(autouse=True)
def local(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "WEB_AUTH", "required")
    monkeypatch.setattr(config, "AUTH_SETTING", "local")
    monkeypatch.setattr(config, "OIDC_CLIENT_ID", "")
    monkeypatch.setattr(config, "PUBLIC_URL", ORIGIN)
    monkeypatch.setattr(la, "_hasher", lambda: PasswordHasher(time_cost=1, memory_cost=8, parallelism=1))
    la._dummy_hash.cache_clear()


def _user(name, role="designer"):
    with get_cursor() as cur:
        cur.execute("INSERT INTO users (oid, display_name, username, password_hash, role, is_owner) VALUES (%s, %s, %s, 'x', %s, %s) "
                    "RETURNING *", ("local:" + name, name, name, role, role == "owner"))
        return cur.fetchone()


def P(user):
    return access.Principal(user["id"], user["role"], user["username"])


def _client(user, fresh=True):
    from app.main import app
    c = TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 50000))
    token = web_auth.create_local_session(user, "totp")
    if not fresh:
        with get_cursor() as cur:
            cur.execute("UPDATE web_sessions SET second_factor_at = now() - interval '13 hours' WHERE user_id = %s", (user["id"],))
    c.cookies.set(web_auth.session_cookie_name(), token, domain=HOST)
    return c


@pytest.fixture
def w():
    owner = _user("boss", "owner")
    creator, admin2, member, viewer = _user("cara"), _user("adam"), _user("mia"), _user("vera", "viewer")
    acct = repo.create_project("acct", "Acct", "internal", "owner", short_name="acct", session_roles=[])
    proj = repo.create_project("web", "Web", "internal", "owner", short_name="web", session_roles=[],
                               owner_user_id=creator["id"])
    with get_cursor() as cur:
        cur.execute("INSERT INTO commissioned_servers (account_id, name, address, login_user, status, host_key, created_by) "
                    "VALUES (%s, 'box', '10.0.0.1', 'deploy', 'commissioned', 'k', 'owner') RETURNING id", (acct["id"],))
        srv = cur.fetchone()["id"]
    access.assign_server(srv, creator["id"], None, proj["id"])          # the instance owner pins the server to ONE project
    access.share(proj["id"], member["id"], "member", None)
    access.share(proj["id"], viewer["id"], "viewer", None)
    dev = deploys.add_target_assigned(P(creator), proj, srv, "dev", "/srv/dev", env="dev")
    prod = deploys.add_target_assigned(P(creator), proj, srv, "live", "/srv/live", env="production")
    return dict(owner=owner, creator=creator, admin2=admin2, member=member, viewer=viewer, proj=proj, srv=srv, dev=dev, prod=prod,
                acct=acct)


def _done(sha, env, project, target_id=None):
    """Mark the deploy of sha as having succeeded (the executor's job)."""
    with get_cursor() as cur:
        cur.execute("UPDATE server_jobs SET state = 'done', finished_at = now() WHERE project_id = %s AND sha = %s AND env = %s",
                    (project["id"], sha, env))


def test_roles_viewer_cannot_deploy_and_only_admin_defines_production(w):
    with pytest.raises(repo.Forbidden):
        deploys.request_release(P(w["viewer"]), w["proj"], w["dev"]["id"], SHA1)
    access.share(w["proj"]["id"], w["admin2"]["id"], "admin", None)
    for who in (w["member"], w["admin2"]):                                     # only the project's Owner defines targets
        with pytest.raises(repo.Forbidden, match="Owner"):
            deploys.add_target_assigned(P(who), w["proj"], w["srv"], "m-dev", "/srv/y", env="dev")
    assert w["prod"]["env"] == "prod"                                         # 'production' is stored as prod


def test_dev_deploy_needs_no_approval_and_production_refuses_a_commit_that_never_ran(w):
    sj = deploys.request_release(P(w["member"]), w["proj"], w["dev"]["id"], SHA1)
    assert sj["state"] == "approved" and sj["decided_by"] == w["member"]["id"] and sj["auto_approved"]
    with pytest.raises(repo.BadRequest, match="has not run"):
        deploys.request_release(P(w["member"]), w["proj"], w["prod"]["id"], SHA1)      # dev ran it, but it has not SUCCEEDED yet
    _done(SHA1, "dev", w["proj"])
    sj = deploys.request_release(P(w["member"]), w["proj"], w["prod"]["id"], SHA1)
    assert sj["state"] == "requested" and sj["env"] == "prod" and sj["requested_by"] == w["member"]["id"]
    with pytest.raises(repo.BadRequest, match="has not run"):
        deploys.request_release(P(w["member"]), w["proj"], w["prod"]["id"], SHA2)      # a different commit


def test_a_runner_preview_does_not_stand_in_for_dev_on_a_standard_project(w):
    _preview(w, SHA1)
    with pytest.raises(repo.BadRequest, match="has not run"):
        deploys.request_release(P(w["member"]), w["proj"], w["prod"]["id"], SHA1)


def test_a_member_cannot_approve_and_an_admin_needs_a_fresh_factor(w):
    _done_prod_request = _promote_to_request(w, SHA1, w["member"])
    with pytest.raises(repo.Forbidden):
        deploys.approve_checked(P(w["member"]), w["proj"]["id"], _done_prod_request["id"], True)
    with pytest.raises(repo.Forbidden, match="second factor"):
        deploys.approve_checked(P(w["creator"]), w["proj"]["id"], _done_prod_request["id"], False)
    sj = deploys.approve_checked(P(w["creator"]), w["proj"]["id"], _done_prod_request["id"], True)
    assert sj["state"] == "approved" and sj["decided_by"] == w["creator"]["id"]
    with pytest.raises(repo.Conflict):
        deploys.approve_checked(P(w["creator"]), w["proj"]["id"], _done_prod_request["id"], True)
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT user_id FROM activity WHERE verb = 'approved' AND entity = 'server_job' AND entity_id = %s",
                    (sj["id"],))
        assert cur.fetchone() is not None


def _promote_to_request(w, sha, who):
    deploys.request_release(P(who) if who["role"] != "owner" else P(who), w["proj"], w["dev"]["id"], sha)
    _done(sha, "dev", w["proj"])
    return deploys.request_release(P(who), w["proj"], w["prod"]["id"], sha)


def test_solo_admin_may_approve_their_own_request_but_two_admins_need_a_second_person(w):
    sj = _promote_to_request(w, SHA1, w["creator"])                         # the creator is the only Admin
    assert deploys.approve_checked(P(w["creator"]), w["proj"]["id"], sj["id"], True)["state"] == "approved"
    access.share(w["proj"]["id"], w["admin2"]["id"], "admin", None)         # now two Admins
    sj2 = _promote_to_request(w, SHA2, w["creator"])
    with pytest.raises(repo.Forbidden, match="another Admin"):
        deploys.approve_checked(P(w["creator"]), w["proj"]["id"], sj2["id"], True)
    assert deploys.approve_checked(P(w["admin2"]), w["proj"]["id"], sj2["id"], True)["decided_by"] == w["admin2"]["id"]


def test_instance_owner_is_above_every_project(w):
    sj = _promote_to_request(w, SHA1, w["member"])
    assert deploys.approve_checked(P(w["owner"]), w["proj"]["id"], sj["id"], True)["state"] == "approved"


def test_hotfix_needs_a_reason_is_flagged_and_opens_the_merge_back_task(w):
    with pytest.raises(repo.BadRequest, match="reason"):
        deploys.request_release(P(w["member"]), w["proj"], w["prod"]["id"], SHA3, hotfix=True, reason="oops")
    with pytest.raises(repo.BadRequest):
        deploys.request_release(P(w["member"]), w["proj"], w["dev"]["id"], SHA3, hotfix=True, reason="it is on fire right now")
    sj = deploys.request_release(P(w["member"]), w["proj"], w["prod"]["id"], SHA3, hotfix=True, reason="checkout is down for everyone")
    assert sj["state"] == "requested" and sj["hotfix"] and sj["hotfix_reason"]
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT summary FROM activity WHERE verb = 'hotfix' AND entity_id = %s", (sj["id"],))
        assert "HOTFIX" in cur.fetchone()["summary"] and "dev/staging skipped" in _all("hotfix", sj["id"])
    done = deploys.approve_checked(P(w["creator"]), w["proj"]["id"], sj["id"], True)
    task = repo.get_task(done["followup_task_id"])
    assert "back into dev and main" in task["title"] and task["project_id"] == w["proj"]["id"]


def _all(verb, eid):
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT summary FROM activity WHERE verb = %s AND entity_id = %s", (verb, eid))
        return " ".join(r["summary"] for r in cur.fetchall())


def _preview(w, sha):
    task = repo.create_task(w["proj"]["id"], "build it", "", "must", "bug", [], {}, "owner")
    job = jobs.create_job(task["id"], w["creator"]["id"], "builder", approved=True)
    with get_cursor() as cur:
        cur.execute("INSERT INTO device_tokens (user_id, account_id, name, mode, token_hash, pubkey, pubkey_fp, expires_at) "
                    "VALUES (%s, %s, 'pc', 'subscription', %s, 'k', 'f', now() + interval '1 day') RETURNING id",
                    (w["creator"]["id"], w["proj"]["id"], f"tok-{sha[:4]}-{job['id']}"))
        dev_id = cur.fetchone()["id"]
    claimed = jobs.claim(dev_id)
    return deploys.record_preview(claimed["job_id"], claimed["fence"], sha)


def test_production_only_refuses_without_a_preview_and_accepts_with_one(w):
    deploys.set_release_policy(P(w["creator"]), w["proj"], "production_only")
    proj = repo.get_project(w["proj"]["id"])
    with pytest.raises(repo.BadRequest, match="preview"):
        deploys.request_release(P(w["member"]), proj, w["prod"]["id"], SHA1)
    pv = _preview(w, SHA1)
    assert pv["env"] == "preview" and pv["state"] == "done" and pv["target_id"] is None
    with pytest.raises(repo.BadRequest):
        deploys.request_release(P(w["member"]), proj, w["prod"]["id"], SHA2)           # the preview was for SHA1 only
    sj = deploys.request_release(P(w["member"]), proj, w["prod"]["id"], SHA1)
    assert sj["state"] == "requested"                                                   # still needs the Admin
    with pytest.raises(repo.Forbidden):
        deploys.approve_checked(P(w["member"]), proj["id"], sj["id"], True)
    assert deploys.approve_checked(P(w["creator"]), proj["id"], sj["id"], True)["state"] == "approved"


def test_release_policy_is_changed_only_by_the_owner_and_audited(w):
    for who in (w["member"], w["viewer"]):
        with pytest.raises(repo.NotFound):
            deploys.set_release_policy(P(who), w["proj"], "production_only")
    access.share(w["proj"]["id"], w["admin2"]["id"], "admin", None)
    with pytest.raises(repo.NotFound):
        deploys.set_release_policy(P(w["admin2"]), w["proj"], "production_only")      # an Admin is not the Owner
    deploys.set_release_policy(P(w["owner"]), w["proj"], "production_only")           # the instance owner may
    assert "production_only" in _all("release policy", w["proj"]["id"])
    with pytest.raises(repo.BadRequest):
        deploys.set_release_policy(P(w["creator"]), w["proj"], "yolo")


def test_rollback_restores_the_previous_release_behind_the_same_approval(w):
    for sha in (SHA1, SHA2):
        sj = _promote_to_request(w, sha, w["creator"])
        deploys.approve_checked(P(w["creator"]), w["proj"]["id"], sj["id"], True)
        _done(sha, "prod", w["proj"])
    prev = deploys.previous_release(w["prod"]["id"])
    assert prev["sha"] == SHA1
    rb = deploys.request_rollback(P(w["member"]), w["proj"], w["prod"]["id"])
    assert rb["rollback"] and rb["sha"] == SHA1 and rb["state"] == "requested" and rb["env"] == "prod"
    with pytest.raises(repo.Forbidden):
        deploys.approve_checked(P(w["member"]), w["proj"]["id"], rb["id"], True)
    assert deploys.approve_checked(P(w["creator"]), w["proj"]["id"], rb["id"], True)["state"] == "approved"
    with pytest.raises(repo.BadRequest):
        deploys.request_rollback(P(w["member"]), w["proj"], w["dev"]["id"])            # nothing earlier on dev


def test_every_way_of_asking_for_production_is_promote_only(w):
    acct = w["acct"]["id"]
    with pytest.raises(repo.BadRequest, match="has not run"):
        deploys.request_by_person(acct, w["owner"]["id"], w["prod"]["id"], SHA2)


def test_merge_to_main_needs_green_tests_an_auditor_pass_and_an_admin(w):
    mr = release_gates.request_merge(P(w["member"]), w["proj"], "dash/j1", SHA1)
    with pytest.raises(repo.Conflict, match="tests"):
        release_gates.approve_merge(P(w["creator"]), w["proj"]["id"], mr["id"], True)
    release_gates.record_check(mr["id"], "tests", True, "job-1")
    with pytest.raises(repo.Conflict, match="auditor"):
        release_gates.approve_merge(P(w["creator"]), w["proj"]["id"], mr["id"], True)
    release_gates.record_check(mr["id"], "audit", True, "code-auditor", "no findings")
    with pytest.raises(repo.Forbidden):
        release_gates.approve_merge(P(w["member"]), w["proj"]["id"], mr["id"], True)
    with pytest.raises(repo.Forbidden, match="second factor"):
        release_gates.approve_merge(P(w["creator"]), w["proj"]["id"], mr["id"], False)
    with pytest.raises(repo.Conflict):
        release_gates.mark_merged(mr["id"])                                              # not approved yet: cannot merge
    assert release_gates.approve_merge(P(w["creator"]), w["proj"]["id"], mr["id"], True)["decided_by"] == w["creator"]["id"]
    assert release_gates.mark_merged(mr["id"])["state"] == "merged"
    bad = release_gates.request_merge(P(w["member"]), w["proj"], "dash/j2", SHA2)
    release_gates.record_check(bad["id"], "tests", True, "job-2")
    release_gates.record_check(bad["id"], "audit", True, "code-auditor")
    release_gates.record_check(bad["id"], "tests", False, "job-2")                       # a later failure clears the pass
    with pytest.raises(repo.Conflict, match="tests"):
        release_gates.approve_merge(P(w["creator"]), w["proj"]["id"], bad["id"], True)


def test_two_admins_means_someone_else_approves_the_merge(w):
    access.share(w["proj"]["id"], w["admin2"]["id"], "admin", None)
    mr = release_gates.request_merge(P(w["creator"]), w["proj"], "dash/j3", SHA3)
    release_gates.record_check(mr["id"], "tests", True, "j")
    release_gates.record_check(mr["id"], "audit", True, "a")
    with pytest.raises(repo.Forbidden, match="another Admin"):
        release_gates.approve_merge(P(w["creator"]), w["proj"]["id"], mr["id"], True)
    assert release_gates.approve_merge(P(w["admin2"]), w["proj"]["id"], mr["id"], True)["state"] == "approved"


class FakeGitHub:
    def __init__(self):
        self.calls = []

    def request(self, method, path, json=None):
        self.calls.append((method, path, json))
        return {"ok": True}


def test_branch_protection_is_a_confirm_card_and_never_calls_github_unconfirmed():
    plan = release_gates.protect_plan("acme/site")
    assert plan["call"]["method"] == "PUT" and plan["call"]["path"] == "/repos/acme/site/branches/main/protection"
    assert plan["call"]["json"]["allow_force_pushes"] is False and plan["call"]["json"]["enforce_admins"] is True
    gh = FakeGitHub()
    for bad in ("", "wrong"):
        with pytest.raises(repo.Forbidden):
            release_gates.protect_apply(gh, plan, bad)
    assert gh.calls == []
    release_gates.protect_apply(gh, plan, plan["confirm_token"])
    assert gh.calls == [("PUT", "/repos/acme/site/branches/main/protection", plan["call"]["json"])]
    with pytest.raises(repo.BadRequest):
        release_gates.protect_plan("not a repo")


def test_pages_the_admin_approves_with_a_fresh_factor_and_a_member_cannot(w):
    sj = _promote_to_request(w, SHA1, w["member"])
    assert _client(w["member"]).post(f"/web/releases/deploys/{sj['id']}/approve", headers=H).status_code == 403
    assert _client(w["creator"], fresh=False).post(f"/web/releases/deploys/{sj['id']}/approve", headers=H).status_code == 403
    assert _client(w["creator"]).post(f"/web/releases/deploys/{sj['id']}/approve", headers=H, follow_redirects=False).status_code == 303
    assert _client(w["member"]).get("/web/releases").status_code == 200
    assert _client(w["viewer"]).get("/web/team").status_code == 200
    assert _client(w["viewer"]).post("/web/releases/deploy", data={"target_id": w["dev"]["id"], "sha": SHA2}, headers=H).status_code == 403


def test_the_project_owner_manages_the_team_and_can_hand_over_ownership(w):
    c = _client(w["creator"])
    assert c.post("/web/team/members", data={"username": "adam", "role": "admin"}, headers=H, follow_redirects=False).status_code == 303
    assert access.role_in(P(w["admin2"]), w["proj"]["id"]) == "admin"
    assert _client(w["member"]).post("/web/team/members", data={"username": "vera", "role": "admin"}, headers=H).status_code == 403
    assert _client(w["admin2"]).post("/web/team/transfer", data={"username": "adam"}, headers=H).status_code == 403   # an Admin is not the Owner
    assert c.post("/web/team/transfer", data={"username": "adam"}, headers=H, follow_redirects=False).status_code == 303
    assert access.role_in(P(w["admin2"]), w["proj"]["id"]) == "owner"
    assert access.role_in(P(w["creator"]), w["proj"]["id"]) == "admin"                 # the old Owner stays on as Admin
    assert c.post("/web/team/policy", data={"policy": "production_only"}, headers=H).status_code == 403


# --- review fixes (Fable) --------------------------------------------------------------------------------------------

def test_a_server_is_pinned_to_one_project_and_only_its_owner_defines_targets_on_it(w):
    other = repo.create_project("other", "Other", "internal", "owner", short_name="oth", session_roles=[],
                                 owner_user_id=w["creator"]["id"])
    with pytest.raises(repo.NotFound):                                          # same owner, same server, another project: not pinned
        deploys.add_target_assigned(P(w["creator"]), other, w["srv"], "x", "/srv/x", env="dev")
    with pytest.raises(repo.BadRequest):                                        # a server is pinned to the assignee's OWN project
        access.assign_server(w["srv"], w["creator"]["id"], None, w["proj"]["id"] + 99)
    with pytest.raises(repo.NotFound):                                          # the target belongs to ONE project: another can't use it
        deploys.request_release(P(w["creator"]), other, w["dev"]["id"], SHA1)
    # the Member only ever deploys what the Owner defined; there is no way for them to set a command
    sj = deploys.request_release(P(w["member"]), w["proj"], w["dev"]["id"], SHA1)
    assert sj["state"] == "approved"
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT repo_path, post_cmd FROM deploy_targets WHERE id = %s", (w["dev"]["id"],))
        assert cur.fetchone()["repo_path"] == "/srv/dev"


def _account_world(w):
    """A real account holding the server and the project, so the old approvals page can be used."""
    acct = repo.create_project("realacct", "Real Account", "internal", "owner", short_name="ra", session_roles=[])
    from app import tree
    tree.set_place(acct["id"], "owner", None, kind="account")
    with get_cursor() as cur:
        cur.execute("UPDATE commissioned_servers SET account_id = %s WHERE id = %s", (acct["id"], w["srv"]))
        cur.execute("UPDATE projects SET parent_id = %s WHERE id = %s", (acct["id"], w["proj"]["id"]))
    return acct


def test_the_old_approvals_page_enforces_the_same_rules(w):
    acct = _account_world(w)
    sj = _promote_to_request(w, SHA1, w["member"])
    owner_local = w["owner"]
    stale = _client(owner_local, fresh=False)
    assert stale.post(f"/realacct/approvals/{sj['id']}/approve", headers=H).status_code == 403          # production needs a fresh factor
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT state FROM server_jobs WHERE id = %s", (sj["id"],))
        assert cur.fetchone()["state"] == "requested"
    ok = _client(owner_local).post(f"/realacct/approvals/{sj['id']}/approve", headers=H, follow_redirects=False)
    assert ok.status_code == 303
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT state, decided_by FROM server_jobs WHERE id = %s", (sj["id"],))
        row = cur.fetchone()
    assert row["state"] == "approved" and row["decided_by"] == owner_local["id"]
    assert acct


def test_ask_again_is_promote_only_and_keeps_a_hotfix_a_hotfix(w):
    acct = _account_world(w)
    # a request for a commit that never ran on dev (made by the old low-level path, as a legacy row) can't be re-asked
    with get_cursor() as cur:
        cur.execute("INSERT INTO server_jobs (account_id, project_id, target_id, target_name, sha, state, env) "
                    "VALUES (%s, %s, %s, 'live', %s, 'refused', 'prod') RETURNING id", (acct["id"], w["proj"]["id"], w["prod"]["id"], SHA2))
        old = cur.fetchone()["id"]
    with pytest.raises(repo.BadRequest, match="has not run"):
        deploys.ask_again(acct["id"], old, w["owner"]["id"])
    hot = deploys.request_release(P(w["member"]), w["proj"], w["prod"]["id"], SHA3, hotfix=True, reason="checkout is down for everyone")
    deploys.refuse_checked(P(w["creator"]), w["proj"]["id"], hot["id"])
    again = deploys.ask_again(acct["id"], hot["id"], w["member"]["id"])
    assert again["hotfix"] and again["hotfix_reason"] and again["state"] == "requested"
    done = deploys.approve_checked(P(w["creator"]), w["proj"]["id"], again["id"], True)
    assert done["followup_task_id"]                                              # the merge-back task still opens


def test_an_admin_with_a_pending_request_cannot_be_demoted_or_removed(w):
    access.share(w["proj"]["id"], w["admin2"]["id"], "admin", None)
    sj = _promote_to_request(w, SHA1, w["admin2"])
    with pytest.raises(repo.Conflict, match="waiting"):
        access.share(w["proj"]["id"], w["admin2"]["id"], "member", None)
    with pytest.raises(repo.Conflict, match="waiting"):
        access.unshare(w["proj"]["id"], w["admin2"]["id"])
    deploys.approve_checked(P(w["creator"]), w["proj"]["id"], sj["id"], True)      # decided: now they can be moved
    access.share(w["proj"]["id"], w["admin2"]["id"], "member", None)


def test_the_two_person_rule_counts_admins_when_the_request_is_made(w):
    access.share(w["proj"]["id"], w["admin2"]["id"], "admin", None)
    sj = _promote_to_request(w, SHA1, w["creator"])
    assert sj["admins_at_request"] == 2
    with get_cursor() as cur:                                                    # an Admin vanishes some other way (e.g. disabled)
        cur.execute("DELETE FROM project_members WHERE project_id = %s AND user_id = %s", (w["proj"]["id"], w["admin2"]["id"]))
    with pytest.raises(repo.Forbidden, match="another Admin"):
        deploys.approve_checked(P(w["creator"]), w["proj"]["id"], sj["id"], True)       # still two when it was asked


def test_the_approver_sees_what_attested_a_preview(w):
    deploys.set_release_policy(P(w["creator"]), w["proj"], "production_only")
    proj = repo.get_project(w["proj"]["id"])
    _preview(w, SHA1)
    sj = deploys.request_release(P(w["member"]), proj, w["prod"]["id"], SHA1)
    ev = [r for r in deploys.list_for_project(proj["id"]) if r["id"] == sj["id"]][0]["evidence"]
    assert ev and ev[0]["env"] == "preview" and ev[0]["device_name"] == "pc" and ev[0]["by_name"] == "cara"
    html = _client(w["creator"]).get("/web/releases").text
    assert "attested by runner" in html and "the runner&#39;s own claim" in html or "the runner's own claim" in html


def test_instance_actions_go_to_the_instance_audit_not_a_project(w):
    from app import users
    users.create_user("guest-designer", "designer")
    users.set_role(w["member"]["id"], "viewer")
    texts = [a["summary"] for a in access.recent_audit()]
    assert any("Added guest-designer" in t for t in texts) and any("now a viewer" in t for t in texts)
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT count(*) AS n FROM activity WHERE entity = 'user'")
        assert cur.fetchone()["n"] == 0
    assert "Added guest-designer" in _client(w["owner"]).get("/admin/users").text
    assert _client(w["creator"]).get("/admin/users").status_code == 404


# merge gates, wired: the job records, the Admin approves on the page, the job reports the merge --------------------

def _running_job(w):
    task = repo.create_task(w["proj"]["id"], "work", "", "must", "bug", [], {}, "owner")
    jobs.create_job(task["id"], w["creator"]["id"], "builder", approved=True)
    with get_cursor() as cur:
        cur.execute("INSERT INTO device_tokens (user_id, account_id, name, mode, token_hash, pubkey, pubkey_fp, expires_at) "
                    "VALUES (%s, %s, 'pc', 'subscription', 'th-merge', 'k', 'f', now() + interval '1 day') RETURNING id",
                    (w["creator"]["id"], w["proj"]["id"]))
        dev = cur.fetchone()["id"]
    return jobs.claim(dev)


def test_the_job_records_checks_the_admin_approves_and_the_job_reports_the_merge(w):
    c = _running_job(w)
    jid, fence = c["job_id"], c["fence"]
    with pytest.raises(repo.BadRequest):
        release_gates.job_record_check(jid, fence, "vibes", SHA1, True)
    release_gates.job_record_check(jid, fence, "tests", SHA1, True, "1953 passed")
    with pytest.raises(repo.Conflict):                                           # only an approved request can be reported merged
        release_gates.job_report_merge(jid, fence, SHA1)
    mr = release_gates.list_merges(w["proj"]["id"])[0]
    assert mr["job_id"] == jid and mr["tests_green_at"] and not mr["audit_passed_at"]
    release_gates.job_record_check(jid, fence, "auditor", SHA1, True, "no findings")
    other = _client(w["member"]).post(f"/web/releases/merges/{mr['id']}/approve", headers=H)
    assert other.status_code == 403                                              # a Member can't approve
    assert _client(w["creator"], fresh=False).post(f"/web/releases/merges/{mr['id']}/approve", headers=H).status_code == 403
    assert _client(w["creator"]).post(f"/web/releases/merges/{mr['id']}/approve", headers=H, follow_redirects=False).status_code == 303
    assert release_gates.job_report_merge(jid, fence, SHA1)["state"] == "merged"
    # a job attests only for the commit it names, on its own task's project
    release_gates.job_record_check(jid, fence, "tests", SHA2, True)
    assert {m["sha"] for m in release_gates.list_merges(w["proj"]["id"])} == {SHA1, SHA2}


def test_protect_card_is_owner_only_names_the_real_default_branch_and_never_writes_to_github(w, monkeypatch):
    from app.repo import BadRequest
    from app.routers import releases_pages
    with get_cursor() as cur:
        cur.execute("UPDATE projects SET git_remote = 'git@github.com:acme/site.git' WHERE id = %s", (w["proj"]["id"],))
        cur.execute("INSERT INTO merge_repos (full_name, project_id, added_by) VALUES ('acme/site', %s, 'user:test')", (w["proj"]["id"],))   # the card needs an enrolled repo

    class Gh(FakeGitHub):
        def request(self, method, path, json=None):
            self.calls.append((method, path, json))
            if path == "/repos/acme/site":
                return {"default_branch": "trunk"}
            if path.endswith("/protection") and method == "GET":
                return {"required_pull_request_reviews": {"x": 1}, "restrictions": {"users": []}}
            raise AssertionError(f"unexpected call {method} {path}")

    gh = Gh()
    monkeypatch.setattr(releases_pages, "github_client", lambda: gh)
    creator = _client(w["creator"])
    plan = creator.post("/web/releases/protect/plan", headers=H)
    assert plan.status_code == 200 and "data-protect-card" in plan.text and "/repos/acme/site/branches/trunk/protection" in plan.text
    assert "gh api -X PUT repos/acme/site/branches/trunk/protection" in plan.text and "main" not in plan.text.split("data-protect-card")[1].split("</div>")[0]
    assert "replaces the protection the branch has now" in plan.text and "required pull-request reviews" in plan.text
    assert [c[0] for c in gh.calls] == ["GET", "GET"]                            # planning only reads
    assert _client(w["member"]).post("/web/releases/protect/plan", headers=H).status_code == 403
    assert creator.post("/web/releases/protect/apply", data={"repo_name": "acme/site", "confirm_token": "x"}, headers=H).status_code in (404, 405)
    assert not [c for c in gh.calls if c[0] != "GET"]                            # Crewboss never applies protection itself
    from app import github_client
    assert github_client.repo_full_name("https://github.com/acme/site") == "acme/site"
    with pytest.raises(BadRequest):
        github_client.repo_full_name("git@gitlab.com:a/b.git")


def test_the_admin_page_pins_a_server_to_one_of_their_projects(w):
    with get_cursor() as cur:
        cur.execute("DELETE FROM server_assignments")
    owner = _client(w["owner"])
    r = owner.post(f"/admin/users/{w['creator']['id']}/servers", data={"server_id": w["srv"], "project_id": w["proj"]["id"]},
                   headers=H, follow_redirects=False)
    assert r.status_code == 303 and access.server_assigned_to_project(w["proj"]["id"], w["srv"])
    bad = owner.post(f"/admin/users/{w['creator']['id']}/servers", data={"server_id": w["srv"], "project_id": 999999}, headers=H)
    assert bad.status_code == 400
    assert "pinned" in owner.get("/admin/users").text.lower()


def test_add_target_assigned_refuses_a_server_that_is_not_ready(w):
    with get_cursor() as cur:
        cur.execute("INSERT INTO commissioned_servers (account_id, name, address, login_user, status, created_by) "
                    "VALUES (%s, 'pend', '10.0.0.2', 'deploy', 'pending', 'owner') RETURNING id", (w["acct"]["id"],))
        pending = cur.fetchone()["id"]
    access.assign_server(pending, w["creator"]["id"], None, w["proj"]["id"])
    with pytest.raises(repo.BadRequest, match="ready"):
        deploys.add_target_assigned(P(w["creator"]), w["proj"], pending, "x", "/srv/x", env="dev")


def test_a_project_owner_cannot_set_a_deploy_time_limit_above_the_instance_default(w):
    """#829: one executor runs one deploy at a time, so only the instance owner may allow longer than the default."""
    default = deploys.config.DEPLOY_TIMEOUT_SECONDS
    long = deploys.add_target_assigned(P(w["creator"]), w["proj"], w["srv"], "slow", "/srv/slow", env="dev", timeout_seconds=7200)
    assert long["timeout_seconds"] == default                       # capped, not refused, not 7200
    short = deploys.add_target_assigned(P(w["creator"]), w["proj"], w["srv"], "quick", "/srv/quick", env="dev", timeout_seconds=120)
    assert short["timeout_seconds"] == 120
    none = deploys.add_target_assigned(P(w["creator"]), w["proj"], w["srv"], "dflt", "/srv/dflt", env="dev")
    assert none["timeout_seconds"] is None
    with pytest.raises(repo.BadRequest):                            # the 60..7200 shape check still applies
        deploys.add_target_assigned(P(w["creator"]), w["proj"], w["srv"], "bad", "/srv/bad", env="dev", timeout_seconds=5)
    ow = deploys._add_target_checked(w["acct"]["id"], w["proj"], w["srv"], "own", "/srv/own", "", "main", "owner", "dev", 7200)
    assert ow["timeout_seconds"] == 7200                            # the instance owner's path is not capped
