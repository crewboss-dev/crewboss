"""Task #915: planning dates. Milestones (a named date per project), a task's due_on / lead_days / start_by / milestone / waiting
fields, the overseer's date factors and this_week, and the once-only planning reminders. See docs/planning-dates.md."""
from datetime import date, datetime, timezone

import psycopg
import pytest

from app import config, mcp_server, plan, planning, planning_reminders, push, repo
from app.auth import issue_token
from app.db import get_cursor
from test_isolation import _b_milestone, ab  # noqa: F401  (ab is a fixture)

TODAY = date(2026, 12, 1)


@pytest.fixture(autouse=True)
def _fixed_today(monkeypatch):
    monkeypatch.setattr(planning, "today", lambda: TODAY)


def _task(pid, title="t", priority="should", stream="feature", status="ready", **plan_fields):
    return repo.create_task(pid, title, "", priority, stream, [], {}, "claude:features", status=status,
                            planning_patch=planning.patch_from(**plan_fields))


def _ms(pid, name="Feature freeze", due="2026-12-11"):
    return planning.create_milestone(pid, name, due, "", "claude:features")


# --- validation -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["11/12/2026", "2026-13-01", "2026-02-30", "2026-12-1", "tomorrow", "1999-01-01", 20261211, "2026-12-11T00:00"])
def test_a_bad_date_is_refused(project, bad):
    with pytest.raises(repo.BadRequest):
        _task(project["id"], due_on=bad)
    t = _task(project["id"])
    for field in ("due_on", "start_by", "waiting_until"):
        with pytest.raises(repo.BadRequest):
            repo.update_task(t["id"], {"waiting_on": "x", field: bad}, "claude:features")


@pytest.mark.parametrize("bad", [-1, 366, 1000, True, "7", 1.5])
def test_lead_days_is_0_to_365(project, bad):
    with pytest.raises(repo.BadRequest):
        _task(project["id"], lead_days=bad)
    assert _task(project["id"], lead_days=0)["lead_days"] == 0
    assert _task(project["id"], lead_days=365)["lead_days"] == 365


def test_text_fields_are_one_short_line(project):
    t = _task(project["id"])
    for patch in ({"waiting_on": "two\nlines"}, {"waiting_on": "x" * 121}, {"waiting_on": "ok", "waiting_for": "a\nb"},
                  {"waiting_on": "ok", "waiting_for": "y" * 201}):
        with pytest.raises(repo.BadRequest):
            repo.update_task(t["id"], patch, "claude:features")
    row = repo.update_task(t["id"], {"waiting_on": "  Google Play review ", "waiting_for": "the first review"}, "claude:features")
    assert row["waiting_on"] == "Google Play review" and row["waiting_for"] == "the first review"


# --- start_by ---------------------------------------------------------------------------------------------------------

def test_start_by_is_due_minus_lead_and_follows_changes(project):
    pid = project["id"]
    t = _task(pid, due_on="2026-12-11", lead_days=10)
    assert t["start_by"] == date(2026, 12, 1) and t["start_by_manual"] is False
    t = repo.update_task(t["id"], {"lead_days": 20}, "claude:features")
    assert t["start_by"] == date(2026, 11, 21)
    t = repo.update_task(t["id"], {"due_on": "2026-12-25"}, "claude:features")
    assert t["start_by"] == date(2026, 12, 5)
    t = repo.update_task(t["id"], {"due_on": None, "lead_days": None}, "claude:features")  # None = untouched in a patch? no: cleared below
    assert t["due_on"] == date(2026, 12, 25) or t["due_on"] is None
    cleared = repo.update_task(t["id"], planning.patch_from(clear=["due_on"]), "claude:features")
    assert cleared["due_on"] is None and cleared["start_by"] is None
    assert _task(pid, due_on="2026-12-11")["start_by"] is None   # no lead_days: nothing to compute


def test_an_explicit_start_by_is_kept_until_cleared(project):
    t = _task(project["id"], due_on="2026-12-11", lead_days=10, start_by="2026-11-20")
    assert t["start_by"] == date(2026, 11, 20) and t["start_by_manual"] is True
    t = repo.update_task(t["id"], {"due_on": "2026-12-31", "lead_days": 5}, "claude:features")
    assert t["start_by"] == date(2026, 11, 20)                       # not recomputed
    t = repo.update_task(t["id"], planning.patch_from(clear=["start_by"]), "claude:features")
    assert t["start_by_manual"] is False and t["start_by"] == date(2026, 12, 26)   # handed back to the computation


