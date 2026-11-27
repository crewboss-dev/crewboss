"""Task #129: dash, the OVERSEER, reads every active project, keeps ONE global plan (Now <= 3 / Next / Later + today,
a reason per item) in its own table, suggests priority changes elsewhere, and never edits another project's tasks.
The owner pins/drags to override; project sessions get their AI work in plan order; changes nudge a re-plan."""
from datetime import datetime, timezone

import pytest

from app import config, mcp_server, plan, repo
from app.auth import issue_token
from app.db import get_cursor
from test_isolation import WF, _b_snapshot, _mcp_cases, ab  # noqa: F401  (ab is a fixture)

OWN = {"Origin": "https://crewboss.example.com", "Sec-Fetch-Site": "same-origin"}
READS = {"list_tasks", "get_task", "search_knowledge", "get_knowledge", "recent_activity", "list_workflows",
         "get_workflow", "list_objectives", "list_changes", "list_materials", "get_material", "list_milestones"}


@pytest.fixture
def overseer_a(ab, monkeypatch, mcp_caller):  # noqa: F811
    """Project A is the overseer project; the caller is A's 'all' session (dash)."""
    monkeypatch.setattr(config, "OVERSEER_PROJECT", "proj-a")
    tok = issue_token("all", "dash", project_id=ab["a"]["id"])
    mcp_caller(tok)
    return {**ab, "token": tok}


# --- 1. the overseer's reads and refused writes ----------------------------------------------------------------

def test_overseer_reads_every_active_project(overseer_a):
    b, bt, bk = overseer_a["b"], overseer_a["bt"], overseer_a["bk"]
    assert [t["title"] for t in mcp_server.list_tasks(project="proj-b")] == ["B secret task"]
    assert mcp_server.get_task(bt["id"])["title"] == "B secret task"
    assert mcp_server.get_knowledge(bk["id"])["title"] == "B decision"
    assert [k["title"] for k in mcp_server.search_knowledge("B", project="proj-b")] == ["B decision"]
    assert mcp_server.recent_activity(project="proj-b")
    assert mcp_server.list_workflows(project="proj-b")
    assert mcp_server.get_workflow(WF, project="proj-b")["id"] == WF
    assert mcp_server.list_objectives(project="proj-b") == []
    # its own project is still the default
    assert [t["title"] for t in mcp_server.list_tasks()] == ["A task"]
    # an archived project is invisible again
    repo.set_project_archived(b["id"], True, "owner")
    with pytest.raises(repo.NotFound):
        mcp_server.get_task(bt["id"])
    with pytest.raises(repo.NotFound):
        mcp_server.list_tasks(project="proj-b")


def test_overseer_writes_stay_its_own_except_suggestions(overseer_a):
    cases = _mcp_cases(overseer_a)
    before = _b_snapshot(overseer_a)
    for name, kwargs in cases.items():
        if name in READS or name in ("suggest_triage", "comment_task", "add_task", "send_handoff", "get_handoff", "reply_handoff",
                                      "close_handoff", "put_doc", "get_doc", "doc_diff"):   # handoffs/docs: the overseer gets PermissionError (#908)
            continue
        with pytest.raises(repo.NotFound):
            getattr(mcp_server, name)(**kwargs)
    assert _b_snapshot(overseer_a) == before
    # the one cross-project write: a triage suggestion the owner accepts or keeps
    row = mcp_server.suggest_triage(overseer_a["bt"]["id"], rationale="ship first", priority="should")
    assert row["triage_state"] == "suggested" and row["priority"] == "must"  # nothing changed until the owner says
    assert row["suggestion"]["created_by"] == "claude:overseer"
    # ...and comments: dash communicates with any session (owner, 2026-10-13); tests in test_overseer_messages.py


def test_only_the_overseer_projects_all_session_gets_it(ab, monkeypatch, mcp_caller):  # noqa: F811
    monkeypatch.setattr(config, "OVERSEER_PROJECT", "proj-a")
    mcp_caller(issue_token("bugs", "a-bugs", project_id=ab["a"]["id"]))
    with pytest.raises(repo.NotFound):
        mcp_server.get_task(ab["bt"]["id"])
    mcp_caller(issue_token("all", "b-all", project_id=ab["b"]["id"]))
    with pytest.raises(repo.NotFound):
        mcp_server.get_task(ab["at"]["id"])
    with pytest.raises(PermissionError):
        mcp_server.get_global_view()


def test_overseer_rest_reads_cross_project_but_writes_404(client, overseer_a):
    h = {"Authorization": f"Bearer {overseer_a['token']}"}
    assert [t["title"] for t in client.get("/api/tasks?project=proj-b", headers=h).json()] == ["B secret task"]
    assert client.get(f"/api/tasks/{overseer_a['bt']['id']}", headers=h).status_code == 200
    r = client.post("/api/tasks", json={"title": "x", "stream": "bug", "project": "proj-b"}, headers=h)
    assert r.status_code == 404
    r = client.patch(f"/api/tasks/{overseer_a['bt']['id']}", json={"title": "hijack"}, headers=h)
    assert r.status_code == 404


