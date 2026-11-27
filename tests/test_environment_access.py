"""Who may change a project's environment (#810 review): the rows run on OTHER people's PCs (a path a runner looks at, a program it looks for), so
adding, editing, removing and suggesting need a project Admin or Owner, not a Member. A Member still sees the tab and may check their own runner.
Sign-in is on (the multi-user fixtures)."""
from app import access, environment
from test_multi_user import H, _client, local, world  # noqa: F401  (fixtures)


def _row(world):
    return environment.add_row(world["p1"], "tool", "git")


def test_a_member_sees_the_tab_but_cannot_change_it(world):
    access.share(world["p1"]["id"], world["d2"]["id"], "member", None)
    row = _row(world)
    c = _client(world["d2"])
    page = c.get("/d1-proj/environment")
    assert page.status_code == 200 and "data-environment-page" in page.text
    for control in ("data-suggest", "data-add-requirement", "data-edit-row", "data-delete-row"):
        assert control not in page.text, control
    for path, data in (("/d1-proj/environment/rows", {"kind": "tool", "name": "java"}),
                       (f"/d1-proj/environment/rows/{row['id']}", {"kind": "tool", "name": "node"}),
                       (f"/d1-proj/environment/rows/{row['id']}/delete", {}), ("/d1-proj/environment/suggest", {})):
        r = c.post(path, data=data, headers=H)
        assert r.status_code in (403, 404), path
    assert [r["name"] for r in environment.list_rows(world["p1"]["id"])] == ["git"]


def test_the_projects_owner_and_an_admin_can(world):
    access.share(world["p1"]["id"], world["d2"]["id"], "admin", None)
    for who in ("d1", "d2"):
        c = _client(world[who])
        page = c.get("/d1-proj/environment").text
        assert "data-suggest" in page and "data-add-requirement" in page, who
        r = c.post("/d1-proj/environment/rows", data={"kind": "tool", "name": "java" if who == "d1" else "node"}, headers=H, follow_redirects=False)
        assert r.status_code == 303, who
    assert {r["name"] for r in environment.list_rows(world["p1"]["id"])} == {"java", "node"}


def test_a_stranger_gets_a_404_not_a_form(world):
    c = _client(world["d2"])
    assert c.get("/d1-proj/environment").status_code == 404
    assert c.post("/d1-proj/environment/rows", data={"kind": "tool", "name": "java"}, headers=H).status_code == 404
