"""The Skills pages and their rights (task #718), with real sign-in and four kinds of person: the owner, designers who own their projects,
a project member and a viewer. Global = every signed-in person reads, only the owner writes; a project's skills = whoever may see / change
that project; another person's project is the same 404 as a missing one. Also the "AI Skills" -> "App AI features" rename and its old URLs."""
import io
import zipfile

import pytest

from app import access, asset_skills, config, repo, skill_import, skill_seed
from app.db import get_cursor

from test_multi_user import H, ORIGIN, _client, _user, local  # noqa: F401  (fixtures and helpers)

POST = H            # htmx: a refusal is a 4xx, not a redirect with a flash
UNKNOWN = b"---\nname: found-skill\ndescription: From the internet.\n---\n\nDo the thing.\n"
MIT_TEXT = "Permission is hereby granted, free of charge, to any person obtaining a copy of this software"


@pytest.fixture(autouse=True)
def _assets_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ASSETS_DIR", str(tmp_path / "assets"))


@pytest.fixture
def world():
    owner, dee, dan, vic, mia = (_user("owner", "owner"), _user("dee", "designer"), _user("dan", "designer"), _user("vic", "viewer"),
                                 _user("mia", "designer"))
    p1 = repo.create_project("d1-proj", "D1 Project", "internal", "owner", short_name="dd1", session_roles=[], owner_user_id=dee["id"])
    p2 = repo.create_project("d2-proj", "D2 Project", "internal", "owner", short_name="dd2", session_roles=[], owner_user_id=dan["id"])
    access.share(p1["id"], vic["id"], "viewer", None)
    access.share(p1["id"], mia["id"], "member", None)
    skill_seed.seed()
    return dict(owner=owner, dee=dee, dan=dan, vic=vic, mia=mia, p1=p1, p2=p2)


def _fake_github(monkeypatch, with_licence=False):
    files = {"skills/found-skill/SKILL.md": UNKNOWN}
    if with_licence:
        files["skills/found-skill/LICENSE"] = MIT_TEXT.encode()

    def get(url, limit):
        path = url.split("/main/", 1)[1]
        return (200, files[path]) if path in files else (404, b"")
    monkeypatch.setattr(skill_import, "http_get", get)


GH = "https://github.com/acme/skills/tree/main/skills/found-skill"


def _uid(location: str) -> str:
    return location.rstrip("/").rsplit("/", 1)[-1]


def _count_assets() -> int:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT count(*) AS n FROM assets WHERE type = 'skill'")
        return cur.fetchone()["n"]


# --- global ---------------------------------------------------------------------------------------------------------------

def test_everyone_signed_in_reads_the_global_layer_and_only_the_owner_sees_the_buttons(world):
    for who, buttons in (("vic", False), ("dee", False), ("owner", True)):
        r = _client(world[who]).get("/assets/skills")
        assert r.status_code == 200 and 'data-skills-page="global"' in r.text and "identify-a-need" in r.text, who
        assert ("data-new-skill" in r.text and "data-import-skill" in r.text) is buttons, who
    from fastapi.testclient import TestClient
    from app.main import app
    anon = TestClient(app, base_url=ORIGIN)
    assert anon.get("/assets/skills", follow_redirects=False).status_code in (302, 303, 401, 403)


