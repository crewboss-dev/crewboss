"""#908 security review fixes: the overseer as a target, free text in activity, moves across accounts, person-level visibility (#713),
per-stream holders, nobody to deliver to, the flood limit, and secrets in titles and notes. The estate here is the production shape: the
dashboard (overseer) project sits INSIDE the Northwind account."""
import pytest

from app import access, config, db, handoffs, mcp_server, repo, tree
from app.auth import issue_token
from app.db import get_cursor
from app.repo import BadRequest, Conflict, NotFound

SECRET = "-----BEGIN OPENSSH " + "PRIVATE KEY" + "-----\nabc"
SUBJ = "SUBJECT-FREE-TEXT-XYZ"


def _project(slug, short, roles, parent=None, kind="project", **kw):
    p = repo.create_project(slug, slug.replace("-", " ").title(), "internal", "owner", short_name=short, session_roles=roles, **kw)
    if parent is not None or kind == "account":
        tree.set_place(p["id"], "owner", None if kind == "account" else parent["id"], kind=kind)
    return repo.get_project(p["id"])


@pytest.fixture
def est(client, monkeypatch):
    with db.connect() as conn:
        conn.execute("TRUNCATE projects RESTART IDENTITY CASCADE")
        conn.commit()
    monkeypatch.setattr(config, "OVERSEER_PROJECT", "crewboss-dash")
    northwind = _project("northwind", "northwind", ["all"], kind="account")
    dash = _project("crewboss-dash", "dash", ["all"], northwind)
    erp = _project("northwind-erp", "erp", [], northwind)
    mobile = _project("northwind-mobile", "mobile", [], northwind)
    site = _project("northwind-website", "website", ["all"], northwind)
    multi = _project("northwind-multi", "multi", ["bugs", "features"], northwind)
    multi2 = _project("northwind-multi2", "multi2", ["bugs", "reviewer"], northwind)
    acme = _project("acme", "acme", ["all"], kind="account")
    acme_web = _project("acme-web", "acmeweb", ["all"], acme)
    solo = _project("solo", "solo", [], kind="account")
    solo_a = _project("solo-a", "soloa", ["all"], solo)
    solo_b = _project("solo-b", "solob", [], solo)
    tree.set_external_agent(mobile["id"], "owner", True)
    es = dict(northwind=northwind, dash=dash, erp=erp, mobile=mobile, site=site, multi=multi, multi2=multi2, acme=acme, acme_web=acme_web,
              solo=solo, solo_a=solo_a, solo_b=solo_b)
    return {k: repo.get_project(v["id"]) for k, v in es.items()}


def _as(mcp_caller, project, stream="all"):
    mcp_caller(issue_token(stream, f"t-{project['short_name']}-{stream}", project_id=project["id"]))


def _ev(project, role="all"):
    return [o for o in repo.session_outbox(project, role) if o["kind"] == "handoff"]


# 1 -------------------------------------------------------------------------------------------------------------------------
def test_the_overseer_project_is_never_a_target_even_inside_the_account(est, mcp_caller):
    _as(mcp_caller, est["site"])
    with pytest.raises(NotFound) as a:
        mcp_server.send_handoff("crewboss-dash", SUBJ, "b")
    with pytest.raises(NotFound) as b:
        mcp_server.send_handoff("no-such-proj", SUBJ, "b")
    assert str(a.value).replace("crewboss-dash", "X") == str(b.value).replace("no-such-proj", "X")
    assert _ev(est["dash"]) == []


# 2 -------------------------------------------------------------------------------------------------------------------------
def test_no_subject_title_or_note_text_reaches_any_activity_row(est, mcp_caller):
    _as(mcp_caller, est["site"])
    hid = mcp_server.send_handoff("northwind-mobile", SUBJ, "body")["id"]
    _as(mcp_caller, est["mobile"])
    mcp_server.reply_handoff(hid, "r")
    mcp_server.close_handoff(hid)
    mcp_server.put_doc("some-doc", "TITLE-FREE-TEXT", "b", note="NOTE-FREE-TEXT")
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT summary FROM activity")
        rows = [r["summary"] for r in cur.fetchall()]
    assert rows and not any(x in r for r in rows for x in (SUBJ, "TITLE-FREE-TEXT", "NOTE-FREE-TEXT"))
    assert any(f"Handoff #{hid} to Northwind Mobile" == r for r in rows) and any(f"Handoff #{hid} closed" == r for r in rows)


