"""The merge executor (task #829): an approved merge request is fast-forwarded into the default branch by Crewboss itself, through a
GitHub client. GitHub is ALWAYS a fake here: nothing in this file reaches the network. (The real HTTPS client is tested against a stubbed opener.)"""
import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from app import access, config, jobs, local_auth as la, merge_executor, release_gates, repo, web_auth
from app.db import get_cursor
from app.github_client import GitHubError
from app.repo import BadRequest, Conflict, Forbidden

HOST = "crewboss.example.com"
ORIGIN = "https://" + HOST
H = {"Origin": ORIGIN, "HX-Request": "true"}
SHA1, SHA2 = "a" * 40, "b" * 40
FULL = "acme/widgets"


@pytest.fixture(autouse=True)
def local(monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH", "required")
    monkeypatch.setattr(config, "AUTH_SETTING", "local")
    monkeypatch.setattr(config, "OIDC_CLIENT_ID", "")
    monkeypatch.setattr(config, "GITHUB_BOT_LOGIN", "crewboss-bot")          # the executor refuses without one (fail closed)
    monkeypatch.setattr(config, "GITHUB_DENY_LOGINS", frozenset())
    monkeypatch.setattr(config, "PUBLIC_URL", ORIGIN)
    monkeypatch.setattr(la, "_hasher", lambda: PasswordHasher(time_cost=1, memory_cost=8, parallelism=1))
    la._dummy_hash.cache_clear()


class FakeGitHub:
    """Records every call. `heads` maps branch -> sha; `status` is what compare says; `patch_error` makes the ref update fail."""

    def __init__(self, heads=None, status="ahead", default="trunk", patch_error=None, get_error=None):
        self.heads, self.status, self.default = heads or {}, status, default
        self.patch_error, self.get_error, self.calls, self.login = patch_error, get_error, [], "crewboss-bot"

    def request(self, method, path, json=None):
        self.calls.append((method, path, json))
        if self.get_error and method == "GET":
            raise self.get_error
        if method == "GET" and path == f"/repos/{FULL}":
            return {"default_branch": self.default}
        if method == "GET" and path.endswith("/protection"):
            raise GitHubError("GitHub refused: HTTP 404 Branch not protected", 404)
        if method == "GET" and "/branches/" in path:
            return {"commit": {"sha": self.heads.get(path.split("/branches/")[1], "")}}
        if method == "GET" and "/compare/" in path:
            return {"status": self.status}
        if method == "PATCH":
            if self.patch_error:
                raise self.patch_error
            return {"object": {"sha": json["sha"]}}
        if method == "GET" and path == "/user":
            return {"login": self.login}
        if method == "PUT":
            return {}
        raise AssertionError(f"unexpected GitHub call {method} {path}")

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


def _user(name, role="designer"):
    with get_cursor() as cur:
        cur.execute("INSERT INTO users (oid, display_name, username, password_hash, role, is_owner) VALUES (%s, %s, %s, 'x', %s, %s) "
                    "RETURNING *", ("local:" + name, name, name, role, role == "owner"))
        return cur.fetchone()


def P(user):
    return access.Principal(user["id"], user["role"], user["username"])


def _client(user):
    from app.main import app
    c = TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 50000))
    c.cookies.set(web_auth.session_cookie_name(), web_auth.create_local_session(user, "totp"), domain=HOST)
    return c


@pytest.fixture
def w():
    _user("boss", "owner")
    creator, member = _user("cara"), _user("mia")
    repo.create_project("acct", "Acct", "internal", "owner", short_name="acct", session_roles=[])
    proj = repo.create_project("web", "Web", "internal", "owner", short_name="web", session_roles=[], owner_user_id=creator["id"])
    access.share(proj["id"], member["id"], "member", None)
    with get_cursor() as cur:
        cur.execute("UPDATE projects SET git_remote = %s, merge_via_crewboss = true, merge_repo = %s, "
                    "merge_via_crewboss_at = now() - interval '1 day' WHERE id = %s RETURNING *", (f"git@github.com:{FULL}.git", FULL, proj["id"]))
        proj = cur.fetchone()
        cur.execute("INSERT INTO merge_repos (full_name, project_id, added_by) VALUES (%s, %s, 'user:test')", (FULL, proj["id"]))
    return dict(proj=proj, creator=creator, member=member)


def _approved(w, branch="feat/x", sha=SHA1, job_id=None):
    mr = release_gates.request_merge(P(w["member"]), w["proj"], branch, sha, job_id=job_id)
    release_gates.record_check(mr["id"], "tests", True, "job:1")
    release_gates.record_check(mr["id"], "audit", True, "job:2")
    return release_gates.approve_merge(P(w["creator"]), w["proj"]["id"], mr["id"], True)


def _row(mid):
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT * FROM merge_requests WHERE id = %s", (mid,))
        return cur.fetchone()


def _task_and_job(w):
    task = repo.create_task(w["proj"]["id"], "work", "", "must", "bug", [], {}, "owner")
    with get_cursor() as cur:
        cur.execute("UPDATE tasks SET status = 'review' WHERE id = %s", (task["id"],))
    return task["id"], jobs.create_job(task["id"], w["creator"]["id"], "builder", approved=True)["id"]


def test_fast_forwards_to_exactly_the_approved_commit_and_records_the_audit(w):
    mr = _approved(w)
    gh = FakeGitHub(heads={"feat/x": SHA1})
    assert merge_executor.execute(mr["id"], gh)["state"] == "merged"
    assert gh.writes() == [("PATCH", f"/repos/{FULL}/git/refs/heads/trunk", {"sha": SHA1, "force": False})]     # never force, never a merge commit
    row = _row(mr["id"])
    assert (row["state"], row["merged_sha"], row["merge_target"], row["merged_by"]) == ("merged", SHA1, "trunk", merge_executor.ACTOR)
    assert row["merged_at"] and row["decided_by"] == w["creator"]["id"] and row["decided_at"] and row["sha"] == SHA1
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT summary FROM activity WHERE entity = 'merge' AND entity_id = %s AND verb = 'merged'", (mr["id"],))
        assert "cara" in cur.fetchone()["summary"]                              # who approved is in the audit log
    assert merge_executor.execute(mr["id"], gh)["state"] == "skipped"          # a merged request is never run twice
    assert len(gh.writes()) == 1