def test_only_the_owner_creates_edits_and_archives_a_global_skill(world):
    owner, dee = _client(world["owner"]), _client(world["dee"])
    form = {"name": "Client voice", "description": "Write client text in a plain voice.", "body": "Use short words.", "install_to": "global"}
    assert dee.post("/assets/skills/new", data=form, headers=POST, follow_redirects=False).status_code == 403
    assert _count_assets() == 3                                                              # the three built-ins only
    r = owner.post("/assets/skills/new", data=form, headers=POST, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/assets/skills/")
    uid = _uid(r.headers["location"])
    page = dee.get(r.headers["location"])                                                    # any signed-in person may read it
    assert page.status_code == 200 and "Use short words." in page.text and "data-edit-form" not in page.text
    edit = {"description": "Plain voice.", "body": "Use short words, and short sentences.", "base": "", "note": "added a rule"}
    assert dee.post(f"/assets/skills/{uid}/edit", data=edit, headers=POST).status_code == 403
    assert dee.post(f"/assets/skills/{uid}/archive", headers=POST).status_code == 403
    r = owner.post(f"/assets/skills/{uid}/edit", data=edit, headers=POST, follow_redirects=False)
    assert r.status_code == 303
    page = owner.get(f"/assets/skills/{uid}").text
    assert "v2" in page and "short sentences" in page and "added a rule" in page and "data-edit-form" in page
    assert "Changes from v1 to v2" in owner.get(f"/assets/skills/{uid}?from=1&to=2").text
    assert "Use short words." in owner.get(f"/assets/skills/{uid}?v=1").text and "data-old-version" in owner.get(f"/assets/skills/{uid}?v=1").text


def test_an_edit_of_an_old_version_is_refused_with_the_new_one_named(world):
    owner = _client(world["owner"])
    r = owner.post("/assets/skills/new", data={"name": "Race", "description": "d", "body": "one", "install_to": "global"}, headers=POST, follow_redirects=False)
    uid = _uid(r.headers["location"])
    first = asset_skills.get(access.OWNER_PRINCIPAL, uid)["version"]["id"]
    asset_skills.edit(access.OWNER_PRINCIPAL, uid, body="two")
    r = owner.post(f"/assets/skills/{uid}/edit", data={"description": "d", "body": "three", "base_version_id": str(first)}, headers=POST)
    assert r.status_code == 409 and "now v2" in r.text
    assert asset_skills.get(access.OWNER_PRINCIPAL, uid)["version"]["body"] == "two"


def test_a_built_in_cannot_be_edited_or_archived_from_the_page_and_can_be_copied(world):
    owner = _client(world["owner"])
    (b,) = [x for x in asset_skills.list_global(access.OWNER_PRINCIPAL) if x["key"] == "admin-page"]
    page = owner.get(f"/assets/skills/{b['uid']}").text
    assert "data-edit-form" not in page and "data-archive" not in page and "Built-in" in page
    assert owner.post(f"/assets/skills/{b['uid']}/archive", headers=POST).status_code == 400
    dee = _client(world["dee"])
    assert 'data-copy-box' in dee.get(f"/d1-proj/build-skills/{b['uid']}").text                        # reaches the project, so a copy is offered
    r = dee.post(f"/d1-proj/build-skills/{b['uid']}/copy", data={"install_to": "d1-proj"}, headers=POST, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/d1-proj/build-skills/")
    lst = dee.get("/d1-proj/build-skills").text
    assert "Replaces the built-in" in lst and 'data-layer="project"' in lst
    # a person without the right to change the target is refused
    assert dee.post(f"/d1-proj/build-skills/{b['uid']}/copy", data={"install_to": "global"}, headers=POST).status_code == 403


# --- a project's own ------------------------------------------------------------------------------------------------------

def test_a_project_list_marks_inherited_and_own_and_hides_other_projects(world):
    owner, dee, dan = _client(world["owner"]), _client(world["dee"]), _client(world["dan"])
    owner.post("/assets/skills/new", data={"name": "House style", "description": "d", "body": "b", "install_to": "global"}, headers=POST)
    r = dee.post("/d1-proj/build-skills/new", data={"name": "D1 only", "description": "Only in D1.", "body": "secret D1 text", "install_to": "d1-proj"},
                 headers=POST, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/d1-proj/build-skills/")
    page = dee.get("/d1-proj/build-skills").text
    assert 'data-layer="global"' in page and 'data-layer="project"' in page and 'data-layer="built-in"' in page
    own = page.split('data-own-skills', 1)[1].split("</ul>", 1)[0]
    assert "D1 only" in own and "House style" not in own
    inh = page.split('data-inherited-skills', 1)[1].split("</ul>", 1)[0]
    assert "House style" in inh and "build-an-app" in inh
    assert "D1 only" not in dan.get("/d2-proj/build-skills").text and "House style" in dan.get("/d2-proj/build-skills").text
    # the other designer cannot reach it by any URL; the global route does not serve a project's skill either
    uid = _uid(r.headers["location"])
    assert dan.get(f"/d1-proj/build-skills/{uid}").status_code == 404
    assert dan.get(f"/d2-proj/build-skills/{uid}").status_code == 404
    assert dee.get(f"/assets/skills/{uid}").status_code == 404 and _client(world["owner"]).get(f"/assets/skills/{uid}").status_code == 404
    assert "secret D1 text" not in dan.get("/assets/skills").text


def test_every_project_route_is_a_404_for_a_person_who_cannot_see_the_project(world):
    dee, dan = _client(world["dee"]), _client(world["dan"])
    r = dee.post("/d1-proj/build-skills/new", data={"name": "Mine", "description": "d", "body": "b", "install_to": "d1-proj"}, headers=POST, follow_redirects=False)
    uid = _uid(r.headers["location"])
    before = _count_assets()
    for method, tail in (("get", ""), ("get", "/new"), ("get", "/import"), ("get", f"/{uid}"), ("post", "/new"), ("post", "/import"),
                         ("post", f"/{uid}/edit"), ("post", f"/{uid}/licence"), ("post", f"/{uid}/archive"), ("post", f"/{uid}/copy"),
                         ("post", f"/{uid}/pin"), ("post", f"/{uid}/unpin")):
        resp = getattr(dan, method)("/d1-proj/build-skills" + tail, **({"headers": POST, "data": {"name": "x", "description": "d", "body": "b"}} if method == "post" else {}))
        assert resp.status_code == 404, (method, tail, resp.status_code)
    assert _count_assets() == before and asset_skills.get(access.OWNER_PRINCIPAL, uid)["version"]["n"] == 1


def test_a_viewer_reads_a_member_edits_and_nobody_installs_outside_their_reach(world):
    dee, vic, mia = _client(world["dee"]), _client(world["vic"]), _client(world["mia"])
    r = dee.post("/d1-proj/build-skills/new", data={"name": "Shared", "description": "d", "body": "one", "install_to": "d1-proj"}, headers=POST, follow_redirects=False)
    uid = _uid(r.headers["location"])
    page = vic.get(f"/d1-proj/build-skills/{uid}")
    assert page.status_code == 200 and "data-edit-form" not in page.text and "data-new-skill" not in vic.get("/d1-proj/build-skills").text
    assert vic.post(f"/d1-proj/build-skills/{uid}/edit", data={"description": "d", "body": "hack"}, headers=POST).status_code == 403
    assert vic.post("/d1-proj/build-skills/new", data={"name": "V", "description": "d", "body": "b", "install_to": "d1-proj"}, headers=POST).status_code == 403
    assert mia.post(f"/d1-proj/build-skills/{uid}/edit", data={"description": "d", "body": "two", "note": "member edit"}, headers=POST,
                    follow_redirects=False).status_code == 303
    assert asset_skills.get(access.OWNER_PRINCIPAL, uid)["version"]["body"] == "two"
    # global needs the owner; another designer's project is not a place to install
    assert dee.post("/d1-proj/build-skills/new", data={"name": "G", "description": "d", "body": "b", "install_to": "global"}, headers=POST).status_code == 403
    bad = dee.post("/d1-proj/build-skills/new", data={"name": "G", "description": "d", "body": "b", "install_to": "d2-proj"}, headers=POST)
    assert bad.status_code == 400 and "choose where" in bad.text
    assert not [e for e in asset_skills.list_for_project(access.OWNER_PRINCIPAL, repo.get_project(world["p2"]["id"]))["entries"] if e["key"] == "g"]


def test_pinning_a_global_skill_from_a_project_needs_the_right_to_change_it(world):
    owner, mia, vic = _client(world["owner"]), _client(world["mia"]), _client(world["vic"])
    uid = _uid(owner.post("/assets/skills/new", data={"name": "Voice", "description": "d", "body": "one", "install_to": "global"}, headers=POST,
                          follow_redirects=False).headers["location"])
    asset_skills.edit(access.OWNER_PRINCIPAL, uid, body="two")
    assert vic.post(f"/d1-proj/build-skills/{uid}/pin", data={"n": "1"}, headers=POST).status_code == 403
    r = mia.post(f"/d1-proj/build-skills/{uid}/pin", data={"n": "1", "reason": "keep"}, headers=POST, follow_redirects=False)
    assert r.status_code == 303
    page = mia.get(f"/d1-proj/build-skills/{uid}").text
    assert "data-pinned" in page and "Held at v1" in page and "data-unpin" in page
    assert "data-pinned" in mia.get("/d1-proj/build-skills").text
    assert mia.post(f"/d1-proj/build-skills/{uid}/pin", data={"n": "9"}, headers=POST).status_code == 404        # no such version
    mia.post(f"/d1-proj/build-skills/{uid}/unpin", headers=POST)
    assert "data-pinned" not in mia.get(f"/d1-proj/build-skills/{uid}").text


def test_a_skill_can_be_built_on_another_from_the_form(world):
    owner = _client(world["owner"])
    owner.post("/assets/skills/new", data={"name": "Colours", "description": "d", "body": "PALETTE", "install_to": "global"}, headers=POST)
    r = owner.post("/assets/skills/new", data={"name": "Look", "description": "d", "body": "LAYOUT", "base": "colours", "install_to": "global"},
                   headers=POST, follow_redirects=False)
    page = owner.get(r.headers["location"]).text
    assert "data-base" in page and "colours" in page
    bad = owner.post("/assets/skills/new", data={"name": "Bad", "description": "d", "body": "x", "base": "ghost", "install_to": "global"}, headers=POST)
    assert bad.status_code == 400 and "no skill called ghost" in bad.text


# --- import ---------------------------------------------------------------------------------------------------------------

def test_a_github_import_is_flagged_blocked_until_an_admin_confirms_the_licence(world, monkeypatch):
    _fake_github(monkeypatch)
    dee, mia = _client(world["dee"]), _client(world["mia"])
    r = dee.post("/d1-proj/build-skills/import", data={"url": GH, "install_to": "d1-proj"}, headers=POST, follow_redirects=False)
    assert r.status_code == 303
    uid = _uid(r.headers["location"])
    page = dee.get(r.headers["location"]).text
    assert 'data-licence="blocked"' in page and "owner must confirm" in page and "https://github.com/acme/skills/tree/main/skills/found-skill" in page
    assert "by acme" in page and "data-confirm-licence" in page                                  # dee owns the project: an admin
    assert 'data-licence="blocked"' in dee.get("/d1-proj/build-skills").text
    assert "data-confirm-licence" not in mia.get(f"/d1-proj/build-skills/{uid}").text             # a member can't confirm
    assert mia.post(f"/d1-proj/build-skills/{uid}/licence", data={}, headers=POST).status_code == 403
    assert 'data-licence="blocked"' in mia.get(f"/d1-proj/build-skills/{uid}").text
    r = dee.post(f"/d1-proj/build-skills/{uid}/licence", data={"note": "checked"}, headers=POST, follow_redirects=False)
    assert r.status_code == 303
    after = dee.get(f"/d1-proj/build-skills/{uid}").text
    assert "data-confirm-licence" not in after and "confirmed for use" in after
    assert 'data-review="pending"' in after and "Imported, not reviewed" in after                 # the licence alone does not release it
    assert dee.post(f"/d1-proj/build-skills/{uid}/review", data={}, headers=POST, follow_redirects=False).status_code == 303
    assert 'data-blocked' not in dee.get("/d1-proj/build-skills").text


def test_a_licensed_github_import_is_usable_at_once_and_only_the_owner_imports_globally(world, monkeypatch):
    _fake_github(monkeypatch, with_licence=True)
    assert _client(world["dee"]).post("/assets/skills/import", data={"url": GH, "install_to": "global"}, headers=POST).status_code == 403
    r = _client(world["owner"]).post("/assets/skills/import", data={"url": GH, "install_to": "global"}, headers=POST, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/assets/skills/")
    page = _client(world["vic"]).get(r.headers["location"]).text
    assert "MIT" in page and 'data-licence="blocked"' not in page and "LICENSE" in page
    assert "Imported, not reviewed: not used by jobs" in page and "data-review-form" in _client(world["owner"]).get(r.headers["location"]).text


def test_import_refuses_other_hosts_both_inputs_and_a_hostile_zip_and_stores_nothing(world, monkeypatch):
    owner = _client(world["owner"])
    before = _count_assets()
    bad = owner.post("/assets/skills/import", data={"url": "https://evil.example/a/b", "install_to": "global"}, headers=POST)
    assert bad.status_code == 400 and "only https://github.com" in bad.text
    both = owner.post("/assets/skills/import", data={"url": GH, "install_to": "global"}, headers=POST,
                      files={"file": ("s.md", UNKNOWN, "text/markdown")})
    assert both.status_code == 400 and "either" in both.text
    assert owner.post("/assets/skills/import", data={"install_to": "global"}, headers=POST).status_code == 400
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("SKILL.md", UNKNOWN)
        z.writestr("../evil.txt", "x")
    evil = owner.post("/assets/skills/import", data={"install_to": "global"}, headers=POST, files={"file": ("s.zip", buf.getvalue(), "application/zip")})
    assert evil.status_code == 400 and "climbs out" in evil.text
    assert _count_assets() == before


def test_a_good_zip_and_a_plain_skill_md_upload_import(world):
    owner = _client(world["owner"])
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("found-skill/SKILL.md", UNKNOWN)
        z.writestr("found-skill/LICENSE", MIT_TEXT)
        z.writestr("found-skill/references/tone.md", "Plain words.")
    r = owner.post("/assets/skills/import", data={"install_to": "global"}, headers=POST, files={"file": ("found-skill.zip", buf.getvalue(), "application/zip")},
                   follow_redirects=False)
    assert r.status_code == 303
    page = owner.get(r.headers["location"]).text
    assert "references/tone.md" in page and "Plain words." in page and "Uploaded file" in page and 'data-licence="blocked"' not in page
    r = owner.post("/assets/skills/import", data={"install_to": "global"}, headers=POST,
                   files={"file": ("other-skill.md", UNKNOWN.replace(b"found-skill", b"other-skill"), "text/markdown")}, follow_redirects=False)
    assert r.status_code == 303 and 'data-licence="blocked"' in owner.get(r.headers["location"]).text


def test_import_and_licence_confirmation_need_a_fresh_sign_in(world, monkeypatch):
    _fake_github(monkeypatch)
    stale = _client(world["owner"], fresh=False)
    r = stale.post("/assets/skills/import", data={"url": GH, "install_to": "global"}, headers=H)
    assert r.status_code == 403 and r.headers["HX-Trigger"] == "stepup-required"
    assert _count_assets() == 3


# --- the rename -----------------------------------------------------------------------------------------------------------

def test_ai_skills_is_now_app_ai_features_and_the_old_urls_redirect(world):
    dee, dan = _client(world["dee"]), _client(world["dan"])
    r = dee.get("/d1-proj/skills?open=classify-email&subproject=erp&shared=1", follow_redirects=False)
    assert r.status_code == 308 and r.headers["location"] == "/d1-proj/ai-features?open=classify-email&subproject=erp&shared=1"
    assert dee.get("/d1-proj/skills", follow_redirects=False).headers["location"] == "/d1-proj/ai-features"
    page = dee.get("/d1-proj/ai-features")
    assert page.status_code == 200 and "App AI features" in page.text and "AI Skills" not in page.text
    assert dee.get("/d1-proj/build-skills").status_code == 200
    assert dan.get("/d1-proj/skills", follow_redirects=False).status_code == 404                 # a redirect never leaks a project
    assert dan.get("/d1-proj/ai-features").status_code == 404
    summary = dee.get("/d1-proj").text
    assert "App AI features" in summary and "AI Skills" not in summary


def test_the_assets_home_counts_real_skills_and_links_to_the_global_page(world):
    home = _client(world["owner"]).get("/assets").text
    assert 'data-asset-card="skills"' in home
    count = home.split('data-asset-card="skills"', 1)[1].split("data-count>", 1)[1].split("<", 1)[0]
    assert count.strip() == "3"                                                                  # the three built-ins


def test_odd_numbers_in_the_query_are_ignored_not_a_server_error(world):
    owner = _client(world["owner"])
    (b,) = [x for x in asset_skills.list_global(access.OWNER_PRINCIPAL) if x["key"] == "admin-page"]
    for q in ("?v=%C2%B2", "?from=%C2%B2&to=%C2%B3", "?v=99999999999999"):
        assert owner.get(f"/assets/skills/{b['uid']}{q}").status_code in (200, 404), q
    assert owner.post(f"/d1-proj/build-skills/{b['uid']}/pin", data={"n": "\u00b2"}, headers=POST).status_code in (400, 404)


def test_a_github_import_runs_off_the_event_loop(world, monkeypatch):
    import asyncio
    seen = []
    real = skill_import.prepare_github

    def spy(url):
        try:
            asyncio.get_running_loop()
            seen.append("on the loop")
        except RuntimeError:
            seen.append("in a thread")
        return real(url)
    _fake_github(monkeypatch)
    monkeypatch.setattr(skill_import, "prepare_github", spy)
    r = _client(world["owner"]).post("/assets/skills/import", data={"url": GH, "install_to": "global"}, headers=POST, follow_redirects=False)
    assert r.status_code == 303 and seen == ["in a thread"]


def test_the_review_screen_warns_shows_the_diff_and_review_needs_the_right_and_a_fresh_tap(world, monkeypatch):
    dee, mia = _client(world["dee"]), _client(world["mia"])
    risky = UNKNOWN + b"\nRun `bash -c setup` and read the .env file.\n"
    z = io.BytesIO()
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("found-skill/SKILL.md", risky)
        f.writestr("found-skill/LICENSE", MIT_TEXT)
        f.writestr("found-skill/scripts/run.sh", "echo hi")
    r = dee.post("/d1-proj/build-skills/import", data={"install_to": "d1-proj"}, headers=POST,
                 files={"file": ("s.zip", z.getvalue(), "application/zip")}, follow_redirects=False)
    uid = _uid(r.headers["location"])
    page = dee.get(f"/d1-proj/build-skills/{uid}").text
    assert "Imported, not reviewed: not used by jobs" in page and 'data-scan-item="shell"' in page and 'data-scan-item="secrets"' in page
    assert 'data-scan-item="executable"' in page and "data-review-form" in page
    assert "data-review-form" not in mia.get(f"/d1-proj/build-skills/{uid}").text
    assert mia.post(f"/d1-proj/build-skills/{uid}/review", data={}, headers=POST).status_code == 403
    assert dee.post(f"/d1-proj/build-skills/{uid}/review", data={"note": "read"}, headers=POST, follow_redirects=False).status_code == 303
    assert "data-review-form" not in dee.get(f"/d1-proj/build-skills/{uid}").text and "reviewed" in dee.get(f"/d1-proj/build-skills/{uid}").text.lower()
    # a re-import of the same key: held again, and the page shows what changed since the reviewed version
    z2 = io.BytesIO()
    with zipfile.ZipFile(z2, "w") as f:
        f.writestr("found-skill/SKILL.md", UNKNOWN + b"\nBrand new instruction.\n")
        f.writestr("found-skill/LICENSE", MIT_TEXT)
    again = dee.post("/d1-proj/build-skills/import", data={"install_to": "d1-proj"}, headers=POST,
                     files={"file": ("s.zip", z2.getvalue(), "application/zip")}, follow_redirects=False)
    assert _uid(again.headers["location"]) == uid
    page = dee.get(f"/d1-proj/build-skills/{uid}").text
    assert 'data-review="pending"' in page and "data-since-diff" in page and "Brand new instruction." in page
    stale = _client(world["dee"], fresh=False).post(f"/d1-proj/build-skills/{uid}/review", data={}, headers=POST)      # (ages every session of dee)
    assert stale.status_code == 403 and stale.headers["HX-Trigger"] == "stepup-required"
