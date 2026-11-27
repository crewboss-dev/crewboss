"""Runner devices (task #627, change #50, phase P1): users, device tokens bound to ONE user and ONE account, one-time
enrolment codes, the server's runner identity key, and the Runners page. A device token (cbr_...) is a different kind of
principal: every session and MCP surface refuses it. Uses real ed25519 keys."""
import base64
import re

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app import auth, config, db, devices, mcp_server, repo, runner_identity, tree, web_auth
from app.auth import issue_token
from app.db import get_cursor
from app.repo import BadRequest, NotFound
from app.routers import api

# The sign-in fixtures live in the sign-in tests (tests/ is on sys.path: no __init__.py).
from test_web_auth import OWNER_OID, _sign_in, auth_on  # noqa: F401


def _project(slug, short, parent=None, kind="project"):
    p = repo.create_project(slug, slug.replace("-", " ").title(), "internal", "owner", short_name=short,
                            session_roles=["all"])
    if parent is not None:
        tree.set_place(p["id"], "owner", None if kind == "account" or parent["slug"] == config.OVERSEER_PROJECT else parent["id"], kind=kind)   # no project root (change #76)
    return repo.get_project(p["id"])


@pytest.fixture
def est(client, monkeypatch, tmp_path):
    with db.connect() as conn:
        conn.execute("TRUNCATE projects RESTART IDENTITY CASCADE")
        conn.commit()
    keyfile = tmp_path / "runner-identity-key"
    runner_identity.keygen(str(keyfile))   # made on the host, once; the app only reads it
    monkeypatch.setattr(config, "RUNNER_IDENTITY_KEY_FILE", str(keyfile))
    monkeypatch.setattr(config, "RUNNER_BASE_URL", "https://dash.example.test:8443")
    monkeypatch.setattr(config, "OVERSEER_PROJECT", "mallen-dashboard")
    devices.reset_rate_limit()
    dash = _project("mallen-dashboard", "dash")
    acme = _project("acme", "acme", dash, kind="account")
    beta = _project("beta", "beta", dash, kind="account")
    return {"dash": dash, "acme": acme, "beta": beta}


def _user(oid="aaaaaaaa-0000-4000-8000-000000000001", name="Alice"):
    uid = web_auth.upsert_user({"oid": oid, "name": name})
    return {"id": uid, "oid": oid}


def _keypair():
    key = Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return key, base64.b64encode(raw).decode()


def _secret(code: str) -> str:
    return code.partition("#")[2].partition("-")[2]


def _enrol(est, mode="subscription", account="acme", ip="10.0.0.1", name="alice-pc"):
    user = _user()
    issued = devices.issue_code(user, est[account], mode)
    key, pub = _keypair()
    dev = devices.enrol(_secret(issued["code"]), pub, name, "linux", ip=ip)
    return dev, key, pub, user


def _activity(account):
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT verb, entity, summary FROM activity WHERE project_id = %s AND actor LIKE 'runner:%%' "
                    "ORDER BY id", (account["id"],))
        return cur.fetchall()


# --- kinds and token issuing ----------------------------------------------------------------------------------

def test_runner_kinds_disjoint_from_session_kinds():
    assert set(config.RUNNER_KINDS) == {"device", "job", "ca"}
    assert not set(config.RUNNER_KINDS) & set(config.SESSION_KINDS)
    assert not set(config.RUNNER_KINDS) & set(config.TOKEN_KINDS)
    assert not set(config.RUNNER_KINDS) & set(config.PROJECT_TOKEN_KINDS)


@pytest.mark.parametrize("kind", ["device", "job", "ca"])
def test_issue_token_refuses_device_kind(project, kind):
    with pytest.raises(ValueError, match="runner token"):
        issue_token(kind, "nope")


# --- a device token is a principal no session surface accepts --------------------------------------------------

def test_device_token_resolves_to_a_device_principal(est):
    dev, _key, _pub, user = _enrol(est)
    row = auth.resolve_token(dev["device_token"])
    assert row["stream"] == "device" and dev["device_token"].startswith("cbr_")
    assert row["user_id"] == user["id"] and row["account_id"] == est["acme"]["id"] and row["device_id"] == dev["device_id"]
    assert auth.resolve_session_token(dev["device_token"]) is None
    assert auth.resolve_token("mdj_whatever") is None   # P2: reserved, resolves to nothing
    assert auth.resolve_token("mdr_" + "x" * 40) is None
    assert auth.resolve_token("cbr_" + "x" * 40) is None and auth.resolve_token("cbj_whatever") is None