def test_nothing_unapproved_or_unopted_ever_reaches_github(w):
    asked = release_gates.request_merge(P(w["member"]), w["proj"], "feat/x", SHA1)
    release_gates.record_check(asked["id"], "tests", True, "job:1")
    gh = FakeGitHub(heads={"feat/x": SHA1})
    assert merge_executor.execute(asked["id"], gh)["state"] == "skipped"       # requested, not approved
    mr = _approved(w, "feat/y", SHA2)
    merge_executor.set_enabled(w["proj"]["id"], False, "owner")
    assert merge_executor.execute(mr["id"], gh)["state"] == "skipped"          # the project did not opt in
    assert gh.calls == [] and _row(mr["id"])["state"] == "approved"


def test_a_branch_that_moved_after_approval_is_sent_back_and_the_task_goes_back_to_doing(w):
    task_id, job_id = _task_and_job(w)
    mr = _approved(w, job_id=job_id)
    gh = FakeGitHub(heads={"feat/x": SHA2})                                     # someone pushed another commit
    out = merge_executor.execute(mr["id"], gh)
    assert out["state"] == "sent_back" and SHA1[:12] in out["note"] and gh.writes() == []
    row = _row(mr["id"])
    assert row["state"] == "sent_back" and row["merge_note"] and row["merged_at"] is None
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT status FROM tasks WHERE id = %s", (task_id,))
        assert cur.fetchone()["status"] == "doing"
        cur.execute("SELECT body FROM task_comments WHERE task_id = %s ORDER BY id DESC LIMIT 1", (task_id,))
        assert "sent back" in cur.fetchone()["body"]


def test_a_branch_not_rebased_on_main_is_refused_with_a_clear_reason_and_nothing_changes(w):
    mr = _approved(w)
    gh = FakeGitHub(heads={"feat/x": SHA1}, status="diverged")
    out = merge_executor.execute(mr["id"], gh)
    assert out["state"] == "sent_back" and "Rebase" in out["note"] and "trunk" in out["note"]
    assert gh.writes() == []


def test_github_refusing_a_non_fast_forward_sends_it_back_never_forces(w):
    mr = _approved(w)
    gh = FakeGitHub(heads={"feat/x": SHA1}, patch_error=BadRequest("GitHub refused: gh: Update is not a fast forward (HTTP 422)"))
    assert merge_executor.execute(mr["id"], gh)["state"] == "sent_back"
    assert [c[2]["force"] for c in gh.writes()] == [False]


def test_a_commit_already_on_main_is_recorded_without_touching_it(w):
    mr = _approved(w)
    gh = FakeGitHub(heads={"feat/x": SHA1}, status="behind")
    assert merge_executor.execute(mr["id"], gh)["state"] == "merged"
    assert gh.writes() == [] and "already" in _row(mr["id"])["merge_note"]


def _due(mid):
    with get_cursor() as cur:
        cur.execute("UPDATE merge_requests SET next_attempt_at = now() - interval '1 second' WHERE id = %s", (mid,))


def test_a_github_error_is_retried_with_backoff_for_about_25_minutes_then_sent_back_and_can_be_requeued(w):
    assert sum(merge_executor.backoff_seconds(n) for n in range(1, merge_executor.MAX_ATTEMPTS)) in range(15 * 60, 30 * 60)
    mr = _approved(w)
    gh = FakeGitHub(get_error=GitHubError("GitHub refused: HTTP 502", 502))
    for n in range(1, merge_executor.MAX_ATTEMPTS):
        assert merge_executor.execute(mr["id"], gh)["state"] == "approved"
        row = _row(mr["id"])
        assert row["merge_attempts"] == n and row["next_attempt_at"] is not None
        assert merge_executor.run_once(gh) == 0 and _row(mr["id"])["merge_attempts"] == n          # not due yet: the sweep leaves it
        _due(mr["id"])
    assert merge_executor.run_once(gh) == 1                                                       # the last try
    row = _row(mr["id"])
    assert row["state"] == "sent_back" and row["sent_back_kind"] == "github" and "7 times" in row["merge_note"]
    # an Admin re-queues it WITHOUT a new commit (fresh factor needed; the same gates still hold)
    with pytest.raises(Forbidden):
        release_gates.requeue_merge(P(w["creator"]), w["proj"]["id"], mr["id"], False)
    with pytest.raises(Forbidden):
        release_gates.requeue_merge(P(w["member"]), w["proj"]["id"], mr["id"], True)
    release_gates.requeue_merge(P(w["creator"]), w["proj"]["id"], mr["id"], True)
    row = _row(mr["id"])
    assert (row["state"], row["merge_attempts"], row["sha"]) == ("approved", 0, SHA1)
    assert merge_executor.execute(mr["id"], FakeGitHub(heads={"feat/x": SHA1}))["state"] == "merged"


def test_a_4xx_from_github_sends_the_request_back_at_once_but_a_rate_limit_or_5xx_is_retried(w):
    mr = _approved(w)
    gh = FakeGitHub(get_error=GitHubError("GitHub refused: HTTP 403 Resource not accessible by personal access token", 403))
    assert merge_executor.execute(mr["id"], gh)["state"] == "sent_back"
    assert len(gh.calls) == 1 and _row(mr["id"])["sent_back_kind"] == "github" and _row(mr["id"])["merge_attempts"] == 1
    for n, err in enumerate((GitHubError("GitHub refused: HTTP 403 API rate limit exceeded", 403), GitHubError("HTTP 429", 429),
                             GitHubError("could not reach GitHub", 0))):
        again = _approved(w, branch=f"feat/r{n}", sha=SHA2)
        assert merge_executor.execute(again["id"], FakeGitHub(get_error=err))["state"] == "approved"


def test_a_moved_or_diverged_branch_cannot_be_requeued_only_asked_for_again(w):
    mr = _approved(w)
    merge_executor.execute(mr["id"], FakeGitHub(heads={"feat/x": SHA1}, status="diverged"))
    assert _row(mr["id"])["sent_back_kind"] == ""
    with pytest.raises(Conflict):
        release_gates.requeue_merge(P(w["creator"]), w["proj"]["id"], mr["id"], True)


