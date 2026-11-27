"""#784 P1: project CONNECTIONS (part_of / depends_on / integrates_with). CRUD through the Settings routes, symmetry, one host and no host
chains, same account (the app's message and the DB trigger), an unseen other end looks like a missing one, listings omit edges to unseen
projects, a viewer or non-manager can't write, and the instance.yaml loader is idempotent."""
import psycopg
import pytest

from app import access, config, connections, repo, tree
from app.db import get_cursor
from tests.test_multi_user import H, _client, local, world  # noqa: F401  (fixtures)


def _mk(slug, owner, parent=None, kind="project"):
    p = repo.create_project(slug, slug.title(), "internal", "owner", short_name=slug.replace("-", "")[:20], session_roles=[],
                            owner_user_id=owner["id"] if owner else None)
    if parent is not None or kind != "project":
        tree.set_place(p["id"], "owner", parent["id"] if parent else None, kind=kind)
    return p


@pytest.fixture
def acct(world):
    """acme (account): a, b, c, h owned by dee; x owned by dan (dee can't see it). Another account 'beta' with z (dee's)."""
    d1, d2 = world["d1"], world["d2"]
    acme = _mk("acme", None, kind="account")
    beta = _mk("beta", None, kind="account")
    out = {"acme": acme, "beta": beta, "d1": d1, "d2": d2, "owner": world["owner"], "v": world["v"]}
    for s in ("a", "b", "c", "h"):
        out[s] = _mk(f"p-{s}", d1, acme)
    out["x"] = _mk("p-x", d2, acme)
    out["z"] = _mk("p-z", d1, beta)
    return out


def _post(c, slug, action, **data):
    return c.post(f"/{slug}/connections/{action}", data=data, headers=H, follow_redirects=False)


def _rows():
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT * FROM project_connections ORDER BY id")
        return cur.fetchall()


# --- CRUD -----------------------------------------------------------------------------------------------------------------------

def test_add_edit_note_and_remove_through_settings(acct):
    c = _client(acct["d1"])
    r = _post(c, "p-a", "add", kind="depends_on", other="p-b", note="  needs   the   API ")
    assert r.status_code == 303 and r.headers["location"] == "/p-a/settings#connections"
    (row,) = _rows()
    assert (row["project_id"], row["other_id"], row["kind"], row["note"]) == (acct["a"]["id"], acct["b"]["id"], "depends_on", "needs the API")
    page = c.get("/p-a/settings").text
    assert "data-connections-editor" in page and "needs the API" in page and "Depends on" in page
    assert _post(c, "p-a", "note", id=row["id"], note="changed").status_code == 303
    assert _rows()[0]["note"] == "changed"
    assert _post(c, "p-a", "remove", id=row["id"]).status_code == 303
    assert _rows() == []


def test_notes_are_one_line_and_at_most_300_characters(acct):
    c = _client(acct["d1"])
    assert _post(c, "p-a", "add", kind="depends_on", other="p-b", note="x" * 301).status_code == 400
    assert _post(c, "p-a", "add", kind="depends_on", other="p-b", note="x" * 300).status_code == 303
    cid = _rows()[0]["id"]
    assert _post(c, "p-a", "note", id=cid, note="y" * 301).status_code == 400
    _post(c, "p-a", "note", id=cid, note="line one\nline two\t end")
    assert _rows()[0]["note"] == "line one line two end"


def test_unknown_kind_self_and_account_ends_are_refused(acct):
    c = _client(acct["d1"])
    assert _post(c, "p-a", "add", kind="owns", other="p-b").status_code == 400
    assert _post(c, "p-a", "add", kind="depends_on", other="p-a").status_code == 400
    assert _post(c, "p-a", "add", kind="depends_on", other="acme").status_code in (400, 404)   # d1 can't see the account: its 404 is the missing one's
    assert _post(_client(acct["owner"]), "p-a", "add", kind="depends_on", other="acme").status_code == 400
    assert _rows() == []


def test_depends_on_reads_both_ways_and_the_reverse_has_no_controls_on_the_object_side(acct):
    c = _client(acct["d1"])
    _post(c, "p-a", "add", kind="depends_on", other="p-b", note="API")
    on_b = connections.for_project(acct["b"]["id"], None)
    assert [e["slug"] for e in on_b["needed_by"]] == ["p-a"] and on_b["depends_on"] == []
    page = c.get("/p-b/settings").text
    assert "Needed by" in page and "/p-b/connections/remove" not in page.split("Needed by", 1)[1].split("</section>")[0]
    cid = _rows()[0]["id"]
    assert _post(c, "p-b", "remove", id=cid).status_code == 403          # changed only from the project it starts at
    assert len(_rows()) == 1