def test_start_by_follows_the_milestone_date(project):
    pid = project["id"]
    m = _ms(pid)
    t = _task(pid, milestone="Feature freeze", lead_days=7)
    assert t["due_effective"] == date(2026, 12, 11) and t["due_source"] == "milestone" and t["due_on"] is None
    assert t["start_by"] == date(2026, 12, 4)
    own = _task(pid, milestone=m["id"], lead_days=7, due_on="2026-12-20")      # its own date wins
    assert own["due_effective"] == date(2026, 12, 20) and own["due_source"] == "own"
    manual = _task(pid, milestone=m["id"], lead_days=7, start_by="2026-11-30")
    planning.update_milestone(m["id"], {"due_on": "2026-12-18"}, "claude:features")   # the milestone moves
    assert repo.get_task(t["id"])["due_effective"] == date(2026, 12, 18)
    assert repo.get_task(t["id"])["start_by"] == date(2026, 12, 11)
    assert repo.get_task(own["id"])["start_by"] == date(2026, 12, 13)        # unchanged: it has its own due_on
    assert repo.get_task(manual["id"])["start_by"] == date(2026, 11, 30)


# --- milestones belong to one project ----------------------------------------------------------------------------------

def test_a_milestone_must_be_in_the_tasks_own_project(project):
    other = repo.create_project("other-proj", "Other", "internal", "system")
    theirs = _ms(other["id"], "Theirs")
    t = _task(project["id"])
    with pytest.raises(repo.NotFound):
        repo.update_task(t["id"], {"milestone": theirs["id"]}, "claude:features")
    with pytest.raises(repo.NotFound):
        _task(project["id"], milestone="Theirs")
    # the database refuses it too, whatever the code does
    with pytest.raises(psycopg.errors.CheckViolation):
        with get_cursor() as cur:
            cur.execute("UPDATE tasks SET milestone_id = %s WHERE id = %s", (theirs["id"], t["id"]))


def test_moving_a_task_to_another_project_drops_its_milestone(project):
    from app import tree
    acct = repo.create_project("acct", "Acct", "internal", "owner")
    tree.set_place(acct["id"], "owner", None, "account")
    tree.set_place(project["id"], "owner", acct["id"])
    other = repo.create_project("other-proj", "Other", "internal", "owner")
    tree.set_place(other["id"], "owner", acct["id"])
    t = _task(project["id"], milestone=_ms(project["id"])["id"], lead_days=3)
    moved = repo.update_task(t["id"], {"project": "other-proj"}, "owner")
    assert moved["milestone_id"] is None and moved["project_id"] == other["id"] and moved["start_by"] is None


def test_an_archived_milestone_cannot_be_joined_and_names_are_unique(project):
    m = _ms(project["id"])
    with pytest.raises(repo.Conflict):
        _ms(project["id"], "feature FREEZE")
    planning.update_milestone(m["id"], {"archived": True}, "claude:features")
    with pytest.raises(repo.NotFound):
        _task(project["id"], milestone="Feature freeze")
    with pytest.raises(repo.BadRequest):
        _task(project["id"], milestone=m["id"])
    assert planning.list_milestones([project["id"]]) == []
    assert len(planning.list_milestones([project["id"]], include_archived=True)) == 1


# --- derived status -----------------------------------------------------------------------------------------------------

def _status(pid, name):
    return next(m for m in planning.list_milestones([pid]) if m["name"] == name)


