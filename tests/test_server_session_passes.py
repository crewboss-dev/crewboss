"""The per-server switch for AI-session SSH passes (OSS review F3, owner decision q747).

POST /api/servers/{id}/ssh-cert hands a SESSION token its own one-hour shell. That is now OFF for every server until the owner
turns it on, per server, on the Machines page (with a fresh passkey tap). A pass a person approves (a deploy, a server read, Check
access) never used the route and is unchanged."""
import pytest

from app import server_access
from app.db import get_cursor

from test_multi_user import H, _client, _user, local  # noqa: F401  (local sign-in mode, real sessions)
from test_server_access import FakeSsh, _allow_sessions, _commissioned, _h, _pubkey, _via_pair, est  # noqa: F401


_n = iter(range(1000))


def _ssh_cert(client, est, sid, tmp_path, who="acme"):
    return client.post(f"/api/servers/{sid}/ssh-cert", json={"public_key": _pubkey(tmp_path, f"k{next(_n)}")}, headers=_h(est[who]))


def _switch(sid):
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT sessions_may_request_passes AS on_ FROM commissioned_servers WHERE id = %s", (sid,))
        return cur.fetchone()["on_"]


def test_a_new_server_refuses_a_session_pass_until_the_switch_is_on(client, est, monkeypatch, tmp_path):
    got = _commissioned(est, monkeypatch, sessions=False)
    assert got["sessions_may_request_passes"] is False                     # OFF by default
    r = _ssh_cert(client, est, got["id"], tmp_path)
    assert r.status_code == 403 and "doesn't let AI sessions" in r.json()["detail"]
    with get_cursor(commit=False) as cur:                                  # nothing was signed
        cur.execute("SELECT count(*) AS n FROM ssh_cert_issues WHERE actor <> 'dashboard-check'")
        assert cur.fetchone()["n"] == 0
    _allow_sessions(got["id"])
    assert _ssh_cert(client, est, got["id"], tmp_path).status_code == 200
    _allow_sessions(got["id"], on=False)
    assert _ssh_cert(client, est, got["id"], tmp_path).status_code == 403


def test_a_person_approved_pass_does_not_need_the_switch(est, monkeypatch):
    got = _commissioned(est, monkeypatch, sessions=False)                  # Check access signs its own pass
    assert got["status"] == "commissioned" and _switch(got["id"]) is False
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT actor FROM ssh_cert_issues")
        assert [r["actor"] for r in cur.fetchall()] == ["dashboard-check"]


def test_a_pass_through_a_jump_server_needs_both_switches(client, est, monkeypatch, tmp_path):
    core, mesh = _via_pair(est, monkeypatch)                               # the helper turns on the leaf only
    _allow_sessions(core["id"], on=False)
    monkeypatch.setattr(server_access, "_run", FakeSsh())
    server_access.check(est["acme"], mesh["id"])
    r = _ssh_cert(client, est, mesh["id"], tmp_path)
    assert r.status_code == 403 and "core-1" in r.json()["detail"]         # the pass would also open core-1
    _allow_sessions(core["id"])
    assert _ssh_cert(client, est, mesh["id"], tmp_path).status_code == 200


def test_only_the_owner_turns_it_on_and_it_needs_a_fresh_tap(client, est, monkeypatch):
    got = _commissioned(est, monkeypatch, sessions=False)
    url = f"/acme/servers/{got['id']}/session-passes"
    owner, designer, viewer = _user("boss", "owner"), _user("dee", "designer"), _user("vic", "viewer")
    with get_cursor() as cur:                                              # both are MEMBERS of the account's project, even so
        for u, role in ((designer, "admin"), (viewer, "viewer")):
            cur.execute("INSERT INTO project_members (project_id, user_id, role) VALUES (%s, %s, %s)", (est["acme"]["id"], u["id"], role))
    for person in (designer, viewer):
        assert _client(person).post(url, data={"allow": "1"}, headers=H).status_code in (403, 404)
        assert _switch(got["id"]) is False
    stale = _client(owner, fresh=False)
    r = stale.post(url, data={"allow": "1"}, headers=H)
    assert r.status_code == 403 and r.headers["HX-Trigger"] == "stepup-required" and _switch(got["id"]) is False
    fresh = _client(owner)
    assert fresh.post(url, data={"allow": "1"}, headers=H, follow_redirects=False).status_code == 303
    assert _switch(got["id"]) is True
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT summary FROM activity WHERE entity = 'server' AND entity_id = %s", (got["id"],))
        assert "Allowed AI sessions" in cur.fetchone()["summary"]
    # turning it OFF is never gated by a tap, and never by anyone but the owner
    assert _client(designer).post(url, data={"allow": "0"}, headers=H).status_code in (403, 404) and _switch(got["id"]) is True
    assert stale.post(url, data={"allow": "0"}, headers=H, follow_redirects=False).status_code == 303
    assert _switch(got["id"]) is False


def test_the_machines_card_shows_the_switch_and_its_plain_sentence(client, est, monkeypatch):
    got = _commissioned(est, monkeypatch, sessions=False)
    boss = _client(_user("boss", "owner"))
    html = boss.get("/acme/machines?tab=servers").text
    assert "data-session-passes" in html and 'data-on="false"' in html
    assert "Lets AI sessions open a 60-minute shell here without asking you." in html
    _allow_sessions(got["id"])
    html = boss.get("/acme/machines?tab=servers").text
    assert 'data-on="true"' in html and "Turn off" in html
