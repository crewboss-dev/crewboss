"""Local sign-in (task #706; passkey-only since #809): email, then the browser's passkey prompt (discoverable credentials); a one-time
temporary password that only opens "add a passkey"; recovery codes; the migration path for a person with an old password + authenticator;
rate limits; sessions. The OIDC mode keeps its own tests (tests/test_web_auth.py). Step-up and admin reset: tests/test_passwordless.py."""
import base64
import hashlib
import json
import os
import re
import struct

import cbor2
import pytest
from argon2 import PasswordHasher
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from app import cli, config, local_auth as la, origin_guard, web_auth
from app.db import get_cursor

import time

HOST = "crewboss.example.com"
ORIGIN = "https://" + HOST


@pytest.fixture(autouse=True)
def local(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "WEB_AUTH", "required")
    monkeypatch.setattr(config, "AUTH_SETTING", "local")
    monkeypatch.setattr(config, "OIDC_CLIENT_ID", "")
    monkeypatch.setattr(config, "PUBLIC_URL", ORIGIN)
    monkeypatch.setattr(config, "LOCAL_AUTH_KEY_FILE", str(tmp_path / "secrets" / "local-auth.key"))
    cheap = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)      # speed only; the defaults are tested separately
    monkeypatch.setattr(la, "_hasher", lambda: cheap)
    la._dummy_hash.cache_clear()
    yield
    la._dummy_hash.cache_clear()


@pytest.fixture
def lc():
    from app.main import app
    return _tc(app)             # no lifespan: the session fixture already migrated the test DB


@pytest.fixture
def owner():
    uid, pw = la.create_owner("owner")
    return {"id": uid, "temp": pw}


def _tc(app, peer="127.0.0.1", base_url=ORIGIN):
    """A client with no lifespan. Its peer is loopback, a trusted proxy by default, so a test can set X-Forwarded-For; pass another
    peer to act as an untrusted address."""
    return TestClient(app, base_url=base_url, client=(peer, 50000))


def _identify(c, identifier="owner", **kw):
    return c.post("/auth/login/identify", data={"identifier": identifier, "next": "/"}, follow_redirects=False, **kw)


def _temp(c, pw, identifier="owner"):
    """Identifier, then the temporary password. Returns the password step's response."""
    _identify(c, identifier)
    return c.post("/auth/login/id/password", data={"password": pw}, follow_redirects=False)


def _codes(html):
    m = re.search(r"<pre[^>]*data-codes[^>]*>(.*?)</pre>", html, re.S)
    return m.group(1).split() if m else []


# --- the mode rule ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("setting,client_id,secret,expected", [
    ("", "", False, "local"),                  # nothing configured: local, the default
    ("", "client", True, "oidc"),              # OIDC already configured and no `auth:` key: unchanged behaviour
    ("", "client", False, "oidc"),             # a client id with a missing secret file: still oidc, so it fails closed (503) as before
    ("local", "client", True, "local"),        # an explicit key always wins
    ("oidc", "", False, "oidc"),
])
def test_mode_default_rule(monkeypatch, tmp_path, setting, client_id, secret, expected):
    f = tmp_path / "secret"
    if secret:
        f.write_text("s3cret\n")
    monkeypatch.setattr(config, "AUTH_SETTING", setting)
    monkeypatch.setattr(config, "OIDC_CLIENT_ID", client_id)
    monkeypatch.setattr(config, "OIDC_CLIENT_SECRET_FILE", str(f))
    assert web_auth.mode() == expected


def test_a_bad_auth_value_is_a_loud_error_at_start(tmp_path):
    import subprocess
    import sys
    home = tmp_path / "home"
    home.mkdir()
    (home / "instance.yaml").write_text("auth: google\n")
    env = {**os.environ, "CREWBOSS_HOME": str(home)}
    env.pop("CREWBOSS_AUTH", None)
    r = subprocess.run([sys.executable, "-c", "import app.config"], env=env, capture_output=True, text=True)
    assert r.returncode != 0 and "auth must be" in r.stderr



# --- a software authenticator -------------------------------------------------------------------------------------------