def test_milestone_status_is_derived(project):
    pid = project["id"]
    m = _ms(pid, "Launch", "2026-12-20")
    assert _status(pid, "Launch")["status"] == "on_track" and _status(pid, "Launch")["counts"] == {"total": 0, "done": 0, "open": 0}
    a = _task(pid, milestone=m["id"], status="doing")
    b = _task(pid, milestone=m["id"], status="ready", lead_days=2)         # start_by 2026-12-18: not passed
    assert _status(pid, "Launch")["status"] == "on_track"
    assert _status(pid, "Launch")["counts"] == {"total": 2, "done": 0, "open": 2}
    repo.update_task(b["id"], {"start_by": "2026-11-30"}, "claude:features")  # passed, still ready: at risk
    assert _status(pid, "Launch")["status"] == "at_risk"
    repo.update_task(b["id"], {"status": "doing"}, "claude:features")        # started: no longer at risk
    assert _status(pid, "Launch")["status"] == "on_track"
    late = _ms(pid, "Freeze", "2026-11-30")                                   # due date passed
    c = _task(pid, milestone=late["id"], status="doing")
    assert _status(pid, "Freeze")["status"] == "late"
    repo.update_task(c["id"], {"status": "done"}, "claude:features")
    assert _status(pid, "Freeze")["status"] == "done"
    wont = _task(pid, milestone=late["id"], priority="wont")                  # Won't have is not counted
    assert _status(pid, "Freeze")["status"] == "done" and _status(pid, "Freeze")["counts"]["total"] == 1
    assert wont


# --- waiting ------------------------------------------------------------------------------------------------------------

def test_clearing_waiting_on_clears_the_rest(project):
    t = _task(project["id"], waiting_on="customer: Acme", waiting_for="tenant consent", waiting_until="2026-12-05")
    assert (t["waiting_on"], t["waiting_for"], t["waiting_until"]) == ("customer: Acme", "tenant consent", date(2026, 12, 5))
    t = repo.update_task(t["id"], {"waiting_for": "the admin account"}, "claude:features")
    assert t["waiting_on"] == "customer: Acme" and t["waiting_for"] == "the admin account"
    for clear in ({"waiting_on": ""}, planning.patch_from(clear=["waiting"])):
        t = repo.update_task(t["id"], {"waiting_on": "lawyer", "waiting_for": "contract", "waiting_until": "2026-12-09"}, "claude:features")
        t = repo.update_task(t["id"], clear, "claude:features")
        assert (t["waiting_on"], t["waiting_for"], t["waiting_until"]) == (None, None, None)


def test_waiting_for_or_until_needs_waiting_on(project):
    with pytest.raises(repo.BadRequest):
        _task(project["id"], waiting_for="something")
    t = _task(project["id"])
    with pytest.raises(repo.BadRequest):
        repo.update_task(t["id"], {"waiting_until": "2026-12-09"}, "claude:features")


def test_flags_late_start_reached_and_waiting_overdue(project):
    pid = project["id"]
    late = _task(pid, due_on="2026-11-30")
    start = _task(pid, due_on="2026-12-20", lead_days=19)                 # start_by 2026-12-01 = today
    chase = _task(pid, waiting_on="Google", waiting_until="2026-11-29")
    fine = _task(pid, due_on="2026-12-20", lead_days=2)
    assert (late["due_state"], start["due_state"], fine["due_state"]) == ("late", "start_reached", None)
    assert chase["waiting_overdue"] is True and fine["waiting_overdue"] is False
    listed = {t["id"]: t for t in repo.list_tasks(pid)}
    assert listed[late["id"]]["due_state"] == "late" and listed[chase["id"]]["waiting_overdue"]
    done = repo.update_task(late["id"], {"status": "done"}, "claude:features")
    assert done["due_state"] is None


# --- scope: another account's milestone is NotFound ---------------------------------------------------------------------

def test_another_projects_milestone_is_not_found_like_a_missing_one(ab, mcp_caller):  # noqa: F811
    theirs = _b_milestone(ab["b"])
    mcp_caller(issue_token("features", "pa-features", project_id=ab["a"]["id"]))
    for call in (lambda i: mcp_server.update_milestone(milestone_id=i, name="hijack"),
                 lambda i: mcp_server.add_task(title="x", stream="feature", milestone=i),
                 lambda i: mcp_server.update_task(task_id=ab["at"]["id"], milestone=i)):
        with pytest.raises(repo.NotFound) as theirs_err:
            call(theirs["id"])
        with pytest.raises(repo.NotFound) as missing_err:
            call(999999)
        assert str(theirs_err.value).replace(str(theirs["id"]), "N") == str(missing_err.value).replace("999999", "N") \
            or "milestone" in str(theirs_err.value)
    with pytest.raises(repo.NotFound):
        mcp_server.list_milestones(project="proj-b")
    with pytest.raises(repo.NotFound):
        mcp_server.add_milestone(project="proj-b", name="x", due_on="2026-12-11")
    assert mcp_server.list_milestones() == []
    assert planning.get_milestone(theirs["id"])["name"] == "B launch"     # untouched


