"""Project roles as data (app/roles.py): who is the overseer / owns System Health / … comes from config (instance.yaml, an
CREWBOSS_* env var over it), never from a literal slug in the logic. A role nobody fills, or one that doesn't exist, grants
NOTHING. The isolation suites (test_isolation.py, test_account_partition.py) hold the behaviour for the filled roles."""
import logging

import pytest

from app import config, db, repo, roles, scope, tree


def test_slug_reads_config_at_call_time(monkeypatch):
    monkeypatch.setattr(config, "OVERSEER_PROJECT", "root-x")
    assert roles.slug("overseer") == "root-x"
    monkeypatch.setattr(config, "OVERSEER_PROJECT", "root-y")
    assert roles.slug("overseer") == "root-y"


def test_unknown_or_unfilled_role_matches_nothing(monkeypatch):
    monkeypatch.setattr(config, "HEALTH_PROJECT", "")
    assert roles.slug("health") == ""                      # known but unfilled: "" / False
    assert not roles.is_role({"slug": ""}, "health")        # an empty slug never matches an unfilled role
    assert not roles.is_role("", "health")
    assert not roles.is_role(None, "overseer")
    assert roles.holder([{"slug": ""}, {"slug": "a"}], "health") is None


def test_is_role_takes_a_row_or_a_slug_and_holder_finds_it(monkeypatch):
    monkeypatch.setattr(config, "HEALTH_PROJECT", "h")
    assert roles.is_role({"slug": "h"}, "health") and roles.is_role("h", "health")
    assert not roles.is_role({"slug": "other"}, "health")
    rows = {1: {"id": 1, "slug": "a"}, 2: {"id": 2, "slug": "h"}}
    assert roles.holder(rows, "health")["id"] == 2 and roles.holder(list(rows.values()), "health")["id"] == 2
    assert roles.assigned()["health"] == "h"


def test_is_overseer_needs_the_role_the_all_stream_and_a_live_project(monkeypatch):
    monkeypatch.setattr(config, "OVERSEER_PROJECT", "root")
    sess = {"stream": "all", "project": {"slug": "root", "archived_at": None}}
    assert scope.is_overseer(sess)
    assert not scope.is_overseer({**sess, "stream": "bugs"})
    assert not scope.is_overseer({"stream": "all", "project": {"slug": "root", "archived_at": "t"}})
    assert not scope.is_overseer({"stream": "all", "project": {"slug": "elsewhere"}})
    assert not scope.is_overseer(None)
    monkeypatch.setattr(config, "OVERSEER_PROJECT", "")     # no overseer filled: nobody is one, even a project named ""
    assert not scope.is_overseer(sess)
    assert not scope.is_overseer({"stream": "all", "project": {"slug": ""}})


@pytest.fixture
def pair(client, monkeypatch):
    """dash (root) -> acct (account, 'a-erp' under it) and a second account 'other'."""
    with db.connect() as conn:
        conn.execute("TRUNCATE projects RESTART IDENTITY CASCADE")
        conn.commit()
    mk = lambda slug, parent=None, kind="project": _mk(slug, parent, kind)  # noqa: E731
    dash = mk("dash-root")
    acct = mk("acct", dash, "account")
    erp = mk("a-erp", acct)
    other = mk("other", dash, "account")
    return {"dash": dash, "acct": acct, "erp": erp, "other": other}


def _mk(slug, parent, kind):
    p = repo.create_project(slug, slug, "internal", "owner", short_name=slug, session_roles=["all"])
    if parent is not None:
        tree.set_place(p["id"], "owner", None if kind == "account" or parent["slug"] == config.OVERSEER_PROJECT else parent["id"], kind=kind)   # no project root (change #76)
    return repo.get_project(p["id"])


def _sess(project):
    return {"stream": "all", "project": project, "project_id": project["id"], "agent": "claude"}


def test_estate_account_follows_the_health_role(pair, monkeypatch):
    monkeypatch.setattr(config, "OVERSEER_PROJECT", "dash-root")
    monkeypatch.setattr(config, "HEALTH_PROJECT", "a-erp")
    assert tree.in_estate_account(pair["erp"]["id"]) and tree.in_estate_account(pair["acct"]["id"])
    assert not tree.in_estate_account(pair["other"]["id"])
    assert scope.sees_estate(_sess(pair["acct"])) and not scope.sees_estate(_sess(pair["other"]))
    # the role moves with the data: point it at the other account and the answers swap
    monkeypatch.setattr(config, "HEALTH_PROJECT", "other")
    assert tree.in_estate_account(pair["other"]["id"]) and not tree.in_estate_account(pair["erp"]["id"])


@pytest.mark.parametrize("value", ["", "no-such-project"])
def test_unfilled_or_dangling_health_role_gives_no_estate_access(pair, monkeypatch, value):
    monkeypatch.setattr(config, "OVERSEER_PROJECT", "dash-root")
    monkeypatch.setattr(config, "HEALTH_PROJECT", value)
    assert tree.estate_account_id() is None
    for key in ("acct", "erp", "other"):
        assert not tree.in_estate_account(pair[key]["id"])
        assert not scope.sees_estate(_sess(pair[key]))
    assert scope.sees_estate(_sess(pair["dash"]))           # the overseer (and the owner's pages) still do
    assert scope.sees_estate(None)