def _b64u(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


class SoftKey:
    """A software authenticator: user-verifying, none attestation, ES256."""

    def __init__(self):
        self.priv = ec.generate_private_key(ec.SECP256R1())
        self.cred_id = os.urandom(32)
        self.count = 0

    def _cose(self):
        n = self.priv.public_key().public_numbers()
        return cbor2.dumps({1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")})

    @staticmethod
    def _client(kind, challenge, origin):
        return json.dumps({"type": kind, "challenge": challenge, "origin": origin, "crossOrigin": False}).encode()

    def create(self, options_json, origin=ORIGIN, rp_id=HOST):
        opts = json.loads(options_json)
        client = self._client("webauthn.create", opts["challenge"], origin)
        auth = (hashlib.sha256(rp_id.encode()).digest() + bytes([0x45]) + struct.pack(">I", 0) + b"\x00" * 16
                + struct.pack(">H", len(self.cred_id)) + self.cred_id + self._cose())
        att = cbor2.dumps({"fmt": "none", "attStmt": {}, "authData": auth})
        return {"id": _b64u(self.cred_id), "rawId": _b64u(self.cred_id), "type": "public-key",
                "response": {"clientDataJSON": _b64u(client), "attestationObject": _b64u(att), "transports": ["internal"]},
                "clientExtensionResults": {}}

    def get(self, options_json, user_id, origin=ORIGIN, rp_id=HOST, count=None):
        opts = json.loads(options_json)
        self.count = count if count is not None else self.count + 1
        client = self._client("webauthn.get", opts["challenge"], origin)
        auth = hashlib.sha256(rp_id.encode()).digest() + bytes([0x05]) + struct.pack(">I", self.count)
        sig = self.priv.sign(auth + hashlib.sha256(client).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": _b64u(self.cred_id), "rawId": _b64u(self.cred_id), "type": "public-key",
                "response": {"clientDataJSON": _b64u(client), "authenticatorData": _b64u(auth), "signature": _b64u(sig),
                             "userHandle": _b64u(la.user_handle(user_id))},
                "clientExtensionResults": {}}




PW = "correct horse battery staple"
_CLOCK = {"t": 0}


@pytest.fixture(autouse=True)
def _totp_clock(monkeypatch):
    """TOTP time is a seam (la._now). Each code the tests ask for moves it one 30 s step on, so every code is a LATER step than the
    last one spent (the replay rule)."""
    _CLOCK["t"] = 0
    monkeypatch.setattr(la, "_now", lambda: time.time() + _CLOCK["t"])


def _code(user_id, pending=False):
    """A valid 6-digit authenticator code for this user at the next 30 s step (the pending secret during enrolment)."""
    import pyotp
    _CLOCK["t"] += 30
    u = la.get_user(user_id)
    secret = la.decrypt_secret(u["totp_pending_enc" if pending else "totp_secret_enc"])
    return pyotp.TOTP(secret).at(la._now())


def _register(c, key=None):
    """Add a passkey from a signed-in session (it needs the fresh confirmation sign-in gives). Returns the verify response."""
    key = key or SoftKey()
    r = c.post("/auth/enrol/passkey/options")
    assert r.status_code == 200, r.text
    return c.post("/auth/enrol/passkey/verify", json={"credential": key.create(r.text), "name": "x"})


def _mfa_enrol(c, user_id, password=PW):
    """From a restricted session at stage 'password': choose a password, confirm the authenticator, make and save the codes.
    Returns (codes, the final response)."""
    r = c.post("/auth/enrol/password", data={"password": password, "confirm": password}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/auth/enrol", r.text
    assert "data-totp-qr" in c.get("/auth/enrol").text
    r = c.post("/auth/enrol/totp", data={"code": _code(user_id, pending=True)}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return _finish(c)


def _finish(c):
    """After the authenticator: make the recovery codes, tick 'saved', continue. Returns (codes, the final response)."""
    r = c.post("/auth/enrol/codes")
    assert r.status_code == 200, r.text
    codes = _codes(r.text)
    return codes, c.post("/auth/enrol/complete", data={"saved": "on"}, follow_redirects=False)


def _enrol_owner(c, owner):
    """The whole first run: identifier, temporary password, password, authenticator, codes, then a passkey added from the new session
    (so the passkey tests have one). Returns (key, codes); the client ends signed in."""
    r = _temp(c, owner["temp"])
    assert r.status_code == 303 and r.headers["location"] == "/auth/enrol", r.text
    codes, r = _mfa_enrol(c, owner["id"])
    assert r.status_code == 303 and r.headers["location"] == "/account/security?notice=offer-passkey", r.text
    key = SoftKey()
    assert _register(c, key).status_code == 200
    return key, codes


def _passkey_signin(c, key, user_id, identifier="owner"):
    assert _identify(c, identifier).status_code == 303
    r = c.post("/auth/login/id/passkey/options")
    assert r.status_code == 200
    return c.post("/auth/login/id/passkey/verify", json=key.get(r.text, user_id))


def _password_signin(c, user_id, password=PW, identifier="owner", code=None):
    """Identifier, then password + code in one POST."""
    assert _identify(c, identifier).status_code == 303
    return c.post("/auth/login/id/password", data={"password": password, "code": code if code is not None else _code(user_id)},
                  follow_redirects=False)


def _enrol_owner_no_passkey(c, owner):
    """The first run without the optional passkey: the client ends signed in with password + authenticator only."""
    assert _temp(c, owner["temp"]).status_code == 303
    codes, r = _mfa_enrol(c, owner["id"])
    assert r.status_code == 303
    return codes


@pytest.fixture
def signed(lc, owner):
    """The owner, enrolled (a passkey + recovery codes), signed in on `client`; `second` is a fresh anonymous client."""
    key, codes = _enrol_owner(lc, owner)
    return {"key": key, "codes": codes, "id": owner["id"], "client": lc, "second": _tc(lc.app)}



# --- migrations / hashing -------------------------------------------------------------------------------------------------

def test_migrations_apply_twice(owner):
    for name in ("0042_local_signin.sql", "0086_signin_identifier.sql", "0087_passwordless_signin.sql", "0118_mfa_default.sql"):
        sql = open("db/migrations/" + name, encoding="utf-8").read()
        with get_cursor() as cur:
            cur.execute(sql)            # already applied once by the session fixture: IF NOT EXISTS all the way
    with get_cursor() as cur:
        cur.execute("SELECT username, is_owner FROM users WHERE id = %s", (owner["id"],))
        assert cur.fetchone() == {"username": "owner", "is_owner": True}


def test_argon2id_and_rehash(monkeypatch):
    pw = "correct horse battery staple"
    monkeypatch.setattr(la, "_hasher", lambda: PasswordHasher())
    la._dummy_hash.cache_clear()
    weak = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1).hash(pw)
    assert weak.startswith("$argon2id$")
    ok, new = la.verify_password(weak, pw)
    assert ok and new and new != weak
    assert la.verify_password(new, pw) == (True, None)
    assert la.verify_password(new, pw + "x") == (False, None)
    assert la.verify_password(None, pw) == (False, None)    # no such user: still does the work, never matches


def test_a_user_chosen_password_is_back_with_the_rules():
    """#976 reverses #809's "no password": a chosen password (12+ characters, not common) is the default sign-in."""
    assert la.PASSWORD_MIN == 12
    assert la.password_problem("short") and la.password_problem("password1234") and la.password_problem("a" * 129)
    assert la.password_problem("aaaaaaaaaaaa")
    assert la.password_problem("owner-owner-owner", "owner")
    assert la.password_problem(PW) == ""
    from app import users
    import inspect
    assert "password" not in inspect.signature(users.create_user).parameters        # people are still given a TEMPORARY one


# --- the temporary password -----------------------------------------------------------------------------------------------------

def test_temp_password_is_strong_hashed_and_expires_in_24_hours(owner):
    pw = owner["temp"]
    assert re.fullmatch(r"[a-z2-9]{4}(-[a-z2-9]{4}){4}", pw)
    u = la.get_user(owner["id"])
    assert u["password_temp"] and pw not in u["password_hash"] and u["password_hash"].startswith("$argon2")
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT password_expires_at BETWEEN now() + interval '23 hours 59 minutes' AND now() + interval '24 hours 1 minute' AS ok "
                    "FROM users WHERE id = %s", (owner["id"],))
        assert cur.fetchone()["ok"]
    assert len({la.new_temp_password() for _ in range(50)}) == 50


def test_temp_password_opens_only_the_enrolment_session(lc, owner):
    r = _temp(lc, owner["temp"])
    assert r.status_code == 303 and r.headers["location"] == "/auth/enrol"
    assert "Choose a password" in lc.get("/auth/enrol").text
    r = lc.get("/", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/auth/enrol"
    assert lc.get("/account/security", follow_redirects=False).headers["location"] == "/auth/enrol"
    assert lc.post("/anything/tasks/1/comments", follow_redirects=False).status_code == 403
    assert lc.get("/api/projects").status_code == 401
    assert lc.post("/account/security/reauth", data={"code": "123456"}).status_code == 403   # the gate: nothing outside /auth
    assert lc.post("/auth/enrol/codes").status_code == 409 and lc.post("/auth/enrol/complete", data={"saved": "on"}).status_code == 409
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT enrol_kind, enrol_stage FROM web_sessions WHERE enrolment_only")
        assert cur.fetchall() == [{"enrol_kind": "password", "enrol_stage": "password"}]
    assert lc.post("/auth/logout", follow_redirects=False).status_code == 303
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT enrol_kind, enrol_stage FROM web_sessions WHERE enrolment_only")
        assert cur.fetchall() == []                                                        # logout ended it


def test_temp_password_is_single_use(lc, owner):
    assert _temp(lc, owner["temp"]).status_code == 303
    again = _tc(lc.app)
    r = _temp(again, owner["temp"])
    assert r.status_code == 401 and web_auth.session_cookie_name() not in again.cookies
    u = la.get_user(owner["id"])
    assert u["password_hash"] is None and not u["password_temp"]                           # spent the moment it was used


def test_temp_password_expires(lc, owner):
    with get_cursor() as cur:
        cur.execute("UPDATE users SET password_expires_at = now() - interval '1 minute'")
    r = _temp(lc, owner["temp"])
    assert r.status_code == 401 and web_auth.session_cookie_name() not in lc.cookies


def test_a_wrong_or_missing_temp_password_is_refused(lc, owner):
    assert _temp(lc, "aaaa-bbbb-cccc-dddd-eeee").status_code == 401
    assert _temp(_tc(lc.app), "").status_code == 401


def test_temp_password_works_typed_without_dashes_or_capitals(lc, owner):
    r = _temp(lc, owner["temp"].replace("-", "").upper())
    assert r.status_code == 303 and r.headers["location"] == "/auth/enrol"


def test_enrolment_walks_password_then_authenticator_then_codes_and_the_password_stays(lc, owner):
    uid = owner["id"]
    _temp(lc, owner["temp"])
    # stage skipping: nothing later works before its stage
    for path, data in (("/auth/enrol/totp", {"code": "123456"}), ("/auth/enrol/codes", {}), ("/auth/enrol/complete", {"saved": "on"})):
        r = lc.post(path, data=data, follow_redirects=False)
        assert r.status_code in (303, 409), path
        if r.status_code == 303:
            assert r.headers["location"] == "/auth/enrol"
    assert la.get_user(uid)["totp_confirmed_at"] is None
    # password rules and the two boxes
    assert "at least 12" in lc.post("/auth/enrol/password", data={"password": "short", "confirm": "short"}).text
    assert "too common" in lc.post("/auth/enrol/password", data={"password": "password1234", "confirm": "password1234"}).text
    assert "The two passwords" in lc.post("/auth/enrol/password", data={"password": PW, "confirm": PW + "x"}).text
    assert la.get_user(uid)["password_hash"] is None                                       # none of that stored anything
    r = lc.post("/auth/enrol/password", data={"password": PW, "confirm": PW}, follow_redirects=False)
    assert r.status_code == 303
    page = lc.get("/auth/enrol").text
    assert "data-totp-qr" in page and "<svg" in page and "authenticator app" in page
    assert lc.get("/auth/enrol").text.count("data-totp-secret") == 1
    assert lc.post("/auth/enrol/codes").status_code == 409                                  # no authenticator yet
    assert lc.post("/auth/enrol/password", data={"password": PW, "confirm": PW}, follow_redirects=False).headers["location"] == "/auth/enrol"
    r = lc.post("/auth/enrol/totp", data={"code": "000000"})                                # a wrong code: still stage totp
    assert r.status_code == 400 and la.get_user(uid)["totp_confirmed_at"] is None
    assert lc.post("/auth/enrol/complete", data={"saved": "on"}).status_code == 409
    r = lc.post("/auth/enrol/totp", data={"code": _code(uid, pending=True)}, follow_redirects=False)
    assert r.status_code == 303
    assert lc.get("/", follow_redirects=False).headers["location"] == "/auth/enrol"        # still restricted: codes next
    assert "data-make-codes" in lc.get("/auth/enrol").text
    assert lc.post("/auth/enrol/password", data={"password": "another password here", "confirm": "another password here"},
                   follow_redirects=False).headers["location"] == "/auth/enrol"            # the password stage is over
    assert lc.post("/auth/enrol/complete", data={"saved": "on"}).status_code == 400        # no codes made yet
    page = lc.post("/auth/enrol/codes").text
    codes = _codes(page)
    assert len(codes) == 10 and "data-codes-copy" in page and "data-codes-download" in page and "data-codes-saved" in page
    assert lc.post("/auth/enrol/complete", data={}).status_code == 400                    # the tick is required
    assert lc.get("/", follow_redirects=False).status_code == 302
    r = lc.post("/auth/enrol/complete", data={"saved": "on"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/account/security?notice=offer-passkey"
    assert lc.get("/", follow_redirects=False).status_code == 200
    u = la.get_user(uid)
    assert la.has_mfa(u) and u["password_hash"] and not u["password_temp"]                # the password STAYS
    assert la.verify_password(u["password_hash"], PW)[0]
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT count(*) AS n FROM web_sessions WHERE enrolment_only")
        assert cur.fetchone()["n"] == 0
        cur.execute("SELECT amr FROM web_sessions WHERE user_id = %s", (uid,))
        assert cur.fetchone()["amr"] == ["pwd", "otp"]
        cur.execute("SELECT code_hash FROM recovery_codes WHERE user_id = %s", (uid,))
        hashes_ = {r["code_hash"] for r in cur.fetchall()}
    assert hashes_ == {la._recovery_hash(c.replace("-", "")) for c in codes}
    assert not hashes_ & {hashlib.sha256(c.replace("-", "").encode()).hexdigest() for c in codes}   # keyed, not bare SHA-256
    assert oct(os.stat(config.LOCAL_AUTH_KEY_FILE).st_mode & 0o777) == "0o600"
    assert "passkey" in lc.get("/account/security").text.lower()


def test_a_restricted_session_cannot_add_a_passkey_or_skip_the_authenticator(lc, owner):
    _temp(lc, owner["temp"])
    assert lc.post("/auth/enrol/passkey/options").status_code == 401                        # a restricted session is no signed-in one
    r = lc.post("/auth/enrol/password", data={"password": PW, "confirm": PW}, follow_redirects=False)
    assert r.status_code == 303
    # a database-level skip does not help: the codes stage needs a confirmed authenticator too
    with get_cursor() as cur:
        cur.execute("UPDATE web_sessions SET enrol_stage = 'codes' WHERE enrolment_only")
    assert lc.post("/auth/enrol/codes").status_code == 409
    assert lc.post("/auth/enrol/complete", data={"saved": "on"}).status_code == 409


# --- passkey options: rk + UV on enrolment, an EMPTY allowCredentials at sign-in -----------------------------------------------

def test_enrolment_requires_a_discoverable_credential_and_user_verification_and_no_attachment(lc, owner):
    _enrol_owner_no_passkey(lc, owner)
    opts = lc.post("/auth/enrol/passkey/options").json()
    sel = opts["authenticatorSelection"]
    assert sel["residentKey"] == "required" and sel["userVerification"] == "required"
    assert "authenticatorAttachment" not in sel                      # platform, phone and password-manager passkeys all stay possible
    assert opts["hints"] == ["client-device", "hybrid"] and opts["rp"]["id"] == HOST
    assert opts.get("attestation", "none") == "none"                 # no attestation is demanded: a security key is never refused


def test_signin_options_are_always_empty_allow_credentials_whoever_the_email_is(signed):
    with get_cursor() as cur:
        cur.execute("INSERT INTO users (oid, username, is_owner) VALUES ('o2', 'second-user', false)")
        cur.execute("INSERT INTO users (oid, username, is_owner, disabled_at) VALUES ('o3', 'gone-user', false, now())")
    la.set_email(signed["id"], "o@example.com")
    shapes = []
    for ident in ("owner", "o@example.com", "second-user", "gone-user", "nobody-at-all"):
        c = _tc(signed["client"].app)
        _identify(c, ident)
        r = c.post("/auth/login/id/passkey/options")
        o = r.json()
        assert r.status_code == 200 and o["allowCredentials"] == [] and o["userVerification"] == "required", ident
        assert o["hints"] == ["client-device", "hybrid"]
        shapes.append(sorted(o))
    assert all(s == shapes[0] for s in shapes)


def test_identified_page_offers_the_passkey_as_an_extra_and_the_password_form_first(signed):
    c = signed["second"]
    _identify(c)
    page = c.get("/auth/login/id").text
    assert 'data-passkey="authenticate"' in page and "data-auto" not in page and "Use a phone or tablet" in page and "Bluetooth" in page
    assert page.index("data-password-form") < page.index('data-passkey="authenticate"')            # password + code is the default
    assert "Lost your phone? Use a recovery code" in page and "6-digit code from your authenticator app" in page


# --- sign-in with a passkey --------------------------------------------------------------------------------------------------------

def test_identifier_then_passkey_signs_in_and_amr_is_hardware(signed):
    c = signed["second"]
    r = _passkey_signin(c, signed["key"], signed["id"])
    assert r.status_code == 200 and r.json()["redirect"] == "/"
    assert c.get("/", follow_redirects=False).status_code == 200
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT amr FROM web_sessions WHERE NOT enrolment_only ORDER BY id DESC LIMIT 1")
        assert cur.fetchone()["amr"] == ["hwk", "user"]
    assert c.get("/auth/login/id", follow_redirects=False).headers["location"] == "/auth/login"     # the flow is spent


def test_a_passkey_of_another_user_does_not_sign_in_the_identified_one(signed):
    with get_cursor() as cur:
        cur.execute("INSERT INTO users (oid, username, is_owner) VALUES ('o2', 'second-user', false)")
    c = _tc(signed["client"].app)
    _identify(c, "second-user")
    r = c.post("/auth/login/id/passkey/options")
    r = c.post("/auth/login/id/passkey/verify", json=signed["key"].get(r.text, signed["id"]))
    assert r.status_code == 401 and r.json()["error"] == "That passkey didn't work."
    assert c.get("/", follow_redirects=False).status_code == 302


def test_passkey_login_refuses_replays_clones_and_strangers(signed):
    c = _tc(signed["client"].app)
    _identify(c)
    r = c.post("/auth/login/id/passkey/options")
    good = signed["key"].get(r.text, signed["id"])
    assert c.post("/auth/login/id/passkey/verify", json=good).status_code == 200
    c2 = _tc(signed["client"].app)
    _identify(c2)
    r2 = c2.post("/auth/login/id/passkey/options")
    assert c2.post("/auth/login/id/passkey/verify", json=good).status_code == 401              # a replayed assertion: wrong challenge
    c3 = _tc(signed["client"].app)
    _identify(c3)
    r3 = c3.post("/auth/login/id/passkey/options")
    clone = signed["key"].get(r3.text, signed["id"], count=1)                                   # counter didn't advance
    assert c3.post("/auth/login/id/passkey/verify", json=clone).status_code == 401
    c4 = _tc(signed["client"].app)
    _identify(c4)
    r4 = c4.post("/auth/login/id/passkey/options")
    assert c4.post("/auth/login/id/passkey/verify", json=SoftKey().get(r4.text, signed["id"], count=5)).status_code == 401   # unknown key
    assert c4.post("/auth/login/id/passkey/verify", json=signed["key"].get(r4.text, signed["id"], origin="https://evil.example")).status_code == 401


def test_an_existing_credential_made_without_resident_key_required_still_signs_in(lc, owner):
    """The owner's passkey from before #809 (residentKey 'preferred'): no re-enrolment, no rk check on sign-in (only NEW enrolments need rk)."""
    key = SoftKey()
    user = la.get_user(owner["id"])
    options_json, challenge = la.registration_options(user)
    o = json.loads(options_json)
    o["authenticatorSelection"]["residentKey"] = "preferred"                    # what the old code asked for
    assert la.verify_registration(owner["id"], key.create(json.dumps(o)), challenge, "Bitwarden")
    la.clear_password(owner["id"])
    c = _tc(lc.app)
    assert _passkey_signin(c, key, owner["id"]).status_code == 200
    assert c.get("/", follow_redirects=False).status_code in (200, 302)          # the one-time "add a backup" nudge may redirect


def test_passkey_signin_keeps_the_password(lc, owner):
    """#976: the password stays when a passkey is added or used (it was wiped under #809)."""
    _enrol_owner(lc, owner)
    assert la.has_mfa(la.get_user(owner["id"]))
    c = _tc(lc.app)
    key = SoftKey()
    options_json, challenge = la.registration_options(la.get_user(owner["id"]))
    assert la.verify_registration(owner["id"], key.create(options_json), challenge, "")
    assert _passkey_signin(c, key, owner["id"]).status_code == 200
    assert la.get_user(owner["id"])["password_hash"]


def test_passkeys_are_off_without_https_but_the_password_and_code_form_is_there(lc, monkeypatch, owner):
    monkeypatch.setattr(config, "PUBLIC_URL", "http://crewboss.example.com")
    c = _tc(lc.app, base_url="http://crewboss.example.com")
    _identify(c)
    assert c.post("/auth/login/id/passkey/options").status_code == 403
    page = c.get("/auth/login/id").text
    assert "data-passkey=" not in page and "data-password-form" in page and "6-digit code" in page


def test_auth_passkeys_off_hides_passkeys_everywhere(lc, monkeypatch, owner):
    monkeypatch.setattr(config, "AUTH_PASSKEYS", "off", raising=False)
    from app import front_door
    monkeypatch.setattr(front_door, "passkeys_enabled_setting", lambda: False)
    c = _tc(lc.app)
    _identify(c)
    assert c.post("/auth/login/id/passkey/options").status_code == 403
    assert "data-passkey=" not in c.get("/auth/login/id").text


def test_passkey_name_comes_from_the_aaguid_where_known():
    assert la.passkey_label("08987058-cadc-4b81-b6e1-30de50dcbe96") == "Windows Hello"
    assert la.passkey_label("D548826E-79B4-DB40-A3D8-11116F7E8349") == "Bitwarden"
    assert la.passkey_label("bada5566-a7aa-401f-bd96-45619a55120d") == "1Password"
    assert la.passkey_label("fbfc3007-154e-4ecc-8c0b-6e020557d7bd") == "iCloud Keychain"
    assert la.passkey_label("ea9b8d66-4d01-1d21-3ce4-b6b48cb575d4") == "Google Password Manager"
    assert la.passkey_label("90a3ccdf-635c-4729-a248-9b709135078f") == "Microsoft Authenticator"
    assert la.passkey_label("00000000-0000-0000-0000-000000000000") == "Passkey" and la.passkey_label(None) == "Passkey"


def test_account_page_offers_the_passkey_upgrade_and_says_where_one_can_live(signed):
    page = signed["client"].get("/account/security").text
    assert "Passwordless sign-in with a passkey" in page and "optional" in page and "QR code" in page


# --- the sign-in page: password + code, generic failures ---------------------------------------------------------------------------------------

def test_login_page_shows_password_code_and_the_recovery_link_for_everyone(signed):
    for who in ("owner", "nobody-here"):
        c = _tc(signed["client"].app)
        _identify(c, who)
        page = c.get("/auth/login/id").text
        assert "data-password-form" in page and "6-digit code from your authenticator app" in page
        assert "Lost your phone? Use a recovery code" in page and "data-passkey=\"authenticate\"" in page


def test_password_and_code_signs_in_and_amr_is_pwd_otp(signed):
    c = _tc(signed["client"].app)
    r = _password_signin(c, signed["id"])
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert c.get("/", follow_redirects=False).status_code == 200
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT amr, second_factor_at FROM web_sessions WHERE user_id = %s ORDER BY id DESC LIMIT 1", (signed["id"],))
        row = cur.fetchone()
    assert row["amr"] == ["pwd", "otp"] and row["second_factor_at"] is not None           # signing in counts as a fresh step-up


def test_every_way_to_fail_is_the_same_answer(signed):
    """No such person, a disabled one, a passkey-only one, a wrong password, a wrong code and a reused code all look alike."""
    uid = signed["id"]
    app = signed["client"].app
    pk_only, _ = _make_user("passkeyonly")
    la.clear_password(pk_only)
    disabled, _ = _make_user("gone")
    with get_cursor() as cur:
        cur.execute("UPDATE users SET disabled_at = now() WHERE id = %s", (disabled,))
    attempts = [("owner", PW, "000000"), ("owner", "wrong password here", "CODE"), ("nobody", PW, "123456"),
                ("passkeyonly", PW, "123456"), ("gone", PW, "123456")]
    outs = []
    for who, pw, code in attempts:
        c = _tc(app)
        _identify(c, who)
        r = c.post("/auth/login/id/password", data={"password": pw, "code": _code(uid) if code == "CODE" else code}, follow_redirects=False)
        assert r.status_code == 401 and web_auth.session_cookie_name() not in c.cookies
        outs.append(re.sub(r"<strong data-identifier>.*?</strong>", "", r.text))
    assert len(set(outs)) == 1 and "Those sign-in details didn" in outs[0]


def test_a_right_password_with_no_code_does_not_sign_in(signed):
    c = _tc(signed["client"].app)
    _identify(c)
    assert c.post("/auth/login/id/password", data={"password": PW, "code": ""}, follow_redirects=False).status_code == 401
    assert c.post("/auth/login/id/password", data={"password": PW}, follow_redirects=False).status_code == 401


def test_a_totp_code_is_spent_by_sign_in_and_cannot_be_replayed_at_sign_in_or_step_up(signed):
    uid = signed["id"]
    code = _code(uid)
    r = _password_signin(_tc(signed["client"].app), uid, code=code)
    assert r.status_code == 303
    assert _password_signin(_tc(signed["client"].app), uid, code=code).status_code == 401      # the same code at sign-in
    c = signed["client"]
    with get_cursor() as cur:
        cur.execute("UPDATE web_sessions SET second_factor_at = now() - interval '6 minutes'")
    assert c.post("/account/security/reauth", data={"code": code}).status_code == 400          # the same code at step-up
    r = c.post("/account/security/reauth", data={"code": _code(uid)}, follow_redirects=False)  # the next one works
    assert r.status_code == 303 and r.headers["location"] == "/account/security?notice=confirmed"
    assert "data-reauth" not in c.get("/account/security").text                                # fresh again


def test_a_wrong_code_does_not_burn_the_real_one(signed):
    uid = signed["id"]
    code = _code(uid)
    c = _tc(signed["client"].app)
    _identify(c)
    assert c.post("/auth/login/id/password", data={"password": "wrong password here", "code": code}).status_code == 401
    assert _password_signin(_tc(signed["client"].app), uid, code=code).status_code == 303        # the code was not spent by the bad password


def test_a_reauth_code_is_totp_only_never_a_recovery_code(signed):
    c = signed["client"]
    with get_cursor() as cur:
        cur.execute("UPDATE web_sessions SET second_factor_at = now() - interval '6 minutes'")
    r = c.post("/account/security/reauth", data={"code": signed["codes"][0]}, headers={"accept": "application/json"})
    assert r.status_code == 401 and la.recovery_codes_left(signed["id"]) == 10


def test_the_failure_limits_apply_to_the_combined_sign_in(signed):
    app = signed["client"].app
    for _ in range(la.USER_THRESHOLD):
        c = _tc(app)
        _identify(c)
        assert c.post("/auth/login/id/password", data={"password": "wrong password here", "code": "000000"}).status_code == 401
    c = _tc(app)
    _identify(c)
    assert c.post("/auth/login/id/password", data={"password": PW, "code": _code(signed["id"])}).status_code == 429   # even the right ones wait


def test_a_passkey_only_user_can_still_sign_in_by_passkey_and_gets_the_backup_nudge_once(lc, owner):
    """Existing users are never locked out: a passkey and no password + authenticator still works, with a one-time nudge."""
    key = SoftKey()
    options_json, challenge = la.registration_options(la.get_user(owner["id"]))
    assert la.verify_registration(owner["id"], key.create(options_json), challenge, "")
    la.clear_password(owner["id"])
    r = _passkey_signin(lc, key, owner["id"])
    assert r.status_code == 200 and r.json()["redirect"] == "/account/security?notice=add-backup"
    page = lc.get("/account/security?notice=add-backup").text
    assert "Add a password and authenticator app" in page or "add a password and authenticator app" in page.lower()
    assert _passkey_signin(_tc(lc.app), key, owner["id"]).json()["redirect"] == "/"            # not again


def test_the_last_passkey_may_go_only_when_there_is_a_password_and_authenticator(lc, owner):
    key = SoftKey()
    options_json, challenge = la.registration_options(la.get_user(owner["id"]))
    la.verify_registration(owner["id"], key.create(options_json), challenge, "")
    only = la.list_passkeys(owner["id"])[0]["id"]
    la.clear_password(owner["id"])
    assert not la.remove_passkey(owner["id"], only)                                            # it is their only way in
    _make_mfa(owner["id"])
    assert la.remove_passkey(owner["id"], only)


def test_passkeys_are_stamped_with_their_rp_id_and_a_foreign_one_is_refused(lc, owner):
    key = SoftKey()
    options_json, challenge = la.registration_options(la.get_user(owner["id"]))
    assert la.verify_registration(owner["id"], key.create(options_json), challenge, "")
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT rp_id FROM webauthn_credentials WHERE user_id = %s", (owner["id"],))
        assert cur.fetchone()["rp_id"] == HOST
    with get_cursor() as cur:
        cur.execute("UPDATE webauthn_credentials SET rp_id = 'other.example.org' WHERE user_id = %s", (owner["id"],))
    la.clear_password(owner["id"])
    assert _passkey_signin(lc, key, owner["id"]).status_code == 401                              # made for another address
    with get_cursor() as cur:
        cur.execute("UPDATE webauthn_credentials SET rp_id = NULL WHERE user_id = %s", (owner["id"],))
    assert la.backfill_rp_id() == 1
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT rp_id FROM webauthn_credentials WHERE user_id = %s", (owner["id"],))
        assert cur.fetchone()["rp_id"] == HOST
    assert _passkey_signin(_tc(lc.app), key, owner["id"]).status_code == 200


def test_the_stepup_info_says_whether_a_passkey_is_offered(signed):
    assert signed["client"].get("/account/security/stepup-info").json() == {"passkey": True}
    assert _tc(signed["client"].app).get("/account/security/stepup-info", follow_redirects=False).status_code in (302, 401)


# --- adding the password + authenticator backup from the account page ----------------------------------------------------------------------------

def test_a_passkey_only_person_adds_a_password_and_authenticator_backup(lc, owner):
    key = SoftKey()
    options_json, challenge = la.registration_options(la.get_user(owner["id"]))
    la.verify_registration(owner["id"], key.create(options_json), challenge, "")
    la.clear_password(owner["id"])
    assert _passkey_signin(lc, key, owner["id"]).status_code == 200
    assert lc.post("/account/security/password", data={"password": "short", "confirm": "short"}).status_code == 400
    assert lc.post("/account/security/password", data={"password": PW, "confirm": PW}, follow_redirects=False).status_code == 303
    assert not la.has_mfa(la.get_user(owner["id"]))                                              # a password alone signs nobody in
    page = lc.post("/account/security/authenticator/start").text
    assert "data-totp-qr" in page
    assert lc.post("/account/security/authenticator/confirm", data={"code": "000000"}).status_code == 400
    r = lc.post("/account/security/authenticator/confirm", data={"code": _code(owner["id"], pending=True)})
    assert r.status_code == 200 and len(_codes(r.text)) == 10
    assert la.has_mfa(la.get_user(owner["id"]))
    assert _password_signin(_tc(lc.app), owner["id"]).status_code == 303


# --- recovery codes -----------------------------------------------------------------------------------------------------------------------

def test_recovery_code_signs_in_once_into_a_new_password_and_authenticator(signed):
    c = signed["second"]
    uid = signed["id"]
    old_secret = la.decrypt_secret(la.get_user(uid)["totp_secret_enc"])
    _identify(c)
    r = c.post("/auth/login/id/recovery", data={"code": signed["codes"][0].upper()}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/auth/enrol"
    page = c.get("/auth/enrol").text
    assert "recovery code is used up" in page and "data-current-password" in page and "Reset sign-in" in page and 'name="password"' not in page
    assert c.get("/", follow_redirects=False).headers["location"] == "/auth/enrol"            # nothing else is reachable
    assert c.get("/account/security", follow_redirects=False).headers["location"] == "/auth/enrol"
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT enrol_kind, enrol_stage, amr FROM web_sessions WHERE enrolment_only")
        row = cur.fetchone()
    assert row["enrol_kind"] == "recovery" and row["enrol_stage"] == "password"
    other = _tc(c.app)                                                                          # the code is consumed
    _identify(other)
    assert other.post("/auth/login/id/recovery", data={"code": signed["codes"][0]}, follow_redirects=False).status_code == 401
    assert la.recovery_codes_left(uid) == 9
    assert c.post("/auth/enrol/codes").status_code == 409                                      # the earlier stages first
    # q828: the current password is REQUIRED (a new one cannot be chosen here)
    assert c.post("/auth/enrol/password", data={"password": "a-brand-new-password-1", "confirm": "a-brand-new-password-1"}).status_code == 400
    r = c.post("/auth/enrol/password", data={"current": PW}, follow_redirects=False)
    assert r.status_code == 303
    assert c.post("/auth/enrol/totp", data={"code": _code(uid)}).status_code == 400            # the OLD authenticator's code is not accepted
    r = c.post("/auth/enrol/totp", data={"code": _code(uid, pending=True)}, follow_redirects=False)
    assert r.status_code == 303
    assert la.decrypt_secret(la.get_user(uid)["totp_secret_enc"]) != old_secret                # a NEW authenticator replaced it
    codes, r = _finish(c)
    assert r.status_code == 303
    assert c.get("/", follow_redirects=False).status_code == 200
    third = _tc(c.app)                                                                          # the OLD codes are void now
    _identify(third)
    assert third.post("/auth/login/id/recovery", data={"code": signed["codes"][1]}, follow_redirects=False).status_code == 401
    assert la.recovery_codes_left(uid) == 10
    assert _password_signin(_tc(c.app), uid).status_code == 303


def test_a_wrong_recovery_code_and_an_unknown_email_look_the_same(signed):
    a, b = signed["second"], _tc(signed["client"].app)
    _identify(a, "owner")
    _identify(b, "no-such-person")
    ra = a.post("/auth/login/id/recovery", data={"code": "aaaa-bbbb-cccc-dddd"})
    rb = b.post("/auth/login/id/recovery", data={"code": "aaaa-bbbb-cccc-dddd"})
    assert ra.status_code == rb.status_code == 401
    assert re.sub(r"<strong data-identifier>.*?</strong>", "", ra.text) == re.sub(r"<strong data-identifier>.*?</strong>", "", rb.text)


# --- an old user with a chosen password + authenticator and no passkey just signs in the normal way ------------------------------------------------

def _make_mfa(user_id, password=PW):
    """Give a user a chosen password and a confirmed authenticator (what a finished enrolment leaves)."""
    import pyotp
    with get_cursor() as cur:
        cur.execute("UPDATE users SET password_hash = %s, password_temp = false, password_expires_at = NULL, totp_secret_enc = %s, "
                    "totp_confirmed_at = now(), totp_last_step = NULL WHERE id = %s",
                    (la.hash_password(password), la.encrypt_secret(pyotp.random_base32()), user_id))


def _make_user(name):
    with get_cursor() as cur:
        cur.execute("INSERT INTO users (oid, display_name, username, role) VALUES (%s, %s, %s, 'designer') RETURNING id",
                    ("local:" + name, name, name))
        uid = cur.fetchone()["id"]
    _make_mfa(uid)
    return uid, name


def test_a_pre_809_person_with_password_and_authenticator_signs_in_normally(lc, owner):
    _make_mfa(owner["id"], "an old chosen passphrase")
    r = _password_signin(lc, owner["id"], password="an old chosen passphrase")
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert lc.get("/", follow_redirects=False).status_code == 200


def test_a_chosen_password_does_not_work_without_an_authenticator(lc, owner):
    _make_mfa(owner["id"])
    with get_cursor() as cur:
        cur.execute("UPDATE users SET totp_confirmed_at = NULL WHERE id = %s", (owner["id"],))
    _identify(lc)
    assert lc.post("/auth/login/id/password", data={"password": PW, "code": "123456"}, follow_redirects=False).status_code == 401
    assert web_auth.session_cookie_name() not in lc.cookies


def test_the_migration_clears_the_password_of_anyone_with_a_passkey(lc, owner):
    key = SoftKey()
    options_json, challenge = la.registration_options(la.get_user(owner["id"]))
    la.verify_registration(owner["id"], key.create(options_json), challenge, "")
    sql = open("db/migrations/0087_passwordless_signin.sql", encoding="utf-8").read()
    with get_cursor() as cur:
        cur.execute(sql)
        cur.execute(open("db/migrations/0118_mfa_default.sql", encoding="utf-8").read())      # 0087 narrowed the stage check again
    assert la.get_user(owner["id"])["password_hash"] is None


# --- no enumeration; the same answer and timing for an unknown email --------------------------------------------------------------------------

def _skeleton(html):
    return re.sub(r"<strong data-identifier>.*?</strong>", "", html)


def test_unknown_identifier_gets_the_same_pages_and_failures(lc, owner):
    known, unknown = _tc(lc.app), _tc(lc.app)
    ra, rb = _identify(known, "owner"), _identify(unknown, "nobody-here@example.com")
    assert (ra.status_code, ra.headers["location"]) == (rb.status_code, rb.headers["location"]) == (303, "/auth/login/id")
    assert _skeleton(known.get("/auth/login/id").text) == _skeleton(unknown.get("/auth/login/id").text)
    pa = known.post("/auth/login/id/password", data={"password": "wrong wrong wrong"})
    pb = unknown.post("/auth/login/id/password", data={"password": "wrong wrong wrong"})
    assert pa.status_code == pb.status_code == 401 and _skeleton(pa.text) == _skeleton(pb.text)
    oa, ob = known.post("/auth/login/id/passkey/options"), unknown.post("/auth/login/id/passkey/options")
    assert oa.status_code == ob.status_code == 200
    strip = lambda t: {k: v for k, v in json.loads(t).items() if k != "challenge"}
    assert strip(oa.text) == strip(ob.text)
    va = known.post("/auth/login/id/passkey/verify", json={"id": "x", "rawId": "x"})
    vb = unknown.post("/auth/login/id/passkey/verify", json={"id": "x", "rawId": "x"})
    assert va.status_code == vb.status_code == 401 and va.json() == vb.json()


def test_unknown_and_disabled_users_still_cost_a_password_hash(lc, owner, monkeypatch):
    calls = []
    real = la._hasher()

    class Spy:
        def verify(self, h, p):
            calls.append(h)
            return real.verify(h, p)

        def __getattr__(self, name):
            return getattr(real, name)

    monkeypatch.setattr(la, "_hasher", lambda: Spy())
    for ident in ("ghost", "owner"):
        c = _tc(lc.app)
        _identify(c, ident)
        calls.clear()
        c.post("/auth/login/id/password", data={"password": "whatever password"})
        assert len(calls) == 1, ident
    with get_cursor() as cur:
        cur.execute("UPDATE users SET disabled_at = now()")
    c = _tc(lc.app)
    _identify(c)
    calls.clear()
    assert c.post("/auth/login/id/password", data={"password": owner["temp"]}).status_code == 401
    assert len(calls) == 1 and la.get_user(owner["id"])["password_hash"]                       # refused, and not spent


# --- rate limits -------------------------------------------------------------------------------------------------------------------------------

def test_the_limit_is_keyed_on_the_resolved_user_so_email_and_username_share_one_counter(lc, owner):
    la.set_email(owner["id"], "boss@example.com")
    for _ in range(la.USER_THRESHOLD):
        c = _tc(lc.app)
        _identify(c, "boss@example.com")                      # typed as the EMAIL
        assert c.post("/auth/login/id/password", data={"password": "wrong wrong wrong"}).status_code == 401
    c = _tc(lc.app)
    assert _identify(c, "owner").status_code == 303            # the identifier step never shows a per-person lock (no email↔username oracle)
    r = c.post("/auth/login/id/password", data={"password": "wrong wrong wrong"})
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0   # typed as the USERNAME: the same person, so the same wait


def test_failures_back_off_then_even_the_right_password_waits(lc, owner):
    for _ in range(la.USER_THRESHOLD):
        _identify(lc)
        assert lc.post("/auth/login/id/password", data={"password": "wrong wrong wrong"}).status_code == 401
    _identify(lc)
    r = lc.post("/auth/login/id/password", data={"password": owner["temp"]})
    assert r.status_code == 429 and "Too many attempts" in r.text
    assert la.get_user(owner["id"])["password_hash"]                                            # and the temporary password was not spent


def test_backoff_doubles(owner):
    key = la.user_key("owner")
    with get_cursor() as cur:
        for _ in range(5):
            cur.execute("INSERT INTO login_attempts (key, at) VALUES (%s, now())", (key,))
    first = la.lockout_seconds([(key, la.USER_THRESHOLD)])
    assert 25 <= first <= 31
    with get_cursor() as cur:
        cur.execute("INSERT INTO login_attempts (key, at) VALUES (%s, now())", (key,))
    assert 55 <= la.lockout_seconds([(key, la.USER_THRESHOLD)]) <= 61
    with get_cursor() as cur:
        cur.execute("UPDATE login_attempts SET at = now() - interval '2 hours'")
    assert la.lockout_seconds([(key, la.USER_THRESHOLD)]) == 0


def test_rate_limits_cover_the_identifier_step_and_do_not_depend_on_the_name_existing(lc, owner):
    for i in range(la.ID_STEP_THRESHOLD):
        assert _identify(lc, f"nobody{i}").status_code == 303
    assert _identify(lc, "owner").status_code == 429
    assert _identify(lc, "owner", headers={"x-forwarded-for": "203.0.113.77"}).status_code == 303   # another address is unaffected


def test_failures_from_one_address_do_not_lock_the_owner_out_elsewhere(lc, owner):
    attacker = {"x-forwarded-for": "198.51.100.7"}
    for _ in range(la.USER_THRESHOLD):
        c = _tc(lc.app)
        _identify(c, headers=attacker)
        c.post("/auth/login/id/password", data={"password": "wrong wrong wrong"}, headers=attacker)
    c = _tc(lc.app)
    _identify(c, headers=attacker)
    assert c.post("/auth/login/id/password", data={"password": "wrong wrong wrong"}, headers=attacker).status_code == 429
    c = _tc(lc.app)
    _identify(c, headers={"x-forwarded-for": "192.0.2.44"})
    assert c.post("/auth/login/id/password", data={"password": owner["temp"]}, headers={"x-forwarded-for": "192.0.2.44"}).status_code != 429


def test_the_per_username_ceiling_stops_a_spread_out_guess(lc, owner):
    with get_cursor() as cur:
        for _ in range(la.USER_CEILING):
            cur.execute("INSERT INTO login_attempts (key) VALUES (%s)", (la.user_key("owner"),))
    other = {"x-forwarded-for": "192.0.2.45"}
    assert _identify(lc, "owner", headers=other).status_code == 303          # the identifier step shows nothing
    assert lc.post("/auth/login/id/password", data={"password": owner["temp"]}, headers=other).status_code == 429   # the credential step waits


def test_flow_dies_after_five_wrong_answers(lc, owner):
    _identify(lc)
    for _ in range(5):
        lc.post("/auth/login/id/recovery", data={"code": "aaaa-bbbb-cccc-dddd"})
    assert lc.get("/auth/login/id", follow_redirects=False).headers["location"] == "/auth/login"


def test_xff_is_believed_only_from_a_trusted_peer(monkeypatch):
    from types import SimpleNamespace
    def req(peer, xff):
        return SimpleNamespace(client=SimpleNamespace(host=peer), headers={"x-forwarded-for": xff} if xff else {})
    assert la.client_ip(req("203.0.113.50", "1.2.3.4")) == "203.0.113.50"
    assert la.client_ip(req("127.0.0.1", "9.9.9.9, 1.2.3.4")) == "1.2.3.4"       # the default trusts loopback only (N3)
    assert la.client_ip(req("fd00::5", "9.9.9.9, 1.2.3.4")) == "fd00::5"         # a private peer (the old default trusted fc00::/7) is NOT a proxy unless listed
    assert la.client_ip(req("fd12:3456::2", "1.2.3.4")) == "fd12:3456::2"
    assert la.client_ip(req("127.0.0.1", "not an address")) == "127.0.0.1"
    assert la.client_ip(req("10.1.2.3", "")) == "10.1.2.3"
    monkeypatch.setattr(config, "TRUSTED_PROXIES", ("198.51.100.2/32",))            # what the installer writes: the actual proxy
    assert la.client_ip(req("198.51.100.2", "1.2.3.4")) == "1.2.3.4"
    assert la.client_ip(req("198.51.100.3", "1.2.3.4")) == "198.51.100.3"
    monkeypatch.setattr(config, "TRUSTED_PROXIES", ("203.0.113.0/24",))
    assert la.client_ip(req("203.0.113.50", "1.2.3.4")) == "1.2.3.4"
    assert la.client_ip(req("10.1.2.3", "1.2.3.4")) == "10.1.2.3"


def test_recovery_codes_are_80_bits_and_keyed(owner):
    codes = la.new_recovery_codes(owner["id"])
    assert all(re.fullmatch(r"[a-z2-9]{4}(-[a-z2-9]{4}){3}", c) for c in codes)
    assert la.use_recovery_code(owner["id"], codes[0].upper()) and not la.use_recovery_code(owner["id"], codes[0])
    assert not la.use_recovery_code(owner["id"], "abcde-fghjk")
    assert not la.use_recovery_code(None, codes[1])                                              # no user: the same work, never a match


# --- sessions and cookies --------------------------------------------------------------------------------------------------------------------

def test_session_cookie_flags_and_rotation(signed):
    c = signed["second"]
    c.cookies.set(web_auth.session_cookie_name(), "attacker-chosen-or-old-token")
    r = _passkey_signin(c, signed["key"], signed["id"])
    header = next(h for h in r.headers.get_list("set-cookie") if h.startswith(web_auth.session_cookie_name() + "="))
    assert "HttpOnly" in header and "Secure" in header and "samesite=lax" in header.lower()
    assert "attacker-chosen" not in header and web_auth.session_cookie_name().startswith("__Host-")


def test_session_cookie_over_plain_http_is_not_secure(monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_URL", "http://crewboss.example.com")
    assert web_auth.session_cookie_name() == "crewboss_session"
    from starlette.responses import Response
    resp = Response()
    web_auth.set_cookie(resp, web_auth.session_cookie_name(), "t", 60)
    assert "Secure" not in resp.headers["set-cookie"] and "HttpOnly" in resp.headers["set-cookie"]


def test_session_idle_timeout_and_absolute_lifetime(signed, monkeypatch):
    monkeypatch.setattr(config, "WEB_IDLE_MINUTES", 30)
    c = signed["client"]
    assert c.get("/", follow_redirects=False).status_code == 200
    with get_cursor() as cur:
        cur.execute("UPDATE web_sessions SET last_seen_at = now() - interval '31 minutes'")
    assert c.get("/", follow_redirects=False).status_code == 302
    monkeypatch.setattr(config, "WEB_IDLE_MINUTES", 43200)
    c2 = _tc(c.app)
    _passkey_signin(c2, signed["key"], signed["id"])
    assert c2.get("/", follow_redirects=False).status_code == 200
    with get_cursor() as cur:
        cur.execute("UPDATE web_sessions SET expires_at = now() - interval '1 second'")
    assert c2.get("/", follow_redirects=False).status_code == 302


def test_sign_out_everywhere_revokes_other_sessions(signed):
    a, b = signed["client"], signed["second"]
    _passkey_signin(b, signed["key"], signed["id"])
    assert a.get("/", follow_redirects=False).status_code == 200 and b.get("/", follow_redirects=False).status_code == 200
    r = a.post("/account/security/signout-everywhere", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/auth/signed-out"
    assert b.get("/", follow_redirects=False).status_code == 302 and a.get("/", follow_redirects=False).status_code == 302
    assert la.get_user(signed["id"])["session_epoch"] == 2        # create_owner's wipe made it 1


def test_a_stale_epoch_ends_a_session_even_if_the_row_survives(signed):
    with get_cursor() as cur:
        cur.execute("UPDATE users SET session_epoch = session_epoch + 1")
    assert signed["client"].get("/", follow_redirects=False).status_code == 302


def test_create_owner_refuses_a_second_owner(owner):
    with pytest.raises(la.OwnerExists):
        la.create_owner("other")


def test_create_owner_reset_wipes_every_way_in(signed):
    uid, pw = la.create_owner("owner", reset=True)
    assert uid == signed["id"] and pw != ""
    assert signed["client"].get("/", follow_redirects=False).status_code == 302
    assert la.passkey_count(uid) == 0 and la.recovery_codes_left(uid) == 0 and la.get_user(uid)["password_temp"]
    assert la.get_user(uid)["totp_confirmed_at"] is None


# --- the gate in local mode -----------------------------------------------------------------------------------------------------------------------

def test_gate_open_paths_in_local_mode(lc):
    assert lc.get("/auth/login").status_code == 200
    assert lc.get("/healthz").status_code == 200
    assert lc.get("/", follow_redirects=False).headers["location"] == "/auth/login?next=%2F"
    assert lc.get("/account/security", follow_redirects=False).status_code == 302
    assert lc.get("/auth/enrol", follow_redirects=False).headers["location"] == "/auth/login"
    assert lc.get("/api/projects").status_code == 401
    r = lc.post("/x/tasks/1/comments", headers={"HX-Request": "true", "HX-Current-URL": ORIGIN + "/x/tasks"})
    assert r.status_code == 401 and r.headers["HX-Redirect"].startswith("/auth/login")


def test_login_page_asks_for_one_field_and_says_when_there_is_no_owner(lc):
    page = lc.get("/auth/login").text
    assert 'name="identifier"' in page and "Email or username" in page and 'name="password"' not in page and "data-no-owner" in page
    la.create_owner("owner")
    assert "data-no-owner" not in lc.get("/auth/login").text


def test_local_routes_do_not_exist_in_oidc_mode(lc, monkeypatch):
    monkeypatch.setattr(config, "AUTH_SETTING", "oidc")
    assert lc.post("/auth/login/identify", data={"identifier": "x"}, follow_redirects=False).status_code == 404
    assert lc.get("/auth/enrol", follow_redirects=False).status_code == 404
    assert lc.get("/auth/login/id", follow_redirects=False).status_code == 404


def test_signed_in_user_row_for_a_local_user(signed):
    from types import SimpleNamespace
    resolved = web_auth.resolve_session(signed["client"].cookies.get(web_auth.session_cookie_name()))
    req = SimpleNamespace(headers={}, state=SimpleNamespace(web_user=resolved))
    row = web_auth.signed_in_user_row(req)
    assert row["id"] == signed["id"] and row["oid"] == "local:owner"


def test_every_new_post_is_covered_by_the_origin_guard(lc):
    from app.routers import auth_pages, local_auth_pages
    posts = sorted({r.path for router in (auth_pages.router, local_auth_pages.router) for r in router.routes
                    if "POST" in getattr(r, "methods", ())})
    assert len(posts) >= 12, posts
    for path in posts:
        concrete = path.replace("{cred_id}", "1").replace("{session_id}", "1")
        assert origin_guard.rejection_reason("POST", concrete, {"origin": "https://evil.example"}), path
        assert origin_guard.rejection_reason("POST", concrete, {"sec-fetch-site": "cross-site"}), path
    assert lc.post("/auth/login/identify", data={"identifier": "owner"}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert lc.post("/account/security/signout-everywhere", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403


# --- the account page ------------------------------------------------------------------------------------------------------------------------------

def test_the_last_passkey_cannot_be_removed_but_one_of_two_can(lc, owner):
    key, _codes_ = _enrol_owner(lc, owner)
    only = la.list_passkeys(owner["id"])[0]["id"]
    with get_cursor() as cur:                                         # no password + authenticator: the passkey is the only way in
        cur.execute("UPDATE users SET password_hash = NULL WHERE id = %s", (owner["id"],))
    r = lc.post(f"/account/security/passkeys/{only}/remove", follow_redirects=False)
    assert r.status_code == 400 and la.passkey_count(owner["id"]) == 1
    opts = lc.post("/auth/enrol/passkey/options")
    assert opts.status_code == 200
    k2 = SoftKey()
    assert lc.post("/auth/enrol/passkey/verify", json={"credential": k2.create(opts.text)}).json()["redirect"].startswith("/account/security")
    assert la.passkey_count(owner["id"]) == 2
    assert lc.post(f"/account/security/passkeys/{only}/remove", follow_redirects=False).status_code == 303
    assert la.passkey_count(owner["id"]) == 1


def test_adding_a_passkey_and_new_codes_need_a_fresh_confirmation(signed):
    c = signed["client"]
    uid = signed["id"]
    with get_cursor() as cur:
        cur.execute("UPDATE web_sessions SET second_factor_at = now() - interval '6 minutes'")
    assert c.post("/auth/enrol/passkey/options").status_code == 403
    r = c.post("/account/security/recovery/regenerate")
    assert r.status_code == 403 and la.recovery_codes_left(uid) == 10
    assert "data-reauth" in r.text and "6-digit code" in r.text
    r = c.post("/account/security/reauth", data={"code": _code(uid)}, follow_redirects=False)       # a fresh authenticator code
    assert r.status_code == 303
    r = c.post("/account/security/recovery/regenerate")
    new_codes = _codes(r.text)
    assert r.status_code == 200 and len(new_codes) == 10 and not set(new_codes) & set(signed["codes"])
    third = _tc(c.app)
    _identify(third)
    assert third.post("/auth/login/id/recovery", data={"code": signed["codes"][0]}).status_code == 401      # the old set is void
    with get_cursor() as cur:                                                                         # and a passkey tap also confirms
        cur.execute("UPDATE web_sessions SET second_factor_at = now() - interval '6 minutes'")
    opts = c.post("/account/security/passkey/options")
    assert c.post("/account/security/passkey/verify", json=signed["key"].get(opts.text, uid)).status_code == 200
    assert c.post("/account/security/recovery/regenerate").status_code == 200


def test_the_notice_is_a_key_never_raw_text(signed):
    page = signed["client"].get("/account/security", params={"notice": "<script>alert(1)</script>"}).text
    assert "alert(1)" not in page and "data-notice" not in page
    assert "Confirmed." in signed["client"].get("/account/security?notice=confirmed").text


def test_a_missing_or_unwritable_key_is_a_clear_error_not_a_500(lc, owner, monkeypatch):
    _temp(lc, owner["temp"])
    monkeypatch.setattr(config, "LOCAL_AUTH_KEY_FILE", "/proc/no-such-dir/local-auth.key")
    r = lc.post("/auth/enrol/password", data={"password": PW, "confirm": PW})
    assert r.status_code == 503 and "sign-in key file" in r.text


# --- the console ----------------------------------------------------------------------------------------------------------------------------------------

def test_cli_owner_create_prints_a_temporary_password_and_refuses_a_second(monkeypatch, capsys, tmp_path):
    key = tmp_path / "vault" / "local-auth.key"
    monkeypatch.setattr(config, "LOCAL_AUTH_KEY_FILE", str(key))
    assert cli.main(["owner", "create"]) == 0
    out = capsys.readouterr().out
    pw = re.search(r"shown once\): (\S+)", out).group(1)
    assert key.exists() and oct(key.stat().st_mode & 0o777) == "0o600"
    owner = la.get_user_by_username("owner")
    assert owner["password_temp"] and la.verify_password(owner["password_hash"], la._normal_code(pw))[0]
    with pytest.raises(SystemExit) as e:
        cli.main(["owner", "create"])
    assert "already exists" in str(e.value)
    assert cli.main(["owner", "create", "--reset"]) == 0
    assert "Temporary password" in capsys.readouterr().out
    monkeypatch.setattr(config, "LOCAL_AUTH_KEY_FILE", "/proc/no-such-dir/local-auth.key")
    with pytest.raises(SystemExit) as e:
        cli.main(["owner", "create", "--reset"])
    assert "key file" in str(e.value)