# --- symmetry -------------------------------------------------------------------------------------------------------------------

def test_integrates_with_is_stored_once_lower_id_first_and_read_both_ways(acct):
    c = _client(acct["d1"])
    hi, lo = sorted((acct["a"], acct["b"]), key=lambda p: p["id"], reverse=True)       # add from the HIGHER id's page
    assert _post(c, hi["slug"], "add", kind="integrates_with", other=lo["slug"], note="sync").status_code == 303
    (row,) = _rows()
    assert (row["project_id"], row["other_id"]) == (lo["id"], hi["id"])
    for me, them in ((lo, hi), (hi, lo)):
        got = connections.for_project(me["id"], None)["integrates_with"]
        assert [e["slug"] for e in got] == [them["slug"]]
        assert them["name"] in c.get(f"/{me['slug']}").text                               # on the Summary of both
    # the other direction is the SAME connection
    assert _post(c, lo["slug"], "add", kind="integrates_with", other=hi["slug"]).status_code == 400
    assert len(_rows()) == 1
    # either end may edit and remove it
    assert _post(c, hi["slug"], "note", id=row["id"], note="both").status_code == 303
    assert _post(c, lo["slug"], "remove", id=row["id"]).status_code == 303 and _rows() == []


def test_the_database_refuses_a_symmetric_row_stored_the_wrong_way_round(acct):
    hi, lo = sorted((acct["a"], acct["b"]), key=lambda p: p["id"], reverse=True)
    with pytest.raises(psycopg.errors.CheckViolation):
        with get_cursor() as cur:
            cur.execute("INSERT INTO project_connections (project_id, other_id, kind) VALUES (%s, %s, 'integrates_with')", (hi["id"], lo["id"]))


# --- one host, no chains ----------------------------------------------------------------------------------------------------------

def test_one_host_only_and_no_host_chains(acct):
    c = _client(acct["d1"])
    assert _post(c, "p-a", "add", kind="part_of", other="p-h").status_code == 303
    assert _post(c, "p-a", "add", kind="part_of", other="p-b").status_code == 400            # a second host
    assert _post(c, "p-c", "add", kind="part_of", other="p-a").status_code == 400            # the host would itself be part of something
    assert _post(c, "p-h", "add", kind="part_of", other="p-b").status_code == 400            # h hosts a, so it cannot be part of anything
    assert len(_rows()) == 1
    assert connections.host_of(acct["a"]["id"]) == acct["h"]["id"] and connections.host_of(acct["h"]["id"]) is None
    contains = connections.for_project(acct["h"]["id"], None)["contains"]
    assert [e["slug"] for e in contains] == ["p-a"]


def test_the_database_enforces_one_host_and_no_chains_on_its_own(acct):
    def ins(sub, host):
        with get_cursor() as cur:
            cur.execute("INSERT INTO project_connections (project_id, other_id, kind) VALUES (%s, %s, 'part_of')", (acct[sub]["id"], acct[host]["id"]))
    ins("a", "h")
    with pytest.raises(psycopg.errors.UniqueViolation):
        ins("a", "b")
    with pytest.raises(psycopg.errors.CheckViolation):
        ins("c", "a")
    with pytest.raises(psycopg.errors.CheckViolation):
        ins("h", "b")


# --- same account -----------------------------------------------------------------------------------------------------------------

def test_cross_account_is_refused_by_the_app_with_a_clear_message(acct):
    r = _post(_client(acct["d1"]), "p-a", "add", kind="depends_on", other="p-z")
    assert r.status_code == 400 and "same account" in r.text
    assert _rows() == []


def test_the_trigger_refuses_cross_account_and_account_ends(acct):
    for sub, other in ((acct["a"]["id"], acct["z"]["id"]), (acct["a"]["id"], acct["acme"]["id"]), (acct["acme"]["id"], acct["a"]["id"])):
        with pytest.raises(psycopg.errors.CheckViolation):
            with get_cursor() as cur:
                cur.execute("INSERT INTO project_connections (project_id, other_id, kind) VALUES (%s, %s, 'depends_on')", (sub, other))
    assert _rows() == []