def test_ssh_cert_refuses_device_token(est, client):
    dev, *_ = _enrol(est)
    r = client.post("/api/servers/1/ssh-cert", json={"public_key": "ssh-ed25519 AAAA"},
                    headers={"Authorization": f"Bearer {dev['device_token']}"})
    assert r.status_code == 403
    # The refusal is the handler's own first line, not only require_session's.
    principal = auth.resolve_token(dev["device_token"])
    with pytest.raises(Exception) as exc:
        api.api_ssh_cert(1, api.SshCertRequest(public_key="ssh-ed25519 AAAA"), principal)
    assert getattr(exc.value, "status_code", None) == 403
    # ...and the REST surface in general.
    assert client.get("/api/servers", headers={"Authorization": f"Bearer {dev['device_token']}"}).status_code == 403


def test_mcp_refuses_device_token(est, mcp_caller):
    dev, *_ = _enrol(est)
    mcp_caller(dev["device_token"])
    assert mcp_server._current_session() is None
    with pytest.raises(PermissionError):
        mcp_server._require_session()
    with pytest.raises(PermissionError):
        mcp_server._require_write_session()


# --- enrolment ---------------------------------------------------------------------------------------------------

def test_enrol_code_single_use(est):
    user = _user()
    secret = _secret(devices.issue_code(user, est["acme"], "api")["code"])
    _key, pub = _keypair()
    assert devices.enrol(secret, pub, "pc", "windows", ip="1.1.1.1")["device_id"]
    with pytest.raises(devices.EnrolRefused):
        devices.enrol(secret, pub, "pc2", "windows", ip="1.1.1.1")
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT count(*) AS n FROM device_tokens")
        assert cur.fetchone()["n"] == 1


def test_enrol_code_expires_5_min(est):
    user = _user()
    issued = devices.issue_code(user, est["acme"], "api")
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT expires_at - created_at AS life FROM enrolment_codes")
        assert cur.fetchone()["life"].total_seconds() <= 300
    with get_cursor() as cur:   # five minutes later
        cur.execute("UPDATE enrolment_codes SET expires_at = now() - interval '1 second'")
    _key, pub = _keypair()
    with pytest.raises(devices.EnrolRefused):
        devices.enrol(_secret(issued["code"]), pub, "pc", "linux")
    # The table refuses a longer life outright.
    with pytest.raises(Exception):
        with get_cursor() as cur:
            cur.execute("INSERT INTO enrolment_codes (secret_hash, user_id, account_id, mode, expires_at) "
                        "VALUES ('h', %s, %s, 'api', now() + interval '10 minutes')", (user["id"], est["acme"]["id"]))


def test_enrol_per_ip_limit_blocks_the_11th_attempt(est, caplog):
    user = _user()
    secret = _secret(devices.issue_code(user, est["acme"], "api")["code"])
    _key, pub = _keypair()
    for i in range(devices.RATE_LIMIT_ATTEMPTS):
        with pytest.raises(devices.EnrolRefused):
            devices.enrol(f"wrong-guess-number-{i:04d}-xx", pub, "pc", "linux", ip="9.9.9.9")
    with caplog.at_level("WARNING", logger="crewboss.devices"):
        with pytest.raises(devices.RateLimited):   # even the RIGHT secret from that address is held off
            devices.enrol(secret, pub, "pc", "linux", ip="9.9.9.9")
    assert "rate limit refused 9.9.9.9" in caplog.text   # the refusal is logged with the address
    assert devices.enrol(secret, pub, "pc", "linux", ip="9.9.9.10")["device_id"]   # another address is fine


def test_wrong_guess_cannot_burn_live_code(est):
    user = _user()
    secret = _secret(devices.issue_code(user, est["acme"], "api")["code"])
    _key, pub = _keypair()
    for i in range(30):   # many wrong guesses, from many addresses: nothing is pinned to the live code
        with pytest.raises(devices.EnrolRefused):
            devices.enrol(f"wrong-guess-number-{i:04d}-xx", pub, "pc", "linux", ip=f"8.8.8.{i}")
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT used_at FROM enrolment_codes")
        assert cur.fetchone()["used_at"] is None
    assert devices.enrol(secret, pub, "pc", "linux", ip="8.8.9.1")["device_id"]