# --- 2/3. the plan model, scoring and the planner's write -------------------------------------------------------

def _t(pid, title, priority="should", stream="feature", assignee="ai", status="ready"):
    return repo.create_task(pid, title, "", priority, stream, [], {}, "claude:features", assignee=assignee,
                            status=status)


def test_score_tasks_default_order_and_factors(overseer_a):
    a, b = overseer_a["a"], overseer_a["b"]
    could = _t(a["id"], "could thing", "could")
    must = _t(b["id"], "must thing", "must")
    review = _t(a["id"], "check me", "should", assignee="owner", status="review")
    fault = _t(b["id"], "prod down", "should", stream="error")
    view = mcp_server.get_global_view()
    order = [s["task_id"] for s in view["default_order"]]
    assert order.index(fault["id"]) < order.index(review["id"]) < order.index(must["id"]) < order.index(could["id"])
    top = view["default_order"][0]
    assert top["task_id"] == fault["id"] and "production fault (error stream)" in top["factors"]
    assert [s["suggested_bucket"] for s in view["default_order"][:4]] == ["now", "now", "now", "next"]
    assert {t["project"] for t in view["tasks"]} == {"proj-a", "proj-b"}
    assert "health_open" in view and "objectives" in view and view["owner_wip"]["limit"] == 3


def test_set_global_plan_rules(overseer_a):
    a, b = overseer_a["a"], overseer_a["b"]
    t1, t2, t3, t4 = (_t(a["id"], "one"), _t(b["id"], "two"), _t(a["id"], "three"), _t(b["id"], "four"))
    items = [{"task_id": t1["id"], "bucket": "now", "reason": "  unblocks   invoicing  "},
             {"task_id": t2["id"], "bucket": "now", "reason": "client waiting"},
             {"task_id": t3["id"], "bucket": "next", "reason": "after the first two"},
             {"task_id": t4["id"], "bucket": "later", "reason": ""}]
    r = mcp_server.set_global_plan(items=items, today=[t1["id"]], today_note="Invoicing first")
    assert r["ok"] and r["items"] == 4 and r["now"] == 2
    home = plan.plan_for_home()
    assert [x["id"] for x in home["now"]] == [t1["id"], t2["id"]]
    assert home["now"][0]["reason"] == "unblocks invoicing"
    assert [x["id"] for x in home["today"]] == [t1["id"]] and home["note"] == "Invoicing first"
    # Now <= 3
    four_now = [{"task_id": t["id"], "bucket": "now", "reason": "x"} for t in (t1, t2, t3, t4)]
    with pytest.raises(repo.BadRequest):
        mcp_server.set_global_plan(items=four_now)
    # today must be in Now/Next; done tasks and bad buckets refused
    with pytest.raises(repo.BadRequest):
        mcp_server.set_global_plan(items=items, today=[t4["id"]])
    with pytest.raises(repo.BadRequest):
        mcp_server.set_global_plan(items=[{"task_id": t1["id"], "bucket": "soon"}])
    repo.update_task(t4["id"], {"status": "done"}, "owner")
    with pytest.raises(repo.BadRequest):
        mcp_server.set_global_plan(items=[{"task_id": t4["id"], "bucket": "later"}])
    # an owner pin is fixed: the planner can't move it, and a re-plan keeps it
    plan.owner_pin(t3["id"], True, "owner")
    with pytest.raises(repo.Conflict):
        mcp_server.set_global_plan(items=[{"task_id": t3["id"], "bucket": "now", "reason": "x"}])
    mcp_server.set_global_plan(items=[{"task_id": t1["id"], "bucket": "now", "reason": "only"}])
    home = plan.plan_for_home()
    assert [x["id"] for x in home["now"]] == [t1["id"]] and [x["id"] for x in home["next"]] == [t3["id"]]
    assert home["next"][0]["pinned"]
    # pinned Now items count toward the limit
    plan.owner_move(t2["id"], "now", None, "owner")
    plan.owner_move(t3["id"], "now", None, "owner")  # Now: t3, t2 (both pinned), t1
    with pytest.raises(repo.BadRequest):
        mcp_server.set_global_plan(items=[{"task_id": t1["id"], "bucket": "now"},
                                          {"task_id": _t(a["id"], "five")["id"], "bucket": "now"}])


def test_owner_overrides_are_owner_only_and_respect_now_limit(overseer_a):
    a = overseer_a["a"]
    ts = [_t(a["id"], f"t{i}") for i in range(4)]
    with pytest.raises(repo.Forbidden):
        plan.owner_move(ts[0]["id"], "now", None, "claude:all")
    for t in ts[:3]:
        plan.owner_move(t["id"], "now", None, "owner")
    with pytest.raises(repo.Conflict):
        plan.owner_move(ts[3]["id"], "now", None, "owner")
    plan.owner_move(ts[2]["id"], "now", ts[0]["id"], "owner")  # reorder within Now is fine
    home = plan.plan_for_home()
    assert [x["id"] for x in home["now"]] == [ts[1]["id"], ts[0]["id"], ts[2]["id"]]
    assert all(x["pinned"] for x in home["now"])


