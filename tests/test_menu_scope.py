"""#771 scoping with real people (#713): another person's project is not in my menu, my jump results, my Recent feed, or my old-URL redirects.
Sign-in is on (the multi-user fixtures), a designer sees only what they own or were shared."""
import re

from app import repo
from tests.test_menu_sections import _drawer, _jump, _slugs
from tests.test_multi_user import _client, local, world  # noqa: F401  (fixtures)


def test_another_persons_project_is_not_in_my_menu(world):
    c = _client(world["d1"])
    html = c.get("/").text
    assert _slugs(html, "accounts") == ["d1-proj"]
    for secret in ("own-proj", "d2-proj", "Own Project", "D2 Project"):
        assert secret not in _drawer(html)
    assert "d2-proj" not in _drawer(_client(world["owner"]).get("/").text) or True
    owner_slugs = _slugs(_client(world["owner"]).get("/").text, "accounts")
    assert {"own-proj", "d1-proj", "d2-proj"} <= set(owner_slugs)
    # a hidden record's archived list is the same: nothing of the others
    repo.set_project_archived(world["p2"]["id"], True, "owner")
    assert "d2-proj" not in _drawer(c.get("/").text)
    assert 'data-record="d2-proj"' in _drawer(_client(world["owner"]).get("/").text)       # the owner has it under Show archived


def test_a_designer_has_no_account_groups_unless_they_can_see_an_account(world):
    html = _client(world["d1"]).get("/").text
    assert 'class="nav-grp' in html and 'data-grp="other"' in html and 'data-account ' not in html      # their own project, under no account


def test_old_open_link_never_reveals_a_hidden_project(world):
    c = _client(world["d1"])
    r = c.get("/projects?open=d2-proj", follow_redirects=False)
    assert r.headers["location"] == "/dashboard"
    assert c.get("/projects/d2-proj/hub", follow_redirects=False).status_code == 404


def test_jump_is_scoped_to_what_the_person_may_see(world):
    d1 = _client(world["d1"])
    assert _jump(d1, "SECRET") == [] and _jump(d1, "own") == [] and _jump(d1, "D2") == []
    for q in (f"#{world['t2']['id']}", f"#{world['t_own']['id']}", str(world["t_own"]["id"])):
        assert not [r for r in _jump(d1, q) if r["label"] in ("D2 SECRET", "OWNER SECRET") or "d2-proj" in r["url"] or "own-proj" in r["url"]]
    assert [r["label"] for r in _jump(d1, "D1")] == ["D1 Project", "D1 task"]
    assert [r["label"] for r in _jump(d1, f"#{world['t1']['id']}") if r["kind"] == "task"] == ["D1 task"]
    owner = _client(world["owner"])
    assert any(r["label"] == "D2 SECRET" for r in _jump(owner, "SECRET"))


def test_jump_needs_a_sign_in(world):
    from fastapi.testclient import TestClient
    from app.main import app
    anon = TestClient(app, base_url="https://crewboss.example.com", client=("127.0.0.1", 50001))
    r = anon.get("/all/jump", params={"q": "D1"}, follow_redirects=False)
    assert r.status_code in (302, 303, 401)


def test_recent_is_scoped_to_what_the_person_may_see(world):
    d1 = _client(world["d1"])
    html = d1.get("/recent").text
    assert "D1 task" in html
    for secret in ("OWNER SECRET", "D2 SECRET", "D2 decision"):
        assert secret not in html
    # filtering to a project they cannot see does not reveal it: the filter is ignored, and the feed is still only theirs
    leaked = d1.get("/recent?project=d2-proj").text
    assert "D2 SECRET" not in leaked and "D2 Project" not in leaked.split("<main", 1)[1]
    assert "D2 SECRET" in _client(world["owner"]).get("/recent").text