def test_the_approve_button_runs_the_executor_and_the_toggle_is_owner_only(w, monkeypatch):
    from app.routers import releases_pages
    gh = FakeGitHub(heads={"feat/x": SHA1})
    monkeypatch.setattr(releases_pages, "github_client", lambda: gh)
    mr = release_gates.request_merge(P(w["member"]), w["proj"], "feat/x", SHA1)
    release_gates.record_check(mr["id"], "tests", True, "j")
    release_gates.record_check(mr["id"], "audit", True, "j")
    assert _client(w["member"]).post(f"/web/releases/merges/{mr['id']}/approve", headers=H).status_code == 403
    assert gh.calls == []
    assert _client(w["creator"]).post(f"/web/releases/merges/{mr['id']}/approve", headers=H, follow_redirects=False).status_code == 303
    assert _row(mr["id"])["state"] == "merged" and len(gh.writes()) == 1
    assert _client(w["member"]).post("/web/releases/merge-executor", data={"enable": ""}, headers=H).status_code == 403
    assert _client(w["creator"]).post("/web/releases/merge-executor", data={"enable": ""}, headers=H, follow_redirects=False).status_code == 303
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT merge_via_crewboss FROM projects WHERE id = %s", (w["proj"]["id"],))
        assert cur.fetchone()["merge_via_crewboss"] is False
    page = _client(w["creator"]).get("/web/releases")
    assert page.status_code == 200 and "Turn on" in page.text and "merged to trunk" in page.text


def test_the_merge_path_protection_card_names_only_crewboss_and_is_a_card_for_a_human_admin(w, monkeypatch):
    from app.routers import releases_pages
    gh = FakeGitHub()
    monkeypatch.setattr(releases_pages, "github_client", lambda: gh)
    plan = release_gates.protect_plan(FULL, "trunk", push_login="crewboss-bot")
    body = plan["call"]["json"]
    assert body["restrictions"]["users"] == ["crewboss-bot"] and body["allow_force_pushes"] is False and body["required_linear_history"] is True
    with pytest.raises(BadRequest):
        release_gates.protect_plan(FULL, "main", push_login="x; rm -rf")
    c = _client(w["creator"])
    page = c.post("/web/releases/protect/plan", data={"mode": "merge-path"}, headers=H)
    assert page.status_code == 200 and "crewboss-bot" in page.text and "trunk/protection" in page.text and "gh api -X PUT" in page.text
    assert [x[0] for x in gh.calls] == ["GET", "GET", "GET"] and gh.writes() == []          # planning only reads, and never writes
    assert c.post("/web/releases/protect/apply", data={"repo_name": FULL, "confirm_token": plan["confirm_token"]}, headers=H).status_code in (404, 405)
    assert gh.writes() == []


def test_the_card_supports_a_github_app_identity_in_apps_and_says_what_it_replaces(w):
    body = release_gates.protection_payload("crewboss-merge[bot]")
    assert body["restrictions"] == {"users": [], "teams": [], "apps": ["crewboss-merge"]}
    plan = release_gates.protect_plan(FULL, "trunk", push_login="crewboss-merge[bot]",
                                      existing={"required_status_checks": {}, "restrictions": {"users": []}, "required_signatures": {"enabled": True}})
    assert "an app" in plan["card"] and "required status checks" in plan["card"] and "required signed commits" in plan["card"]
    assert "trunk" in plan["command"] and "main" not in plan["command"]
    assert "replaces" in release_gates.describe_protection({"restrictions": {}}) and "nothing is replaced" in release_gates.describe_protection({"x": 1})


def test_a_job_report_is_not_an_attestation_when_crewboss_merges_itself(w):
    task = repo.create_task(w["proj"]["id"], "work", "", "must", "bug", [], {}, "owner")
    jobs.create_job(task["id"], w["creator"]["id"], "builder", approved=True)
    with get_cursor() as cur:
        cur.execute("INSERT INTO device_tokens (user_id, account_id, name, mode, token_hash, pubkey, pubkey_fp, expires_at) "
                    "VALUES (%s, %s, 'pc', 'subscription', 'th-me', 'k', 'f', now() + interval '1 day') RETURNING id",
                    (w["creator"]["id"], w["proj"]["id"]))
        dev = cur.fetchone()["id"]
    c = jobs.claim(dev)
    release_gates.job_record_check(c["job_id"], c["fence"], "tests", SHA1, True)
    release_gates.job_record_check(c["job_id"], c["fence"], "auditor", SHA1, True)
    mr = release_gates.list_merges(w["proj"]["id"])[0]
    release_gates.approve_merge(P(w["creator"]), w["proj"]["id"], mr["id"], True)
    out = release_gates.job_report_merge(c["job_id"], c["fence"], SHA1)
    assert out["state"] == "approved" and _row(mr["id"])["state"] == "approved"       # only the executor marks it merged


# === security review fixes (#829) ==========================================================================================

def _aged_client(user, minutes):
    c = _client(user)
    with get_cursor() as cur:
        cur.execute("UPDATE web_sessions SET second_factor_at = now() - make_interval(mins => %s) WHERE user_id = %s", (minutes, user["id"]))
    return c


def _decided_ago(mid, interval):
    with get_cursor() as cur:
        cur.execute("UPDATE merge_requests SET decided_at = now() - %s::interval WHERE id = %s", (interval, mid))


# 1. authority is the repository's, held by the instance owner -----------------------------------------------------------------

def test_a_shadow_project_pointed_at_someone_elses_repo_cannot_move_its_main(w, monkeypatch):
    """The reviewer's demo A: a designer makes a project, points its git_remote at an enrolled repo and tries to opt in."""
    from app.routers import releases_pages
    dana = _user("dana")
    shadow = repo.create_project("shadow", "Shadow", "internal", "owner", short_name="shadow", session_roles=[], owner_user_id=dana["id"])
    c = _client(dana)
    gh = FakeGitHub(default="main")
    monkeypatch.setattr(releases_pages, "github_client", lambda: gh)
    assert c.post("/shadow/details", data={"git_remote": f"git@github.com:{FULL}.git"}, headers=H, follow_redirects=False).status_code == 303
    r = c.post("/shadow/releases/merge-executor", data={"enable": "1"}, headers=H, follow_redirects=False)
    assert r.status_code in (400, 409)                      # not enrolled for HER project, and the repo is already in use
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT merge_via_crewboss FROM projects WHERE id = %s", (shadow["id"],))
        assert cur.fetchone()["merge_via_crewboss"] is False
    # even with the other project switched off, the enrolment is for 'web', not for her: she still cannot turn it on
    merge_executor.set_enabled(w["proj"]["id"], False, "owner")
    assert c.post("/shadow/releases/merge-executor", data={"enable": "1"}, headers=H, follow_redirects=False).status_code == 400
    # and she cannot enrol it herself: only the instance owner edits the list
    assert c.post("/shadow/releases/merge-repos", data={"full_name": FULL}, headers=H).status_code == 403
    assert gh.writes() == []