def test_non_ascii_code_is_refused_and_logged(est, client, caplog):
    _key, pub = _keypair()
    with caplog.at_level("WARNING", logger="crewboss.devices"):
        r = client.post("/runner/enrol", json={"code": "\u00b2\u00b2\u00b2\u00b2\u00b2\u00b2\u00b2\u00b2" * 4, "pubkey": pub})
    assert r.status_code == 403
    # no account behind it: logged against the overseer project (the server log when there is none)
    assert any("Malformed enrolment code" in a["summary"] for a in _activity(est["dash"])) or "Malformed" in caplog.text


def test_fp8_mismatch_is_logged(est, client, caplog):
    _key, pub = _keypair()
    with caplog.at_level("WARNING", logger="crewboss.devices"):
        r = client.post("/runner/enrol", json={"code": "dash.example.test:8443#aaaaaaaa-" + "x" * 30, "pubkey": pub})
    assert r.status_code == 403 and "fp8 mismatch" in caplog.text


def test_missing_identity_key_is_503_never_generated(est, client, monkeypatch, tmp_path):
    _key, pub = _keypair()
    for bad in (tmp_path / "nope", tmp_path):   # missing, and a directory
        monkeypatch.setattr(config, "RUNNER_IDENTITY_KEY_FILE", str(bad))
        assert client.get("/runner/identity").status_code == 503
        r = client.post("/runner/enrol", json={"code": "x" * 30, "pubkey": pub})
        assert r.status_code == 503 and "keygen" in r.json()["detail"]
    empty = tmp_path / "empty"
    empty.write_text("")
    monkeypatch.setattr(config, "RUNNER_IDENTITY_KEY_FILE", str(empty))
    assert client.get("/runner/identity").status_code == 503
    assert empty.read_text() == "" and not (tmp_path / "nope").exists()   # nothing was written


def test_identity_keygen_never_overwrites(tmp_path):
    path = tmp_path / "sub" / "key"
    fp = runner_identity.keygen(str(path))
    assert len(fp) == 8
    import os
    import stat
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    before = path.read_bytes()
    with pytest.raises(SystemExit):
        runner_identity.keygen(str(path))
    with pytest.raises(SystemExit):
        runner_identity.keygen(str(tmp_path))
    assert path.read_bytes() == before


def test_enrol_code_carries_fingerprint(est, client):
    user = _user()
    code = devices.issue_code(user, est["acme"], "subscription")["code"]
    m = re.fullmatch(r"dash\.example\.test:8443#([a-z2-7]{8})-([A-Za-z0-9_-]{32})", code)
    assert m, code
    ident = client.get("/runner/identity").json()
    assert m.group(1) == ident["fp8"] == runner_identity.fp8()
    assert base64.b64decode(ident["public_key"]) == runner_identity.public_key_raw()
    assert runner_identity.fp8() == m.group(1)   # stable across calls


def test_enrol_endpoint_with_the_whole_code(est, client):
    user = _user()
    code = devices.issue_code(user, est["acme"], "api")["code"]
    _key, pub = _keypair()
    wrong = code.replace("#", "#aaaaaaaa-x", 1)
    assert client.post("/runner/enrol", json={"code": wrong, "pubkey": pub}).status_code == 403   # another dashboard's
    r = client.post("/runner/enrol", json={"code": code, "pubkey": pub, "name": "alice-pc", "platform": "linux"})
    assert r.status_code == 200 and r.json()["device_token"].startswith("cbr_")
    assert client.post("/runner/enrol", json={"code": code, "pubkey": pub}).status_code == 403   # single use
    assert client.post("/runner/enrol", json={"code": "x", "pubkey": "not a key"}).status_code in (400, 403)


def test_enrol_stores_pubkey_not_private(est):
    user = _user()
    key, pub = _keypair()
    seed = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    raw_pub = base64.b64decode(pub)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    secret = _secret(devices.issue_code(user, est["acme"], "api")["code"])
    for bad in (base64.b64encode(seed + raw_pub).decode(), base64.b64encode(seed[:16]).decode(), pem.decode(), ""):
        with pytest.raises(BadRequest):
            devices.enrol(secret, bad, "pc", "linux")
    dev = devices.enrol(secret, pub, "pc", "linux")   # the refused tries did not burn the code
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT * FROM device_tokens WHERE id = %s", (dev["device_id"],))
        row = cur.fetchone()
    blob = repr(row)
    assert row["pubkey"] == pub
    assert base64.b64encode(seed).decode() not in blob and dev["device_token"] not in blob
    assert row["token_hash"] == auth.hash_token(dev["device_token"])
    # signatures check out against the stored public key, and only that
    sig = key.sign(b"hello")
    assert devices.verify_signature(dev["device_id"], b"hello", sig)
    assert devices.verify_signature(dev["device_id"], b"hello", base64.b64encode(sig).decode())
    assert not devices.verify_signature(dev["device_id"], b"other", sig)
    assert not devices.verify_signature(dev["device_id"], b"hello", b"x" * 64)
    assert not devices.verify_signature(99999, b"hello", sig)


