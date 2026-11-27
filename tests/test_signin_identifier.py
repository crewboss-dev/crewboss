"""Identifier-first sign-in and 30-day sessions (task #809): the sign-in email is an identifier only, sessions last 30 days by default and
are listed and ended one by one. (The identifier + passkey flow itself: tests/test_local_auth.py.)"""
import pytest

from app import config, local_auth as la, web_auth
from app.db import get_cursor
from tests.test_local_auth import (  # noqa: F401  (fixtures + helpers)
    HOST, SoftKey, _identify, _passkey_signin, _tc, lc, local, owner, signed,
)


# --- the sign-in email -----------------------------------------------------------------------------------------------------------------

def test_email_is_set_only_after_a_fresh_tap_and_validated(signed):
    c = signed["client"]
    with get_cursor() as cur:
        cur.execute("UPDATE web_sessions SET second_factor_at = now() - interval '6 minutes'")
    r = c.post("/account/security/email", data={"email": "me@example.com"}, follow_redirects=False)
    assert r.status_code == 403 and la.get_user(signed["id"])["email"] is None
    opts = c.post("/account/security/passkey/options")
    assert c.post("/account/security/passkey/verify", json=signed["key"].get(opts.text, signed["id"])).status_code == 200
    assert c.post("/account/security/email", data={"email": "not an email"}).status_code == 400
    r = c.post("/account/security/email", data={"email": "  Me@Example.com "}, follow_redirects=False)
    assert r.status_code == 303 and "email-saved" in r.headers["location"]
    assert la.get_user(signed["id"])["email"] == "Me@Example.com"
    assert 'value="Me@Example.com"' in c.get("/account/security").text
    r = c.post("/account/security/email", data={"email": ""}, follow_redirects=False)
    assert r.status_code == 303 and la.get_user(signed["id"])["email"] is None


def test_email_works_as_an_identifier_case_insensitively(signed):
    la.set_email(signed["id"], "Owner.Name@Example.com")
    c = _tc(signed["client"].app)
    assert _identify(c, "  OWNER.name@example.COM ").status_code == 303
    r = _passkey_signin(c, signed["key"], signed["id"], identifier="OWNER.name@example.COM")
    assert r.status_code == 200 and c.get("/", follow_redirects=False).status_code == 200
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT username FROM web_sessions ORDER BY id DESC LIMIT 1")
        assert cur.fetchone()["username"] == "owner"


def test_email_is_unique_ignoring_case(owner):
    with get_cursor() as cur:
        cur.execute("INSERT INTO users (oid, username, is_owner) VALUES ('o2', 'second-user', false) RETURNING id")
        other = cur.fetchone()["id"]
    la.set_email(owner["id"], "a@example.com")
    with pytest.raises(la.EmailTaken):
        la.set_email(other, "A@EXAMPLE.COM")
    with pytest.raises(ValueError):
        la.set_email(other, "nope")
    la.set_email(owner["id"], "")
    la.set_email(other, "A@EXAMPLE.COM")
    la.set_email(other, "")
    la.set_email(owner["id"], "")


def test_email_taken_by_another_user_is_refused_on_the_page(signed):
    with get_cursor() as cur:
        cur.execute("INSERT INTO users (oid, username, email, is_owner) VALUES ('o2', 'second-user', 'taken@example.com', false)")
    r = signed["client"].post("/account/security/email", data={"email": "TAKEN@example.com"})
    assert r.status_code == 400 and "already used" in r.text


# --- 30-day sessions -------------------------------------------------------------------------------------------------------------------

def test_defaults_are_thirty_days():
    assert config.WEB_SESSION_MINUTES == 43200 and config.WEB_IDLE_MINUTES == 43200
    assert config.OIDC_SESSION_MINUTES == 120                            # Microsoft sign-in keeps its own, shorter cap
    assert web_auth.session_minutes() == 43200 and web_auth.idle_minutes() == 43200


def test_cookie_lasts_as_long_as_the_session(signed):
    r = _passkey_signin(signed["second"], signed["key"], signed["id"])
    assert f"Max-Age={web_auth.session_minutes() * 60}" in r.headers["set-cookie"]


def test_session_survives_past_the_old_two_hours_and_thirty_minutes_idle(signed):
    c = signed["client"]
    with get_cursor() as cur:
        cur.execute("UPDATE web_sessions SET created_at = now() - interval '5 days', auth_time = now() - interval '5 days', "
                    "last_seen_at = now() - interval '3 days'")
    assert c.get("/", follow_redirects=False).status_code == 200
    with get_cursor() as cur:
        cur.execute("UPDATE web_sessions SET expires_at = now() - interval '1 second'")
    assert c.get("/", follow_redirects=False).status_code == 302