def test_only_the_instance_owner_edits_the_enrolled_repositories_with_a_fresh_factor(w):
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT * FROM users WHERE username = 'boss'")
        boss = cur.fetchone()
    c = _client(boss)
    assert c.post("/web/releases/merge-repos", data={"full_name": "acme/other"}, headers=H, follow_redirects=False).status_code == 303
    assert [r["full_name"] for r in merge_executor.list_repos()] == sorted([FULL, "acme/other"])
    assert _client(w["creator"]).post("/web/releases/merge-repos", data={"full_name": "acme/x"}, headers=H).status_code == 403   # a project Owner
    assert _aged_client(boss, 30).post("/web/releases/merge-repos", data={"full_name": "acme/y"}, headers=H).status_code == 403   # not 5 minutes
    c = _client(boss)                                       # (the aged client above shared this person's session row)
    assert c.post("/web/releases/merge-repos", data={"full_name": "not a repo"}, headers=H).status_code == 400
    # removing a repository switches its project off
    assert c.post("/web/releases/merge-repos/remove", data={"full_name": FULL}, headers=H, follow_redirects=False).status_code == 303
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT merge_via_crewboss, merge_repo FROM projects WHERE id = %s", (w["proj"]["id"],))
        assert dict(cur.fetchone()) == {"merge_via_crewboss": False, "merge_repo": ""}


def test_two_projects_cannot_both_merge_into_one_repository(w):
    other = repo.create_project("twin", "Twin", "internal", "owner", short_name="twin", session_roles=[], owner_user_id=w["creator"]["id"])
    with get_cursor() as cur:
        cur.execute("UPDATE projects SET git_remote = %s WHERE id = %s", (f"https://github.com/{FULL}.git", other["id"]))
        with pytest.raises(Exception) as err:
            cur.execute("UPDATE projects SET merge_via_crewboss = true, merge_repo = %s WHERE id = %s", (FULL, other["id"]))
    assert "projects_merge_repo_uniq" in str(err.value)


def test_changing_the_git_remote_turns_the_opt_in_off_and_a_request_for_the_old_repo_is_refused(w):
    from app import tree
    mr = _approved(w)
    tree.set_details(w["proj"]["id"], "owner", git_remote=f"git@github.com:{FULL}.git")             # the same remote: stays on
    assert repo.get_project(w["proj"]["id"])["merge_via_crewboss"] is True
    tree.set_details(w["proj"]["id"], "owner", git_remote="git@github.com:victim/their-repo.git")
    after = repo.get_project(w["proj"]["id"])
    assert (after["merge_via_crewboss"], after["merge_repo"], after["merge_via_crewboss_at"]) == (False, "", None)
    gh = FakeGitHub(heads={"feat/x": SHA1})
    assert merge_executor.execute(mr["id"], gh)["state"] == "skipped" and gh.calls == []
    # even if the opt-in is forced back on in SQL, the request remembers the repo it was made for
    with get_cursor() as cur:
        cur.execute("UPDATE projects SET merge_via_crewboss = true, merge_repo = 'victim/their-repo', merge_via_crewboss_at = now() - interval '2 days' "
                    "WHERE id = %s", (w["proj"]["id"],))
        cur.execute("INSERT INTO merge_repos (full_name, project_id) VALUES ('victim/their-repo', %s)", (w["proj"]["id"],))
    out = merge_executor.execute(mr["id"], gh)
    assert out["state"] == "sent_back" and "acme/widgets" in out["note"] and gh.calls == []


def test_a_request_made_before_the_repo_was_recorded_is_sent_back_not_guessed(w):
    mr = _approved(w)
    with get_cursor() as cur:
        cur.execute("UPDATE merge_requests SET merge_repo = '' WHERE id = %s", (mr["id"],))
    gh = FakeGitHub(heads={"feat/x": SHA1})
    assert merge_executor.execute(mr["id"], gh)["state"] == "sent_back" and gh.calls == []


# 2. the credential --------------------------------------------------------------------------------------------------------------

def _enable_ready(w):
    """The project, not yet opted in, with its repo enrolled."""
    merge_executor.set_enabled(w["proj"]["id"], False, "owner")
    return repo.get_project(w["proj"]["id"])


def test_the_toggle_refuses_without_a_token_a_working_get_user_a_non_human_login_and_web_sign_in(w, monkeypatch):
    from app import github_client
    project = _enable_ready(w)
    # no token file: the real client says so and nothing turns on
    monkeypatch.setattr(config, "GITHUB_TOKEN_FILE", "/nonexistent/github-token")
    with pytest.raises(BadRequest, match="no GitHub token"):
        merge_executor.enable(project, "user:1", github_client.GitHubClient())
    # GET /user fails
    with pytest.raises(BadRequest):
        merge_executor.enable(project, "user:1", FakeGitHub(get_error=GitHubError("GitHub refused: HTTP 401 Bad credentials", 401)))
    # the login is on the deny-list of human accounts (case-insensitive), or is not the configured bot
    monkeypatch.setattr(config, "GITHUB_DENY_LOGINS", frozenset({"human-admin"}))
    with pytest.raises(BadRequest, match="person's account"):
        merge_executor.enable(project, "user:1", _gh_as("Human-Admin"))
    monkeypatch.setattr(config, "GITHUB_BOT_LOGIN", "crewboss-bot")
    with pytest.raises(BadRequest, match="configured bot"):
        merge_executor.enable(project, "user:1", _gh_as("someone-else"))
    assert repo.get_project(w["proj"]["id"])["merge_via_crewboss"] is False
    # web sign-in off
    monkeypatch.setattr(config, "WEB_AUTH", "off")
    with pytest.raises(BadRequest, match="sign-in"):
        merge_executor.enable(project, "user:1", FakeGitHub())
    monkeypatch.setattr(config, "WEB_AUTH", "required")
    assert merge_executor.enable(project, "user:1", FakeGitHub()) == "crewboss-bot"
    row = repo.get_project(w["proj"]["id"])
    assert (row["merge_via_crewboss"], row["merge_repo"]) == (True, FULL) and row["merge_via_crewboss_at"]


