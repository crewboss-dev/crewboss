"""The Machines page for a designer (#810, visibility is #713's and unchanged): their own runners, and the servers the owner assigned them,
READ-ONLY (no paste line, no setup commands, no Check access, no Remove, no Add a server). Never another person's runner or a server that
isn't theirs. Sign-in is on (the multi-user fixtures)."""
import pytest

from app import access, config, devices, runner_identity
from app.db import get_cursor

from test_multi_user import _client, local, world  # noqa: F401  (fixtures)


@pytest.fixture(autouse=True)
def _identity(monkeypatch, tmp_path):
    keyfile = tmp_path / "runner-identity"
    runner_identity.keygen(str(keyfile))
    monkeypatch.setattr(config, "RUNNER_IDENTITY_KEY_FILE", str(keyfile))
    monkeypatch.setattr(config, "RUNNER_BASE_URL", "https://dash.example.test:8443")
    devices.reset_rate_limit()


def _server(account_id, name):
    with get_cursor() as cur:
        cur.execute("INSERT INTO commissioned_servers (account_id, name, address, login_user, status, host_key, created_by) "
                    "VALUES (%s, %s, '10.9.9.9', 'deploy', 'commissioned', 'mallen-dash-srv-1 ssh-ed25519 AAAA', 'owner') RETURNING id",
                    (account_id, name))
        return cur.fetchone()["id"]


def _runner(world, user, project, name):
    issued = devices.issue_code(user, project, "subscription")
    from test_devices import _keypair, _secret
    secret = _secret(issued["code"])
    _key, pub = _keypair()
    return devices.enrol(secret, pub, name, "linux", ip="10.0.0.7")


def test_a_designer_sees_their_own_runner_and_an_assigned_server_read_only(world):
    mine = _server(world["p1"]["id"], "d1-box")
    _server(world["p1"]["id"], "unassigned-box")                           # theirs by account, but never pinned to them
    access.assign_server(mine, world["d1"]["id"], None, world["p1"]["id"])
    _runner(world, world["d1"], world["p1"], "dees-pc")
    _runner(world, world["d2"], world["p2"], "dans-pc")
    runners = _client(world["d1"]).get("/d1-proj/machines?tab=runners").text
    html = _client(world["d1"]).get("/d1-proj/machines?tab=servers").text
    assert "dees-pc" in runners and "dans-pc" not in runners + html            # (#838) their runner is on Runners, the server on Servers
    assert "d1-box" in html and "unassigned-box" not in html + runners
    block = html.split('data-block="deploys"', 1)[1].split("</section>", 1)[0]
    for hidden in ("data-paste-line", "data-setup", "data-check", "data-remove", "data-access", "authorized_keys"):
        assert hidden not in block, hidden                                  # read-only: no CA line, no setup, no controls
    for page in (runners, html):
        assert "data-add-server" not in page and "A server that receives deploys" not in page      # the owner adds servers
        assert "+ Add a server" not in page and "data-add-btn=\"server\"" not in page and "data-sheet=\"server\"" not in page
    assert 'data-block="jobs"' in runners and "data-revoke" in runners        # their own runner is theirs to manage


def test_another_designers_machines_page_is_a_404(world):
    assert _client(world["d1"]).get("/d2-proj/machines").status_code == 404


def test_a_designer_cannot_join_a_runner_to_a_server(world):
    from app.main import app  # noqa: F401
    sid = _server(world["p1"]["id"], "d1-box")
    access.assign_server(sid, world["d1"]["id"], None, world["p1"]["id"])
    dev = _runner(world, world["d1"], world["p1"], "dees-pc")
    r = _client(world["d1"]).post(f"/d1-proj/machines/{dev['device_id']}/join", data={"server_id": sid},
                                  headers={"Origin": "https://crewboss.example.com", "HX-Request": "true"})
    assert r.status_code in (403, 404)
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT server_id FROM device_tokens WHERE id = %s", (dev["device_id"],))
        assert cur.fetchone()["server_id"] is None


def test_a_designer_sees_only_used_by_chips_of_projects_they_can_see(world):
    sid = _server(world["p1"]["id"], "d1-box")
    access.assign_server(sid, world["d1"]["id"], None, world["p1"]["id"])
    with get_cursor() as cur:
        for pid, name in ((world["p1"]["id"], "mine"), (world["p2"]["id"], "theirs")):
            cur.execute("INSERT INTO deploy_targets (project_id, server_id, name, repo_path, branch, created_by) VALUES (%s, %s, %s, '/srv/x', 'main', 'owner')",
                        (pid, sid, name))
    html = _client(world["d1"]).get("/d1-proj/machines?tab=servers").text
    assert "d1-proj · " in html and "d2-proj" not in html                           # another person's project is never named
    owner_html = _client(world["owner"]).get("/own-proj/machines?tab=servers").text
    assert owner_html is not None


def test_a_designer_never_sees_a_hidden_projects_host_in_the_tls_tooltip(world, monkeypatch):
    import time

    from app import health_reports, host_signal, tls_check

    sid = _server(world["p1"]["id"], "d1-box")
    access.assign_server(sid, world["d1"]["id"], None, world["p1"]["id"])
    with get_cursor() as cur:
        for pid, name in ((world["p1"]["id"], "mine"), (world["p2"]["id"], "theirs")):
            cur.execute("INSERT INTO deploy_targets (project_id, server_id, name, repo_path, branch, created_by) VALUES (%s, %s, %s, '/srv/x', 'main', 'owner')",
                        (pid, sid, name))
    health_reports.use_config({
        "d1-proj": {"mode": "push", "expect_every_seconds": 120, "sources": [], "erp": False, "erp_source": None,
                    "public_url": "https://mine.example.test"},
        "d2-proj": {"mode": "push", "expect_every_seconds": 120, "sources": [], "erp": False, "erp_source": None,
                    "public_url": "https://hidden-theirs.example.test"}})
    monkeypatch.setattr(host_signal, "_spawn", lambda fn: None)
    now = time.monotonic()
    host_signal._tls_cache.clear()
    host_signal._tls_cache["https://mine.example.test"] = (now, tls_check.Result(True, "", 60, "mine.example.test"))
    host_signal._tls_cache["https://hidden-theirs.example.test"] = (now, tls_check.Result(True, "", 20, "hidden-theirs.example.test"))
    try:
        html = _client(world["d1"]).get("/d1-proj/machines?tab=servers").text
        assert "mine.example.test" in html and "TLS 60 days left" in html
        assert "hidden-theirs" not in html and "TLS 20 days left" not in html
        assert "hidden-theirs.example.test" in _client(world["owner"]).get("/d1-proj/machines?tab=servers").text   # the owner sees both
    finally:
        health_reports.use_config(None)
        host_signal._tls_cache.clear()
