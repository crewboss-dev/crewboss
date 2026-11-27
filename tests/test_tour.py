"""#938: the first-run how-to tour. Progress is per PERSON and server-side (app/tour.py, /all/tour); the step text is ONE file (app/tour_steps.yaml)
and docs/getting-started.md is checked against it, so the page and the tour cannot disagree."""
import os
import re

import pytest

from app import tour
from tests.test_multi_user import H, _client, local, world  # noqa: F401  (fixtures)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _post(c, **data):
    return c.post("/all/tour", data=data, headers=H)


# --- the progress API ----------------------------------------------------------------------------------------------------

def test_a_person_who_has_never_seen_it_is_new_and_gets_every_step(world):
    d = _client(world["d1"]).get("/all/tour").json()
    assert d["status"] == "new" and d["step"] == ""
    assert [s["id"] for s in d["steps"]] == tour.step_ids()
    assert all(s["title"] and s["text"] for s in d["steps"])


def test_progress_is_saved_and_resumes_at_the_last_step(world):
    c = _client(world["d1"])
    assert _post(c, action="step", step="runner").json()["step"] == "runner"
    d = c.get("/all/tour").json()
    assert d["status"] == "active" and d["step"] == "runner"
    assert _post(c, action="step", step="task").json()["step"] == "task"
    assert c.get("/all/tour").json()["step"] == "task"


def test_finish_and_skip_end_it_and_keep_the_last_step(world):
    c = _client(world["d1"])
    _post(c, action="step", step="run")
    d = _post(c, action="skip", step="run").json()
    assert d["status"] == "skipped" and d["step"] == "run"
    d = _post(c, action="finish", step="done").json()
    assert d["status"] == "finished" and d["step"] == "done"
    assert c.get("/all/tour").json()["status"] == "finished"


def test_skip_without_a_step_keeps_where_they_were(world):
    c = _client(world["d1"])
    _post(c, action="step", step="task")
    assert _post(c, action="skip").json()["step"] == "task"


def test_restart_goes_back_to_the_first_step_and_is_active_again(world):
    c = _client(world["d1"])
    _post(c, action="finish", step="done")
    d = _post(c, action="restart").json()
    assert d["status"] == "active" and d["step"] == tour.step_ids()[0]


def test_progress_is_per_person(world):
    a, b = _client(world["d1"]), _client(world["d2"])
    _post(a, action="skip", step="project")
    assert a.get("/all/tour").json()["status"] == "skipped"
    assert b.get("/all/tour").json()["status"] == "new"
    _post(b, action="step", step="assistant")
    assert a.get("/all/tour").json()["step"] == "project" and b.get("/all/tour").json()["step"] == "assistant"


def test_a_bad_action_or_step_is_refused_and_changes_nothing(world):
    c = _client(world["d1"])
    for data in ({"action": "nope"}, {"action": "step", "step": "nope"}, {"action": "step"}, {"action": "finish", "step": "nope"}, {}):
        assert _post(c, **data).status_code == 400
    assert c.get("/all/tour").json()["status"] == "new"


def test_signed_out_gets_nothing(world):
    from fastapi.testclient import TestClient
    from app.main import app
    from tests.test_multi_user import ORIGIN
    anon = TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 50001))
    assert anon.get("/all/tour", follow_redirects=False).status_code in (302, 401, 403, 404)
    assert anon.post("/all/tour", data={"action": "skip"}, headers=H, follow_redirects=False).status_code in (302, 401, 403, 404)


def test_a_cross_site_post_is_refused(world):
    c = _client(world["d1"])
    r = c.post("/all/tour", data={"action": "skip"}, headers={"Origin": "https://evil.example", "HX-Request": "true"})
    assert r.status_code == 403
    assert c.get("/all/tour").json()["status"] == "new"


def test_steps_point_at_the_persons_own_work_and_say_so_when_there_is_none(world):
    d1 = {s["id"]: s for s in _client(world["d1"]).get("/all/tour").json()["steps"]}
    assert d1["run"]["go"] == f"/d1-proj/tasks/{world['t1']['id']}" and d1["run"]["met"] is True
    assert "go" not in d1["review"] and d1["review"]["met"] is False and d1["review"]["else_text"] and "target" not in d1["review"]   # not the owner
    own = {s["id"]: s for s in _client(world["owner"]).get("/all/tour").json()["steps"]}
    assert own["review"]["go"].endswith("/releases") and own["review"]["met"] is True
    # the viewer owns nothing and sees nothing of the others' projects: no project, so no task page to go to
    v = {s["id"]: s for s in _client(world["v"]).get("/all/tour").json()["steps"]}
    assert v["run"]["met"] is False and "go" not in v["run"] and v["run"]["else_text"]
    assert "d2" not in str(v) and "SECRET" not in str(v)