def test_the_trigger_walks_to_the_account_through_nested_projects(acct):
    deep = _mk("p-deep", acct["d1"], acct["a"])                      # still nested in P1: acme > p-a > p-deep
    with get_cursor() as cur:
        cur.execute("INSERT INTO project_connections (project_id, other_id, kind) VALUES (%s, %s, 'depends_on')", (deep["id"], acct["b"]["id"]))
        cur.execute("SELECT project_account_root(%s) AS r", (deep["id"],))
        assert cur.fetchone()["r"] == acct["acme"]["id"]
    with pytest.raises(psycopg.errors.CheckViolation):
        with get_cursor() as cur:
            cur.execute("INSERT INTO project_connections (project_id, other_id, kind) VALUES (%s, %s, 'depends_on')", (deep["id"], acct["z"]["id"]))


def test_the_picker_lists_only_visible_projects_of_the_same_account(acct):
    got = {p["slug"] for p in connections.picker(access.Principal(acct["d1"]["id"], "designer"), acct["a"])}
    assert got == {"p-b", "p-c", "p-h"}                              # not x (unseen), z (other account), the account itself, or itself


# --- visibility -------------------------------------------------------------------------------------------------------------------

def test_an_unseen_other_end_is_the_same_404_as_a_missing_one(acct):
    c = _client(acct["d1"])
    unseen = _post(c, "p-a", "add", kind="depends_on", other="p-x")
    missing = _post(c, "p-a", "add", kind="depends_on", other="p-nope")
    assert unseen.status_code == missing.status_code == 404
    assert unseen.text.replace("p-x", "p-nope") == missing.text
    assert _rows() == []


def test_a_connection_id_of_another_project_looks_like_a_missing_id(acct):
    owner = _client(acct["owner"])
    _post(owner, "p-x", "add", kind="depends_on", other="p-b")
    cid = _rows()[0]["id"]
    c = _client(acct["d1"])
    other = _post(c, "p-a", "note", id=cid, note="pwn")
    missing = _post(c, "p-a", "note", id=cid + 999, note="pwn")
    assert other.status_code == missing.status_code == 404 and other.text == missing.text
    assert _post(c, "p-a", "remove", id=cid).status_code == 404
    assert _post(c, "p-a", "remove", id="abc").status_code == 404
    assert len(_rows()) == 1 and _rows()[0]["note"] == ""


def test_listings_omit_edges_to_unseen_projects(acct):
    owner = _client(acct["owner"])
    _post(owner, "p-a", "add", kind="depends_on", other="p-x", note="secret link")
    _post(owner, "p-a", "add", kind="integrates_with", other="p-b", note="visible link")
    d1 = _client(acct["d1"])
    for url in ("/p-a", "/p-a/settings"):
        html = d1.get(url).text
        assert 'href="/p-b"' in html and "visible link" in html
        assert 'href="/p-x"' not in html and "secret link" not in html and "P-X" not in html, url
    assert [e["slug"] for e in connections.for_project(acct["a"]["id"], {acct["a"]["id"], acct["b"]["id"]})["depends_on"]] == []
    # the owner sees both, on Summary and on the (owner-only) Integrations tab
    for url in ("/p-a", "/p-a/integrations"):
        html = owner.get(url).text
        assert "secret link" in html and "visible link" in html, url
    assert "Connected projects" in owner.get("/p-a/integrations").text
    # the edge is hidden from the other side too: the owner of p-x can't see p-a, so p-x shows no "Needed by" and no note
    dan = _client(acct["d2"]).get("/p-x").text
    assert "Needed by" not in dan and "secret link" not in dan


def test_summary_shows_each_kind_with_its_reverse_label(acct):
    c = _client(acct["d1"])
    _post(c, "p-a", "add", kind="part_of", other="p-h", note="same repo")
    _post(c, "p-b", "add", kind="depends_on", other="p-a")
    host, child = c.get("/p-h").text, c.get("/p-a").text
    assert "Contains" in host and "same repo" in host
    assert "Part of" in child and "Needed by" in child
    assert "data-connections-card" not in c.get("/p-c").text          # nothing connected: no card


def test_the_part_of_host_needs_manage_on_the_host_too(acct):
    # dan's project p-x is visible to dee only as a viewer: she may see it but not manage it
    access.share(acct["x"]["id"], acct["d1"]["id"], "viewer", acct["owner"]["id"])
    r = _post(_client(acct["d1"]), "p-a", "add", kind="part_of", other="p-x")
    assert r.status_code == 403
    assert _post(_client(acct["d1"]), "p-a", "add", kind="depends_on", other="p-x").status_code == 303    # a plain link needs only view
    assert [r_["kind"] for r_ in _rows()] == ["depends_on"]