# 3 -------------------------------------------------------------------------------------------------------------------------
def test_a_move_that_splits_a_handoff_across_accounts_is_refused(est, mcp_caller):
    _as(mcp_caller, est["site"])
    mcp_server.send_handoff("northwind-mobile", "s", "b")
    with pytest.raises(BadRequest) as e:
        tree.set_place(est["mobile"]["id"], "owner", est["acme"]["id"])
    assert "handoffs" in str(e.value)
    assert tree.same_account(est["mobile"]["id"], est["site"]["id"])             # nothing moved
    tree.set_place(est["mobile"]["id"], "owner", est["erp"]["id"])               # within the account is fine


def test_a_move_that_orphans_a_shared_doc_is_refused(est, mcp_caller):
    _as(mcp_caller, est["site"])
    mcp_server.put_doc("d", "D", "b")
    with pytest.raises(BadRequest):
        tree.set_place(est["northwind"]["id"], "owner", est["acme"]["id"], kind="project")


def test_even_if_a_project_moves_by_other_means_reply_and_close_do_not_cross(est, mcp_caller):
    _as(mcp_caller, est["site"])
    hid = mcp_server.send_handoff("northwind-mobile", "s", "b")["id"]
    with get_cursor() as cur:                                                      # bypass set_place
        cur.execute("UPDATE projects SET parent_id = %s WHERE id = %s", (est["acme"]["id"], est["mobile"]["id"]))
    before = len(_ev(repo.get_project(est["mobile"]["id"])))
    for call in (lambda: mcp_server.get_handoff(hid), lambda: mcp_server.reply_handoff(hid, "x"), lambda: mcp_server.close_handoff(hid)):
        with pytest.raises(NotFound):
            call()
    assert len(_ev(repo.get_project(est["mobile"]["id"]))) == before


# 4 -------------------------------------------------------------------------------------------------------------------------
@pytest.fixture
def mu(client, monkeypatch):
    from tests.test_multi_user import _user, local  # noqa: F401
    return _user


def test_person_level_visibility_of_threads_counts_and_docs(client, est, mcp_caller, monkeypatch, tmp_path):
    from tests import test_multi_user as tm
    monkeypatch.setattr(config, "WEB_AUTH", "required")
    monkeypatch.setattr(config, "AUTH_SETTING", "local")
    monkeypatch.setattr(config, "OIDC_CLIENT_ID", "")
    monkeypatch.setattr(config, "PUBLIC_URL", tm.ORIGIN)
    monkeypatch.setattr(config, "LOCAL_AUTH_KEY_FILE", str(tmp_path / "secrets" / "local-auth.key"))
    from argon2 import PasswordHasher
    cheap = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
    monkeypatch.setattr(tm.la, "_hasher", lambda: cheap)
    tm.la._dummy_hash.cache_clear()
    owner, vic = tm._user("owner", "owner"), tm._user("vic", "viewer")
    access.share(est["site"]["id"], vic["id"], "viewer", owner["id"])
    _as(mcp_caller, est["northwind"])                                                  # account session: from = the account (vic cannot see it)
    mcp_server.send_handoff("northwind-website", SUBJ, "BODY-FREE-TEXT")
    mcp_server.put_doc("internals", "Internals", "DOC-BODY-TEXT")
    c = tm._client(vic)
    page = c.get("/northwind-website/handoffs")
    assert page.status_code == 200
    assert SUBJ not in page.text and "BODY-FREE-TEXT" not in page.text                # a thread with an end vic cannot see is left out
    assert "DOC-BODY-TEXT" not in page.text and "visible to people who can see the whole account" in page.text
    assert "0</span> open handoff" in c.get("/northwind-website").text                    # and not counted
    assert handoffs.open_count(est["site"]) == 1 and handoffs.open_count(est["site"], visible={est["site"]["id"]}) == 0
    # the same rule for a session acting for such a person
    with get_cursor() as cur:
        cur.execute("UPDATE projects SET owner_user_id = %s WHERE id = %s", (vic["id"], est["site"]["id"]))
    _as(mcp_caller, repo.get_project(est["site"]["id"]))
    for call in (lambda: mcp_server.get_doc("internals"), mcp_server.list_docs, lambda: mcp_server.put_doc("x", "X", "b"),
                 lambda: mcp_server.send_handoff("northwind-mobile", "s", "b", doc_slugs=["internals"])):
        with pytest.raises(NotFound):
            call()


