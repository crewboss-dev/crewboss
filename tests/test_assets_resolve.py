"""Assets P2 (task #778): the resolver. Layer precedence, shares, pins replacing the live version, locked rules (extends only),
ties, archive, job-kind filter, the repo layer, and the query plan."""
import json

import pytest

from app import asset_resolve as ar, asset_store, config, db
from app.repo import NotFound
from tests.asset_helpers import make_tree, mk_rule, mk_skill, rule_fields, skill_fields


@pytest.fixture
def t(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ASSETS_DIR", str(tmp_path))
    return make_tree()


def _skills(project, **kw):
    rs = ar.resolve(project["id"], types=("skill",), **kw)
    return rs, {e.key: e for e in rs.entries}


def _inst(key, builtin=False, share_all=False, **kw):
    return asset_store.create("skill", key, key.title(), "instance", None, skill_fields(), kw.pop("body", "b"), share_all=share_all,
                              origin="builtin" if builtin else "authored", actor="t", **kw)


def test_precedence_highest_layer_wins_per_key(t):
    app = t["app"]
    b = _inst("tone", builtin=True)
    rs, got = _skills(app)
    assert got["tone"].layer == 0 and got["tone"].asset_id == b["id"]
    i = _inst("tone", share_all=True)
    assert _skills(app)[1]["tone"].layer == 1
    a = mk_skill(t["acme"], "tone", scope="account", share_all=True)
    assert _skills(app)[1]["tone"].layer == 2 and _skills(app)[1]["tone"].asset_id == a["id"]
    s = mk_skill(t["app2"], "tone", share_all=True)                        # a sibling project's, shared with all
    assert _skills(app)[1]["tone"].layer == 3 and _skills(app)[1]["tone"].asset_id == s["id"]
    o = mk_skill(app, "tone")
    rs, got = _skills(app)
    assert got["tone"].layer == 4 and got["tone"].asset_id == o["id"] and got["tone"].owner["slug"] == "app"
    assert [e.layer for e in sorted(rs.overridden, key=lambda e: e.layer)] == [0, 1, 2, 3]
    r = asset_store.create("skill", "tone", "Tone", "project", app["id"], skill_fields(), "x", origin="repo", actor="t")
    rs, got = _skills(app)
    assert got["tone"].layer == 5 and got["tone"].asset_id == r["id"]
    assert len(rs.entries) == 1 and len(rs.overridden) == 5 and not rs.conflicts
    # the sub-project below `app` sees the shared and account ones, not app's own (own is not inherited by a child)
    assert _skills(t["sub"])[1]["tone"].asset_id == s["id"]
    del b, i


def test_reach_shares_and_partitions(t):
    app, app2, bapp, solo = t["app"], t["app2"], t["bapp"], t["solo"]
    acct = mk_skill(t["acme"], "acct-only", scope="account")                  # not share_all, not shared: nobody gets it
    assert "acct-only" not in _skills(app)[1]
    asset_store.share(acct["id"], app["id"])
    assert "acct-only" in _skills(app)[1] and "acct-only" not in _skills(app2)[1]
    mine = mk_skill(app, "mine")
    assert "mine" not in _skills(app2)[1] and "mine" not in _skills(t["sub"])[1]
    asset_store.share(mine["id"], app2["id"])
    assert _skills(app2)[1]["mine"].layer == 3
    asset_store.unshare(mine["id"], app2["id"])
    assert "mine" not in _skills(app2)[1]
    # share_all on a project asset reaches every project in ITS account, never another account
    shared = mk_skill(app, "everyone", share_all=True)
    assert "everyone" in _skills(app2)[1] and "everyone" in _skills(t["sub"])[1]
    assert "everyone" not in _skills(bapp)[1] and "everyone" not in _skills(solo)[1] and "everyone" not in _skills(t["beta"])[1]
    # instance assets reach only who they are shared with (a share to an account reaches all its projects)
    inst = _inst("global")
    assert "global" not in _skills(app)[1]
    asset_store.share(inst["id"], t["acme"]["id"])
    assert "global" in _skills(app)[1] and "global" in _skills(t["sub"])[1] and "global" not in _skills(bapp)[1]
    asset_store.share(inst["id"], solo["id"])
    assert "global" in _skills(solo)[1]
    asset_store.set_share_all(inst["id"], True)
    assert "global" in _skills(bapp)[1]
    del shared


def test_archived_excluded_and_types_filter(t):
    a = mk_skill(t["app"], "gone")
    mk_rule(t["app"], "keep")
    assert {e.key for e in ar.resolve(t["app"]["id"]).entries} == {"gone", "keep"}
    assert {e.key for e in ar.resolve(t["app"]["id"], types=("rule",)).entries} == {"keep"}
    asset_store.archive(a["id"], "t")
    assert {e.key for e in ar.resolve(t["app"]["id"]).entries} == {"keep"}
    with pytest.raises(NotFound):
        ar.resolve(99999)


def test_pin_replaces_the_live_version_for_that_project_only(t):
    a = mk_skill(t["acme"], "pinned", scope="account", share_all=True, body="v1 text")
    asset_store.new_version(a["id"], skill_fields(), "v2 text", actor="x")
    asset_store.pin(a["id"], t["app"]["id"], 1, "hold still")
    e = _skills(t["app"])[1]["pinned"]
    assert e.pinned and e.n == 1 and e.layer == 2
    assert ar.hydrate([e])[e.version_id]["body"] == "v1 text"
    f = _skills(t["app2"])[1]["pinned"]
    assert not f.pinned and f.n == 2 and ar.hydrate([f])[f.version_id]["body"] == "v2 text"
    before = _skills(t["app"])[0].set_hash
    asset_store.new_version(a["id"], skill_fields(), "v3 text", actor="x")          # live moves on; the pinned project does not
    assert _skills(t["app"])[0].set_hash == before
    assert _skills(t["app"])[1]["pinned"].n == 1 and _skills(t["app2"])[1]["pinned"].n == 3
    asset_store.unpin(a["id"], t["app"]["id"])
    assert _skills(t["app"])[1]["pinned"].n == 3 and not _skills(t["app"])[1]["pinned"].pinned
    # a pin holds on an asset that reaches the project with no share row at all (share_all), and on instance assets
    inst = _inst("builtin-ish", builtin=True)
    asset_store.new_version(inst["id"], skill_fields(), "b2", actor="x")
    asset_store.pin(inst["id"], t["bapp"]["id"], 1)
    assert _skills(t["bapp"])[1]["builtin-ish"].n == 1 and _skills(t["app"])[1]["builtin-ish"].n == 2


def test_locked_rule_cannot_be_overridden_or_pinned(t):
    lock = mk_rule(t["acme"], "no-secrets", scope="account", share_all=True, body="Never print secrets.",
                   fields={"locked": True, "lock_reason": "security"})
    # a same-key rule below it is dropped and reported
    mk_rule(t["app"], "no-secrets", body="Print them if useful.")
    rs = ar.resolve(t["app"]["id"], types=("rule",))
    assert [(e.key, e.layer, e.locked) for e in rs.entries] == [("no-secrets", 2, True)]
    assert rs.entries[0].asset_id == lock["id"]
    assert len(rs.overridden) == 1 and rs.conflicts[0]["reason"] == "locked" and rs.conflicts[0]["dropped_layer"] == 4
    # it also beats a shared one (3) and a repo mirror (5)
    mk_rule(t["app2"], "no-secrets", share_all=True)
    asset_store.create("rule", "no-secrets", "x", "project", t["app"]["id"], rule_fields(), "y", origin="repo", actor="t")
    rs = ar.resolve(t["app"]["id"], types=("rule",))
    assert len(rs.entries) == 1 and rs.entries[0].asset_id == lock["id"] and len(rs.conflicts) == 3
    # `extends` naming the locked key: both are delivered, the locked one first
    ext = mk_rule(t["sub"], "no-secrets", body="And never log tokens.", fields={"extends": "no-secrets"})
    rs = ar.resolve(t["sub"]["id"], types=("rule",))
    assert [e.asset_id for e in rs.entries] == [lock["id"], ext["id"]]
    assert ext["id"] not in [c["dropped_asset_id"] for c in rs.conflicts]       # (app2's shared plain one is still dropped)
    assert rs.entries[1].extends == "no-secrets" and rs.by_key("rule")["no-secrets"].asset_id == lock["id"]
    # a lock can't be pinned, and a pin made before it was locked is ignored
    from app.repo import BadRequest
    with pytest.raises(BadRequest):
        asset_store.pin(lock["id"], t["app2"]["id"], 1)
    soft = mk_rule(t["acme"], "later-locked", scope="account", share_all=True, body="old")
    asset_store.pin(soft["id"], t["app2"]["id"], 1)
    asset_store.new_version(soft["id"], rule_fields(locked=True, lock_reason="now"), "new", actor="x")
    e = ar.resolve(t["app2"]["id"], types=("rule",)).by_key("rule")["later-locked"]
    assert e.n == 2 and e.locked and not e.pinned
    # an extension that names a DIFFERENT key doesn't excuse a same-key override
    from app import repo, tree
    p5 = repo.create_project("app5", "App5", "client", "owner", short_name="app5", session_roles=[])
    tree.set_place(p5["id"], "owner", t["acme"]["id"], "project")
    mk_rule(p5, "no-secrets", fields={"extends": "something-else"})
    rs = ar.resolve(p5["id"], types=("rule",))
    assert rs.by_key("rule")["no-secrets"].asset_id == lock["id"] and any(c["reason"] == "locked" for c in rs.conflicts)


def test_locked_rule_at_a_lower_layer_beats_a_broader_unlocked_one(t):
    mk_rule(t["acme"], "style", scope="account", share_all=True, body="account text")
    own = mk_rule(t["app"], "style", body="locked project text", fields={"locked": True, "lock_reason": "mine"})
    rs = ar.resolve(t["app"]["id"], types=("rule",))
    assert [e.asset_id for e in rs.entries] == [own["id"]] and len(rs.overridden) == 1 and not rs.conflicts


def test_layer3_tie_is_reported_and_the_earliest_share_wins(t):
    first = mk_skill(t["app"], "dup")
    second = mk_skill(t["app2"], "dup")
    asset_store.share(first["id"], t["sub"]["id"])
    asset_store.share(second["id"], t["sub"]["id"])
    rs, got = _skills(t["sub"])
    assert got["dup"].asset_id == first["id"] and rs.conflicts[0]["reason"] == "tie"
    assert rs.conflicts[0]["other_asset_ids"] == [second["id"]] and [e.asset_id for e in rs.overridden] == [second["id"]]


def test_job_kind_filter(t):
    mk_skill(t["app"], "any-job")
    mk_skill(t["app"], "build-only", fields={"job_kinds": ["build"]})
    mk_rule(t["app"], "review-rule", fields={"job_kinds": ["review"]})
    mk_rule(t["app"], "lock-review", fields={"job_kinds": ["review"], "locked": True, "lock_reason": "r"})
    mat = asset_store.create("material", "logo", "L", "project", t["app"]["id"], {"kind": "logo", "title": "L", "purpose": "p"}, "",
                             [{"path": "l.svg", "raw": b"<svg/>"}], actor="t")
    rs = ar.resolve(t["app"]["id"], job="build")
    assert {e.key for e in rs.entries} == {"any-job", "build-only", "lock-review", "logo"}
    assert {e.key for e in rs.not_for_job} == {"review-rule"}
    rs = ar.resolve(t["app"]["id"], job={"task_types": ["review", "design"]})
    assert {e.key for e in rs.entries} == {"any-job", "review-rule", "lock-review", "logo"}
    assert {e.key for e in rs.not_for_job} == {"build-only"}
    assert {e.key for e in ar.resolve(t["app"]["id"]).entries} == {"any-job", "build-only", "review-rule", "lock-review", "logo"}
    del mat


def test_repo_layer_and_commit(t):
    c1, c2 = "a" * 40, "b" * 40
    r = asset_store.create("skill", "conv", "Conv", "project", t["app"]["id"], skill_fields(), "at c1", origin="repo", via="repo",
                           source_commit=c1, actor="runner")
    asset_store.new_version(r["id"], skill_fields(), "at c2", via="repo", source_commit=c2, actor="runner")
    h = lambda **kw: (lambda e: ar.hydrate([e])[e.version_id]["body"])(_skills(t["app"], **kw)[1]["conv"])      # noqa: E731
    assert h() == "at c2" and h(repo_commit=c1) == "at c1" and h(repo_commit=c2) == "at c2"
    assert h(repo_commit="c" * 40) == "at c2"                                  # unknown commit: the latest mirrored version


def test_set_hash_tracks_content(t):
    a = mk_skill(t["app"], "h")
    h1 = _skills(t["app"])[0].set_hash
    assert _skills(t["app"])[0].set_hash == h1
    asset_store.new_version(a["id"], skill_fields(), "changed", actor="x")
    assert _skills(t["app"])[0].set_hash != h1


def test_query_plan_uses_indexes(t):
    for i in range(3):
        mk_skill(t["app"], f"s{i}")
    mk_skill(t["acme"], "a", scope="account", share_all=True)
    with db.connect() as conn:
        conn.execute("SET LOCAL enable_seqscan = off")
        plan = conn.execute("EXPLAIN (FORMAT JSON) " + ar._SQL, {"pid": t["app"]["id"], "types": None, "commit": None}).fetchone()
        text = json.dumps(plan)
    for table in ("asset_versions", "assets", "asset_shares", "asset_pins"):
        assert f'"Seq Scan", "Parallel Aware": false, "Async Capable": false, "Relation Name": "{table}"' not in text, table
    assert "Seq Scan" not in text.replace('"Relation Name": "projects"', "")
