"""#976 slice A: password + authenticator app is the default sign-in; passkeys are an optional upgrade. The sign-in, enrolment, step-up
and no-lock-out tests are in tests/test_local_auth.py (they share its helpers); this file holds the security-review points."""
import json
import re
import time

import pytest

from app import config, local_auth as la, web_auth
from app.db import get_cursor
from tests.test_local_auth import (  # noqa: F401  (fixtures + helpers)
    HOST, PW, SoftKey, _code, _codes, _identify, _make_mfa, _make_user, _password_signin, _tc, _temp, local, lc, owner, signed,
    _totp_clock,
)


def test_the_stepup_dialog_asks_for_a_code_and_offers_the_passkey_as_the_other_way():
    js = open("app/static/js/stepup.js", encoding="utf-8").read()
    assert "/account/security/reauth" in js and "6-digit" in js and "Use my passkey instead" in js
    assert "/account/security/stepup-info" in js and "CrewbossPasskey.confirm" in js


def test_the_stepup_page_has_a_code_field():
    tpl = open("app/templates/pages/auth_stepup.html", encoding="utf-8").read()
    assert "6-digit code" in tpl and "Use my passkey instead" in tpl


def test_the_password_step_runs_the_same_work_for_every_kind_of_person(lc, owner, monkeypatch):
    """An unknown name and a passkey-only person still cost a password hash and a TOTP match (no timing oracle)."""
    calls = {"hash": 0, "totp": 0}
    real_v, real_m = la.verify_password, la.match_totp_step
    monkeypatch.setattr(la, "verify_password", lambda *a, **k: (calls.__setitem__("hash", calls["hash"] + 1), real_v(*a, **k))[1])
    monkeypatch.setattr(la, "match_totp_step", lambda *a, **k: (calls.__setitem__("totp", calls["totp"] + 1), real_m(*a, **k))[1])
    for who in ("owner", "nobody-at-all"):
        c = _tc(lc.app)
        _identify(c, who)
        assert c.post("/auth/login/id/password", data={"password": PW, "code": "123456"}).status_code == 401
    assert calls == {"hash": 2, "totp": 2}


def test_no_code_is_spent_when_the_password_is_wrong_and_a_reset_wipes_the_authenticator(signed):
    uid = signed["id"]
    before = la.get_user(uid)["totp_last_step"]
    c = _tc(signed["client"].app)
    _identify(c)
    c.post("/auth/login/id/password", data={"password": "nope nope nope nope", "code": _code(uid)})
    assert la.get_user(uid)["totp_last_step"] == before
    la.create_owner("owner", reset=True)
    u = la.get_user(uid)
    assert u["totp_secret_enc"] is None and u["totp_confirmed_at"] is None and u["password_temp"]
    assert _password_signin(_tc(signed["client"].app), uid, code="123456").status_code == 401     # nothing left to sign in with


def test_an_expired_or_stale_enrolment_session_cannot_reach_the_codes(lc, owner):
    _temp(lc, owner["temp"])
    assert lc.post("/auth/enrol/password", data={"password": PW, "confirm": PW}, follow_redirects=False).status_code == 303
    uid = owner["id"]
    # a confirmed authenticator exists from before, but THIS session never confirmed one: the stage is still 'totp'
    _make_mfa(uid)
    assert lc.post("/auth/enrol/codes").status_code == 409
    assert lc.post("/auth/enrol/complete", data={"saved": "on"}).status_code == 409


def test_a_session_that_began_before_this_change_at_stage_passkey_starts_at_the_password(lc, owner):
    _temp(lc, owner["temp"])
    with get_cursor() as cur:
        cur.execute("UPDATE web_sessions SET enrol_stage = 'passkey' WHERE enrolment_only")
    assert "Choose a password" in lc.get("/auth/enrol").text
    assert lc.post("/auth/enrol/password", data={"password": PW, "confirm": PW}, follow_redirects=False).status_code == 303