# --- 4. the global home panel -------------------------------------------------------------------------------------

def test_home_shows_the_plan_first_and_owner_can_pin_and_drag(client, overseer_a):
    a, b = overseer_a["a"], overseer_a["b"]
    t1, t2 = _t(a["id"], "Ship invoicing fix"), _t(b["id"], "Answer client email")
    html = client.get("/").text
    assert "No plan yet" in html
    mcp_server.set_global_plan(items=[{"task_id": t1["id"], "bucket": "now", "reason": "money waiting <b>x</b>"},
                                      {"task_id": t2["id"], "bucket": "next", "reason": "reply today"}],
                               today=[t1["id"]], today_note="Fix first")
    html = client.get("/").text
    assert html.index('id="global-plan"') < html.index('id="board"')  # the plan sits above Focus
    assert "money waiting &lt;b&gt;x&lt;/b&gt;" in html and 'data-plan-col="now"' in html
    assert "planned just now by dash" in html and "Fix first" in html
    r = client.post(f"/all/plan/{t2['id']}/pin", data={"pinned": "1"}, headers={**OWN, "HX-Request": "true"})
    assert r.status_code == 200 and 'aria-pressed="true"' in r.text
    r = client.post("/all/plan/move", data={"task_id": t2["id"], "bucket": "now", "after_id": str(t1["id"])},
                    headers={**OWN, "HX-Request": "true"})
    assert r.status_code == 200
    assert [x["id"] for x in plan.plan_for_home()["now"]] == [t1["id"], t2["id"]]
    # the same-origin guard: a cross-site post is refused
    r = client.post(f"/all/plan/{t2['id']}/pin", data={"pinned": "0"},
                    headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    over = {"Authorization": f"Bearer {overseer_a['token']}"}
    assert client.get("/api/plan", headers=over).json()["now"][0]["id"] == t1["id"]


def test_home_without_an_overseer_still_renders(client, project):
    assert client.get("/").status_code == 200


# --- 5. sessions get their AI work in plan order; dash's briefing shows the plan ---------------------------------

def test_ground_lists_ai_work_in_plan_order(overseer_a):
    b = overseer_a["b"]
    first, second = _t(b["id"], "later-made"), _t(b["id"], "earlier-made")
    mcp_server.set_global_plan(items=[{"task_id": second["id"], "bucket": "now", "reason": "x"},
                                      {"task_id": first["id"], "bucket": "next", "reason": "y"}])
    g = repo.build_ground("all", session_project=b)
    ids = [t["id"] for t in g["assigned_to_ai"]]
    assert ids.index(second["id"]) < ids.index(first["id"])
    text = repo.render_ground_text(g)
    assert "in the GLOBAL PLAN's order" in text and f"#{second['id']}" in text and "plan: now" in text
    assert g["global_plan"] is None  # only the overseer's own briefing carries the plan status
    mine = repo.build_ground("all", session_project=overseer_a["a"])
    assert mine["global_plan"]["now"] == 1 and "GLOBAL PLAN (you are the overseer" in repo.render_ground_text(mine)


# --- 6. re-plan nudges ---------------------------------------------------------------------------------------------

def _stale_rows(overseer_pid):
    return [o for o in repo.list_outbox("all", undelivered_only=True, project_id=overseer_pid)
            if o["kind"] == "plan_stale"]


def test_changes_nudge_dash_once_until_delivered(overseer_a, monkeypatch):
    monkeypatch.setattr(config, "PLAN_STALE_COOLDOWN_MINUTES", 0)
    a, b = overseer_a["a"], overseer_a["b"]
    t = repo.create_task(b["id"], "new B work", "", "should", "feature", [], {}, "owner")
    rows = _stale_rows(a["id"])
    assert len(rows) == 1 and f"task #{t['id']} in proj-b (created)" in rows[0]["message"]
    repo.update_task(t["id"], {"status": "doing"}, "owner")
    repo.update_task(t["id"], {"priority": "must"}, "owner")
    rows = _stale_rows(a["id"])
    assert len(rows) == 1 and "(priority)" in rows[0]["message"]  # coalesced, latest reason
    # a title-only edit doesn't matter to the plan
    with get_cursor() as cur:
        cur.execute("UPDATE outbox SET delivered_at = now() WHERE kind = 'plan_stale'")
    repo.update_task(t["id"], {"title": "renamed"}, "owner")
    assert _stale_rows(a["id"]) == []
    # questions nudge too; dash's own edits to its own project don't
    repo.ask_owner(t["id"], "Which first?", "Context long enough to answer cold.", None, "claude:all")
    assert len(_stale_rows(a["id"])) == 1
    with get_cursor() as cur:
        cur.execute("UPDATE outbox SET delivered_at = now() WHERE kind = 'plan_stale'")
    repo.create_task(a["id"], "dash's own", "", "should", "feature", [], {}, "claude:all")
    assert _stale_rows(a["id"]) == []


def test_morning_replan_fires_once_a_day(overseer_a):
    a = overseer_a["a"]
    with get_cursor() as cur:
        cur.execute("UPDATE global_plan_state SET last_morning_date = NULL")
    early = datetime(2026, 10, 2, 18, 0, tzinfo=timezone.utc)   # 04:00 Sydney
    late = datetime(2026, 10, 2, 21, 0, tzinfo=timezone.utc)    # 07:00 Sydney
    assert plan.maybe_morning_replan(early) is False
    assert plan.maybe_morning_replan(late) is True
    assert plan.maybe_morning_replan(late) is False
    rows = _stale_rows(a["id"])
    assert len(rows) == 1 and "morning re-plan" in rows[0]["message"]


def test_no_overseer_no_nudges(project):
    repo.create_task(project["id"], "x", "", "should", "feature", [], {}, "owner")
    assert [o for o in repo.list_outbox("features", undelivered_only=True) if o["kind"] == "plan_stale"] == []


# --- audit fixes (2026-10-01) ------------------------------------------------------------------------------------

def test_api_plan_is_owner_or_overseer_only(client, overseer_a):  # 1
    # task #368: no token is a 401 (the owner sees the plan on the global home, never through /api)
    assert client.get("/api/plan").status_code == 401
    assert client.get("/api/plan", headers={"Authorization": f"Bearer {overseer_a['token']}"}).status_code == 200
    other = issue_token("all", "b-all", project_id=overseer_a["b"]["id"])
    assert client.get("/api/plan", headers={"Authorization": f"Bearer {other}"}).status_code == 404
    scanner = issue_token("scanner", "s")
    assert client.get("/api/plan", headers={"Authorization": f"Bearer {scanner}"}).status_code == 403


def test_dead_rows_never_block_a_now_slot(overseer_a):  # 2 (+10: Won't refused)
    a, b = overseer_a["a"], overseer_a["b"]
    done, wont, gone = _t(a["id"], "done soon"), _t(a["id"], "wont soon"), _t(b["id"], "archived soon")
    for t in (done, wont, gone):
        plan.owner_move(t["id"], "now", None, "owner")
    repo.update_task(done["id"], {"status": "done"}, "owner")
    repo.update_task(wont["id"], {"priority": "wont"}, "owner")
    repo.set_project_archived(b["id"], True, "owner")
    assert plan.plan_for_home()["now"] == []
    live = [_t(a["id"], f"live {i}") for i in range(3)]
    for t in live:  # the owner can fill all three slots: the invisible pinned cards don't count
        plan.owner_move(t["id"], "now", None, "owner")
    with get_cursor() as cur:
        cur.execute("DELETE FROM global_plan WHERE task_id = ANY(%s)", ([t["id"] for t in live],))
    r = mcp_server.set_global_plan(items=[{"task_id": t["id"], "bucket": "now", "reason": "x"} for t in live])
    assert r["now"] == 3 and r["pinned_kept"] == 0
    with get_cursor() as cur:  # set_plan removed the dead rows, pinned included
        cur.execute("SELECT task_id FROM global_plan WHERE task_id = ANY(%s)", ([done["id"], wont["id"], gone["id"]],))
        assert cur.fetchall() == []
    with pytest.raises(repo.BadRequest):  # a Won't task can't be planned
        mcp_server.set_global_plan(items=[{"task_id": wont["id"], "bucket": "later"}])


def test_odd_link_shapes_never_break_the_view(overseer_a):  # 3
    import json

    a = overseer_a["a"]
    odd = [{"knowledge": [{"id": 1}]}, {"knowledge": 7}, {"tasks": 7}, {"tasks": ["x", None, True, "12"]},
           {"knowledge": "5"}, {"tasks": {"a": 1}}, [1, 2]]
    for i, links in enumerate(odd):
        t = _t(a["id"], f"odd {i}")
        with get_cursor() as cur:
            cur.execute("UPDATE tasks SET links = %s::jsonb WHERE id = %s", (json.dumps(links), t["id"]))
    view = mcp_server.get_global_view()
    assert len(view["default_order"]) == len(view["tasks"]) >= len(odd)


def test_overseer_suggestion_is_recorded_as_overseer_and_never_overwrites(client, overseer_a):  # 4
    bt = overseer_a["bt"]
    repo.suggest_triage(bt["id"], "claude:all", priority="could", rationale="B's own session thinks so")
    with pytest.raises(ValueError, match="already has a pending suggestion"):
        mcp_server.suggest_triage(bt["id"], rationale="dash disagrees", priority="should")
    assert repo.get_task(bt["id"])["suggestion"]["created_by"] == "claude:all"
    with get_cursor() as cur:  # the owner settles B's suggestion
        cur.execute("UPDATE tasks SET triage_state = 'overridden' WHERE id = %s", (bt["id"],))
    with pytest.raises(repo.BadRequest):  # bounded: it lands in that project's activity feed
        mcp_server.suggest_triage(bt["id"], rationale="x" * 2001)
    mcp_server.suggest_triage(bt["id"], rationale="ship first", priority="should")
    html = client.get(f"/proj-b/tasks/{bt['id']}/panel").text
    assert "dash (overseer) suggests" in html and "dash (overseer)</span>" in html
    assert "claude:overseer" not in html


def test_replan_cooldown_and_one_follow_up(overseer_a, monkeypatch):  # 5
    monkeypatch.setattr(config, "PLAN_STALE_COOLDOWN_MINUTES", 10)
    a, b = overseer_a["a"], overseer_a["b"]
    t = _t(b["id"], "first")
    mcp_server.set_global_plan(items=[{"task_id": t["id"], "bucket": "now", "reason": "x"}])
    with get_cursor() as cur:
        cur.execute("UPDATE outbox SET delivered_at = now() WHERE kind = 'plan_stale'")
    repo.update_task(t["id"], {"status": "doing"}, "owner")   # inside the cooldown: remembered, not sent
    repo.create_task(b["id"], "second", "", "should", "feature", [], {}, "owner")
    assert _stale_rows(a["id"]) == []
    assert plan.flush_pending_nudge() is False                 # still in the cooldown
    with get_cursor() as cur:                                  # the cooldown passes
        cur.execute("UPDATE global_plan_runs SET planned_at = now() - interval '11 minutes'")
        cur.execute("UPDATE outbox SET created_at = now() - interval '11 minutes' WHERE kind = 'plan_stale'")
        cur.execute("UPDATE global_plan_state SET pending_since = now() - interval '5 minutes'")
    assert plan.flush_pending_nudge() is True
    rows = _stale_rows(a["id"])
    assert len(rows) == 1 and "(created)" in rows[0]["message"]
    assert plan.flush_pending_nudge() is False                 # exactly one follow-up
    # a plan made AFTER the pending changes already saw them: the mark is cleared, no nudge
    with get_cursor() as cur:
        cur.execute("UPDATE outbox SET delivered_at = now(), created_at = now() - interval '20 minutes' "
                    "WHERE kind = 'plan_stale'")
        cur.execute("UPDATE global_plan_runs SET planned_at = now() - interval '20 minutes'")
        cur.execute("UPDATE global_plan_state SET pending_since = now() - interval '1 minute', pending_why = 'x'")
    mcp_server.get_global_view()   # dash LOOKS after the change, then writes: that plan has seen it
    mcp_server.set_global_plan(items=[{"task_id": t["id"], "bucket": "now", "reason": "x"}])
    assert plan.flush_pending_nudge() is False
    with get_cursor() as cur:
        cur.execute("SELECT pending_since FROM global_plan_state")
        assert cur.fetchone()["pending_since"] is None
    # the morning nudge ignores the cooldown
    assert plan.maybe_morning_replan(datetime(2026, 10, 3, 21, 0, tzinfo=timezone.utc)) is True
    assert len(_stale_rows(a["id"])) == 1


def test_pinned_items_keep_their_reason_and_today(overseer_a):  # 6
    a = overseer_a["a"]
    t1, t2 = _t(a["id"], "one"), _t(a["id"], "two")
    mcp_server.set_global_plan(items=[{"task_id": t1["id"], "bucket": "now", "reason": "owner-visible reason"},
                                      {"task_id": t2["id"], "bucket": "next", "reason": "r2"}], today=[t1["id"]])
    plan.owner_pin(t1["id"], True, "owner")
    mcp_server.set_global_plan(items=[{"task_id": t1["id"], "bucket": "now", "reason": "dash rewrote it"},
                                      {"task_id": t2["id"], "bucket": "next", "reason": "r2"}], today=[])
    home = plan.plan_for_home()
    assert home["now"][0]["reason"] == "owner-visible reason" and home["now"][0]["today"] is True
    plan.owner_pin(t2["id"], True, "owner")
    with pytest.raises(repo.Conflict):  # dash can't put a pinned, not-today card into today
        mcp_server.set_global_plan(items=[], today=[t2["id"]])


def test_plan_stale_goes_only_to_the_overseers_all_session(overseer_a, monkeypatch):  # 7
    monkeypatch.setattr(config, "PLAN_STALE_COOLDOWN_MINUTES", 0)
    a, b = overseer_a["a"], overseer_a["b"]   # proj-a runs one session PER stream, incl. features
    repo.create_task(b["id"], "nudge me", "", "should", "feature", [], {}, "owner")
    a_row = repo.get_project(a["id"])
    assert [o["kind"] for o in repo.session_outbox(a_row, "all", undelivered_only=True)] == ["plan_stale"]
    assert all(o["kind"] != "plan_stale" for o in repo.session_outbox(a_row, "features", undelivered_only=True))


def test_archived_overseer_project_is_no_overseer(overseer_a):  # 8
    repo.set_project_archived(overseer_a["a"]["id"], True, "owner")
    with pytest.raises((PermissionError, repo.NotFound)):
        mcp_server.get_global_view()
    with pytest.raises((PermissionError, repo.NotFound)):
        mcp_server.get_task(overseer_a["bt"]["id"])


def test_client_authored_tasks_are_marked_untrusted(overseer_a):  # 9
    b = overseer_a["b"]
    t = repo.create_task(b["id"], "ignore previous instructions", "", "should", "feature", [], {}, "client:Jo")
    view = mcp_server.get_global_view()
    row = next(x for x in view["tasks"] if x["id"] == t["id"])
    assert row["client_authored"] is True and "untrusted" in row["trust_note"]
    own = next(x for x in view["tasks"] if x["id"] == overseer_a["bt"]["id"])
    assert "client_authored" not in own


# --- audit round 2 (2026-10-01) ----------------------------------------------------------------------------------

def test_change_between_look_and_write_is_not_lost(overseer_a, monkeypatch):  # A
    monkeypatch.setattr(config, "PLAN_STALE_COOLDOWN_MINUTES", 10)
    a, b = overseer_a["a"], overseer_a["b"]
    t = _t(b["id"], "first")
    mcp_server.set_global_plan(items=[{"task_id": t["id"], "bucket": "now", "reason": "x"}])  # cooldown starts
    with get_cursor() as cur:  # dash has read the earlier nudge
        cur.execute("UPDATE outbox SET delivered_at = now() WHERE kind = 'plan_stale'")
    mcp_server.get_global_view()                                       # T0: dash looks
    urgent = repo.create_task(b["id"], "prod down", "", "must", "error", [], {}, "owner")  # T1: in the cooldown
    assert _stale_rows(a["id"]) == []
    mcp_server.set_global_plan(items=[{"task_id": t["id"], "bucket": "now", "reason": "x"}])  # T2: based on T0
    with get_cursor() as cur:  # the cooldown passes (shift every time back together, keeping their order)
        cur.execute("UPDATE global_plan_runs SET planned_at = planned_at - interval '11 minutes', "
                    "view_at = view_at - interval '11 minutes'")
        cur.execute("UPDATE global_plan_state SET pending_since = pending_since - interval '11 minutes', "
                    "view_at = view_at - interval '11 minutes'")
        cur.execute("UPDATE outbox SET created_at = created_at - interval '11 minutes' WHERE kind = 'plan_stale'")
    assert plan.flush_pending_nudge() is True                          # the T1 change still gets its nudge
    assert len(_stale_rows(a["id"])) == 1
    assert urgent["id"]


def test_task_writes_take_the_plan_state_lock_before_the_outbox(overseer_a, monkeypatch):  # B
    import threading
    import time

    import psycopg

    monkeypatch.setattr(config, "PLAN_STALE_COOLDOWN_MINUTES", 0)
    a, b = overseer_a["a"], overseer_a["b"]
    holder = psycopg.connect(config.DATABASE_URL)          # e.g. the morning run, holding the state row
    watcher = psycopg.connect(config.DATABASE_URL, autocommit=True)
    try:
        holder.execute("SELECT 1 FROM global_plan_state WHERE id FOR UPDATE")
        th = threading.Thread(target=lambda: repo.create_task(b["id"], "w", "", "should", "feature", [], {},
                                                               "claude:features"))
        th.start()
        pid = None
        for _ in range(100):
            row = watcher.execute("SELECT pid FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                                  "AND datname = current_database() AND pid <> %s", (holder.info.backend_pid,)).fetchone()
            if row:
                pid = row[0]
                break
            time.sleep(0.05)
        assert pid, "the task write should wait on the plan-state lock"
        modes = {r[0] for r in watcher.execute("SELECT mode FROM pg_locks WHERE pid = %s "
                                                "AND relation = 'outbox'::regclass", (pid,)).fetchall()}
        assert "RowExclusiveLock" not in modes  # it hasn't written the outbox while waiting: one lock order
        holder.rollback()
        th.join(10)
        assert not th.is_alive()
    finally:
        holder.close()
        watcher.close()
    assert len(_stale_rows(a["id"])) == 1


def test_owner_today_toggle_and_morning_expiry(client, overseer_a):  # C
    a = overseer_a["a"]
    t1, t2, t3 = _t(a["id"], "one"), _t(a["id"], "two"), _t(a["id"], "three")
    mcp_server.set_global_plan(items=[{"task_id": t1["id"], "bucket": "now", "reason": "x"},
                                      {"task_id": t2["id"], "bucket": "next", "reason": "y"},
                                      {"task_id": t3["id"], "bucket": "later", "reason": "z"}])
    plan.owner_pin(t1["id"], True, "owner")
    r = client.post(f"/all/plan/{t1['id']}/today", data={"today": "1"}, headers={**OWN, "HX-Request": "true"})
    assert r.status_code == 200 and 'data-today-toggle' in r.text
    assert plan.plan_for_home()["today"][0]["id"] == t1["id"]  # the owner CAN mark a pinned card for today
    with pytest.raises(repo.Forbidden):
        plan.owner_today(t2["id"], True, "claude:all")
    with pytest.raises(repo.BadRequest):
        plan.owner_today(t3["id"], True, "owner")              # only Now / Next
    r = client.post(f"/all/plan/{t1['id']}/today", data={"today": "1"},
                    headers={"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    with get_cursor() as cur:
        cur.execute("UPDATE global_plan_state SET last_morning_date = NULL")
    assert plan.maybe_morning_replan(datetime(2026, 10, 4, 21, 0, tzinfo=timezone.utc)) is True
    assert plan.plan_for_home()["today"] == []                  # every today mark (pinned too) expires at 6am
    client.post(f"/all/plan/{t1['id']}/today", data={"today": "1"}, headers={**OWN, "HX-Request": "true"})
    client.post(f"/all/plan/{t1['id']}/today", data={"today": "0"}, headers={**OWN, "HX-Request": "true"})
    assert plan.plan_for_home()["today"] == []


def test_briefing_counts_live_plan_rows_only(overseer_a):  # trivial
    a, b = overseer_a["a"], overseer_a["b"]
    w, gone, live = _t(a["id"], "wont"), _t(b["id"], "gone"), _t(a["id"], "live")
    for t in (w, gone, live):
        plan.owner_move(t["id"], "next", None, "owner")
    repo.update_task(w["id"], {"priority": "wont"}, "owner")
    repo.set_project_archived(b["id"], True, "owner")
    assert repo.build_ground("all", session_project=a)["global_plan"]["next"] == 1


# --- #578: compact view, since filter, patch_global_plan -------------------------------------------------------

def _seed(overseer_a, n=8):
    a, b = overseer_a["a"], overseer_a["b"]
    ts = [_t((a if i % 2 else b)["id"], f"task number {i} " + "long " * 30, "must" if i < 3 else "could")
          for i in range(n)]
    mcp_server.set_global_plan(items=[
        {"task_id": ts[0]["id"], "bucket": "now", "reason": "r0"},
        {"task_id": ts[1]["id"], "bucket": "now", "reason": "r1"},
        {"task_id": ts[2]["id"], "bucket": "next", "reason": "r2"},
        {"task_id": ts[3]["id"], "bucket": "next", "reason": "r3"},
        {"task_id": ts[4]["id"], "bucket": "later", "reason": "r4"}], today=[ts[0]["id"]], today_note="note")
    return ts


def _ids(bucket):
    return [x["id"] for x in plan.plan_for_home()[bucket]]


def test_compact_view_shape_and_size(overseer_a):
    import json
    ts = _seed(overseer_a)
    full = mcp_server.get_global_view()
    raw = mcp_server.get_global_view(compact=True)
    assert isinstance(raw, str) and "\n" not in raw and ": " not in raw.split('"title"')[0]
    c = json.loads(raw)
    assert len(raw) < len(json.dumps(full, indent=2, default=str)) / 3
    t0 = next(t for t in c["tasks"] if t["id"] == ts[0]["id"])
    assert set(t0) == {"id", "project", "priority", "status", "stream", "assignee", "title", "open_questions",
                       "needs_owner", "updated_at"}
    assert len(t0["title"]) <= 80 and isinstance(t0["open_questions"], int)
    assert [r["id"] for r in c["plan"]["now"]] == _ids("now")
    assert all("reason" in r for r in c["plan"]["now"]) and all("reason" not in r for r in c["plan"]["next"])
    assert set(c["plan"]["next"][0]) == {"id", "bucket", "pinned"}
    assert c["plan"]["counts"] == {"now": 2, "next": 2, "later": 1}
    assert set(c["default_order"][0]) == {"task_id", "project", "suggested_bucket", "factors"}
    assert len(c["default_order"]) <= 30
    for k in ("health_open", "objectives", "owner_wip", "changes_waiting"):
        assert c[k] == json.loads(json.dumps(full[k], default=str))
    # the default call is untouched
    assert isinstance(full, dict) and "age_days" in full["tasks"][0]


def test_since_filter_keeps_updated_and_now(overseer_a):
    import json
    ts = _seed(overseer_a)
    cut = datetime.now(timezone.utc).isoformat()
    repo.update_task(ts[6]["id"], {"title": "touched later"}, "owner")
    ids = {t["id"] for t in json.loads(mcp_server.get_global_view(compact=True, since=cut))["tasks"]}
    assert ids == {ts[6]["id"], ts[0]["id"], ts[1]["id"]}  # the touched one + Now
    full = mcp_server.get_global_view(since=cut)
    assert {t["id"] for t in full["tasks"]} == ids and full["plan"]["next"] and "default_order" in full
    with pytest.raises(repo.BadRequest):
        mcp_server.get_global_view(since="yesterday")


def test_patch_ops_move_remove_reason(overseer_a):
    ts = _seed(overseer_a)
    i = [t["id"] for t in ts]
    r = mcp_server.patch_global_plan(ops=[
        {"op": "move", "task_id": i[4], "bucket": "next", "after": i[2]},   # later -> between 2 and 3
        {"op": "move", "task_id": i[5], "bucket": "next", "after": None},   # not in plan yet -> first
        {"op": "reason", "task_id": i[5], "reason": "  added   today "},
        {"op": "remove", "task_id": i[1]}])
    assert r["ok"] and (r["now"], r["next"], r["later"]) == (1, 4, 0) and r["planned_at"]
    assert _ids("next") == [i[5], i[2], i[4], i[3]] and _ids("now") == [i[0]]
    home = plan.plan_for_home()
    assert home["next"][0]["reason"] == "added today"
    # today and its note are untouched when not given
    assert [x["id"] for x in home["today"]] == [i[0]] and home["note"] == "note"
    mcp_server.patch_global_plan(ops=[], today=[i[2]], today_note="new")
    home = plan.plan_for_home()
    assert [x["id"] for x in home["today"]] == [i[2]] and home["note"] == "new"
    mcp_server.patch_global_plan(ops=[], today=[])
    assert plan.plan_for_home()["today"] == [] and plan.plan_for_home()["note"] == "new"


def test_patch_refusals_and_atomic_rollback(overseer_a):
    ts = _seed(overseer_a)
    i = [t["id"] for t in ts]
    before = {b: _ids(b) for b in ("now", "next", "later")}

    def same():
        return {b: _ids(b) for b in ("now", "next", "later")} == before
    plan.owner_pin(i[2], True, "owner")
    with pytest.raises(repo.Conflict):  # pinned: not moved to another bucket (same message as set_global_plan)
        mcp_server.patch_global_plan(ops=[{"op": "move", "task_id": i[2], "bucket": "later", "after": None}])
    with pytest.raises(repo.Conflict):
        mcp_server.patch_global_plan(ops=[{"op": "remove", "task_id": i[2]}])
    with pytest.raises(repo.Conflict):
        mcp_server.patch_global_plan(ops=[{"op": "reason", "task_id": i[2], "reason": "x"}])
    assert same()
    # Now > 3: the earlier valid op must roll back too
    with pytest.raises(repo.BadRequest, match="at most 3"):
        mcp_server.patch_global_plan(ops=[{"op": "reason", "task_id": i[0], "reason": "changed"},
                                          {"op": "move", "task_id": i[3], "bucket": "now", "after": None},
                                          {"op": "move", "task_id": i[4], "bucket": "now", "after": None}])
    # a bad op after good ones
    for bad in ({"op": "nope", "task_id": i[0]}, {"op": "move", "task_id": i[0], "bucket": "soon"},
                {"op": "move", "task_id": i[0], "bucket": "next", "after": i[1]},   # after not in target bucket
                {"op": "remove", "task_id": i[7]}):                                   # not in the plan
        with pytest.raises((repo.BadRequest, repo.NotFound)):
            mcp_server.patch_global_plan(ops=[{"op": "move", "task_id": i[4], "bucket": "now", "after": None}, bad])
    assert same() and plan.plan_for_home()["now"][0]["reason"] == "r0"
    # done tasks can't be inserted
    repo.update_task(i[6], {"status": "done"}, "owner")
    with pytest.raises(repo.BadRequest):
        mcp_server.patch_global_plan(ops=[{"op": "move", "task_id": i[6], "bucket": "later", "after": None}])


def test_patch_today_validation(overseer_a):
    ts = _seed(overseer_a)
    i = [t["id"] for t in ts]
    with pytest.raises(repo.BadRequest):   # in Later
        mcp_server.patch_global_plan(ops=[], today=[i[4]])
    with pytest.raises(repo.BadRequest):   # not in the plan
        mcp_server.patch_global_plan(ops=[], today=[i[7]])
    with pytest.raises(repo.BadRequest):   # moved to Later by the same patch
        mcp_server.patch_global_plan(ops=[{"op": "move", "task_id": i[2], "bucket": "later"}], today=[i[2]])
    plan.owner_pin(i[3], True, "owner")
    with pytest.raises(repo.Conflict):     # pinned, not today: the owner's flag
        mcp_server.patch_global_plan(ops=[], today=[i[3]])
    assert [x["id"] for x in plan.plan_for_home()["today"]] == [i[0]]
    mcp_server.patch_global_plan(ops=[], today=[i[0], i[2]])
    assert {x["id"] for x in plan.plan_for_home()["today"]} == {i[0], i[2]}


def test_patch_moves_a_pinned_card_within_its_bucket_as_a_noop(overseer_a):
    ts = _seed(overseer_a)
    i = [t["id"] for t in ts]
    plan.owner_pin(i[2], True, "owner")
    mcp_server.patch_global_plan(ops=[{"op": "move", "task_id": i[2], "bucket": "next", "after": i[3]}])
    assert _ids("next") == [i[2], i[3]]