def _gh_as(login):
    gh = FakeGitHub()
    gh.login = login
    return gh


def test_the_toggle_page_shows_a_refusal_instead_of_a_500(w, monkeypatch):
    from app.routers import releases_pages
    _enable_ready(w)
    monkeypatch.setattr(releases_pages, "github_client", lambda: FakeGitHub(get_error=GitHubError("could not reach GitHub (URLError)", 0)))
    r = _client(w["creator"]).post("/web/releases/merge-executor", data={"enable": "1"}, headers=H)
    assert r.status_code == 400 and "could not reach GitHub" in r.text
    monkeypatch.setattr(releases_pages, "github_client", lambda: FakeGitHub())
    assert _client(w["creator"]).post("/web/releases/merge-executor", data={"enable": "1"}, headers=H, follow_redirects=False).status_code == 303
    assert repo.get_project(w["proj"]["id"])["merge_via_crewboss"] is True


def test_the_protect_card_shows_a_github_failure_instead_of_a_500(w, monkeypatch):
    from app.routers import releases_pages
    monkeypatch.setattr(releases_pages, "github_client", lambda: FakeGitHub(get_error=GitHubError("could not reach GitHub", 0)))
    r = _client(w["creator"]).post("/web/releases/protect/plan", headers=H)
    assert r.status_code == 200 and "data-protect-error" in r.text


class _Resp:
    def __init__(self, body=b"{}"):
        self.body = body

    def read(self, n=-1):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_the_real_client_is_plain_https_with_a_token_from_a_0600_file(tmp_path, monkeypatch):
    import io
    import os
    import urllib.error
    from app import github_client as gc
    seen = []
    monkeypatch.setattr(gc._OPENER, "open", lambda req, timeout=None: (seen.append(req), _Resp(b'{"login": "bot"}'))[1])
    tok = tmp_path / "token"
    tok.write_text("github_pat_" + "x" * 40)
    os.chmod(tok, 0o644)
    with pytest.raises(GitHubError, match="readable by others"):
        gc.GitHubClient(str(tok)).request("GET", "/user")
    os.chmod(tok, 0o600)
    assert gc.GitHubClient(str(tok)).request("GET", "/user") == {"login": "bot"}
    req = seen[0]
    assert req.full_url == "https://api.github.com/user" and req.get_header("Authorization") == "Bearer github_pat_" + "x" * 40
    gc.GitHubClient(str(tok)).request("PATCH", f"/repos/{FULL}/git/refs/heads/main", json={"sha": SHA1, "force": False})
    assert seen[1].get_method() == "PATCH" and seen[1].data == b'{"sha": "' + SHA1.encode() + b'", "force": false}'
    for bad in ("https://evil.example/x", "/orgs/x", f"/repos/{FULL}/../../other/repo", "/user//x", "/repos/a/b/c d"):
        with pytest.raises(GitHubError):
            gc.GitHubClient(str(tok)).request("GET", bad)
    # an error never carries the token and is classified: 4xx is a refusal, 5xx / network are transient
    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 422, "x", {}, io.BytesIO(b'{"message": "Update is not a fast forward"}'))
    monkeypatch.setattr(gc._OPENER, "open", boom)
    with pytest.raises(GitHubError) as err:
        gc.GitHubClient(str(tok)).request("GET", "/user")
    assert err.value.status == 422 and not err.value.transient and "fast forward" in str(err.value) and "github_pat" not in str(err.value)
    monkeypatch.setattr(gc._OPENER, "open", lambda req, timeout=None: (_ for _ in ()).throw(urllib.error.URLError("down")))
    with pytest.raises(GitHubError) as err:
        gc.GitHubClient(str(tok)).request("GET", "/user")
    assert err.value.transient and err.value.status == 0


def test_a_missing_gh_binary_is_no_longer_a_thing(w):
    import inspect
    from app import github_client
    assert "subprocess" not in inspect.getsource(github_client).replace("no `gh`", "")


# 3. a fresh factor means five minutes --------------------------------------------------------------------------------------------

def test_approving_the_opt_in_and_the_repo_list_need_a_tap_in_the_last_five_minutes_not_twelve_hours(w, monkeypatch):
    from app.routers import releases_pages
    gh = FakeGitHub(heads={"feat/x": SHA1})
    monkeypatch.setattr(releases_pages, "github_client", lambda: gh)
    mr = release_gates.request_merge(P(w["member"]), w["proj"], "feat/x", SHA1)
    release_gates.record_check(mr["id"], "tests", True, "j")
    release_gates.record_check(mr["id"], "audit", True, "j")
    old = _aged_client(w["creator"], 30)                    # tapped half an hour ago: fine for the old 12 hours, not for this
    assert old.post(f"/web/releases/merges/{mr['id']}/approve", headers=H).status_code == 403
    assert old.post("/web/releases/merge-executor", data={"enable": ""}, headers=H).status_code == 403
    assert _row(mr["id"])["state"] == "requested" and gh.calls == []
    assert _aged_client(w["creator"], 2).post(f"/web/releases/merges/{mr['id']}/approve", headers=H, follow_redirects=False).status_code == 303
    # the approve function itself no longer trusts a hard-coded True: the page passes the real freshness
    z = release_gates.request_merge(P(w["member"]), w["proj"], "feat/z", SHA2)
    release_gates.record_check(z["id"], "tests", True, "j")
    release_gates.record_check(z["id"], "audit", True, "j")
    with pytest.raises(Forbidden):
        release_gates.approve_merge(P(w["creator"]), w["proj"]["id"], z["id"], False)


# 4. approvals expire, and only approvals made after the opt-in count -------------------------------------------------------------

