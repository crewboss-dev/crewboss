"""Assets P2 (task #778): PARITY. The resolver, fed fixture data imported by the test-only importer (tests/asset_helpers.py),
must give each project exactly what TODAY's code gives it: materials.effective along tree.inherit_chain, and shared.inherited
(knowledge kinds skill / rule on the account). This is the gate for P3's real import and P4's reader switch."""
import pytest

from app import asset_resolve as ar, config, materials as mat, repo, shared, tree
from tests.asset_helpers import import_materials, import_shared_knowledge, make_tree

SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect width="10" height="10"/></svg>'


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "MATERIALS_DIR", str(tmp_path / "materials"))
    monkeypatch.setattr(config, "MATERIALS_HOST_DIR", "/host/materials")
    monkeypatch.setattr(config, "ASSETS_DIR", str(tmp_path / "materials"))
    t = make_tree()
    leaf = repo.create_project("leaf", "Leaf", "client", "owner", short_name="leaf", session_roles=[])
    tree.set_place(leaf["id"], "owner", t["sub"]["id"], "project")
    t["leaf"] = repo.get_project(leaf["id"])
    return t


def _mat(p, slot, title, raw=SVG, kind="logo", fn="f.svg", notes=""):
    return mat.add(p, slot, kind, title, f"for {slot}", fn, raw, "owner", usage_notes=notes)


def _populate_materials(t):
    _mat(t["acme"], "logo.primary", "Acme logo", SVG)
    _mat(t["acme"], "logo.primary", "Acme logo v2", SVG + b" ")                   # a replaced slot: 2 versions
    _mat(t["acme"], "email.signature", "Signature", b"<p>Hi</p>", "email-template", "sig.html", "use as is")
    _mat(t["acme"], "favicon.32", "Acme favicon", SVG + b"1", "icon")
    _mat(t["app"], "logo.primary", "App logo", SVG + b"2")                        # overrides the account's
    _mat(t["app"], "header.bg", "Header", SVG + b"3", "image")
    _mat(t["sub"], "header.bg", "Sub header", SVG + b"4", "image")                # overrides its parent's
    _mat(t["sub"], "logo.primary", "Sub logo", SVG + b"5")                        # 3 levels deep: sub, app, account
    _mat(t["leaf"], "logo.primary", "Leaf logo", SVG + b"6")                      # 4 levels: leaf, sub, app, account
    _mat(t["app2"], "favicon.32", "App2 favicon", SVG + b"7", "icon")
    _mat(t["beta"], "logo.primary", "Beta logo", SVG + b"8")                      # another account: never leaks
    _mat(t["bapp"], "own.only", "Beta app", b"x", "other", "x.txt")
    _mat(t["solo"], "logo.primary", "Solo logo", SVG + b"9")
    _mat(t["solo"], "email.signature", "Solo sig", b"<p>solo</p>", "email-template", "s.html")


def _old_materials(p):
    return {r["slot"]: (r["sha256"], r["kind"], r["title"], r["purpose"], r["usage_notes"], r["from"]["id"], r["inherited"])
            for r in mat.effective(p)}


def _new_materials(p):
    rs = ar.resolve(p["id"], types=("material",))
    h = ar.hydrate(rs.entries)
    out = {}
    for e in rs.entries:
        f = h[e.version_id]
        out[e.key] = (f["files"][0]["sha256"], f["fields"]["kind"], f["fields"]["title"], f["fields"]["purpose"],
                      f["fields"].get("usage_notes", ""), e.owner["project_id"], e.owner["project_id"] != p["id"])
    return out, rs


def test_materials_effective_parity_for_every_project(world):
    _populate_materials(world)
    import_materials()
    checked = 0
    for p in tree.index().values():
        new, _ = _new_materials(p)
        assert new == _old_materials(p), p["slug"]
        checked += len(new)
    assert checked > 15
    # spot checks of the interesting cases
    assert _old_materials(world["leaf"])["header.bg"][5] == world["sub"]["id"]
    assert _new_materials(world["leaf"])[0]["favicon.32"][5] == world["acme"]["id"]       # inherited from the account
    assert "own.only" not in _new_materials(world["app"])[0]
    assert _new_materials(world["bapp"])[0]["logo.primary"][5] == world["beta"]["id"]     # its own account's, not Acme's


def test_materials_versions_survive_the_import(world):
    _populate_materials(world)
    import_materials()
    from app import asset_store
    a = asset_store.find("material", "logo.primary", world["acme"]["id"])
    vs = asset_store.versions(a["id"])
    assert [v["n"] for v in vs] == [2, 1] and vs[1]["fields"]["title"] == "Acme logo"
    old = {r["version"]: r for r in mat.own(world["acme"]["id"], include_old=True) if r["slot"] == "logo.primary"}
    for v in vs:
        assert v["files"][0]["sha256"] == old[v["n"]]["sha256"] and v["files"][0]["rel_path"] == old[v["n"]]["rel_path"]
        assert asset_store.read_blob(v["files"][0]["sha256"]) == mat.read_bytes(old[v["n"]])


