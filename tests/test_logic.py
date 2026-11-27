"""The logic map, dashboard side (task #715): stored reports, the release gate, the one-release override, the release diff, the Logic pages
and their access rules, and POST /api/logic/report."""
import pytest

from app import deploys, logic, repo
from app.auth import issue_token
from app.db import get_cursor

from test_release_governance import P, SHA1, SHA2, SHA3, _client, _done, _user, local, w  # noqa: F401  (fixtures/helpers)


def _wf(wid="quote.won", steps=("save",)):
    return {"id": wid, "name": wid.title(), "summary": "s", "trigger": {"type": "button", "detail": "d", "control": "", "route": ""},
            "steps": [{"id": s, "kind": "action", "label": f"Step {s}", "detail": "", "data": [], "effects": []} for s in steps],
            "edges": [], "data": [], "covers": []}


def _u(key):
    return {"key": key, "kind": "route", "file": "app/a.py", "function": key, "name": f"POST /{key}", "line": 1}


def _report(workflows=None, uncovered=(), stale=(), errors=()):
    return {"version": 1, "workflows": workflows if workflows is not None else [_wf()], "errors": list(errors), "stale": list(stale),
            "uncovered": [_u(k) for k in uncovered], "discovered": len(uncovered)}


def _promotable(w, sha):
    """The commit has already succeeded on dev (production is promote-only)."""
    deploys.request_release(P(w["creator"]), w["proj"], w["dev"]["id"], sha)
    _done(sha, "dev", w["proj"])


def _enforce(w):
    logic.set_gate(P(w["creator"]), w["proj"], "enforce")
    w["proj"] = repo.get_project(w["proj"]["id"])


def _release(w, sha, who="creator", **kw):
    return deploys.request_release(P(w[who]), w["proj"], w["prod"]["id"], sha, **kw)


# --- reports --------------------------------------------------------------------------------------------------------------------

def test_a_report_is_checked_and_replaced_per_commit(w):
    pid = w["proj"]["id"]
    for bad in (None, {}, {"version": 2, "workflows": [], "errors": [], "stale": [], "uncovered": []},
                {"version": 1, "workflows": "x", "errors": [], "stale": [], "uncovered": []},
                {"version": 1, "workflows": [], "errors": [], "stale": [], "uncovered": [{"nokey": 1}]}):
        with pytest.raises(repo.BadRequest):
            logic.record_report(pid, SHA1, bad)
    with pytest.raises(repo.BadRequest, match="40-character"):
        logic.record_report(pid, "abc", _report())
    logic.record_report(pid, SHA1, _report(uncovered=["a"]), "job:1")
    logic.record_report(pid, SHA1, _report(uncovered=["a", "b"]), "job:2")           # same commit: replaced
    assert len(logic.get_report(pid, SHA1)["uncovered"]) == 2 and len(logic.list_reports(pid)) == 1
    assert logic.latest_report(pid)["sha"] == SHA1


# --- the gate -------------------------------------------------------------------------------------------------------------------

def test_gate_off_lets_production_through_with_no_report(w):
    _promotable(w, SHA1)
    assert _release(w, SHA1)["state"] == "requested"


def test_enforced_gate_refuses_with_no_report_errors_stale_or_new_uncovered(w):
    pid = w["proj"]["id"]
    _enforce(w)
    for sha in (SHA1, SHA2, SHA3):
        _promotable(w, sha)
    with pytest.raises(repo.BadRequest, match="no logic map report"):
        _release(w, SHA1)
    logic.record_report(pid, SHA1, _report(errors=[".crewboss/logic/x.yaml: not valid YAML at line 3"]))
    with pytest.raises(repo.BadRequest, match="schema check"):
        _release(w, SHA1)
    stale = [{"workflow": "quote.won", "step": "save", "file": "app/q.py", "function": "f", "why": "f is no longer in app/q.py"}]
    logic.record_report(pid, SHA2, _report(stale=stale))
    with pytest.raises(repo.BadRequest, match="point at code that is gone|points at code that is gone"):
        _release(w, SHA2)
    logic.record_report(pid, SHA3, _report())
    assert _release(w, SHA3)["state"] == "requested"                                   # a clean report passes