def test_the_page_carries_the_tour_and_the_how_to_item(world):
    html = _client(world["d1"]).get("/").text
    assert 'data-tour-root' in html and "js/tour.js" in html and "css/tour.css" in html
    assert re.search(r"<button[^>]*data-tour-restart[^>]*>\s*How-to\s*</button>", html)
    assert "data-tour=\"new-project\"" in html


# --- one source of text: the steps file and getting-started.md ----------------------------------------------------------

def _doc_sections():
    with open(os.path.join(ROOT, "docs", "getting-started.md"), encoding="utf-8") as fh:
        return re.findall(r"^## (\d+)\. (.+?)\s*$", fh.read(), re.M)


def test_getting_started_sections_and_tour_steps_agree():
    """Every numbered section of docs/getting-started.md is either listed in the steps file's not_in_tour (it comes before the tour) or is taught by
    exactly one step, in the same order; and no step names a section that is not on the page."""
    data = tour.load()
    sections = [(int(n), title) for n, title in _doc_sections()]
    assert [n for n, _t in sections] == list(range(1, len(sections) + 1)), "getting-started.md numbered sections must run 1, 2, 3 ... with no gaps"
    titles = [t for _n, t in sections]
    before = data["not_in_tour"]
    assert titles[:len(before)] == before, f"the sections before the tour are {before}; the page has {titles[:len(before)]}"
    in_tour = titles[len(before):]
    docs = [s["doc"] for s in data["steps"] if s.get("doc")]
    assert docs == in_tour, f"tour steps teach {docs} but docs/getting-started.md sections after the install are {in_tour}"


def test_the_steps_file_is_well_formed():
    steps = tour.load()["steps"]
    assert steps[0].get("target") is None and steps[-1]["id"] == "done"          # it opens with the welcome and ends with "You're set"
    for s in steps:
        assert len(s["text"]) <= 260, f"{s['id']}: one or two short sentences"
        assert "doc" in s or s["id"] in ("welcome", "done")
        if s.get("page"):
            re.compile(s["page"])
    assert "How-to" in steps[-1]["text"] or "how-to" in steps[-1]["text"].lower()


def test_the_tour_and_its_doc_have_no_instance_names():
    for rel in ("app/tour_steps.yaml", "docs/getting-started.md", "app/static/js/tour.js", "app/templates/partials/tour.html"):
        with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
            assert "mal" + "len" not in fh.read().lower(), rel      # (spelled in two halves so this file does not trip the ratchet itself)


BAD_GO = ["//evil.com", "/\\evil.com", "/\t/evil", "/\n/evil", "/\r/evil", "/a\x00b", "/a\x7fb", "https://evil.com", "evil", "", None]


@pytest.mark.parametrize("bad", BAD_GO)
def test_safe_path_refuses_every_other_host_form(bad):
    assert tour.safe_path(bad) is False


def test_safe_path_accepts_a_path_on_this_site():
    for ok in ("/", "/x", "/first-project/releases", "/a/b?c=1#d"):
        assert tour.safe_path(ok) is True


@pytest.mark.parametrize("bad", ["/\\evil.com", "/\t/evil"])
def test_the_loader_and_the_renderer_refuse_them_too(bad, monkeypatch):
    # loader: a steps file whose `go` is one of them fails at load, not in a person's browser
    monkeypatch.setattr(tour, "_cache", None)
    monkeypatch.setattr(tour.yaml, "safe_load", lambda fh: {"steps": [{"id": "a", "title": "t", "text": "x", "go": bad}]})
    with pytest.raises(ValueError, match="path on this site"):
        tour.load()
    monkeypatch.setattr(tour, "_cache", None)

    # renderer: a `{project}` slug cannot smuggle one in (the template is fine, the filled-in path is not)
    class _Who:
        is_owner = True

    out = tour._render({"id": "b", "title": "t", "text": "x", "go": "/{project}"}, _Who(), {"project": bad[1:], "task": None, "runner": True})
    assert "go" not in out