def test_the_passkey_routes_use_the_request_rp_id_and_refuse_when_unavailable(lc, owner, monkeypatch):
    monkeypatch.setattr(la, "passkeys_available", lambda request: False)
    c = _tc(lc.app)
    _identify(c)
    assert c.post("/auth/login/id/passkey/options").status_code == 403
    assert c.post("/auth/login/id/passkey/verify", json={}).status_code == 403
    seen = []
    monkeypatch.setattr(la, "passkeys_available", lambda request: True)
    monkeypatch.setattr(la, "rp_id_for", lambda request: seen.append(1) or "localhost")
    opts = json.loads(c.post("/auth/login/id/passkey/options").text)
    assert opts["rpId"] == "localhost" and seen


def test_trust_is_an_open_path_and_note_signin_ip_is_told_about_attempts(lc, owner, monkeypatch):
    assert web_auth.is_open_path("/trust")
    got = []
    monkeypatch.setattr(la, "note_signin_ip", lambda ip, ok: got.append(ok))
    c = _tc(lc.app)
    _identify(c)
    c.post("/auth/login/id/password", data={"password": "wrong wrong wrong", "code": "000000"})
    assert got == [False]


def test_password_stays_after_a_passkey_is_added_and_signing_in_is_a_fresh_step_up(lc, owner):
    from tests.test_local_auth import _enrol_owner
    key, codes = _enrol_owner(lc, owner)
    assert la.has_mfa(la.get_user(owner["id"])) and la.passkey_count(owner["id"]) == 1
    c = _tc(lc.app)
    assert _password_signin(c, owner["id"]).status_code == 303
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT (second_factor_at > now() - interval '1 minute') AS fresh FROM web_sessions "
                    "WHERE user_id = %s ORDER BY id DESC LIMIT 1", (owner["id"],))
        assert cur.fetchone()["fresh"]


def test_the_ui_uses_plain_words(signed):
    page = signed["client"].get("/account/security").text + _tc(signed["client"].app).get("/auth/login").text
    for banned in ("TOTP", "ACME", "TLS", "SSL"):
        assert banned not in page
    assert "authenticator app" in page


# --- review fixes: the second-factor guess limit, stale secrets, other sessions ------------------------------------------------

def _wrong_codes(app, uid, n):
    """n sign-in attempts with the RIGHT password and a wrong code, each from its own address (the hourly limits never bite)."""
    last = None
    for i in range(n):
        c = _tc(app, peer=f"198.51.100.{i % 250 + 1}")
        _identify(c)
        last = c.post("/auth/login/id/password", data={"password": PW, "code": "000000"}, follow_redirects=False)
    return last


def test_the_code_guess_count_has_no_time_window_and_locks_after_ten(signed):
    uid = signed["id"]
    assert la.totp_lock_state(9, 0) == ("ok", 0) and la.totp_lock_state(10, 0)[0] == "wait" and la.totp_lock_state(100, 0)[0] == "hard"
    assert la.totp_lock_state(10, 100000) == ("ok", 0) or la.totp_lock_state(10, 100000)[0] == "ok"
    d10 = la.totp_lock_state(10, 0)[1]
    assert la.totp_lock_state(12, 0)[1] > d10                                     # the delay escalates
    _wrong_codes(signed["client"].app, uid, 10)
    assert la.get_user(uid)["totp_fail_streak"] == 10
    with get_cursor() as cur:                                                    # the count does not age out: only the delay does
        cur.execute("UPDATE login_attempts SET at = now() - interval '2 days'")
        cur.execute("UPDATE users SET totp_fail_at = now() WHERE id = %s", (uid,))
    c = _tc(signed["client"].app, peer="203.0.113.7")
    r = _password_signin(c, uid)                                                 # even a RIGHT code is refused while locked
    assert r.status_code == 401 and "Too many wrong codes" in r.text
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT verb FROM instance_audit WHERE verb = 'signin.code_lock'")
        assert cur.fetchone()                                                    # the owner can see it
    with get_cursor() as cur:                                                    # the delay passes: the right code works and clears the count
        cur.execute("UPDATE users SET totp_fail_at = now() - interval '2 days' WHERE id = %s", (uid,))
    assert _password_signin(_tc(signed["client"].app, peer="203.0.113.8"), uid).status_code == 303
    assert la.get_user(uid)["totp_fail_streak"] == 0