def test_the_first_report_is_the_starting_point_then_new_uncovered_entries_refuse(w):
    pid = w["proj"]["id"]
    _enforce(w)
    _promotable(w, SHA1)
    logic.record_report(pid, SHA1, _report(uncovered=["old1", "old2"]))
    row = _release(w, SHA1)                                                            # nothing released before: today's gaps are the baseline
    assert row["state"] == "requested"
    with get_cursor() as cur:                                                          # SHA1 reached production
        cur.execute("UPDATE server_jobs SET state = 'done', finished_at = now() WHERE id = %s", (row["id"],))
    _promotable(w, SHA2)
    logic.record_report(pid, SHA2, _report(uncovered=["old1", "old2", "brand-new"]))
    with pytest.raises(repo.BadRequest, match=r"1 new entry point.*POST /brand-new"):
        _release(w, SHA2)
    _promotable(w, SHA3)
    logic.record_report(pid, SHA3, _report(uncovered=["old1"]))                        # fewer gaps than the last release: fine
    assert _release(w, SHA3)["state"] == "requested"


def test_a_rollback_is_never_gated(w):
    _promotable(w, SHA1)
    first = _release(w, SHA1)
    with get_cursor() as cur:
        cur.execute("UPDATE server_jobs SET state = 'done', finished_at = now() WHERE id = %s", (first["id"],))
    _enforce(w)                                                                        # gate on, no report for anything
    row = deploys.request_release(P(w["creator"]), w["proj"], w["prod"]["id"], SHA1, rollback=True)
    assert row["rollback"] and row["state"] == "requested"


# --- the one-release override ----------------------------------------------------------------------------------------------------

def test_override_is_admin_only_needs_a_reason_and_is_used_up_by_one_release(w):
    pid = w["proj"]["id"]
    _enforce(w)
    _promotable(w, SHA1)
    with pytest.raises(repo.Forbidden):
        logic.grant_override(P(w["member"]), w["proj"], SHA1, "because I said so")
    with pytest.raises(repo.BadRequest, match="why"):
        logic.grant_override(P(w["creator"]), w["proj"], SHA1, "ok")
    with pytest.raises(repo.BadRequest, match="40-character"):
        logic.grant_override(P(w["creator"]), w["proj"], "xyz", "a good enough reason")
    ov = logic.grant_override(P(w["creator"]), w["proj"], SHA1, "urgent fix for the invoice run")
    assert ov["granted_by"] == w["creator"]["id"] and ov["granted_by_name"] == "cara" and ov["reason"] == "urgent fix for the invoice run"
    with pytest.raises(repo.Conflict, match="one release"):                            # one at a time
        logic.grant_override(P(w["creator"]), w["proj"], SHA2, "a second reason here")
    row = _release(w, SHA1)                                                            # no report at all, yet it goes
    assert row["state"] == "requested"
    used = logic.list_overrides(pid)[0]
    assert used["used_server_job_id"] == row["id"] and used["used_at"] is not None
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT summary FROM activity WHERE project_id = %s AND verb = 'overrode' ORDER BY id", (pid,))
        said = [r["summary"] for r in cur.fetchall()]
    assert any("Overrode the logic map gate" in s and "urgent fix" in s for s in said) and any("went past the logic map gate" in s for s in said)
    # NOT reusable: the same commit asked again after that request ended is refused again
    with get_cursor() as cur:
        cur.execute("UPDATE server_jobs SET state = 'refused' WHERE id = %s", (row["id"],))
    with pytest.raises(repo.BadRequest, match="logic map gate refuses"):
        _release(w, SHA1)
    # and a new override is possible once the first was used
    assert logic.grant_override(P(w["creator"]), w["proj"], SHA1, "second one, with a reason")["id"] != ov["id"]


def test_an_override_covers_only_its_own_commit_and_can_be_withdrawn(w):
    _enforce(w)
    _promotable(w, SHA1)
    _promotable(w, SHA2)
    ov = logic.grant_override(P(w["creator"]), w["proj"], SHA1, "only for the first commit")
    with pytest.raises(repo.BadRequest, match="logic map gate refuses"):
        _release(w, SHA2)
    logic.revoke_override(P(w["creator"]), w["proj"], ov["id"])
    with pytest.raises(repo.BadRequest, match="logic map gate refuses"):
        _release(w, SHA1)
    with pytest.raises(repo.NotFound):
        logic.revoke_override(P(w["creator"]), w["proj"], ov["id"])