def test_materials_overridden_is_a_subset_of_todays(world):
    """Known gap (see the report): the importer shares only the NEAREST ancestor's version of a slot with each descendant, so
    the resolver's `overridden` omits the intermediate (non-account) ancestors' hidden versions that mat.overridden lists."""
    _populate_materials(world)
    import_materials()
    for p in tree.index().values():
        old = {(r["slot"], r["from"]["id"]) for r in mat.overridden(p)}
        rs = ar.resolve(p["id"], types=("material",))
        new = {(e.key, e.owner["project_id"]) for e in rs.overridden}
        assert new <= old, p["slug"]
        if p["slug"] != "leaf":
            assert new == old, p["slug"]
    leaf_old = {(r["slot"], r["from"]["id"]) for r in mat.overridden(world["leaf"])}
    leaf_new = {(e.key, e.owner["project_id"]) for e in ar.resolve(world["leaf"]["id"], types=("material",)).overridden}
    assert leaf_old - leaf_new == {("logo.primary", world["app"]["id"])}


def test_known_gap_identical_files_share_one_blob_path(world):
    """Two slots holding byte-identical files keep two rel_paths today; content-addressed blobs keep one."""
    _mat(world["acme"], "logo.primary", "Logo", SVG)
    _mat(world["acme"], "app.icon", "Icon", SVG, "icon", "icon.svg")
    import_materials()
    new, rs = _new_materials(world["acme"])
    h = ar.hydrate(rs.entries)
    paths = {e.key: h[e.version_id]["files"][0]["rel_path"] for e in rs.entries}
    old = {r["slot"]: r["rel_path"] for r in mat.effective(world["acme"])}
    assert old["logo.primary"] != old["app.icon"] and paths["logo.primary"] == paths["app.icon"]
    assert new["logo.primary"][0] == new["app.icon"][0]


# --- account shared skills and rules --------------------------------------------------------------------------------------

def _knowledge(account, kind, title, summary, body, archive=False):
    k = repo.create_knowledge(account["id"], kind, title, body, [], "", False, "owner", summary=summary)
    if archive:
        repo.archive_knowledge(k["id"], "retired", "owner")
    return k


def _populate_knowledge(t):
    _knowledge(t["acme"], "rule", "Australian English", "Spell the Australian way", "Use colour, not color.")
    _knowledge(t["acme"], "skill", "Humanizer", "Make text sound human", "Steps:\n1. Read\n2. Rewrite")
    _knowledge(t["acme"], "rule", "Old rule", "Retired", "Gone.", archive=True)
    _knowledge(t["acme"], "rule", "Australian English", "A second one with the same title", "Dates are day first.")
    _knowledge(t["acme"], "decision", "We picked X", "Not an asset", "x")
    _knowledge(t["acme"], "skill", "Release notes", "Write release notes", "Be brief.")
    _knowledge(t["beta"], "skill", "Beta only", "Stays in beta", "b")
    _knowledge(t["beta"], "rule", "Beta rule", "Beta", "r")


def _old_shared(p):
    """Today's effective rules + skills for a project: what it inherits from its account, or an account's own."""
    if tree.is_account(p):
        groups = [shared.items(p["id"])]
    else:
        groups = [g for g in shared.inherited(p)]
    return [(("rule" if k == "rules" else "skill"), r["title"], r["summary"], r["body"])
            for g in groups for k in ("rules", "skills") for r in g[k]]


def _new_shared(p):
    rs = ar.resolve(p["id"], types=("rule", "skill"))
    h = ar.hydrate(rs.entries)
    # today's order is oldest first within a section: rules, then skills (the readers sort by created_at, asset id)
    rows = sorted(rs.entries, key=lambda e: (e.type != "rule", e.created_at, e.asset_id))
    return [(e.type, e.name, e.summary, h[e.version_id]["body"]) for e in rows]


def test_shared_skills_and_rules_parity_for_every_project(world):
    _populate_knowledge(world)
    import_shared_knowledge()
    n = 0
    for p in tree.index().values():
        assert _new_shared(p) == _old_shared(p), p["slug"]
        n += len(_old_shared(p))
    assert n >= 12
    assert _old_shared(world["solo"]) == [] and _old_shared(world["bapp"]) != _old_shared(world["app"])
    # same-title rules were told apart by key and both resolve
    assert sum(1 for x in _new_shared(world["app"]) if x[1] == "Australian English") == 2


def test_archived_knowledge_is_archived_asset_and_not_resolved(world):
    _populate_knowledge(world)
    import_shared_knowledge()
    from app import asset_store
    assert any(a["state"] == "archived" for a in asset_store.list_(state=None))
    assert "Old rule" not in {x[1] for x in _new_shared(world["app"])}


def test_materials_and_knowledge_together_leave_each_other_alone(world):
    _populate_materials(world)
    _populate_knowledge(world)
    import_materials()
    import_shared_knowledge()
    for p in tree.index().values():
        assert _new_materials(p)[0] == _old_materials(p)
        assert _new_shared(p) == _old_shared(p)