def test_a_wrong_password_never_raises_the_code_count_and_a_hard_lock_needs_a_recovery_code(signed):
    uid = signed["id"]
    c = _tc(signed["client"].app, peer="203.0.113.9")
    _identify(c)
    c.post("/auth/login/id/password", data={"password": "wrong wrong wrong", "code": "000000"})
    assert la.get_user(uid)["totp_fail_streak"] == 0                             # a stranger who knows the name cannot lock the owner
    with get_cursor() as cur:
        cur.execute("UPDATE users SET totp_fail_streak = 100, totp_fail_at = now() - interval '9 days' WHERE id = %s", (uid,))
    r = _password_signin(_tc(signed["client"].app, peer="203.0.113.10"), uid)
    assert r.status_code == 401 and "recovery code" in r.text and "Reset sign-in" in r.text
    r2 = _tc(signed["client"].app, peer="203.0.113.11")
    _identify(r2)
    assert r2.post("/auth/login/id/recovery", data={"code": signed["codes"][0]}, follow_redirects=False).status_code == 303
    assert la.get_user(uid)["totp_fail_streak"] == 0                             # a recovery code is a second factor: it clears the count
    with get_cursor() as cur:
        cur.execute("UPDATE users SET totp_fail_streak = 100 WHERE id = %s", (uid,))
        la.reset_signin_in(cur, uid)
    assert la.get_user(uid)["totp_fail_streak"] == 0                             # and so does an admin's Reset sign-in


def test_step_up_obeys_an_existing_lock_but_wrong_step_up_codes_never_raise_it(signed):
    uid = signed["id"]
    c = signed["client"]
    assert c.post("/account/security/reauth", data={"code": "000000"}, headers={"accept": "application/json"}).status_code == 401
    assert la.get_user(uid)["totp_fail_streak"] == 0                             # a step-up miss is not a sign-in failure
    with get_cursor() as cur:
        cur.execute("UPDATE users SET totp_fail_streak = 10, totp_fail_at = now() WHERE id = %s", (uid,))
    r = c.post("/account/security/reauth", data={"code": _code(uid)}, headers={"accept": "application/json"})
    assert r.status_code == 429 and "Too many wrong codes" in r.text


def test_ten_wrong_step_up_codes_end_that_session_and_leave_the_sign_in_alone(signed, monkeypatch):
    uid, c = signed["id"], signed["client"]
    monkeypatch.setattr(la, "lockout_seconds", lambda limits: 0)                 # the per-address backoff is its own test
    for i in range(9):
        assert c.post("/account/security/reauth", data={"code": "000000"}, headers={"accept": "application/json"}).status_code == 401
    assert c.get("/account/security", follow_redirects=False).status_code == 200      # nine misses: still signed in
    r = c.post("/account/security/reauth", data={"code": "000000"}, headers={"accept": "application/json"})
    assert r.status_code == 401 and "signed out" in r.text
    assert c.get("/account/security", follow_redirects=False).status_code in (302, 303, 401)
    u = la.get_user(uid)
    assert u["totp_fail_streak"] == 0 and la.totp_lock(u)[0] == "ok"                   # the owner can still sign in
    assert _password_signin(signed["second"], uid).status_code == 303
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT summary FROM instance_audit WHERE verb = 'signin.stepup_lock'")
        assert "10 wrong step-up codes" in cur.fetchone()["summary"]


def test_a_stale_authenticator_secret_without_a_password_is_cleared_at_start_up(lc, owner):
    uid = owner["id"]
    _make_mfa(uid)                                                               # a confirmed secret ...
    with get_cursor() as cur:
        cur.execute("UPDATE users SET password_hash = NULL, totp_confirmed_at = now() - interval '10 years' WHERE id = %s", (uid,))   # ... on someone with no password (pre-#809: confirmed before 0118)
    assert la.clear_stale_totp() >= 1
    u = la.get_user(uid)
    assert u["totp_secret_enc"] is None and u["totp_confirmed_at"] is None and not la.has_mfa(u)


def test_a_password_change_and_a_new_authenticator_end_the_other_sign_ins_but_not_this_one(signed):
    c, uid = signed["client"], signed["id"]
    _password_signin(signed["second"], uid)
    other = signed["second"]
    assert other.get("/account/security", follow_redirects=False).status_code == 200
    assert c.post("/account/security/reauth", data={"code": _code(uid)}, headers={"accept": "application/json"}).status_code == 200
    r = c.post("/account/security/password", data={"password": "another-long-password-2", "confirm": "another-long-password-2"}, follow_redirects=False)
    assert r.status_code == 303
    assert other.get("/account/security", follow_redirects=False).status_code in (302, 303, 401)       # signed out
    assert c.get("/account/security", follow_redirects=False).status_code == 200                  # this one stays