# --- who may write ------------------------------------------------------------------------------------------------------------------

def test_a_viewer_and_a_non_manager_cannot_write_and_get_no_controls(acct):
    access.share(acct["a"]["id"], acct["v"]["id"], "viewer", acct["owner"]["id"])
    access.share(acct["a"]["id"], acct["d2"]["id"], "member", acct["owner"]["id"])
    access.share(acct["b"]["id"], acct["v"]["id"], "viewer", acct["owner"]["id"])
    access.share(acct["b"]["id"], acct["d2"]["id"], "viewer", acct["owner"]["id"])
    creator = _client(acct["d1"])
    _post(creator, "p-a", "add", kind="depends_on", other="p-b", note="keep")
    cid = _rows()[0]["id"]
    for who in ("v", "d2"):
        c = _client(acct[who])
        assert _post(c, "p-a", "add", kind="depends_on", other="p-c").status_code == 403, who
        assert _post(c, "p-a", "note", id=cid, note="x").status_code == 403, who
        assert _post(c, "p-a", "remove", id=cid).status_code == 403, who
        assert "data-connections-editor" not in c.get("/p-a/settings").text, who
    assert len(_rows()) == 1 and _rows()[0]["note"] == "keep"
    # a person who can't see the project at all gets the missing-project 404
    assert _post(_client(acct["d2"]), "p-c", "add", kind="depends_on", other="p-b").status_code == 404


# --- instance.yaml loader ---------------------------------------------------------------------------------------------------------

def test_the_instance_config_loader_is_idempotent_additive_and_skips_unknown_slugs(acct, caplog):
    specs = [{"project": "p-a", "kind": "part_of", "other": "p-h", "note": ""},
             {"project": "p-b", "kind": "depends_on", "other": "p-c", "note": "to be retired"},
             {"project": "p-b", "kind": "integrates_with", "other": "p-h", "note": ""},
             {"project": "p-ghost", "kind": "depends_on", "other": "p-h", "note": ""},
             {"project": "p-a", "kind": "depends_on", "other": "p-z", "note": ""}]          # cross-account: refused and logged
    with caplog.at_level("WARNING", logger="crewboss.connections"):
        assert connections.seed_from_config(specs) == {"added": 3, "present": 0, "skipped": 2}
    assert "p-ghost" in caplog.text and "same account" in caplog.text
    before = [dict(r) for r in _rows()]
    assert connections.seed_from_config(specs) == {"added": 0, "present": 3, "skipped": 2}
    assert [dict(r) for r in _rows()] == before
    # a note a person changed is not overwritten; a row removed from the config is not deleted
    with get_cursor() as cur:
        cur.execute("UPDATE project_connections SET note = 'edited' WHERE kind = 'depends_on'")
    assert connections.seed_from_config(specs[:1]) == {"added": 0, "present": 1, "skipped": 0}
    assert len(_rows()) == 3 and {r["note"] for r in _rows()} >= {"edited"}
    # the symmetric pair is recognised whichever way round the config writes it
    assert connections.seed_from_config([{"project": "p-h", "kind": "integrates_with", "other": "p-b", "note": ""}])["present"] == 1


def test_config_parsing_accepts_the_documented_shape_and_rejects_the_rest():
    assert config._connections(None) == []
    ok = config._connections([{"project": "a", "kind": "depends_on", "other": "b", "note": "n"}, {"project": "c", "kind": "part_of", "other": "d"}])
    assert ok[1] == {"project": "c", "kind": "part_of", "other": "d", "note": ""}
    for bad in ("nope", [{"project": "a", "kind": "owns", "other": "b"}], [{"project": "a", "kind": "part_of"}], ["a"],
                [{"project": "a", "kind": "part_of", "other": "b", "note": 5}]):
        with pytest.raises(ValueError):
            config._connections(bad)


def test_the_index_is_one_query_per_request(acct, monkeypatch):
    class State:
        pass

    class Req:
        state = State()

    calls = []
    orig = connections._index
    monkeypatch.setattr(connections, "_index", lambda cur: (calls.append(1), orig(cur))[1])
    r = Req()
    connections.index(r)
    connections.index(r)
    assert len(calls) == 1


# --- review fixes ---------------------------------------------------------------------------------------------------------------