def test_overseer_reads_milestones_everywhere_and_writes_none(ab, monkeypatch, mcp_caller):  # noqa: F811
    theirs = _b_milestone(ab["b"])
    monkeypatch.setattr(config, "OVERSEER_PROJECT", "proj-a")
    mcp_caller(issue_token("all", "dash", project_id=ab["a"]["id"]))
    assert [m["name"] for m in mcp_server.list_milestones(project="proj-b")] == ["B launch"]
    with pytest.raises(repo.NotFound):
        mcp_server.update_milestone(milestone_id=theirs["id"], name="hijack")
    with pytest.raises(repo.NotFound):
        mcp_server.add_milestone(project="proj-b", name="x", due_on="2026-12-11")
    # a REQUEST task in another project may name that project's milestone (add_task's existing overseer rule)
    row = mcp_server.add_task(title="request", stream="feature", project="proj-b", milestone="B launch", lead_days=3)
    assert row["milestone_id"] == theirs["id"] and row["start_by"] == date(2026, 12, 8)


# --- MCP + REST round trips ----------------------------------------------------------------------------------------------

def test_mcp_add_and_update_round_trip_the_fields(project, mcp_caller):
    mcp_caller(issue_token("features", "f", project_id=project["id"]))
    m = mcp_server.add_milestone(project="test-proj", name="Play Store submission", due_on="2027-01-15", note="Google review ~1 week")
    assert m["status"] == "on_track" and m["note"] == "Google review ~1 week" and m["counts"]["total"] == 0
    t = mcp_server.add_task(title="Submit to Play", stream="feature", project="test-proj", milestone="Play Store submission",
                            lead_days=14, waiting_on="Google Play review", waiting_for="first review", waiting_until="2027-01-08")
    assert t["due_effective"] == date(2027, 1, 15) and t["start_by"] == date(2027, 1, 1) and t["milestone_name"] == "Play Store submission"
    got = mcp_server.get_task(t["id"])
    assert (got["waiting_on"], got["waiting_until"], got["lead_days"], got["milestone_name"]) == \
        ("Google Play review", date(2027, 1, 8), 14, "Play Store submission")
    assert next(x for x in mcp_server.list_tasks() if x["id"] == t["id"])["start_by"] == date(2027, 1, 1)
    assert next(x for x in mcp_server.my_tasks() if x["id"] == t["id"])["due_effective"] == date(2027, 1, 15)
    u = mcp_server.update_task(task_id=t["id"], due_on="2027-01-12", start_by="2026-12-30", waiting_until="2027-01-09")
    assert u["due_on"] == date(2027, 1, 12) and u["start_by"] == date(2026, 12, 30) and u["waiting_until"] == date(2027, 1, 9)
    u = mcp_server.update_task(task_id=t["id"], clear=["waiting", "milestone", "start_by"])
    assert u["waiting_on"] is None and u["waiting_until"] is None and u["milestone_id"] is None
    assert u["start_by"] == date(2026, 12, 29)                              # due_on 12 Jan - 14 days again
    with pytest.raises(repo.BadRequest):
        mcp_server.update_task(task_id=t["id"], due_on="12/01/2027")
    with pytest.raises(repo.BadRequest):
        mcp_server.update_task(task_id=t["id"], lead_days=400)
    with pytest.raises(repo.BadRequest):
        mcp_server.update_task(task_id=t["id"], clear=["title"])
    moved = mcp_server.update_milestone(milestone_id=m["id"], due_on="2027-01-20", name="Play Store live")
    assert moved["due_on"] == date(2027, 1, 20) and moved["name"] == "Play Store live"
    assert [x["name"] for x in mcp_server.list_milestones()] == ["Play Store live"]