def test_finishing_a_recovery_enrolment_ends_every_earlier_sign_in(signed):
    uid = signed["id"]
    old = _tc(signed["client"].app)
    _password_signin(old, uid)
    c = signed["second"]
    _identify(c)
    assert c.post("/auth/login/id/recovery", data={"code": signed["codes"][0]}, follow_redirects=False).status_code == 303
    assert c.post("/auth/enrol/password", data={"current": PW}, follow_redirects=False).status_code == 303
    from tests.test_local_auth import _finish
    assert c.post("/auth/enrol/totp", data={"code": _code(uid, pending=True)}, follow_redirects=False).status_code == 303
    _finish(c)
    assert old.get("/account/security", follow_redirects=False).status_code in (302, 303, 401)
    assert signed["client"].get("/account/security", follow_redirects=False).status_code in (302, 303, 401)
    assert c.get("/account/security", follow_redirects=False).status_code == 200


def test_start_up_never_clears_an_authenticator_confirmed_after_the_migration(lc, owner):
    uid = owner["id"]
    _make_mfa(uid)
    with get_cursor() as cur:
        cur.execute("UPDATE users SET password_hash = NULL, totp_confirmed_at = now() WHERE id = %s", (uid,))   # confirmed under #976
    la.clear_stale_totp()
    u = la.get_user(uid)
    assert u["totp_secret_enc"] is not None and u["totp_confirmed_at"] is not None


def test_the_authenticator_needs_a_password_first(signed):
    c, uid = signed["client"], signed["id"]
    with get_cursor() as cur:
        cur.execute("UPDATE users SET password_hash = NULL, totp_secret_enc = NULL, totp_confirmed_at = NULL WHERE id = %s", (uid,))
        cur.execute("UPDATE web_sessions SET second_factor_at = now() WHERE user_id = %s", (uid,))
    for url, data in (("/account/security/authenticator/start", {}), ("/account/security/authenticator/confirm", {"code": "123456"})):
        r = c.post(url, data=data, follow_redirects=False)
        assert r.status_code == 400 and "Choose a password first" in r.text, (url, r.status_code)
    assert la.get_user(uid)["totp_pending_enc"] is None
    page = c.get("/account/security").text
    assert "1. Choose a password" in page and "2. Set up the authenticator app" in page


def test_a_new_authenticator_ends_other_sessions_at_stage_two_not_only_at_the_end(signed):
    old, c, uid = signed["client"], signed["second"], signed["id"]
    assert old.get("/account/security", follow_redirects=False).status_code == 200
    _identify(c)
    assert c.post("/auth/login/id/recovery", data={"code": signed["codes"][0]}, follow_redirects=False).status_code == 303
    assert c.post("/auth/enrol/password", data={"current": PW}, follow_redirects=False).status_code == 303
    assert old.get("/account/security", follow_redirects=False).status_code == 200            # still in: no new authenticator yet
    assert c.post("/auth/enrol/totp", data={"code": _code(uid, pending=True)}, follow_redirects=False).status_code == 303
    assert old.get("/account/security", follow_redirects=False).status_code in (302, 303, 401)  # out at once, before the codes step
    assert c.get("/auth/enrol", follow_redirects=False).status_code == 200                      # the enrolment itself carries on


def test_regenerating_recovery_codes_ends_the_other_sessions_but_not_this_one(signed):
    c, uid = signed["client"], signed["id"]
    other = signed["second"]
    _password_signin(other, uid)
    assert other.get("/account/security", follow_redirects=False).status_code == 200
    assert c.post("/account/security/reauth", data={"code": _code(uid)}, headers={"accept": "application/json"}).status_code == 200
    assert c.post("/account/security/recovery/regenerate", follow_redirects=False).status_code == 200
    assert other.get("/account/security", follow_redirects=False).status_code in (302, 303, 401)
    assert c.get("/account/security", follow_redirects=False).status_code == 200