def test_gate_switch_is_admin_only(w):
    with pytest.raises(repo.Forbidden):
        logic.set_gate(P(w["member"]), w["proj"], "enforce")
    with pytest.raises(repo.BadRequest):
        logic.set_gate(P(w["creator"]), w["proj"], "sometimes")


# --- the release diff -----------------------------------------------------------------------------------------------------------

def test_release_diff_compares_with_the_last_release_and_the_first_report_shows_everything_added(w):
    pid = w["proj"]["id"]
    assert logic.release_diff(pid, SHA1) is None
    logic.record_report(pid, SHA1, _report([_wf("a", ("x",))]))
    first = logic.release_diff(pid, SHA1)
    assert first["first"] and [x["id"] for x in first["diff"]["added"]] == ["a"]
    with get_cursor() as cur:        # SHA1 went to production
        cur.execute("INSERT INTO server_jobs (account_id, project_id, target_name, sha, state, env, finished_at) VALUES "
                    "(%s, %s, 'live', %s, 'done', 'prod', now())", (w["acct"]["id"], pid, SHA1))
    logic.record_report(pid, SHA2, _report([_wf("a", ("x", "y")), _wf("b")]))
    logic.record_report(pid, SHA3, _report([_wf("a", ("x",))]))                        # a later, unreleased report does not become the baseline
    d = logic.release_diff(pid, SHA2)
    assert d["baseline_sha"] == SHA1 and not d["first"]
    assert [x["id"] for x in d["diff"]["added"]] == ["b"]
    assert d["diff"]["changed"][0]["changes"] == [{"what": "step added", "step": "y", "detail": "Step y"}]


def test_step_exists_for_fault_links(w):
    pid = w["proj"]["id"]
    assert not logic.step_exists(pid, "quote.won", "save")
    logic.record_report(pid, SHA1, _report())
    assert logic.step_exists(pid, "quote.won", "save") and not logic.step_exists(pid, "quote.won", "nope")


# --- the pages ------------------------------------------------------------------------------------------------------------------

def test_logic_pages_show_the_map_to_anyone_who_can_see_the_project_and_nobody_else(w):
    pid = w["proj"]["id"]
    logic.record_report(pid, SHA1, _report([_wf("quote.won", ("save", "note"))], uncovered=["fresh"]))
    slug = w["proj"]["slug"]
    stranger = _user("sam")
    for who, status in (("creator", 200), ("member", 200), ("viewer", 200)):
        c = _client(w[who])
        r = c.get(f"/{slug}/logic")
        assert r.status_code == status and "data-logic-canvas" in r.text and 'data-uncovered="fresh"' in r.text, who
        assert c.get(f"/{slug}/logic/diff?sha={SHA1}").status_code == 200
    assert _client(stranger).get(f"/{slug}/logic").status_code == 404                  # same 404 as a missing project
    assert _client(stranger).get(f"/{slug}/logic/diff").status_code == 404
    page = _client(w["creator"]).get(f"/{slug}/logic?wf=quote.won&step=note").text
    assert 'data-step-panel="note"' in page and 'class="lg-node lg-action is-sel"' in page
    assert "data-step-missing" in _client(w["creator"]).get(f"/{slug}/logic?wf=quote.won&step=ghost").text


def test_the_page_with_no_report_says_how_to_make_one(w):
    r = _client(w["creator"]).get(f"/{w['proj']['slug']}/logic")
    assert r.status_code == 200 and "data-no-report" in r.text and "crewboss logic" in r.text