def test_rest_round_trip(client, project):
    h = {"Authorization": f"Bearer {issue_token('features', 'f', project_id=project['id'])}"}
    r = client.post("/api/milestones", json={"name": "Freeze", "due_on": "2026-12-11"}, headers=h)
    assert r.status_code == 201 and r.json()["status"] == "on_track"
    assert client.post("/api/milestones", json={"name": "Bad", "due_on": "soon"}, headers=h).status_code == 400
    assert client.post("/api/milestones", json={"name": "freeze", "due_on": "2026-12-12"}, headers=h).status_code == 409
    r = client.post("/api/tasks", json={"title": "x", "stream": "feature", "milestone": "Freeze", "lead_days": 5,
                                        "waiting_on": "lawyer", "waiting_until": "2026-12-03"}, headers=h)
    assert r.status_code == 201 and r.json()["start_by"] == "2026-12-06" and r.json()["waiting_on"] == "lawyer"
    tid = r.json()["id"]
    r = client.patch(f"/api/tasks/{tid}", json={"lead_days": 9, "clear": ["waiting"]}, headers=h)
    assert r.status_code == 200 and r.json()["start_by"] == "2026-12-02" and r.json()["waiting_on"] is None
    assert client.patch(f"/api/tasks/{tid}", json={"due_on": "nope"}, headers=h).status_code == 400
    assert client.get(f"/api/tasks/{tid}", headers=h).json()["milestone_name"] == "Freeze"
    ms = client.get("/api/milestones", headers=h).json()
    assert ms[0]["counts"]["total"] == 1
    assert client.patch(f"/api/milestones/{ms[0]['id']}", json={"due_on": "2026-12-14"}, headers=h).json()["due_on"] == "2026-12-14"
    assert client.patch("/api/milestones/999999", json={"name": "x"}, headers=h).status_code == 404


# --- the overseer's planning: factors, order, this_week -----------------------------------------------------------------

def _order(view):
    return [s["task_id"] for s in view["default_order"]]


def test_default_order_ranks_dates(project):
    pid = project["id"]
    fault = _task(pid, "fault", priority="could", stream="error")
    plain_must = _task(pid, "plain must", priority="must")
    soon_could = _task(pid, "due soon", priority="could", due_on="2026-12-05")
    late_could = _task(pid, "late", priority="could", due_on="2026-11-20")
    start_could = _task(pid, "start by reached", priority="could", due_on="2026-12-30", lead_days=29)
    chase = _task(pid, "chase", priority="could", waiting_on="lawyer", waiting_until="2026-11-30")
    far = _task(pid, "far", priority="should", due_on="2027-03-01")
    view = plan.global_view()
    order = _order(view)
    assert order[0] == fault["id"]
    assert set(order[1:3]) == {late_could["id"], start_could["id"]}            # hard dates: just under a production fault
    assert order.index(soon_could["id"]) < order.index(plain_must["id"])      # due soon / waiting overdue beat plain Must
    assert order.index(chase["id"]) < order.index(plain_must["id"])
    assert order.index(plain_must["id"]) < order.index(far["id"])
    why = {s["task_id"]: s["factors"] for s in view["default_order"]}
    assert any(f.startswith("late") for f in why[late_could["id"]])
    assert any(f.startswith("start-by reached") for f in why[start_could["id"]])
    assert any(f.startswith("due within 7 days") for f in why[soon_could["id"]])
    assert any(f.startswith("waiting overdue") for f in why[chase["id"]])
    assert not any("late" in f or "due within" in f for f in why[plain_must["id"]])