def test_threads_are_filtered_before_the_limit(est, mcp_caller):
    _as(mcp_caller, est["northwind"])
    for i in range(3):
        mcp_server.send_handoff("northwind-mobile", f"s{i}", "b")
    _as(mcp_caller, est["site"])
    mcp_server.send_handoff("northwind-mobile", "visible", "b")
    ids = {est["site"]["id"], est["mobile"]["id"]}
    assert [t["subject"] for t in handoffs.for_page(est["northwind"]["id"], None, ids)] == ["visible"]
    assert len(handoffs.for_page(est["northwind"]["id"], None, None)) == 4


# 5 -------------------------------------------------------------------------------------------------------------------------
def test_a_holder_with_dedicated_streams_gets_a_claimable_event(est, mcp_caller):
    _as(mcp_caller, est["site"])
    out = mcp_server.send_handoff("northwind-multi", "s", "b")
    assert out["delivered_to"] == "northwind-multi"
    assert [o["stream"] for o in _ev(est["multi"], "features")] == ["features"]       # 'features' when it has that session
    assert _ev(est["multi"], "bugs") == []
    row = _ev(est["multi"], "features")[0]
    assert repo.mark_outbox_delivered(row["id"], "mcp", stream="features", project=est["multi"])
    mcp_server.send_handoff("northwind-multi2", "s", "b")
    first = est["multi2"]["session_roles"][0]
    assert [o["stream"] for o in _ev(est["multi2"], first)] == [first]                  # else its first stream, in order


# 6 -------------------------------------------------------------------------------------------------------------------------
def test_nobody_to_deliver_to_is_said_plainly(est, mcp_caller):
    _as(mcp_caller, est["solo_a"])
    assert tree.session_holder(est["solo_b"]["id"]) is None                            # its account runs no session
    out = mcp_server.send_handoff("solo-b", "anyone", "b")
    assert out["delivered_to"] is None and out["id"]
    assert out["warning"] == ("nobody works Solo B right now: the handoff is stored and shows on the owner's Handoffs page; "
                              "it will be read when a session works that project")
    assert mcp_server.get_handoff(out["id"])["subject"] == "anyone"                    # stored all the same
    _as(mcp_caller, est["site"])
    assert mcp_server.send_handoff("northwind-mobile", "s", "b")["delivered_to"] == "northwind-mobile"


# 7 -------------------------------------------------------------------------------------------------------------------------
def test_flood_limit_and_event_coalescing(est, mcp_caller):
    _as(mcp_caller, est["site"])
    mobile = est["mobile"]
    for i in range(20):
        mcp_server.send_handoff("northwind-mobile", f"s{i}", "b")
    with pytest.raises(Conflict) as e:
        mcp_server.send_handoff("northwind-mobile", "one too many", "b")
    assert "close or reply on the open ones first" in str(e.value)
    evs = _ev(mobile)
    assert len(evs) == 1                                                                # 20 sends, one live event
    assert mcp_server.send_handoff("northwind-multi", "fine", "b")["id"]                   # the limit is per target
    # once the session claims the event, the next handoff is announced afresh
    assert repo.mark_outbox_delivered(evs[0]["id"], "mcp", stream="all", project=mobile)
    first = mcp_server.list_handoffs(direction="from_me")[-1]["id"]
    mcp_server.close_handoff(first)                                                     # frees a slot
    mcp_server.send_handoff("northwind-mobile", "after claim", "b")
    evs = _ev(mobile)
    assert len(evs) == 2 and sum(1 for o in evs if o["delivered_at"] is None) == 1


# 8 -------------------------------------------------------------------------------------------------------------------------
def test_secret_in_doc_title_or_note_is_refused(est, mcp_caller):
    _as(mcp_caller, est["site"])
    with pytest.raises(BadRequest):
        mcp_server.put_doc("a", SECRET, "b")
    with pytest.raises(BadRequest):
        mcp_server.put_doc("a", "t", "b", note=SECRET)