def test_epoch_bump_disable_and_sign_out_everywhere_still_end_the_session(signed):
    c = signed["client"]
    with get_cursor() as cur:
        cur.execute("UPDATE users SET session_epoch = session_epoch + 1")
    assert c.get("/", follow_redirects=False).status_code == 302
    c = _tc(c.app)
    assert _passkey_signin(c, signed["key"], signed["id"]).status_code == 200
    with get_cursor() as cur:
        cur.execute("UPDATE users SET disabled_at = now()")
    assert c.get("/", follow_redirects=False).status_code == 302


def test_auth_age_seam(signed):
    c = signed["client"]
    s = web_auth.resolve_session(c.cookies.get(web_auth.session_cookie_name()))
    assert 0 <= web_auth.auth_age_seconds(s) < 60
    with get_cursor() as cur:
        cur.execute("UPDATE web_sessions SET auth_time = now() - interval '2 days'")
    s = web_auth.resolve_session(c.cookies.get(web_auth.session_cookie_name()))
    assert 2 * 86400 - 60 < web_auth.auth_age_seconds(s) < 2 * 86400 + 60
    assert web_auth.auth_age_seconds({}) > 10 ** 6


# --- the sessions list -------------------------------------------------------------------------------------------------------------------

def test_sessions_list_and_per_session_sign_out(signed):
    ua = {"user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36"}
    a = _tc(signed["client"].app)
    c = a.post("/auth/login/identify", data={"identifier": "owner"}, headers=ua, follow_redirects=False)
    opts = a.post("/auth/login/id/passkey/options", headers=ua)
    a.post("/auth/login/id/passkey/verify", json=signed["key"].get(opts.text, signed["id"]), headers=ua)
    b = _tc(signed["client"].app)
    b.post("/auth/login/identify", data={"identifier": "owner"}, follow_redirects=False)
    opts = b.post("/auth/login/id/passkey/options")
    b.post("/auth/login/id/passkey/verify", json=signed["key"].get(opts.text, signed["id"]),
           headers={"user-agent": "Mozilla/5.0 (X11; Linux x86_64) Firefox/130"})
    page = a.get("/account/security")
    assert page.status_code == 200 and page.text.count("data-session>") == 3        # the fixture's own, a and b
    assert "Firefox on Linux" in page.text and "Chrome on Windows" in page.text and "(this browser)" in page.text
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT token_hash FROM web_sessions")
        hashes = [r["token_hash"] for r in cur.fetchall()]
        cur.execute("SELECT id FROM web_sessions WHERE user_agent LIKE '%Firefox%'")
        other = cur.fetchone()["id"]
    assert not any(h in page.text for h in hashes)
    r = a.post(f"/account/security/sessions/{other}/end", follow_redirects=False)
    assert r.status_code == 303 and "session-ended" in r.headers["location"]
    assert b.get("/", follow_redirects=False).status_code == 302 and a.get("/", follow_redirects=False).status_code == 200
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT id FROM web_sessions WHERE user_agent LIKE '%Chrome%'")
        mine = cur.fetchone()["id"]
    r = a.post(f"/account/security/sessions/{mine}/end", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/auth/signed-out"
    assert a.get("/", follow_redirects=False).status_code == 302


def test_one_cannot_end_another_users_session(signed):
    with get_cursor() as cur:
        cur.execute("INSERT INTO users (oid, username, is_owner) VALUES ('o2', 'second-user', false) RETURNING id")
        other = cur.fetchone()["id"]
        cur.execute("INSERT INTO web_sessions (token_hash, oid, auth_time, expires_at, user_id, epoch) "
                    "VALUES ('h', 'o2', now(), now() + interval '1 day', %s, 0) RETURNING id", (other,))
        sid = cur.fetchone()["id"]
    assert signed["client"].post(f"/account/security/sessions/{sid}/end", follow_redirects=False).status_code == 303
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT count(*) AS n FROM web_sessions WHERE id = %s", (sid,))
        assert cur.fetchone()["n"] == 1


@pytest.mark.parametrize("ua,expected", [
    ("Mozilla/5.0 (Windows NT 10.0) Chrome/130 Safari/537", "Chrome on Windows"),
    ("Mozilla/5.0 (Windows NT 10.0) Chrome/130 Edg/130", "Edge on Windows"),
    ("Mozilla/5.0 (iPhone; CPU iPhone OS 17) Safari/604", "Safari on iPhone"),
    ("Mozilla/5.0 (X11; Linux x86_64; rv:130) Firefox/130", "Firefox on Linux"),
    ("", "Unknown browser"),
])
def test_user_agent_description(ua, expected):
    assert web_auth.describe_user_agent(ua) == expected