def test_an_approval_from_before_the_opt_in_is_never_merged_and_is_sent_back_to_ask_again(w):
    """The reviewer's demo B, and re-review N7: such an approval used to sit 'approved' for ever. Choice: turning the executor on sends it back
    (plain sent_back, not requeueable: a NEW request is made, by someone who knows Crewboss now merges) with a note."""
    merge_executor.set_enabled(w["proj"]["id"], False, "user:1")
    mr = _approved(w)                                       # approved under the old model, with the executor off
    _decided_ago(mr["id"], "90 days")
    gh = FakeGitHub(heads={"feat/x": SHA1})
    merge_executor.enable(repo.get_project(w["proj"]["id"]), "user:1", gh)
    row = _row(mr["id"])
    assert row["state"] == "sent_back" and row["merge_note"] == "approved before Crewboss merging was on; ask again" and row["sent_back_kind"] == ""
    assert merge_executor.run_once(gh) == 0 and gh.writes() == []
    with pytest.raises(Conflict):
        release_gates.requeue_merge(P(w["creator"]), w["proj"]["id"], mr["id"], True)       # not a re-queue: ask again
    # an approval made after the opt-in is claimed
    fresh = _approved(w, branch="feat/after", sha=SHA2)
    gh.heads["feat/after"] = SHA2
    assert merge_executor.execute(fresh["id"], gh)["state"] == "merged"


def test_a_pre_opt_in_approval_left_behind_is_swept_by_the_pass_too(w):
    """Re-review N7 as shown: the row is already 'approved' from before the opt-in (rows from earlier builds): the pass sends it back."""
    mr = _approved(w)
    _decided_ago(mr["id"], "3 days")                         # the fixture's opt-in was 1 day ago
    gh = FakeGitHub(heads={"feat/x": SHA1})
    assert merge_executor.run_once(gh) == 0 and gh.calls == []
    row = _row(mr["id"])
    assert row["state"] == "sent_back" and "ask again" in row["merge_note"]


def test_an_approval_expires_after_24_hours_and_an_admin_can_requeue_it(w):
    with get_cursor() as cur:
        cur.execute("UPDATE projects SET merge_via_crewboss_at = now() - interval '10 days' WHERE id = %s", (w["proj"]["id"],))
    mr = _approved(w)
    _decided_ago(mr["id"], "25 hours")
    gh = FakeGitHub(heads={"feat/x": SHA1})
    assert merge_executor.execute(mr["id"], gh)["state"] == "skipped" and gh.calls == []      # not even claimed
    merge_executor.run_once(gh)
    row = _row(mr["id"])
    assert row["state"] == "sent_back" and row["sent_back_kind"] == "expired" and "24 hours" in row["merge_note"] and gh.calls == []
    release_gates.requeue_merge(P(w["creator"]), w["proj"]["id"], mr["id"], True)
    assert merge_executor.execute(mr["id"], gh)["state"] == "merged"


def test_the_approver_must_still_be_an_enabled_admin_when_the_executor_acts(w):
    mr = _approved(w)
    with get_cursor() as cur:
        cur.execute("UPDATE users SET disabled_at = now() WHERE id = %s", (w["creator"]["id"],))
        cur.execute("UPDATE projects SET owner_user_id = %s WHERE id = %s", (w["member"]["id"], w["proj"]["id"]))
    gh = FakeGitHub(heads={"feat/x": SHA1})
    out = merge_executor.execute(mr["id"], gh)
    assert out["state"] == "sent_back" and "no longer an Admin" in out["note"] and gh.calls == []


def test_the_executor_does_nothing_when_web_sign_in_is_off(w, monkeypatch):
    mr = _approved(w)
    monkeypatch.setattr(config, "WEB_AUTH", "off")
    gh = FakeGitHub(heads={"feat/x": SHA1})
    assert merge_executor.execute(mr["id"], gh)["state"] == "skipped" and gh.calls == [] and _row(mr["id"])["state"] == "approved"


# 5. a crash in 'merging' is recovered ---------------------------------------------------------------------------------------------

def test_a_claim_left_merging_by_a_dead_process_is_returned_and_a_rerun_is_idempotent(w):
    """The reviewer's demo C, fixed."""
    mr = _approved(w)
    assert merge_executor._claim(mr["id"]) is not None       # ...the process dies here (deploy, OOM, restart)
    gh = FakeGitHub(heads={"feat/x": SHA1})
    assert merge_executor.run_once(gh) == 0 and _row(mr["id"])["state"] == "merging" and gh.calls == []    # a live claim is left alone
    assert merge_executor.recover() == 0
    with get_cursor() as cur:
        cur.execute("UPDATE merge_requests SET merging_at = now() - interval '4 minutes' WHERE id = %s", (mr["id"],))
    assert merge_executor.run_once(gh) == 1                  # recovered at the start of the pass, then run
    assert _row(mr["id"])["state"] == "merged" and gh.writes() == [("PATCH", f"/repos/{FULL}/git/refs/heads/trunk", {"sha": SHA1, "force": False})]
    # the push LANDED but the process died before recording it: the rerun sees 'identical' and records it without a second write
    mr2 = _approved(w, branch="feat/y", sha=SHA2)
    merge_executor._claim(mr2["id"])
    with get_cursor() as cur:
        cur.execute("UPDATE merge_requests SET merging_at = now() - interval '1 hour' WHERE id = %s", (mr2["id"],))
    gh2 = FakeGitHub(heads={"feat/y": SHA2}, status="identical")
    merge_executor.run_once(gh2)
    assert _row(mr2["id"])["state"] == "merged" and gh2.writes() == []


def test_a_slow_first_process_cannot_overwrite_a_recovered_and_rerun_request(w):
    mr = _approved(w)
    claimed, _ = merge_executor._claim(mr["id"])
    with get_cursor() as cur:
        cur.execute("UPDATE merge_requests SET merging_at = now() - interval '1 hour' WHERE id = %s", (mr["id"],))
    merge_executor.recover()
    assert merge_executor.execute(mr["id"], FakeGitHub(heads={"feat/x": SHA1}))["state"] == "merged"
    merge_executor._back(claimed, "late and wrong")          # the slow original finishes now: it must not touch a merged row
    assert _row(mr["id"])["state"] == "merged"