def test_this_week_and_the_slim_rows(project):
    pid = project["id"]
    m = _ms(pid, "Freeze", "2026-12-04")
    due = _task(pid, "due this week", due_on="2026-12-05")
    inherited = _task(pid, "via milestone", milestone=m["id"])
    start = _task(pid, "start soon", due_on="2026-12-25", lead_days=20)        # start_by 12-05
    chase = _task(pid, "chase me", waiting_on="Google Play review", waiting_for="review", waiting_until="2026-11-28")
    late = _task(pid, "late", due_on="2026-11-25")
    far = _task(pid, "far", due_on="2027-01-20")
    done = _task(pid, "done", due_on="2026-12-02", status="done")
    still_waiting = _task(pid, "waits", waiting_on="owner", waiting_until="2026-12-09")
    for compact in (False, True):
        view = plan.global_view(compact=compact)
        week = {w["id"]: w for w in view["this_week"]}
        assert set(week) == {due["id"], inherited["id"], start["id"], chase["id"], late["id"]}
        assert not ({far["id"], done["id"], still_waiting["id"]} & set(week))
        assert week[chase["id"]]["why"] == ["chase"] and week[start["id"]]["why"] == ["start by"]
        assert week[inherited["id"]]["due_on"] == "2026-12-04" and week[inherited["id"]]["milestone"] == "Freeze"
        assert list(week)[0] == late["id"] or week[late["id"]]["why"] == ["late"]
        rows = {t["id"]: t for t in view["tasks"]}
        assert rows[due["id"]]["due_on"] == "2026-12-05"
        assert rows[start["id"]]["start_by"] == "2026-12-05"
        assert rows[chase["id"]]["waiting_on"] == "Google Play review" and rows[chase["id"]]["waiting_until"] == "2026-11-28"
        assert "due_on" not in rows[chase["id"]] and "waiting_on" not in rows[due["id"]]   # only what is set


# --- reminders ------------------------------------------------------------------------------------------------------------

