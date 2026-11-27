"""Task #166: the owner's 'Move project' UI over the existing tree.set_place. Picker options, the POST /{slug}/move
route (owner-only, same-origin), the Change-log 'done' entry, and the validation refusals surfacing as a flash."""
import pytest

from app import changes, config, repo, tree
from app.routers.pages import _move_targets

OWN = {"Origin": "https://crewboss.example.com", "Sec-Fetch-Site": "same-origin"}
EVIL = {"Origin": "https://evil.example", "Sec-Fetch-Site": "cross-site"}


@pytest.fixture
def fam(monkeypatch):
    """dash (root) → Acme (account) → acme-app → acme-portal (grandchild); Acme → acme-site; a top-level 'other'."""
    monkeypatch.setattr(config, "OVERSEER_PROJECT", "dash-root")
    mk = lambda slug, name, roles=(): repo.create_project(slug, name, "internal", "owner",  # noqa: E731
                                                          short_name=slug, session_roles=list(roles))
    dash = mk("dash-root", "Dash", ["all"])
    acct = mk("acme", "Acme")
    tree.set_place(acct["id"], "owner", None, kind="account")
    app_ = mk("acme-app", "Acme App", ["all"])
    tree.set_place(app_["id"], "owner", acct["id"])
    portal = mk("acme-portal", "Acme Portal")
    tree.set_place(portal["id"], "owner", app_["id"])
    site = mk("acme-site", "Acme Site")
    tree.set_place(site["id"], "owner", acct["id"])
    other = mk("other", "Other", ["all"])
    return {"dash": dash, "acct": acct, "app": app_, "portal": portal, "site": site, "other": other}


# --- the picker options ------------------------------------------------------------------------------------------

def test_picker_excludes_self_and_descendants(fam):
    slugs = {t["slug"] for t in _move_targets(fam["app"])}
    # acme-app's own branch is out; everything else is a candidate parent
    assert "acme-app" not in slugs and "acme-portal" not in slugs
    assert slugs == {"dash-root", "acme", "acme-site", "other"}
    # each target carries its depth for the indent
    depth = {t["slug"]: t["depth"] for t in _move_targets(fam["app"])}
    assert depth["dash-root"] == 0 and depth["acme"] == 0 and depth["acme-site"] == 1


def test_root_has_no_move_targets(fam):
    assert _move_targets(fam["dash"]) is None


def test_overview_renders_the_picker(client, fam):
    html = client.get("/acme-app/settings").text
    assert 'data-move-project' in html and 'action="/acme-app/move"' in html
    block = html.split("data-move-project")[1].split("</section>")[0]
    assert "Acme Site" in block and "Top level (no parent)" in block   # valid parents offered
    assert "Acme Portal" not in block                                   # the descendant is not offered
    assert f'value="{fam["app"]["id"]}"' not in block                   # nor is the project itself
    # the dashboard root's overview offers no move block
    assert 'data-move-project' not in client.get("/dash-root/settings").text


# --- the POST route ----------------------------------------------------------------------------------------------

def test_owner_moves_a_project_and_logs_the_change(client, fam):
    r = client.post(f"/acme-site/move", data={"parent_id": str(fam["dash"]["id"])}, headers=OWN,
                    follow_redirects=False)
    assert r.status_code == 303
    assert "crewboss_flash" in r.headers.get("set-cookie", "")
    assert repo.get_project(fam["site"]["id"])["parent_id"] == fam["dash"]["id"]
    done = changes.grouped(fam["site"]["id"])["done"]
    assert any(c["title"] == "Moved to Dash" and c["decided_by"] == "owner" for c in done)


def test_move_to_top_level_sets_no_parent(client, fam):
    r = client.post(f"/other/move", data={"parent_id": ""}, headers=OWN, follow_redirects=False)
    assert r.status_code == 303
    assert repo.get_project(fam["other"]["id"])["parent_id"] is None


def test_cycle_is_refused_with_a_flash_not_a_500(client, fam):
    # move acme-app under its own descendant acme-portal
    r = client.post(f"/acme-app/move", data={"parent_id": str(fam["portal"]["id"])}, headers=OWN,
                    follow_redirects=False)
    assert r.status_code == 303 and "crewboss_flash" in r.headers.get("set-cookie", "")
    assert repo.get_project(fam["app"]["id"])["parent_id"] == fam["acct"]["id"]  # unchanged


def test_the_dashboard_project_moves_like_any_other_project(client, fam):
    """The tree has no project root (change #76): the dashboard project can sit under an account."""
    r = client.post(f"/dash-root/move", data={"parent_id": str(fam["acct"]["id"])}, headers=OWN,
                    follow_redirects=False)
    assert r.status_code == 303 and "crewboss_flash" in r.headers.get("set-cookie", "")
    assert repo.get_project(fam["dash"]["id"])["parent_id"] == fam["acct"]["id"]


def test_an_account_only_sits_at_the_top(client, fam):
    r = client.post(f"/acme/move", data={"parent_id": str(fam["other"]["id"])}, headers=OWN,
                    follow_redirects=False)
    assert r.status_code == 303 and "crewboss_flash" in r.headers.get("set-cookie", "")
    assert repo.get_project(fam["acct"]["id"])["parent_id"] is None  # unchanged


def test_cross_site_post_is_refused(client, fam):
    r = client.post(f"/acme-site/move", data={"parent_id": str(fam["dash"]["id"])}, headers=EVIL,
                    follow_redirects=False)
    assert r.status_code == 403
    assert repo.get_project(fam["site"]["id"])["parent_id"] == fam["acct"]["id"]  # unchanged