def test_no_overseer_role_means_no_global_root(pair, monkeypatch):
    monkeypatch.setattr(config, "OVERSEER_PROJECT", "")
    assert not tree.is_global_root(pair["dash"])
    assert not scope.is_overseer(_sess(pair["dash"]))


def test_an_unknown_role_name_raises():
    for call in (lambda: roles.slug("nope"), lambda: roles.slug(""), lambda: roles.is_role({"slug": "x"}, "nope"),
                 lambda: roles.is_role(None, "nope"), lambda: roles.holder([], "nope")):
        with pytest.raises(ValueError, match="unknown project role"):
            call()


@pytest.mark.parametrize("role,attr", [("overseer", "OVERSEER_PROJECT"), ("health", "HEALTH_PROJECT")])
@pytest.mark.parametrize("variant", ["Role-Proj", "ROLE-PROJ", " role-proj", "role-proj ", "role-proj\n", "role_proj"])
def test_case_and_whitespace_variants_of_a_role_slug_never_match(monkeypatch, role, attr, variant):
    monkeypatch.setattr(config, attr, "role-proj")
    assert roles.is_role({"slug": "role-proj"}, role)
    assert not roles.is_role({"slug": variant}, role) and not roles.is_role(variant, role)
    monkeypatch.setattr(config, attr, variant)             # and the other way round
    assert not roles.is_role({"slug": "role-proj"}, role)


@pytest.mark.parametrize("role", list(roles.ROLES))
def test_the_slug_lock_covers_every_role(pair, monkeypatch, role):
    monkeypatch.setattr(config, roles.ROLES[role], "a-erp")
    why = tree.slug_locked(pair["erp"])
    assert why.startswith("the server's settings name it as"), role
    monkeypatch.setattr(config, roles.ROLES[role], "")
    assert not tree.slug_locked(pair["acct"]).startswith("the server's settings")   # unfilled pins nothing


def test_a_dangling_overseer_is_nobody(pair, monkeypatch):
    monkeypatch.setattr(config, "OVERSEER_PROJECT", "no-such-project")
    assert not scope.is_overseer(_sess(pair["dash"]))
    assert tree._global_root_id(tree.index()) is None
    assert not tree.is_global_root(pair["dash"])
    # accounts stay separate partitions: a's data is not other's
    assert not tree.same_account(pair["erp"]["id"], pair["other"]["id"])
    assert tree.same_account(pair["erp"]["id"], pair["acct"]["id"])


def test_an_old_slug_alias_is_not_the_role(client, monkeypatch):
    with db.connect() as conn:
        conn.execute("TRUNCATE projects RESTART IDENTITY CASCADE")
        conn.commit()
    monkeypatch.setattr(config, "OVERSEER_PROJECT", "dash-root")
    dash = repo.create_project("dash-root", "dash", "internal", "owner", short_name="dd", session_roles=[])
    acct = _mk("acct2", repo.get_project(dash["id"]), "account")
    proj = repo.create_project("health-old", "health", "internal", "owner", short_name="hh", session_roles=[])
    tree.set_place(proj["id"], "owner", acct["id"], kind="project")
    tree.rename_project(proj["id"], "owner", "health", slug="health-new")   # the old slug is now an alias
    assert repo.get_project_by_slug("health-old")["id"] == proj["id"]        # callers naming it still resolve…
    monkeypatch.setattr(config, "HEALTH_PROJECT", "health-old")              # …but the role refilled with it matches nothing
    renamed = repo.get_project(proj["id"])
    assert renamed["slug"] == "health-new"
    assert not roles.is_role(renamed, "health") and roles.holder(tree.index(), "health") is None
    assert tree.estate_account_id() is None and not tree.in_estate_account(proj["id"])
    bad = roles.unmatched(p["slug"] for p in repo.list_projects(include_archived=True))
    assert bad["health"] == "health-old" and "overseer" not in bad


def test_startup_warns_for_a_role_that_names_no_current_project(pair, monkeypatch, caplog):
    monkeypatch.setattr(config, "OVERSEER_PROJECT", "dash-root")
    monkeypatch.setattr(config, "HEALTH_PROJECT", "gone")
    monkeypatch.setattr(config, "DEFAULT_PROJECT", "")        # unfilled: not warned about
    with caplog.at_level(logging.WARNING):
        bad = roles.warn_unmatched()
    assert bad["health"] == "gone" and "overseer" not in bad and "default" not in bad
    assert "'health'" in caplog.text and "'gone'" in caplog.text and "'overseer'" not in caplog.text


def test_an_archived_health_project_keeps_the_estate_account(pair, monkeypatch):
    """Pins today's behaviour: the tree index includes archived projects, so archiving the health project does not
    move or drop the estate account."""
    monkeypatch.setattr(config, "OVERSEER_PROJECT", "dash-root")
    monkeypatch.setattr(config, "HEALTH_PROJECT", "a-erp")
    repo.set_project_archived(pair["erp"]["id"], True, "owner")
    assert tree.estate_account_id() == pair["acct"]["id"]
    assert tree.in_estate_account(pair["acct"]["id"]) and not tree.in_estate_account(pair["other"]["id"])