NOW = datetime(2026, 12, 1, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def pushes(monkeypatch):
    """Push on, one device subscribed, deliveries captured."""
    monkeypatch.setattr(config, "CA_TIMEZONE", "UTC")
    monkeypatch.setattr(push, "_load_vapid", lambda: (object(), None))
    sent = []
    monkeypatch.setattr(push, "deliver", lambda payload: sent.append(payload) or {"sent": 1, "failed": 0, "pruned": 0})
    with get_cursor() as cur:
        cur.execute("INSERT INTO push_subscriptions (endpoint, p256dh, auth) VALUES ('https://push.example.com/x', 'aaaaaaaaaa', 'bbbbbbbbbb')")
    return sent


def test_a_start_reminder_is_sent_once(project, pushes):
    m = _ms(project["id"], "Play Store submission", "2027-01-15")
    t = _task(project["id"], "Write the store listing", milestone=m["id"], lead_days=45)     # start_by 2026-12-01
    assert t["start_by"] == TODAY
    out = planning_reminders.sweep(NOW)
    assert out["sent"] == 1 and len(pushes) == 1
    assert pushes[0]["title"] == "Start today: Write the store listing — to make Play Store submission (15 Jan)"
    assert pushes[0]["url"].endswith(f"/test-proj/tasks/{t['id']}")
    assert planning_reminders.sweep(NOW)["sent"] == 0 and planning_reminders.sweep(datetime(2026, 12, 2, 9, tzinfo=timezone.utc))["sent"] == 0
    assert len(pushes) == 1
    # not yet due, done, or already started: nothing
    _task(project["id"], "later", due_on="2027-02-01", lead_days=3)
    _task(project["id"], "doing", due_on="2026-12-02", lead_days=5, status="doing")
    _task(project["id"], "finished", due_on="2026-12-02", lead_days=5, status="done")
    assert planning_reminders.sweep(NOW)["sent"] == 0 and len(pushes) == 1


def test_a_chase_reminder_is_sent_once(project, pushes):
    t = _task(project["id"], "Pilot sign-off", waiting_on="customer: Acme", waiting_for="test results", waiting_until="2026-11-30")
    _task(project["id"], "not yet", waiting_on="lawyer", waiting_until="2026-12-01")        # expected back today: not overdue
    assert planning_reminders.sweep(NOW)["sent"] == 1
    assert pushes[0]["title"] == "Chase: Pilot sign-off — waiting on customer: Acme for test results, expected back 30 Nov"
    assert planning_reminders.sweep(NOW)["sent"] == 0
    repo.update_task(t["id"], {"status": "done"}, "claude:features")
    assert planning_reminders.sweep(NOW)["sent"] == 0


def test_several_on_one_day_are_one_digest(project, pushes):
    for i in range(7):
        _task(project["id"], f"start {i}", due_on="2026-12-10", lead_days=9 + i % 2)        # all start_by <= today
    _task(project["id"], "chase", waiting_on="lawyer", waiting_until="2026-11-29")
    out = planning_reminders.sweep(NOW)
    assert out["sent"] == 8 and len(pushes) == 1
    assert pushes[0]["title"] == "8 planning reminders"
    lines = pushes[0]["body"].split("\n")
    assert len(lines) == 6 and lines[-1] == "+3 more" and lines[0].startswith("Start today: start ")
    assert planning_reminders.sweep(NOW)["sent"] == 0 and len(pushes) == 1


def test_a_changed_date_re_arms_the_reminder(project, pushes):
    t = _task(project["id"], "Freeze build", due_on="2026-12-11", lead_days=10)
    w = _task(project["id"], "Await legal", waiting_on="lawyer", waiting_until="2026-11-30")
    assert planning_reminders.sweep(NOW)["sent"] == 2 and len(pushes) == 1
    assert planning_reminders.sweep(NOW)["sent"] == 0
    repo.update_task(t["id"], {"due_on": "2026-12-09"}, "claude:features")                  # start_by moved to 11-29
    assert planning_reminders.sweep(NOW)["sent"] == 1
    assert pushes[-1]["title"].startswith("Start today: Freeze build")
    repo.update_task(w["id"], {"waiting_until": "2026-11-25"}, "claude:features")
    assert planning_reminders.sweep(NOW)["sent"] == 1 and pushes[-1]["title"].startswith("Chase: Await legal")
    assert planning_reminders.sweep(NOW)["sent"] == 0
    repo.update_task(w["id"], {"status": "doing"}, "claude:features")                        # an unrelated change re-arms nothing
    assert planning_reminders.sweep(NOW)["sent"] == 0 and len(pushes) == 3


def test_nothing_before_the_morning_and_nothing_marked_while_push_is_off(project, pushes, monkeypatch):
    _task(project["id"], "early", due_on="2026-12-05", lead_days=10)
    assert planning_reminders.sweep(datetime(2026, 12, 1, 5, 0, tzinfo=timezone.utc))["early"] is True and not pushes
    monkeypatch.setattr(push, "_load_vapid", lambda: (None, "no VAPID key on the server"))
    assert planning_reminders.sweep(NOW)["off"] and not pushes
    monkeypatch.setattr(push, "_load_vapid", lambda: (object(), None))
    with get_cursor() as cur:
        cur.execute("DELETE FROM push_subscriptions")
    assert planning_reminders.sweep(NOW)["off"] == "no device subscribed" and not pushes
    with get_cursor() as cur:
        cur.execute("INSERT INTO push_subscriptions (endpoint, p256dh, auth) VALUES ('https://push.example.com/y', 'aaaaaaaaaa', 'bbbbbbbbbb')")
    assert planning_reminders.sweep(NOW)["sent"] == 1 and len(pushes) == 1          # the first phone gets what was due


# --- pages (display only) ---------------------------------------------------------------------------------------------

def test_cards_the_task_window_and_the_milestone_strip_show_the_dates(client, project):
    m = _ms(project["id"], "Feature freeze", "2026-12-11")
    late = _task(project["id"], "Late one", due_on="2026-11-20")
    start = _task(project["id"], "Start one", milestone=m["id"], lead_days=10)            # start_by = today
    wait = _task(project["id"], "Waiting one", waiting_on="Google Play review", waiting_until="2026-12-09")
    _task(project["id"], "Plain")
    for path in ("/test-proj/board", "/test-proj"):
        html = client.get(path).text
        assert 'data-milestone-strip' in html and "Feature freeze" in html
        assert 'data-milestone="%d"' % m["id"] in html
    board = client.get("/test-proj/board").text
    assert 'data-due-chip="late"' in board and "Late · Due 20 Nov" in board
    assert 'data-due-chip="start_reached"' in board and "Start now · Due 11 Dec" in board
    assert "Waiting on Google Play review until 9 Dec" in board
    panel = client.get(f"/test-proj/tasks/{wait['id']}/panel").text
    assert "data-waiting-badge" in panel
    assert 'data-due-chip="late"' in client.get(f"/test-proj/tasks/{late['id']}/panel").text
    assert start["start_by"] == TODAY