def test_gate_and_override_forms_need_an_admin_and_a_fresh_tap(w):
    slug = w["proj"]["slug"]
    logic.record_report(w["proj"]["id"], SHA1, _report(errors=["x: bad"]))
    H = {"Origin": "https://crewboss.example.com"}
    r = _client(w["member"]).post(f"/{slug}/logic/gate", data={"mode": "enforce"}, headers=H)
    assert r.status_code == 403 and repo.get_project(w["proj"]["id"])["logic_gate"] == "off"
    r = _client(w["creator"], fresh=False).post(f"/{slug}/logic/gate", data={"mode": "enforce"}, headers=H)
    assert r.status_code == 403 and "data-stepup-page" in r.text
    c = _client(w["creator"])
    assert c.post(f"/{slug}/logic/gate", data={"mode": "enforce"}, headers=H, follow_redirects=False).status_code == 303
    assert repo.get_project(w["proj"]["id"])["logic_gate"] == "enforce"
    page = c.get(f"/{slug}/logic").text
    assert 'data-verdict="fail"' in page and "data-override-form" in page
    r = c.post(f"/{slug}/logic/override", data={"sha": SHA1, "reason": "x"}, headers=H)
    assert r.status_code == 400 and "why" in r.text
    r = c.post(f"/{slug}/logic/override", data={"sha": SHA1, "reason": "release is urgent, owner agreed"}, headers=H, follow_redirects=False)
    assert r.status_code == 303 and "data-open-override" in c.get(f"/{slug}/logic").text
    assert "data-override-form" not in _client(w["member"]).get(f"/{slug}/logic").text      # a member sees the verdict, not the form


def test_the_release_page_links_each_commits_logic_changes(w):
    pid = w["proj"]["id"]
    logic.record_report(pid, SHA1, _report())
    _promotable(w, SHA1)
    r = _client(w["creator"]).get(f"/{w['proj']['slug']}/releases")
    assert "data-logic-chip" in r.text and f"/logic/diff?sha={SHA1}" in r.text


def test_the_diff_page_lists_added_removed_and_changed(w):
    pid = w["proj"]["id"]
    logic.record_report(pid, SHA1, _report([_wf("a", ("x",)), _wf("gone")]))
    with get_cursor() as cur:
        cur.execute("INSERT INTO server_jobs (account_id, project_id, target_name, sha, state, env, finished_at) VALUES "
                    "(%s, %s, 'live', %s, 'done', 'prod', now())", (w["acct"]["id"], pid, SHA1))
    logic.record_report(pid, SHA2, _report([_wf("a", ("x", "y")), _wf("fresh")]))
    page = _client(w["creator"]).get(f"/{w['proj']['slug']}/logic/diff?sha={SHA2}").text
    assert 'data-added="fresh"' in page and 'data-removed="gone"' in page and 'data-changed="a"' in page and "step added" in page


# --- the API --------------------------------------------------------------------------------------------------------------------

def test_api_report_needs_a_scanner_or_session_token_and_a_valid_body(client, project):
    url = f"/api/logic/report?sha={SHA1}&project={project['slug']}"
    assert client.post(url, json=_report()).status_code in (401, 403)
    assert client.post(url, json=_report(), headers={"Authorization": "Bearer nope"}).status_code in (401, 403)
    zab = issue_token("zabbix", "z")
    assert client.post(url, json=_report(), headers={"Authorization": f"Bearer {zab}"}).status_code == 403
    tok = issue_token("scanner", "ci")
    h = {"Authorization": f"Bearer {tok}"}
    ok = client.post(url, json=_report(uncovered=["a"]), headers=h)
    assert ok.status_code == 201 and ok.json()["uncovered"] == 1 and logic.get_report(project["id"], SHA1)
    assert client.post(f"/api/logic/report?sha=zz&project={project['slug']}", json=_report(), headers=h).status_code == 400
    assert client.post(url, json={"version": 9}, headers=h).status_code == 400
    assert client.post(f"/api/logic/report?sha={SHA1}&project=nope", json=_report(), headers=h).status_code == 404


def test_logic_routes_are_slug_scoped_or_token_scoped():
    from test_isolation import _route_table
    table = _route_table()
    for m, p in (("GET", "/{slug}/logic"), ("GET", "/{slug}/logic/diff"), ("POST", "/{slug}/logic/gate"), ("POST", "/{slug}/logic/override"),
                 ("POST", "/{slug}/logic/override/{override_id:int}/revoke")):
        assert "slug" in table[(m, p)], (m, p)                       # behind project_from_path: a hidden project is the same 404
    assert "token" in table[("POST", "/api/logic/report")]            # a scanner or session token, checked
    from app.routers import pages
    assert all(pages.slug_policy(p) == "project" for p in ("/{slug}/logic", "/{slug}/logic/gate"))