def test_walking_ids_cannot_count_hidden_incoming_edges(acct):
    _post(_client(acct["owner"]), "p-x", "add", kind="depends_on", other="p-a")      # starts at p-x, which dee can't see
    cid = _rows()[0]["id"]
    c = _client(acct["d1"])
    hidden = _post(c, "p-a", "note", id=cid, note="x")
    missing = _post(c, "p-a", "note", id=cid + 999, note="x")
    assert hidden.status_code == missing.status_code == 404 and hidden.text == missing.text
    assert _post(c, "p-a", "remove", id=cid).status_code == 404
    # a VISIBLE incoming edge still says why it can't be changed from here
    _post(c, "p-b", "add", kind="depends_on", other="p-a")
    assert _post(c, "p-a", "remove", id=_rows()[-1]["id"]).status_code == 403


def test_the_part_of_refusal_names_no_project(acct):
    c = _client(acct["d1"])
    _post(c, "p-a", "add", kind="part_of", other="p-h")
    for sub, other in (("p-a", "p-b"), ("p-c", "p-a"), ("p-h", "p-b")):
        r = _post(c, sub, "add", kind="part_of", other=other)
        assert r.status_code == 400 and not any(n in r.text for n in ("P-A", "P-H", "P-B", "p-a", "p-h", "p-b")), r.text


def test_the_editor_is_shown_only_to_people_the_route_lets_write(acct):
    access.share(acct["a"]["id"], acct["d2"]["id"], "admin", acct["owner"]["id"])
    admin = _client(acct["d2"])
    assert "data-connections-editor" not in admin.get("/p-a/settings").text
    assert _post(admin, "p-a", "add", kind="depends_on", other="p-b").status_code == 403
    assert "data-connections-editor" in _client(acct["d1"]).get("/p-a/settings").text
    assert "data-connections-editor" in _client(acct["owner"]).get("/p-a/settings").text


def test_a_move_or_kind_change_that_would_break_a_connection_is_refused(acct):
    from app.repo import BadRequest
    _post(_client(acct["d1"]), "p-a", "add", kind="depends_on", other="p-b")
    with pytest.raises(BadRequest, match="remove its connections first"):
        tree.set_place(acct["a"]["id"], "owner", acct["beta"]["id"])
    with pytest.raises(BadRequest, match="remove its connections first"):
        tree.set_place(acct["a"]["id"], "owner", None, kind="account")
    with pytest.raises(BadRequest, match="remove its connections first"):
        tree.set_place(acct["b"]["id"], "owner", None)                  # to the top level: its own partition
    assert repo.get_project(acct["a"]["id"])["parent_id"] == acct["acme"]["id"]
    tree.set_place(acct["c"]["id"], "owner", acct["beta"]["id"])        # an unconnected project moves freely
    _post(_client(acct["d1"]), "p-a", "remove", id=_rows()[0]["id"])
    tree.set_place(acct["a"]["id"], "owner", acct["beta"]["id"])


def test_a_note_on_a_row_that_became_invalid_is_a_400_not_a_500(acct):
    _post(_client(acct["d1"]), "p-a", "add", kind="depends_on", other="p-b")
    with get_cursor() as cur:                                            # a move that bypassed set_place
        cur.execute("UPDATE projects SET parent_id = %s WHERE id = %s", (acct["beta"]["id"], acct["a"]["id"]))
    r = _post(_client(acct["d1"]), "p-a", "note", id=_rows()[0]["id"], note="x")
    assert r.status_code == 400 and "same account" in r.text


def test_a_seeded_pair_that_a_person_removed_is_not_re_added(acct):
    specs = [{"project": "p-a", "kind": "depends_on", "other": "p-b", "note": ""},
             {"project": "p-b", "kind": "integrates_with", "other": "p-c", "note": ""}]
    assert connections.seed_from_config(specs)["added"] == 2
    c = _client(acct["d1"])
    for row in _rows():
        assert _post(c, "p-a" if row["kind"] == "depends_on" else "p-b", "remove", id=row["id"]).status_code == 303
    assert _rows() == []
    assert connections.seed_from_config(specs) == {"added": 0, "present": 0, "skipped": 2}
    assert _rows() == []
    # adding one back by hand clears its tombstone; removing a different pair never blocks the rest
    assert _post(c, "p-a", "add", kind="depends_on", other="p-b").status_code == 303
    assert connections.seed_from_config(specs) == {"added": 0, "present": 1, "skipped": 1}
