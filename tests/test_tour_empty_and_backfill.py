"""#938 review fixes: an EMPTY instance (no step may navigate to a page that does not exist, or build a blank path), the light answer for a person who has
finished or skipped, and the migration's backfill (owner decision q744: only new people get the tour by themselves)."""
import os

import pytest

from app import tour
from tests.test_multi_user import H, _client, _user, local, world  # noqa: F401  (fixtures)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _post(c, **data):
    return c.post("/all/tour", data=data, headers=H)


def _steps(c):
    return {s["id"]: s for s in c.get("/all/tour").json()["steps"]}


def test_on_an_empty_instance_no_step_navigates_and_none_builds_a_blank_path():
    """A fresh owner, no projects, no tasks: nothing may send them to a page that does not exist (or, with a blank {project}, to //releases, another host)."""
    steps = _steps(_client(_user("fresh", "owner")))
    for sid in ("runner", "run", "review"):
        assert steps[sid]["met"] is False and "go" not in steps[sid] and "target" not in steps[sid] and steps[sid]["else_text"], sid
    assert "Make a project first" in steps["runner"]["else_text"]
    assert steps["project"]["interactive"] is True and steps["project"]["do"] and steps["project"]["met"] is True
    for s in steps.values():
        assert "//" not in s.get("go", "x"), s


def test_a_go_is_always_a_path_on_this_site(world):
    for who in ("owner", "d1", "v"):
        for s in _steps(_client(world[who])).values():
            go = s.get("go")
            assert go is None or (go.startswith("/") and not go.startswith("//") and "{" not in go and "\\" not in go), s


def test_the_runner_step_needs_a_runner_home(world):
    owner = _steps(_client(world["owner"]))
    assert owner["runner"]["met"] is True and owner["runner"]["go"] == "/runners"
    assert _steps(_client(world["v"]))["runner"]["met"] is False        # sees nothing, so /runners would 404 for them


def test_a_step_that_builds_its_path_from_work_needs_that_work():
    assert tour._needs({"go": "/{project}/tasks/{task}"}) == ["project", "task"]
    assert tour._needs({"go": "/{project}/releases", "needs": "owner"}) == ["owner", "project"]
    assert tour._needs({"needs": ["owner", "runner"]}) == ["owner", "runner"]


def test_a_step_with_an_off_site_go_is_refused_in_the_file(monkeypatch, tmp_path):
    f = tmp_path / "bad.yaml"
    f.write_text("steps:\n  - {id: a, title: t, text: x, go: '//evil.example/x'}\n")
    monkeypatch.setattr(tour, "_cache", None)
    monkeypatch.setattr(tour, "STEPS_FILE", str(f))
    with pytest.raises(ValueError):
        tour.load()
    monkeypatch.setattr(tour, "_cache", None)


def test_a_finished_or_skipped_person_gets_only_status_and_step(world):
    c = _client(world["d1"])
    _post(c, action="skip", step="run")
    assert c.get("/all/tour").json() == {"status": "skipped", "step": "run"}
    assert _post(c, action="step", step="task").json() == {"status": "active", "step": "task"}      # saving a step answers small too
    d = c.get("/all/tour").json()
    assert d["status"] == "active" and len(d["steps"]) == len(tour.step_ids())
    assert "steps" in _post(c, action="restart").json()                                               # only a restart brings the steps back
    _post(c, action="finish", step="done")
    assert set(c.get("/all/tour").json()) == {"status", "step"}


def test_migration_backfills_existing_users_as_finished_and_leaves_new_people_alone():
    """Running 0113 marks every EXISTING user finished, keeps a row someone already has, and inserts nothing where there are no users."""
    from app.db import get_cursor
    sql = open(os.path.join(ROOT, "db", "migrations", "0113_tour_progress.sql"), encoding="utf-8").read()
    with get_cursor() as cur:
        cur.execute(sql)                                                   # no users yet: nothing is inserted
        cur.execute("SELECT count(*) AS n FROM tour_progress")
        assert cur.fetchone()["n"] == 0
    old, mid = _user("old", "designer"), _user("mid", "designer")
    with get_cursor() as cur:
        cur.execute("INSERT INTO tour_progress (user_id, status, last_step) VALUES (%s, 'active', 'task')", (mid["id"],))
        cur.execute(sql)
        cur.execute("SELECT user_id, status, last_step FROM tour_progress WHERE user_id IS NOT NULL")
        rows = {r["user_id"]: (r["status"], r["last_step"]) for r in cur.fetchall()}
        cur.execute("SELECT count(*) AS n FROM tour_progress WHERE user_id IS NULL")
        assert cur.fetchone()["n"] == 0                                    # no tasks: not an existing sign-in-off instance
    assert rows[old["id"]] == ("finished", "done") and rows[mid["id"]] == ("active", "task")
    assert _client(old).get("/all/tour").json()["status"] == "finished"   # the existing user is not shown it
    new = _user("newbie", "designer")                                      # a person made after the migration is new
    assert _client(new).get("/all/tour").json()["status"] == "new"
