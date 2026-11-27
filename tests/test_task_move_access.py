"""Moving a task to another project needs the right to change the DESTINATION (OSS review F2).

Dee is a member of acme-x only. Posting move_to=acme-secret (a project in the same account she cannot see) used to move her task
there; now it is the same 404 as a project that does not exist, the task stays where it was, and nothing in the answer says the
hidden project exists (not its name, and no difference for an archived one or one in another account)."""
import pytest

from app import repo, web_auth
from app.db import get_cursor

from test_devices import _project, _user, est, signed_in  # noqa: F401
from test_web_auth import auth_on  # noqa: F401
from test_jobs import _task

DEE_OID = "dddddddd-0000-4000-8000-0000000000d1"
EVE_OID = "eeeeeeee-0000-4000-8000-0000000000e1"


@pytest.fixture
def world(est, client, monkeypatch, auth_on):
    w = dict(est)
    w["x"] = _project("acme-x", "acmex", est["acme"])
    w["y"] = _project("acme-y", "acmey", est["acme"])                       # Dee may change this one too
    w["secret"] = _project("acme-secret", "acmesec", est["acme"])
    w["archived"] = _project("acme-old-hidden", "acmeold", est["acme"])
    w["other"] = _project("beta-secret", "betasec", est["beta"])            # another account
    w["dee"], w["eve"] = _user(DEE_OID, "Dee"), _user(EVE_OID, "Eve")
    with get_cursor() as cur:
        cur.execute("UPDATE users SET role = 'designer', is_owner = false WHERE id = ANY(%s)", ([w["dee"]["id"], w["eve"]["id"]],))
        for pid, role in ((w["x"]["id"], "member"), (w["y"]["id"], "member")):
            cur.execute("INSERT INTO project_members (project_id, user_id, role) VALUES (%s, %s, %s)", (pid, w["dee"]["id"], role))
        cur.execute("INSERT INTO project_members (project_id, user_id, role) VALUES (%s, %s, 'viewer')", (w["y"]["id"], w["eve"]["id"]))
        cur.execute("INSERT INTO project_members (project_id, user_id, role) VALUES (%s, %s, 'member')", (w["x"]["id"], w["eve"]["id"]))
        cur.execute("UPDATE projects SET archived_at = now() WHERE id = %s", (w["archived"]["id"],))
    names = {"dee": w["dee"], "eve": w["eve"]}

    def resolve(tok):
        if tok not in names:
            return None
        with get_cursor(commit=False) as cur:
            cur.execute("SELECT id, role, is_owner FROM users WHERE id = %s", (names[tok]["id"],))
            u = cur.fetchone()
        return {"oid": names[tok]["oid"], "name": tok, "username": f"{tok}@x.test", "user_id": u["id"], "role": u["role"],
                "is_owner": u["is_owner"]}
    monkeypatch.setattr(web_auth, "resolve_session", resolve)
    w["client"] = client
    w["dee_h"] = {"cookie": f"{web_auth.SESSION_COOKIE}=dee", "hx-request": "true"}
    w["eve_h"] = {"cookie": f"{web_auth.SESSION_COOKIE}=eve", "hx-request": "true"}
    w["task"] = _task(w["x"], title="Dee's task")
    return w


def _move(w, who, slug):
    return w["client"].post(f"/acme-x/tasks/{w['task']['id']}", data={"move_to": slug}, headers=w[who + "_h"])


def _where(w):
    return repo.get_task(w["task"]["id"])["project_id"]


def test_a_member_cannot_move_a_task_into_a_project_she_cannot_see(world):
    w = world
    def activity():
        with get_cursor(commit=False) as cur:
            cur.execute("SELECT count(*) AS n FROM activity WHERE project_id = ANY(%s)", ([w["secret"]["id"], w["other"]["id"]],))
            return cur.fetchone()["n"]
    before = activity()
    missing = _move(w, "dee", "no-such-project-at-all")
    assert missing.status_code == 404
    for slug in ("acme-secret", "acme-old-hidden", "beta-secret"):         # hidden, hidden + archived, another account
        r = _move(w, "dee", slug)
        assert r.status_code == 404, slug
        assert r.text.replace(slug, "X") == missing.text.replace("no-such-project-at-all", "X")     # nothing tells them apart
        assert "Acme Secret" not in r.text and "Beta Secret" not in r.text
        assert _where(w) == w["x"]["id"]                                    # the task did not move
    assert activity() == before


def test_a_move_needs_edit_rights_on_the_destination_and_records_the_person(world):
    w = world
    t2 = _task(w["x"], title="Eve's task")
    r = w["client"].post(f"/acme-x/tasks/{t2['id']}", data={"move_to": "acme-y"}, headers=w["eve_h"])   # Eve only VIEWS acme-y
    assert r.status_code == 403 and repo.get_task(t2["id"])["project_id"] == w["x"]["id"]
    assert _move(w, "dee", "acme-y").status_code == 200                      # Dee may change both: it moves
    assert _where(w) == w["y"]["id"]
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT user_id FROM activity WHERE entity = 'task' AND entity_id = %s ORDER BY id DESC LIMIT 1", (w["task"]["id"],))
        assert cur.fetchone()["user_id"] == w["dee"]["id"]                   # the audit row names the real person
