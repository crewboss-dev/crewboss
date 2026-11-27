"""Materials (task #295): shared resources per account/project, inherited down the tree (nearest slot wins), files in a
plain folder with what each is for in the database; secrets refused; served sandboxed; MCP list/get/add."""
import pytest

from app import config, materials as mat, mcp_server, repo, tree
from app.auth import issue_token

SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect width="10" height="10"/></svg>'
OWN = {"Origin": "https://crewboss.example.com", "Sec-Fetch-Site": "same-origin"}


@pytest.fixture
def fam(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MATERIALS_DIR", str(tmp_path))
    monkeypatch.setattr(config, "MATERIALS_HOST_DIR", "/host/materials")
    acct = repo.create_project("acme", "Acme", "client", "owner", short_name="acme", session_roles=["all"])
    tree.set_place(acct["id"], "owner", None, "account")
    app_ = repo.create_project("acme-app", "Acme App", "client", "owner", short_name="acmeapp", session_roles=[])
    tree.set_place(app_["id"], "owner", acct["id"], "project")
    other = repo.create_project("other", "Other", "client", "owner", short_name="oth", session_roles=["all"])
    return {"acct": repo.get_project(acct["id"]), "app": repo.get_project(app_["id"]), "other": other,
            "dir": tmp_path}


def test_inherit_and_override_nearest_wins(fam):
    a = mat.add(fam["acct"], "logo.primary", "logo", "Acme logo", "Every header", "logo.svg", SVG, "owner")
    mat.add(fam["acct"], "email.signature", "email-template", "Signature", "All emails", "sig.html", b"<p>Hi</p>",
            "owner")
    eff = {r["slot"]: r for r in mat.effective(fam["app"])}
    assert eff["logo.primary"]["id"] == a["id"] and eff["logo.primary"]["inherited"]
    assert eff["logo.primary"]["host_path"] == "/host/materials/acme/logo.primary/v1-logo.svg"
    assert (fam["dir"] / "acme/logo.primary/v1-logo.svg").read_bytes() == SVG
    own = mat.add(fam["app"], "logo.primary", "logo", "App logo", "The sub-app header", "app.svg", SVG + b" ", "owner")
    eff = {r["slot"]: r for r in mat.effective(fam["app"])}
    assert eff["logo.primary"]["id"] == own["id"] and not eff["logo.primary"]["inherited"]
    assert eff["email.signature"]["inherited"]
    assert [r["slot"] for r in mat.overridden(fam["app"])] == ["logo.primary"]
    # the account itself still sees its own
    assert {r["slot"]: r["id"] for r in mat.effective(fam["acct"])}["logo.primary"] == a["id"]


def test_replace_keeps_history_and_identical_is_noop(fam):
    v1 = mat.add(fam["acct"], "logo.primary", "logo", "Logo", "Header", "logo.svg", SVG, "owner")
    same = mat.add(fam["acct"], "logo.primary", "logo", "Logo", "Header", "logo.svg", SVG, "owner")
    assert same["id"] == v1["id"]
    v2 = mat.add(fam["acct"], "logo.primary", "logo", "Logo", "Header", "logo.svg", SVG + b"\n", "owner")
    assert v2["version"] == 2
    old = mat.get(v1["id"])
    assert old["replaced_at"] is not None and old["replaced_by"] == v2["id"]
    assert (fam["dir"] / old["rel_path"]).exists()


@pytest.mark.parametrize("name,raw", [
    ("server.pem", b"x"), ("prod.env", b"A=1"), ("id_ed25519", b"x"), ("notes.txt", b"-----BEGIN OPENSSH PRIVATE KEY"),
    ("tool.exe", b"MZ"), ("empty.svg", b""),
])
def test_refuses_secrets_bad_types_and_empty(fam, name, raw):
    with pytest.raises(repo.BadRequest):
        mat.add(fam["acct"], "x", "other", "X", "Y", name, raw, "owner")


def test_bad_slot_kind_and_missing_purpose(fam):
    for slot, kind, purpose in [("Logo Primary", "logo", "p"), ("logo", "banner", "p"), ("logo", "logo", " ")]:
        with pytest.raises(repo.BadRequest):
            mat.add(fam["acct"], slot, kind, "T", purpose, "a.svg", SVG, "owner")


def test_page_upload_and_sandboxed_file(client, fam):
    r = client.post("/acme/materials", headers=OWN, files={"file": ("logo.svg", SVG, "image/svg+xml")},
                    data={"slot": "logo.primary", "kind": "logo", "title": "Acme logo", "purpose": "Headers"},
                    follow_redirects=False)
    assert r.status_code == 303
    page = client.get("/acme-app/materials").text
    assert 'data-material="logo.primary"' in page and 'data-inherited-from="acme"' in page
    mid = mat.effective(fam["app"])[0]["id"]
    f = client.get(f"/media/materials/{mid}/logo.svg")
    assert f.status_code == 200 and f.content == SVG
    assert "sandbox" in f.headers["content-security-policy"] and f.headers["x-content-type-options"] == "nosniff"
    assert client.get(f"/media/materials/{mid}/other.svg").status_code == 404
    bad = client.post("/acme/materials", headers=OWN, files={"file": ("k.pem", b"x", "text/plain")},
                      data={"slot": "k", "kind": "other", "title": "K", "purpose": "P"})
    assert bad.status_code == 400 and "data-material-error" in bad.text


def test_mcp_list_get_add_and_isolation(fam, mcp_caller):
    mat.add(fam["acct"], "logo.primary", "logo", "Acme logo", "Headers", "logo.svg", SVG, "owner")
    tok = issue_token("all", "acme", project_id=fam["acct"]["id"])
    mcp_caller(tok)
    rows = mcp_server.list_materials(project="acme-app")
    assert [r["slot"] for r in rows] == ["logo.primary"] and rows[0]["inherited"]
    assert mcp_server.get_material(rows[0]["id"])["host_path"].startswith("/host/materials/")
    (fam["dir"] / "_inbox").mkdir()
    (fam["dir"] / "_inbox" / "sig.html").write_bytes(b"<p>Regards</p>")
    out = mcp_server.add_material("email.signature", "email-template", "Signature", "Every email",
                                  "/host/materials/_inbox/sig.html", project="acme")
    assert out["version"] == 1 and not (fam["dir"] / "_inbox" / "sig.html").exists()
    with pytest.raises(repo.BadRequest):
        mcp_server.add_material("x", "other", "X", "Y", "/etc/passwd", project="acme")
    # another account's session can't read Acme's materials
    mcp_caller(issue_token("all", "oth", project_id=fam["other"]["id"]))
    with pytest.raises(repo.NotFound):
        mcp_server.get_material(rows[0]["id"])
    with pytest.raises(repo.NotFound):
        mcp_server.list_materials(project="acme")


def test_ground_lists_materials(fam, mcp_caller):
    mat.add(fam["acct"], "logo.primary", "logo", "Acme logo", "Headers", "logo.svg", SVG, "owner")
    g = repo._ground_materials([fam["app"]])
    assert g == [{"project": "acme-app", "slots": ["logo.primary (Acme logo)"]}]


def test_order_logos_first_then_natural_slots(fam):
    for slot, kind in [("favicon.180", "icon"), ("favicon.16", "icon"), ("brand.guide", "other"), ("logo.primary", "logo")]:
        mat.add(fam["acct"], slot, kind, slot, "p", f"{slot.replace('.', '-')}.png", slot.encode() + b"\x89", "owner")
    assert [r["slot"] for r in mat.effective(fam["acct"])] == ["logo.primary", "favicon.16", "favicon.180",
                                                               "brand.guide"]