def test_an_error_while_recording_the_outcome_never_raises_and_recover_returns_the_claim(w, monkeypatch):
    mr = _approved(w)
    boom = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db went away"))  # noqa: E731
    monkeypatch.setattr(merge_executor, "_merged", boom)
    monkeypatch.setattr(merge_executor, "_retry", boom)
    out = merge_executor.execute(mr["id"], FakeGitHub(heads={"feat/x": SHA1}))        # no exception reaches the caller
    assert out["state"] == "merging" and _row(mr["id"])["state"] == "merging"
    with get_cursor() as cur:
        cur.execute("UPDATE merge_requests SET merging_at = now() - interval '5 minutes' WHERE id = %s", (mr["id"],))
    assert merge_executor.recover() == 1 and _row(mr["id"])["state"] == "approved"


def test_only_one_process_runs_the_merge_pass(w):
    mr = _approved(w)
    gh = FakeGitHub(heads={"feat/x": SHA1})
    with merge_executor._run_lock() as got:
        assert got is True
        assert merge_executor.run_once(gh) == 0 and gh.calls == [] and _row(mr["id"])["state"] == "approved"      # another holder: this one does nothing
    assert merge_executor.run_once(gh) == 1


# 8. branch names ---------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["x/../../../other-org/other-repo/branches/main", "a..b", "a//b", "/a", "a/", "a.", "x.lock", ".hidden", "a b"])
def test_dot_segments_and_slashes_are_not_branch_names(w, bad):
    """The reviewer's demo D, fixed: the name is refused when asked for, and the database refuses it too."""
    assert not release_gates.BRANCH_RE.match(bad)
    with pytest.raises(BadRequest):
        release_gates.request_merge(P(w["member"]), w["proj"], bad, SHA1)
    with get_cursor() as cur:
        with pytest.raises(Exception) as err:
            cur.execute("INSERT INTO merge_requests (project_id, branch, sha) VALUES (%s, %s, %s)", (w["proj"]["id"], bad, SHA1))
    assert "merge_requests_branch_check" in str(err.value) or "violates" in str(err.value)


def test_ordinary_branch_names_still_pass():
    for ok in ("main", "feat/829-merge-executor", "dash/j12-e1", "release-1.2.3", "a.b/c_d"):
        assert release_gates.BRANCH_RE.match(ok), ok


def test_an_old_row_with_a_dotted_branch_is_sent_back_by_the_executor_before_any_github_call(w):
    mr = dict(_approved(w))
    mr["branch"] = "x/../../../other-org/other-repo/branches/main"      # a row from before migration 0110 (NOT VALID leaves old rows alone)
    with pytest.raises(merge_executor._Back):
        merge_executor._check_authority(mr, w["proj"])


# 9. mark_merged ---------------------------------------------------------------------------------------------------------------

def test_mark_merged_refuses_an_opted_in_project_inside_the_update_itself(w):
    mr = _approved(w)
    with pytest.raises(Conflict):
        release_gates.mark_merged(mr["id"])
    assert _row(mr["id"])["state"] == "approved"
    merge_executor.set_enabled(w["proj"]["id"], False, "owner")          # off: the manual path works as before
    assert release_gates.mark_merged(mr["id"])["state"] == "merged"


# 6. re-review fixes (Fable, 8d2f19f) --------------------------------------------------------------------------------------------

def _age_claim(mid):
    with get_cursor() as cur:
        cur.execute("UPDATE merge_requests SET merging_at = now() - interval '1 hour' WHERE id = %s", (mid,))


def test_N1_a_stalled_first_attempt_cannot_overwrite_a_reclaimed_row(w):
    mr = _approved(w)
    first, proj = merge_executor._claim(mr["id"])            # attempt 1 starts, then stalls past the stale limit
    _age_claim(mr["id"])
    assert merge_executor.recover() == 1
    second, _ = merge_executor._claim(mr["id"])              # attempt 2 claims the row again
    gh = FakeGitHub(heads={"feat/x": SHA1})
    target, landed, note = merge_executor._attempt(second, proj, gh)
    assert gh.writes()
    late = merge_executor._back(first, "late verdict of the stalled first attempt")        # attempt 1 wakes up
    assert late["state"] == "skipped" and _row(mr["id"])["state"] == "merging"             # it changed nothing
    assert merge_executor._retry(first, "late retry")["state"] == "skipped" and _row(mr["id"])["state"] == "merging"
    assert merge_executor._merged(first, "trunk", SHA2)["state"] == "skipped" and _row(mr["id"])["state"] == "merging"
    out = merge_executor._merged(second, target, landed, note)
    row = _row(mr["id"])
    assert out["state"] == "merged" and row["state"] == "merged" and row["merged_sha"] == SHA1 and row["merged_at"]
    assert merge_executor._merged(second, target, landed, note)["state"] == "skipped"       # never reports merged twice


def test_N2_a_disabled_project_creator_is_not_an_enabled_admin(w):
    mr = _approved(w)                                         # approved by cara, the project's creator
    assert w["creator"]["id"] in access.admin_user_ids(w["proj"]["id"])
    with get_cursor() as cur:
        cur.execute("UPDATE users SET disabled_at = now() WHERE id = %s", (w["creator"]["id"],))
    assert w["creator"]["id"] not in access.admin_user_ids(w["proj"]["id"])
    gh = FakeGitHub(heads={"feat/x": SHA1})
    out = merge_executor.execute(mr["id"], gh)
    assert out["state"] == "sent_back" and "no longer an Admin" in out["note"] and gh.writes() == []


def test_N3_an_old_bad_open_row_is_closed_by_the_migration_and_the_check_stays_in_force(w):
    import psycopg
    from pathlib import Path
    sql = (Path(__file__).resolve().parent.parent / "db" / "migrations" / "0110_merge_executor.sql").read_text()
    conn = psycopg.connect(config.DATABASE_URL, autocommit=True)
    try:
        conn.execute("ALTER TABLE merge_requests DROP CONSTRAINT merge_requests_branch_check")
        ids = [conn.execute("INSERT INTO merge_requests (project_id, branch, sha, requested_by, tests_green_at, audit_passed_at, state) "
                            "VALUES (%s, %s, %s, %s, now(), now(), %s) RETURNING id",
                            (w["proj"]["id"], b, SHA1, w["member"]["id"], st)).fetchone()[0]
               for b, st in (("release-1.", "requested"), ("a..b", "approved"), ("ok/branch", "requested"), ("bad//x", "merged"))]
        conn.execute(sql)                                     # the migration is re-run: idempotent, and closes the open bad rows first
        conn.execute(sql)
    finally:
        conn.close()
    states = [_row(i)["state"] for i in ids]
    assert states == ["sent_back", "sent_back", "requested", "merged"]
    assert "not a plain git branch name" in _row(ids[0])["merge_note"]
    # nothing open trips the CHECK any more: the open good row updates fine, and a new bad row is refused
    release_gates.refuse_merge(P(w["creator"]), w["proj"]["id"], ids[2])
    import psycopg.errors
    with pytest.raises(psycopg.errors.CheckViolation), get_cursor() as cur:
        cur.execute("INSERT INTO merge_requests (project_id, branch, sha, requested_by) VALUES (%s, 'x..y', %s, %s)",
                    (w["proj"]["id"], SHA1, w["member"]["id"]))