def test_revoked_device_rejected(est):
    dev, key, *_ = _enrol(est)
    assert auth.resolve_token(dev["device_token"])
    devices.revoke(est["acme"]["id"], dev["device_id"], "user:1")
    assert auth.resolve_token(dev["device_token"]) is None
    assert not devices.verify_signature(dev["device_id"], b"m", key.sign(b"m"))
    assert devices.list_for(est["acme"]["id"])[0]["status"] == "revoked"
    devices.revoke(est["acme"]["id"], dev["device_id"], "user:1")   # twice is harmless


def test_expired_or_disabled_user_device_rejected(est):
    dev, _key, _pub, user = _enrol(est)
    with get_cursor() as cur:
        cur.execute("UPDATE users SET disabled_at = now() WHERE id = %s", (user["id"],))
    assert auth.resolve_token(dev["device_token"]) is None
    with get_cursor() as cur:
        cur.execute("UPDATE users SET disabled_at = NULL WHERE id = %s", (user["id"],))
        cur.execute("UPDATE device_tokens SET expires_at = now() - interval '1 second'")
    assert auth.resolve_token(dev["device_token"]) is None


def test_other_account_device_is_404(est):
    dev, *_ = _enrol(est, account="acme")
    with pytest.raises(NotFound):
        devices.revoke(est["beta"]["id"], dev["device_id"], "user:1")
    assert auth.resolve_token(dev["device_token"])   # untouched
    assert devices.list_for(est["beta"]["id"]) == []
    with pytest.raises(NotFound):
        devices.revoke(est["acme"]["id"], 99999, "user:1")


def _under(est, project):
    tree.set_place(project["id"], "owner", est["acme"]["id"])
    return repo.get_project(project["id"])


def test_a_device_must_belong_to_an_account(est):
    with pytest.raises(BadRequest):
        devices.issue_code(_user(), _under(est, repo.create_project("loose", "Loose", "internal", "owner")), "api")   # inside an account: no runner home
    with pytest.raises(BadRequest):
        devices.issue_code(_user(), est["acme"], "root")


# --- sign-in makes the users row ---------------------------------------------------------------------------------

def test_users_upserted_on_signin(client, auth_on):
    r = _sign_in(client, auth_on)
    assert r.status_code == 303
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT oid, display_name, disabled_at FROM users")
        rows = cur.fetchall()
    assert [(u["oid"], u["display_name"], u["disabled_at"]) for u in rows] == [(OWNER_OID, "Owner Person", None)]
    _sign_in(client, auth_on)   # again: still one row
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT count(*) AS n FROM users")
        assert cur.fetchone()["n"] == 1


# --- the Runners page --------------------------------------------------------------------------------------------

@pytest.fixture
def signed_in(monkeypatch, auth_on):   # auth_on: sign-in configured and required (tests/test_web_auth.py)
    monkeypatch.setattr(web_auth, "resolve_session", lambda tok: (
        {"id": 1, "oid": "bbbbbbbb-0000-4000-8000-000000000002", "name": "Bob", "username": "bob@x.test",
         "auth_age": 60}   # signed in with Microsoft a minute ago: fresh for the step-up (#809)
        if tok == "good" else None))
    return {"cookie": f"{web_auth.SESSION_COOKIE}=good"}


