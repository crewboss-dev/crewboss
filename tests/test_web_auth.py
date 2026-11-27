"""Dashboard sign-in (task #394): every page needs a signed-in super admin; /api + /mcp keep their Bearer tokens."""
import base64
import hashlib
import time
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app import config, web_auth
from app.db import get_cursor

TENANT = "00000000-0000-4000-8000-000000000001"
CLIENT = "00000000-0000-4000-8000-000000000002"
OWNER_OID = "11111111-2222-4333-8444-555555555555"
OTHER_OID = "99999999-8888-4777-8666-555555555555"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _id_token(nonce, **over):
    now = int(time.time())
    claims = {"iss": f"https://login.microsoftonline.com/{TENANT}/v2.0", "aud": CLIENT, "tid": TENANT,
              "sub": "s", "oid": OWNER_OID, "nonce": nonce, "iat": now, "nbf": now, "exp": now + 3600,
              "name": "Owner Person", "preferred_username": "owner@example.test"}
    claims.update(over)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, _KEY, algorithm="RS256", headers={"kid": "k1"})


@pytest.fixture
def auth_on(monkeypatch, tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("s3cret\n")
    monkeypatch.setattr(config, "WEB_AUTH", "required")
    monkeypatch.setattr(config, "OIDC_TENANT_ID", TENANT)
    monkeypatch.setattr(config, "OIDC_CLIENT_ID", CLIENT)
    monkeypatch.setattr(config, "OIDC_CLIENT_SECRET_FILE", str(secret))
    monkeypatch.setattr(config, "SUPERADMIN_OIDS", frozenset({OWNER_OID}))
    monkeypatch.setattr(config, "PUBLIC_URL", "https://testserver")
    monkeypatch.setattr(web_auth, "signing_key", lambda _t: _KEY.public_key())
    seen = {}

    def exchange(code, verifier):
        seen["code"], seen["verifier"] = code, verifier
        return {"id_token": seen["make"](seen["nonce"])}

    seen["make"] = lambda n: _id_token(n)
    monkeypatch.setattr(web_auth, "exchange_code", exchange)
    return seen


def _start(client, seen, next_path="/"):
    r = client.get("/auth/login", params={"next": next_path}, follow_redirects=False)
    assert r.status_code == 302, r.text
    q = parse_qs(urlsplit(r.headers["location"]).query)
    state = r.cookies.get(web_auth.FLOW_COOKIE) or _cookie_value(r, web_auth.FLOW_COOKIE)
    with get_cursor() as cur:
        cur.execute("SELECT nonce FROM oidc_flows WHERE state_hash = %s", (hashlib.sha256(state.encode()).hexdigest(),))
        seen["nonce"] = cur.fetchone()["nonce"]
    return r, q, state


def _cookie_value(resp, name):
    for h in resp.headers.get_list("set-cookie"):
        if h.startswith(name + "="):
            return h.split(";", 1)[0].split("=", 1)[1]
    return None


def _set_cookie_header(resp, name):
    return next(h for h in resp.headers.get_list("set-cookie") if h.startswith(name + "="))


def _sign_in(client, seen, next_path="/"):
    _r, q, state = _start(client, seen, next_path)
    r = client.get("/auth/callback", params={"code": "c0de", "state": q["state"][0]},
                   headers={"cookie": f"{web_auth.FLOW_COOKIE}={state}"}, follow_redirects=False)
    return r


def test_pages_redirect_to_sign_in_and_apis_keep_their_tokens(client, auth_on):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/auth/login?next=%2F"
    r = client.get("/acme-erp/board?x=1", follow_redirects=False)
    assert r.headers["location"] == "/auth/login?next=%2Facme-erp%2Fboard%3Fx%3D1"
    # Bearer-token surfaces are untouched: /api still answers its own 401, not a sign-in redirect.
    r = client.get("/api/projects", follow_redirects=False)
    assert r.status_code == 401 and "Bearer" in r.json()["detail"]
    for open_path in ("/healthz", "/manifest.webmanifest", "/favicon.ico", "/manifest.json", "/apple-touch-icon.png",
                      "/apple-touch-icon-precomposed.png"):
        assert client.get(open_path, follow_redirects=False).status_code == 200, open_path
    for open_path in ("/manifest.webmanifest", "/favicon.ico", "/manifest.json", "/apple-touch-icon.png",
                      "/apple-touch-icon-precomposed.png"):   # install tools probe with HEAD (an Omarchy check got 405)
        assert client.head(open_path, follow_redirects=False).status_code == 200, open_path
    # The live channel and writes get a plain 401 (an EventSource gives up on it; no redirect loop).
    assert client.get("/all/events", follow_redirects=False).status_code == 401
    # FastAPI's docs and schema are pages: owner only.
    assert client.get("/api/docs", follow_redirects=False).status_code == 302
    assert client.get("/openapi.json", follow_redirects=False).status_code == 302


def test_unconfigured_sign_in_fails_closed(client, auth_on, monkeypatch):
    monkeypatch.setattr(config, "OIDC_CLIENT_SECRET_FILE", "/nonexistent/secret")
    assert client.get("/", follow_redirects=False).status_code == 503
    r = client.get("/auth/login", follow_redirects=False)
    assert r.status_code == 503 and "client secret file" in r.text and "s3cret" not in r.text
    monkeypatch.setattr(config, "WEB_AUTH", "off")  # the explicit opt-out
    assert client.get("/", follow_redirects=False).status_code == 200


def test_htmx_request_gets_hx_redirect_back_to_the_whole_page(client, auth_on):
    r = client.post("/acme-erp/tasks/1/comments", headers={"HX-Request": "true",
                    "HX-Current-URL": "https://testserver/acme-erp/tasks?open=1"}, follow_redirects=False)
    assert r.status_code == 401
    assert r.headers["HX-Redirect"] == "/auth/login?next=%2Facme-erp%2Ftasks%3Fopen%3D1"


def test_login_starts_a_pkce_flow_with_a_host_only_cookie(client, auth_on):
    r, q, state = _start(client, auth_on, "/acme-erp")
    loc = urlsplit(r.headers["location"])
    assert loc.netloc == "login.microsoftonline.com" and loc.path == f"/{TENANT}/oauth2/v2.0/authorize"
    assert q["client_id"] == [CLIENT] and q["response_type"] == ["code"]
    assert q["redirect_uri"] == ["https://testserver/auth/callback"]
    assert q["code_challenge_method"] == ["S256"] and q["state"] == [state] and q["nonce"][0]
    c = _set_cookie_header(r, web_auth.FLOW_COOKIE).lower()
    assert "secure" in c and "httponly" in c and "samesite=lax" in c and "path=/" in c and "domain" not in c
    assert r.headers["cache-control"] == "no-store"


def test_login_on_another_dashboard_name_goes_to_the_public_host(client, auth_on):
    r = client.get("/auth/login", params={"next": "/x"}, headers={"host": "crewboss.example.com"},
                   follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "https://testserver/auth/login?next=%2Fx"
    assert not _cookie_value(r, web_auth.FLOW_COOKIE)


def test_full_sign_in_makes_a_session_and_the_page_opens(client, auth_on):
    _r, q, state = _start(client, auth_on, "/acme-erp/board")
    r = client.get("/auth/callback", params={"code": "c0de", "state": q["state"][0]},
                   headers={"cookie": f"{web_auth.FLOW_COOKIE}={state}"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/acme-erp/board"
    # PKCE: the verifier sent to Microsoft matches the challenge in the authorize URL.
    digest = hashlib.sha256(auth_on["verifier"].encode()).digest()
    assert base64.urlsafe_b64encode(digest).rstrip(b"=").decode() == q["code_challenge"][0]
    c = _set_cookie_header(r, web_auth.SESSION_COOKIE).lower()
    assert "secure" in c and "httponly" in c and "samesite=lax" in c and "max-age=7200" in c and "domain" not in c
    token = _cookie_value(r, web_auth.SESSION_COOKIE)
    with get_cursor() as cur:  # only the hash is stored
        cur.execute("SELECT count(*) AS n FROM web_sessions WHERE token_hash = %s OR token_hash = %s",
                    (token, hashlib.sha256(token.encode()).hexdigest()))
        assert cur.fetchone()["n"] == 1
        cur.execute("SELECT token_hash FROM web_sessions WHERE token_hash = %s", (token,))
        assert cur.fetchone() is None
    page = client.get("/", headers={"cookie": f"{web_auth.SESSION_COOKIE}={token}"}, follow_redirects=False)
    assert page.status_code == 200 and "owner@example.test" in page.text and "data-sign-out" in page.text


def test_callback_refuses_a_state_mismatch_and_a_replay(client, auth_on):
    _r, q, state = _start(client, auth_on)
    r = client.get("/auth/callback", params={"code": "c", "state": q["state"][0]},
                   headers={"cookie": f"{web_auth.FLOW_COOKIE}=someone-elses"}, follow_redirects=False)
    assert r.status_code == 400 and not _cookie_value(r, web_auth.SESSION_COOKIE)
    ok = client.get("/auth/callback", params={"code": "c", "state": q["state"][0]},
                    headers={"cookie": f"{web_auth.FLOW_COOKIE}={state}"}, follow_redirects=False)
    assert ok.status_code == 303
    again = client.get("/auth/callback", params={"code": "c", "state": q["state"][0]},
                       headers={"cookie": f"{web_auth.FLOW_COOKIE}={state}"}, follow_redirects=False)
    assert again.status_code == 400 and not _cookie_value(again, web_auth.SESSION_COOKIE)


@pytest.mark.parametrize("over", [
    {"tid": "00000000-0000-4000-8000-0000000000ff",
     "iss": "https://login.microsoftonline.com/00000000-0000-4000-8000-0000000000ff/v2.0"},  # another tenant
    {"tid": "00000000-0000-4000-8000-0000000000ff"},                          # right issuer, wrong tid
    {"aud": "some-other-app"},
    {"nonce": "not-the-flows"},
    {"exp": int(time.time()) - 3600, "iat": int(time.time()) - 7200, "nbf": int(time.time()) - 7200},
    {"oid": None},
])
def test_callback_refuses_a_bad_id_token(client, auth_on, over):
    auth_on["make"] = lambda n: _id_token(over.get("nonce", n), **{k: v for k, v in over.items() if k != "nonce"})
    r = _sign_in(client, auth_on)
    assert r.status_code == 400 and not _cookie_value(r, web_auth.SESSION_COOKIE)


def test_callback_refuses_a_token_signed_by_another_key(client, auth_on, monkeypatch):
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(web_auth, "signing_key", lambda _t: other.public_key())
    r = _sign_in(client, auth_on)
    assert r.status_code == 400 and not _cookie_value(r, web_auth.SESSION_COOKIE)


def test_a_tenant_user_who_is_not_a_super_admin_is_refused(client, auth_on):
    auth_on["make"] = lambda n: _id_token(n, oid=OTHER_OID, preferred_username="staff@example.test")
    r = _sign_in(client, auth_on)
    assert r.status_code == 403 and not _cookie_value(r, web_auth.SESSION_COOKIE)
    assert OTHER_OID in r.text  # so the owner can add it, if that's intended


def test_a_session_ends_at_its_cap_and_when_its_oid_leaves_the_allowlist(client, auth_on, monkeypatch):
    token = _cookie_value(_sign_in(client, auth_on), web_auth.SESSION_COOKIE)
    cookie = {"cookie": f"{web_auth.SESSION_COOKIE}={token}"}
    assert client.get("/", headers=cookie, follow_redirects=False).status_code == 200
    monkeypatch.setattr(config, "SUPERADMIN_OIDS", frozenset({OTHER_OID}))
    assert client.get("/", headers=cookie, follow_redirects=False).status_code == 302
    monkeypatch.setattr(config, "SUPERADMIN_OIDS", frozenset({OWNER_OID}))
    with get_cursor() as cur:
        cur.execute("UPDATE web_sessions SET expires_at = now() - interval '1 second' WHERE token_hash = %s",
                    (hashlib.sha256(token.encode()).hexdigest(),))
    assert client.get("/", headers=cookie, follow_redirects=False).status_code == 302


def test_sign_out_ends_the_session_and_a_cross_site_sign_out_is_refused(client, auth_on):
    token = _cookie_value(_sign_in(client, auth_on), web_auth.SESSION_COOKIE)
    cookie = f"{web_auth.SESSION_COOKIE}={token}"
    evil = client.post("/auth/logout", headers={"cookie": cookie, "Origin": "https://evil.example",
                                                 "Sec-Fetch-Site": "cross-site"}, follow_redirects=False)
    assert evil.status_code == 403
    r = client.post("/auth/logout", headers={"cookie": cookie, "Origin": "https://crewboss.example.net",
                                              "Sec-Fetch-Site": "same-origin"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/auth/signed-out"
    assert client.get("/", headers={"cookie": cookie}, follow_redirects=False).status_code == 302
    assert client.get("/auth/signed-out").status_code == 200


@pytest.mark.parametrize("value,expected", [
    ("/acme-erp/board?x=1", "/acme-erp/board?x=1"),
    ("//evil.example/x", "/"), ("https://evil.example", "/"), ("/\\evil.example", "/"),
    ("/auth/callback?code=x", "/"), ("", "/"), (None, "/"), ("/ok\r\nSet-Cookie: x", "/"),
])
def test_safe_next_stays_on_this_site(value, expected):
    assert web_auth.safe_next(value) == expected


def test_an_empty_allowlist_lets_nobody_in_but_shows_the_object_id(client, auth_on, monkeypatch):
    monkeypatch.setattr(config, "SUPERADMIN_OIDS", frozenset())
    assert client.get("/", follow_redirects=False).status_code == 302  # sign-in runs...
    r = _sign_in(client, auth_on)
    assert r.status_code == 403 and OWNER_OID in r.text  # ...and refuses, showing the id to add
    assert not _cookie_value(r, web_auth.SESSION_COOKIE)
