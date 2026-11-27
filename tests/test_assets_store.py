"""Assets P1 (task #778): the schema and the store. Immutable versions (trigger), the canonical content hash, the partition
guard that stops a share crossing accounts, scope shape, pins, type schemas, files."""
import psycopg
import pytest

from app import asset_hash, asset_store, asset_types, config, db, tree
from app.repo import BadRequest, Conflict, NotFound
from tests.asset_helpers import make_tree, mk_rule, mk_skill, rule_fields, skill_fields


@pytest.fixture
def t(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ASSETS_DIR", str(tmp_path))
    return make_tree()


def _sql(sql, *args):
    with db.connect() as conn:
        conn.execute(sql, args)
        conn.commit()


# --- the content hash ----------------------------------------------------------------------------------------------------

def test_hash_golden_vectors():
    """Frozen: the runner re-implements this (docs/asset-hash.md). If these change, every stored hash is wrong."""
    h = asset_hash.content_hash("skill", 1, "x", {"description": "d", "job_kinds": ["build"]}, "line1\nline2", [("a.md", "0" * 64)])
    assert asset_hash.canonical_json("skill", 1, "x", {"description": "d", "job_kinds": ["build"]}, "line1\nline2",
                                     [("a.md", "0" * 64)]) == (
        '{"body":"line1\\nline2","fields":{"description":"d","job_kinds":["build"]},'
        '"files":[["a.md","' + "0" * 64 + '"]],"key":"x","schema_version":1,"type":"skill"}')
    assert h == "1839851ce531d91a32286ae9f21ebadbbb07fb655b19521d9ae8ede87a5d8cb3"
    # non-ASCII is hashed as itself (UTF-8), not escaped
    assert asset_hash.content_hash("rule", 1, "caf", {"kind": "rule", "severity": "must"}, "caf\u00e9 \u2713") == \
        "fe72a9e68e69944b5518705b571023eff93accce864d56ae072edc1a851dac73"


def test_hash_stable_across_key_order_whitespace_and_unicode_form():
    a = asset_hash.content_hash("skill", 1, "k", {"description": "d", "job_kinds": ["b"]}, "x\ny", [("b", "1" * 64), ("a", "2" * 64)])
    b = asset_hash.content_hash("skill", 1, "k", {"job_kinds": ["b"], "description": "d"}, "x\r\ny", [("a", "2" * 64), ("b", "1" * 64)])
    assert a == b
    assert asset_hash.content_hash("skill", 1, "k", {"description": "d"}, "x\ry") == \
        asset_hash.content_hash("skill", 1, "k", {"description": "d"}, "x\ny")
    # NFC: e + combining acute equals the precomposed character
    assert asset_hash.content_hash("rule", 1, "k", {}, "café") == asset_hash.content_hash("rule", 1, "k", {}, "café")
    # but real changes change it
    assert a != asset_hash.content_hash("skill", 1, "k", {"description": "d"}, "x\ny", [("a", "2" * 64), ("b", "1" * 64)])
    assert asset_hash.content_hash("rule", 1, "k", {}, "a ") != asset_hash.content_hash("rule", 1, "k", {}, "a")
    with pytest.raises(ValueError):
        asset_hash.content_hash("rule", 1, "k", {"x": 1.5}, "")


# --- the registry -----------------------------------------------------------------------------------------------------------

def test_registry_schemas(t):
    assert set(asset_types.TYPES) >= {"skill", "rule", "material"}
    with pytest.raises(BadRequest):
        asset_types.get("nope")
    for fields in ({}, {"description": ""}, {"description": "d", "bogus": 1}, {"description": "d", "job_kinds": ["Bad Kind"]}):
        with pytest.raises(BadRequest):
            asset_types.get("skill").check_fields(fields)
    with pytest.raises(BadRequest):                  # locked needs a reason
        asset_types.get("rule").check_fields(rule_fields(locked=True))
    asset_types.get("rule").check_fields(rule_fields(locked=True, lock_reason="legal"))
    with pytest.raises(BadRequest):
        asset_types.get("material").check_fields({"kind": "banana", "title": "t", "purpose": "p"})
    with pytest.raises(BadRequest):
        asset_types.get("skill").check_key("Bad Key")
    with pytest.raises(ValueError):                  # a registered type is never replaced
        asset_types.register(asset_types.TYPES["skill"])
    from app import materials
    assert asset_types.MATERIAL_KINDS == materials.KINDS


# --- create / new_version / get / list / versions --------------------------------------------------------------------------

def test_create_new_version_get_list_versions(t):
    a = mk_skill(t["app"], "deploy-it", tags=["ops"], fields={"job_kinds": ["build"]})
    assert a["version"]["n"] == 1 and a["current_version_id"] == a["version"]["id"] and a["tags"] == ["ops"]
    assert a["account_id"] == t["acme"]["id"] and a["owner_scope"] == "project"
    same = asset_store.new_version(a["id"], skill_fields(job_kinds=["build"]), "# Steps\n", actor="x")
    assert same["created"] is False and same["version"]["n"] == 1            # identical content: a no-op
    b = asset_store.new_version(a["uid"], skill_fields(job_kinds=["build"]), "# Steps\nmore", note="more", actor="x",
                                base_version_id=a["version"]["id"])
    assert b["created"] and b["version"]["n"] == 2 and b["current_version_id"] == b["version"]["id"]
    with pytest.raises(asset_store.StaleVersion) as e:                        # edited from v1 while v2 is live
        asset_store.new_version(a["id"], skill_fields(), "stale", actor="x", base_version_id=a["version"]["id"])
    assert isinstance(e.value, Conflict) and e.value.current_n == 2
    assert [v["n"] for v in asset_store.versions(a["id"])] == [2, 1]
    assert "body" not in asset_store.versions(a["id"])[0] and asset_store.versions(a["id"], with_body=True)[1]["body"] == "# Steps\n"
    assert asset_store.get(a["id"], n=1)["version"]["body"] == "# Steps\n"
    assert asset_store.get(a["uid"])["version"]["n"] == 2
    assert [x["key"] for x in asset_store.list_(type_="skill", owner_project_id=t["app"]["id"])] == ["deploy-it"]
    assert asset_store.list_(owner_project_id=t["app2"]["id"]) == []
    with pytest.raises(NotFound):
        asset_store.get("nope")
    with pytest.raises(BadRequest):                                           # the key is reserved at its owner
        mk_skill(t["app"], "deploy-it")


def test_create_validates_scope(t):
    for args in (("project", t["acme"]["id"]), ("account", t["app"]["id"]), ("instance", t["app"]["id"]), ("project", None)):
        with pytest.raises((BadRequest, NotFound)):
            asset_store.create("skill", "k", "K", args[0], args[1], skill_fields(), "b", actor="t")
    inst = asset_store.create("skill", "k", "K", "instance", None, skill_fields(), "b", actor="t")
    assert inst["account_id"] is None and inst["owner_project_id"] is None
    with pytest.raises(BadRequest):
        asset_store.create("skill", "j", "J", "project", t["app"]["id"], skill_fields(), "b", origin="builtin", actor="t")
    acct = asset_store.create("rule", "r", "R", "account", t["acme"]["id"], rule_fields(), "b", actor="t")
    assert acct["account_id"] == t["acme"]["id"]


def test_locked_flag_follows_the_version(t):
    r = mk_rule(t["acme"], "legal", scope="account", fields={"locked": True, "lock_reason": "law"})
    assert r["locked"]
    r2 = asset_store.new_version(r["id"], rule_fields(), "Do it.", actor="x")
    assert not r2["locked"]


def test_files_blobs_and_materials_rules(t, tmp_path):
    a = mk_skill(t["app"], "withfile", files=[{"path": "ref/notes.md", "raw": b"# notes"}])
    f = a["version"]["files"][0]
    assert f["path"] == "ref/notes.md" and f["rel_path"].startswith("_blobs/")
    assert asset_store.read_blob(f["sha256"]) == b"# notes"
    # a new version keeps its files unless told otherwise, and the hash covers them
    b = asset_store.new_version(a["id"], skill_fields(), "# Steps\n", actor="x", note="same")
    assert b["created"] is False and [x["path"] for x in b["version"]["files"]] == ["ref/notes.md"]
    c = asset_store.new_version(a["id"], skill_fields(), "# Steps\n", files=[], actor="x")
    assert c["created"] and c["version"]["files"] == []
    with pytest.raises(BadRequest):
        mk_skill(t["app"], "secretfile", files=[{"path": "x.md", "raw": b"-----BEGIN OPENSSH PRIVATE KEY"}])
    with pytest.raises(BadRequest):
        mk_skill(t["app"], "dots", files=[{"path": "../x.md", "raw": b"x"}])
    with pytest.raises(BadRequest):                  # a rule takes no files
        mk_rule(t["app"], "nofiles", files=[{"path": "x.md", "raw": b"x"}])
    mf = {"kind": "logo", "title": "Logo", "purpose": "Header"}
    with pytest.raises(BadRequest):                  # a material takes exactly one file
        asset_store.create("material", "logo.primary", "Logo", "project", t["app"]["id"], mf, "", [], actor="t")
    m = asset_store.create("material", "logo.primary", "Logo", "project", t["app"]["id"], mf, "",
                           [{"path": "logo.svg", "raw": b"<svg xmlns='http://www.w3.org/2000/svg'/>"}], actor="t")
    assert m["version"]["files"][0]["content_type"] == "image/svg+xml"
    with pytest.raises(BadRequest):                  # existing materials rules: .exe refused
        asset_store.create("material", "tool", "Tool", "project", t["app"]["id"], mf, "", [{"path": "a.exe", "raw": b"MZ"}], actor="t")


# --- immutability -------------------------------------------------------------------------------------------------------------

def test_versions_are_immutable(t):
    a = mk_skill(t["app"], "frozen")
    vid = a["version"]["id"]
    with pytest.raises(psycopg.errors.IntegrityConstraintViolation):
        _sql("UPDATE asset_versions SET body = 'x' WHERE id = %s", vid)
    with pytest.raises(psycopg.errors.IntegrityConstraintViolation):
        _sql("UPDATE asset_versions SET content_hash = %s WHERE id = %s", "0" * 64, vid)
    with pytest.raises(psycopg.errors.IntegrityConstraintViolation):
        _sql("DELETE FROM asset_versions WHERE id = %s", vid)
    with db.connect() as conn:
        assert conn.execute("SELECT body FROM asset_versions WHERE id = %s", (vid,)).fetchone()["body"] == "# Steps\n"
    # deleting the asset itself (the cascade) is the one way a version goes
    _sql("DELETE FROM assets WHERE id = %s", a["id"])
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM asset_versions WHERE id = %s", (vid,)).fetchone()["n"] == 0


def test_a_version_used_by_a_job_cannot_be_deleted_with_its_asset(t):
    from app import repo
    from test_devices import _user
    a = mk_skill(t["app"], "used")
    task = repo.create_task(t["app"]["id"], "Do it", "body", "should", "bug", [], {}, "owner")
    user = _user()
    with db.connect() as conn:
        job = conn.execute("INSERT INTO jobs (task_id, account_id, user_id, profile_id) VALUES (%s, %s, %s, "
                           "(SELECT id FROM agent_profiles LIMIT 1)) RETURNING id", (task["id"], t["acme"]["id"], user["id"])).fetchone()
        conn.execute("INSERT INTO job_asset_uses (job_id, fence, asset_id, version_id, layer, type, key, content_hash) "
                     "VALUES (%s, 1, %s, %s, 4, 'skill', 'used', %s)",
                     (job["id"], a["id"], a["version"]["id"], a["version"]["content_hash"]))
        conn.commit()
    with pytest.raises(psycopg.errors.ForeignKeyViolation):                    # RESTRICT: the record of use stays whole
        _sql("DELETE FROM assets WHERE id = %s", a["id"])
    assert asset_store.get(a["id"])["key"] == "used"


# --- partitions ------------------------------------------------------------------------------------------------------------------

def test_projects_partition_follows_the_tree(t):
    def part(p):
        with db.connect() as conn:
            return conn.execute("SELECT partition_id FROM projects WHERE id = %s", (p["id"],)).fetchone()["partition_id"]
    assert part(t["acme"]) == part(t["app"]) == part(t["sub"]) == part(t["app2"]) == t["acme"]["id"]
    assert part(t["bapp"]) == t["beta"]["id"] and part(t["solo"]) == t["solo"]["id"]
    for p in t.values():                              # and it is exactly tree.account_root_id
        assert part(p) == tree.account_root_id(p["id"])
    tree.set_place(t["sub"]["id"], "owner", t["bapp"]["id"], "project")        # move a branch across accounts
    assert part(t["sub"]) == t["beta"]["id"]
    tree.set_place(t["solo"]["id"], "owner", t["app2"]["id"], "project")
    assert part(t["solo"]) == t["acme"]["id"]


def test_schema_blocks_cross_account_shares_and_pins(t):
    a = mk_skill(t["app"], "mine")
    asset_store.share(a["id"], t["app2"]["id"])                                   # same account: fine
    with pytest.raises(BadRequest):
        asset_store.share(a["id"], t["bapp"]["id"])                              # store's plain message
    with pytest.raises(BadRequest):
        asset_store.share(a["id"], t["solo"]["id"])
    # the schema refuses it too, even around the store, and a caller cannot claim another partition on the row
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _sql("INSERT INTO asset_shares (asset_id, target_project_id, account_id) VALUES (%s, %s, %s)", a["id"], t["bapp"]["id"],
             t["beta"]["id"])
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _sql("INSERT INTO asset_shares (asset_id, target_project_id) VALUES (%s, %s)", a["id"], t["bapp"]["id"])
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _sql("INSERT INTO asset_pins (asset_id, project_id, version_id) VALUES (%s, %s, %s)", a["id"], t["bapp"]["id"],
             a["version"]["id"])
    # an instance asset may reach any account, project or the personal one
    inst = asset_store.create("skill", "everywhere", "E", "instance", None, skill_fields(), "b", actor="t")
    for target in ("beta", "bapp", "solo", "acme"):
        asset_store.share(inst["id"], t[target]["id"])
    # an account asset cannot be re-owned into another account
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _sql("UPDATE assets SET account_id = %s WHERE id = %s", t["beta"]["id"], a["id"])
    # moving the owner project to another account is refused while it owns assets
    with pytest.raises(Exception):
        tree.set_place(t["app"]["id"], "owner", t["bapp"]["id"], "project")


def test_scope_shape_checks(t):
    with pytest.raises(psycopg.errors.CheckViolation):
        _sql("INSERT INTO assets (uid, type, key, name, owner_scope, owner_project_id, account_id, created_by) "
             "VALUES ('u1', 'skill', 'k', 'K', 'instance', %s, NULL, 't')", t["app"]["id"])
    with pytest.raises(psycopg.errors.CheckViolation):
        _sql("INSERT INTO assets (uid, type, key, name, owner_scope, owner_project_id, account_id, created_by) "
             "VALUES ('u2', 'skill', 'k', 'K', 'account', %s, %s, 't')", t["app"]["id"], t["acme"]["id"])
    with pytest.raises(psycopg.errors.CheckViolation):
        _sql("INSERT INTO assets (uid, type, key, name, owner_scope, account_id, created_by, origin) "
             "VALUES ('u3', 'skill', 'k', 'K', 'instance', NULL, 't', 'repo')")


def test_pin_rules(t):
    a = mk_skill(t["app"], "pinme")
    asset_store.share(a["id"], t["app2"]["id"])
    asset_store.new_version(a["id"], skill_fields(), "v2 body", actor="x")
    asset_store.pin(a["id"], t["app2"]["id"], 1, "hold")
    with pytest.raises(BadRequest):
        asset_store.pin(a["id"], t["app"]["id"], 1)                              # own asset
    with pytest.raises(NotFound):
        asset_store.pin(a["id"], t["app2"]["id"], 9)
    other = mk_skill(t["app2"], "other")
    with pytest.raises(psycopg.errors.ForeignKeyViolation):                    # a version of ANOTHER asset
        _sql("UPDATE asset_pins SET version_id = %s WHERE asset_id = %s", other["version"]["id"], a["id"])
    locked = mk_rule(t["acme"], "lk", scope="account", share_all=True, fields={"locked": True, "lock_reason": "r"})
    with pytest.raises(BadRequest):
        asset_store.pin(locked["id"], t["app"]["id"], 1)
    asset_store.unpin(a["id"], t["app2"]["id"])


def test_archive_and_restore(t):
    a = mk_skill(t["app"], "arch")
    assert asset_store.archive(a["id"], "me", "old")["state"] == "archived"
    assert asset_store.list_() == [] and len(asset_store.list_(state=None)) == 1
    with pytest.raises(BadRequest):
        asset_store.new_version(a["id"], skill_fields(), "x", actor="x")
    assert asset_store.restore(a["id"])["state"] == "active"