def test_N4_with_no_bot_login_configured_enable_refuses(w, monkeypatch):
    monkeypatch.setattr(config, "GITHUB_BOT_LOGIN", "")
    merge_executor.set_enabled(w["proj"]["id"], False, "user:1")
    with pytest.raises(BadRequest, match="no GitHub bot account is configured"):
        merge_executor.enable(repo.get_project(w["proj"]["id"]), "user:1", _gh_as("some-human-admin"))
    assert repo.get_project(w["proj"]["id"])["merge_via_crewboss"] is False


def test_N5_identity_is_rechecked_before_each_write_and_once_per_pass(w, monkeypatch, caplog):
    monkeypatch.setattr(config, "GITHUB_DENY_LOGINS", frozenset({"human-admin"}))
    mr = _approved(w)
    gh = _gh_as("human-admin")                                # the token file now holds a person's token
    gh.heads = {"feat/x": SHA1}
    with caplog.at_level("WARNING"):
        out = merge_executor.execute(mr["id"], gh)
    assert out["state"] == "sent_back" and "not the configured bot" in out["note"] and gh.writes() == []
    assert any(c[1] == "/user" for c in gh.calls) and "identity check" in caplog.text
    assert _row(mr["id"])["sent_back_kind"] == "github"        # an Admin re-queues it once the right token is mounted
    # a different login that is merely not the bot is refused too
    gh2 = _gh_as("someone-else")
    gh2.heads = {"feat/x": SHA1}
    release_gates.requeue_merge(P(w["creator"]), w["proj"]["id"], mr["id"], True)
    assert merge_executor.execute(mr["id"], gh2)["state"] == "sent_back" and gh2.writes() == []
    # the bot passes; a pass asks GET /user once for several requests
    release_gates.requeue_merge(P(w["creator"]), w["proj"]["id"], mr["id"], True)
    other = _approved(w, branch="feat/y", sha=SHA2)
    ok = _gh_as("crewboss-bot")
    ok.heads = {"feat/x": SHA1, "feat/y": SHA2}
    assert merge_executor.run_once(ok) == 2
    assert [c for c in ok.calls if c[1] == "/user"] == [("GET", "/user", None)] and len(ok.writes()) == 2
    assert _row(mr["id"])["state"] == "merged" and _row(other["id"])["state"] == "merged"


def test_N6_the_protect_card_needs_an_enrolled_repo_for_this_project_and_a_fresh_factor(w, monkeypatch):
    from app.routers import releases_pages
    dana = _user("dana")
    repo.create_project("shadow", "Shadow", "internal", "owner", short_name="shadow", session_roles=[], owner_user_id=dana["id"])
    c = _client(dana)
    assert c.post("/shadow/details", data={"git_remote": f"git@github.com:{FULL}.git"}, headers=H, follow_redirects=False).status_code < 400
    gh = FakeGitHub(default="secret-trunk")
    monkeypatch.setattr(releases_pages, "github_client", lambda: gh)
    r = c.post("/shadow/releases/protect/plan", data={"mode": "merge-path"}, headers=H)       # a remote that is enrolled for ANOTHER project
    assert "secret-trunk" not in r.text and "crewboss-bot" not in r.text and gh.calls == []
    assert "not enrolled for this project" in r.text
    # a stale factor never reaches the card even for the right project
    r2 = _aged_client(w["creator"], 30).post("/web/releases/protect/plan", data={"mode": "merge-path"}, headers=H)
    assert r2.status_code == 403 and gh.calls == []


def test_N8_deny_list_forms_in_instance_yaml_and_the_environment(tmp_path):
    import os
    import subprocess
    import sys

    def run(yaml_text, env=None):
        home = tmp_path / "home"
        home.mkdir(exist_ok=True)
        (home / "instance.yaml").write_text(yaml_text)
        e = {k: v for k, v in os.environ.items() if not k.startswith("CREWBOSS_GITHUB")}
        e.update(CREWBOSS_HOME=str(home), **(env or {}))
        out = subprocess.run([sys.executable, "-c", "from app import config; print(sorted(config.GITHUB_DENY_LOGINS), config.GITHUB_BOT_LOGIN)"],
                             capture_output=True, text=True, env=e, cwd=os.getcwd())
        assert out.returncode == 0, out.stderr[-400:]
        return out.stdout.strip()

    assert run("merge_executor:\n  deny_logins: [Alice, bob]\n") == "['alice', 'bob']"
    assert run("merge_executor:\n  deny_logins: 'Alice, bob'\n") == "['alice', 'bob']"
    assert run("merge_executor:\n  bot_login: Crew-Bot\n") == "[] crew-bot"
    assert run("merge_executor:\n  deny_logins: [x]\n", {"CREWBOSS_GITHUB_DENY_LOGINS": "Carol, dave", "CREWBOSS_GITHUB_BOT_LOGIN": "B"}) \
        == "['carol', 'dave'] b"


def test_N9_regexes_refuse_a_trailing_newline_and_the_path_refuses_percent():
    from app import github_client
    assert not release_gates.BRANCH_RE.match("main\n") and not release_gates.SHA_RE.match("a" * 40 + "\n")
    assert not release_gates.REPO_RE.match("a/b\n") and not github_client._PATH.match("/user\n")
    assert release_gates.BRANCH_RE.match("feat/x-1.2") and github_client._PATH.match("/repos/a/b/branches/feat/x")
    assert not github_client._PATH.match("/repos/a/b/branches/%2e%2e/%2e%2e/c") and not github_client._PATH.match("/repos/a/b/x%2fy")