def test_runners_page_flow(est, client, signed_in, monkeypatch):
    h = {"cookie": signed_in["cookie"]}
    page = client.get("/acme/machines", headers=h)
    assert page.status_code == 200 and "No runners enrolled yet" in page.text and "/acme/runners" in page.text
    made = client.post("/acme/runners/codes", data={"mode": "api"}, headers=h)
    assert made.status_code == 200 and made.headers["cache-control"] == "no-store"
    code = re.search(r"data-code>([^<]+)<", made.text).group(1)
    assert code.startswith("dash.example.test:8443#") and "5 minutes" in made.text
    assert "data-code" not in client.get("/acme/machines", headers=h).text   # shown once
    assert 'data-section="howto"' in page.text and "crewboss-runner enrol" in page.text and "pipx install ./runner" in page.text
    assert "<details" in page.text and "open>" in page.text.split('data-section="howto"')[1].split(">")[0] + ">"   # open while no runner is enrolled
    _key, pub = _keypair()
    dev = client.post("/runner/enrol", json={"code": code, "pubkey": pub, "name": "bobs-pc", "platform": "windows"}).json()
    listing = client.get("/acme/machines", headers=h).text
    assert "bobs-pc" in listing and "api" in listing and "windows" in listing and "active" in listing and "Bob" in listing
    assert 'data-online="0"' in listing and ">offline<" in listing and ">online<" not in listing   # no socket
    from app import runner_ws
    real = runner_ws.connected
    monkeypatch.setattr(runner_ws, "connected", lambda did: did == dev["device_id"])
    live = client.get("/acme/machines", headers=h).text
    assert 'data-online="1"' in live and ">online<" in live and ">offline<" not in live
    monkeypatch.setattr(runner_ws, "connected", real)
    # another account's page never lists it, and revoking through the wrong account is a 404
    assert "bobs-pc" not in client.get("/beta/machines", headers=h).text
    # A page write that hits NotFound is the app's usual 303 back with a flash (pages.handle_repo_error); nothing changed.
    r = client.post(f"/beta/runners/{dev['device_id']}/revoke", headers=h, follow_redirects=False)
    assert r.status_code == 303 and auth.resolve_token(dev["device_token"])
    assert client.post(f"/acme/runners/{dev['device_id']}/revoke", headers=h, follow_redirects=False).status_code == 303
    assert auth.resolve_token(dev["device_token"]) is None
    gone = client.get("/acme/machines", headers=h).text
    assert "revoked" in gone and "data-online" not in gone   # no online chip once revoked
    assert client.post("/acme/runners/codes", data={"mode": "bad"}, headers=h).status_code == 400


def test_runners_page_needs_a_web_session_and_an_account(est, client, signed_in, monkeypatch):
    assert client.get("/acme/runners", follow_redirects=False).status_code == 302   # sign in first
    assert client.get("/acme/machines", headers={"cookie": signed_in["cookie"]}).status_code == 200
    assert client.get("/acme-nonexistent/machines", headers={"cookie": signed_in["cookie"]}).status_code == 404
    _project("acme-app", "acmeapp", est["acme"])
    assert client.get("/acme-app/machines", headers={"cookie": signed_in["cookie"]}).status_code == 404   # not an account
    monkeypatch.setattr(config, "WEB_AUTH", "off")   # no sign-in = no person to make a code for
    assert client.get("/acme/runners").status_code == 403


def test_runner_paths_are_open_to_the_gates():
    from app import origin_guard
    assert web_auth.is_open_path("/runner/enrol") and web_auth.is_open_path("/runner/identity")
    assert origin_guard.rejection_reason("POST", "/runner/enrol", {"sec-fetch-site": "cross-site"}) is None
    assert not web_auth.is_open_path("/acme/runners")


def test_revoke_with_a_non_latin1_name_does_not_500(est, client, signed_in):
    h = {"cookie": signed_in["cookie"]}
    user = _user()
    secret = _secret(devices.issue_code(user, est["acme"], "api")["code"])
    _key, pub = _keypair()
    dev = devices.enrol(secret, pub, "\U0001f4bb laptop \u4e2d", "linux")
    r = client.post(f"/acme/runners/{dev['device_id']}/revoke", headers=h, follow_redirects=False)
    assert r.status_code == 303
    assert auth.resolve_token(dev["device_token"]) is None   # the revoke stood
    page = client.get("/acme/machines", headers=h)
    assert page.status_code == 200 and "laptop" in page.text


def test_runners_page_shows_needs_attention_from_the_fixed_text(est, client, signed_in, monkeypatch):
    from app import runner_ws
    h = {"cookie": signed_in["cookie"]}
    code = re.search(r"data-code>([^<]+)<", client.post("/acme/runners/codes", data={"mode": "subscription"}, headers=h).text).group(1)
    _key, pub = _keypair()
    dev = client.post("/runner/enrol", json={"code": code, "pubkey": pub, "name": "pc-1", "platform": "linux"}).json()
    assert "data-attention-chip" not in client.get("/acme/machines", headers=h).text
    monkeypatch.setattr(runner_ws, "attention", lambda did: [runner_ws.ATTENTION_TEXT["not_signed_in"]] if did == dev["device_id"] else [])
    page = client.get("/acme/machines", headers=h).text
    assert "data-attention-chip" in page and "Claude Code is not signed in: run `claude` on that PC and log in" in page
    assert "If a job never shows up at claude.ai/code, run" in page
