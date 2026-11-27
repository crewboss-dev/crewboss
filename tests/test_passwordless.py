"""Step-up (access.require_fresh), the admin's Reset sign-in, the owner's console reset, and the instance-wide "Sign-in lasts" setting (#809)."""
import re
from types import SimpleNamespace

import pytest

from app import access, cli, config, local_auth as la, users, web_auth
from app.db import get_cursor
from tests.test_local_auth import (  # noqa: F401  (fixtures + helpers)
    HOST, ORIGIN, SoftKey, _codes, _finish, _identify, _mfa_enrol, _passkey_signin, _register, _tc, _temp, lc, local, owner, signed, _totp_clock,
)

H = {"Origin": ORIGIN, "HX-Request": "true"}


def _age_tap(hours=0, minutes=0):
    with get_cursor() as cur:
        cur.execute("UPDATE web_sessions SET second_factor_at = now() - make_interval(hours => %s::int, mins => %s::int)", (hours, minutes))


# --- require_fresh: every listed route ------------------------------------------------------------------------------------------------

LONG = [  # 12 h window
    "/x/external-agent/token",
    "/x/keys/s/n/set", "/x/keys/s/n/meta", "/x/keys/s/n/end-grace", "/x/keys/s/n/clear",
    "/x/servers", "/x/servers/1/via", "/x/servers/1/remove", "/x/releases/targets",
    # #810: deploy targets are edited on the project's Releases tab (the old /servers/{id}/reach and /servers/targets* routes are gone),
    # the machine join, and the environment rows (they run on other people's PCs)
    "/x/releases/targets/1/env", "/x/releases/targets/1/enable", "/x/machines/1/join", "/x/machines/1/unjoin",
    "/x/environment/rows", "/x/environment/rows/1", "/x/environment/rows/1/delete", "/x/environment/suggest",
    # Skills (#718): importing outside text, and confirming an unknown licence, both end up in every job's instructions
    "/x/build-skills/import", "/x/build-skills/abc/licence", "/x/build-skills/abc/review", "/assets/skills/import", "/assets/skills/abc/licence", "/assets/skills/abc/review",
    "/x/releases/deploys/1/approve", "/x/approvals/1/approve",
    "/admin/users/create", "/admin/users/1/role", "/admin/users/1/disable", "/admin/users/1/enable", "/admin/users/1/revoke-sessions",
    "/admin/users/1/share", "/admin/users/1/unshare", "/admin/users/1/servers", "/admin/users/1/servers/remove",
    "/admin/users/1/clear-email",
    # Fable review #809: project Admin grants, ownership, release policy, branch protection, runner enrolment codes
    "/x/logic/gate", "/x/logic/override", "/x/logic/override/1/revoke",
    # Agent files (#829): pinning or unpinning an agent / command / skill file lets (or stops) jobs on loading it
    "/x/machines/agent-files/pin", "/x/machines/agent-files/1/unpin",
    "/x/team/members", "/x/team/members/remove", "/x/team/transfer", "/x/team/policy", "/x/runners/codes",
]
SHORT = ["/admin/users/1/reset-signin", "/admin/settings/session-length",       # 5 minute window
         # the merge executor (#829): approving a merge, re-queueing one, the opt-in and the instance owner's repository list
         "/x/releases/merges/1/approve", "/x/releases/merges/1/requeue", "/x/releases/merge-executor", "/x/releases/merge-repos",
         "/x/releases/merge-repos/remove"]


@pytest.mark.parametrize("path", LONG)
def test_a_stale_session_is_asked_for_a_passkey_on_every_sensitive_route(signed, path):
    c = signed["client"]
    _age_tap(hours=13)
    r = c.post(path, headers=H)
    assert r.status_code == 403 and r.headers["HX-Trigger"] == "stepup-required", path
    r = c.post(path, data={}, headers={"Origin": ORIGIN})                             # not htmx: a full "Confirm it's you" page
    assert r.status_code == 403 and "data-stepup-page" in r.text, path


@pytest.mark.parametrize("path", LONG + SHORT)
def test_a_fresh_tap_gets_past_the_gate(signed, path):
    r = signed["client"].post(path, headers=H)
    assert r.headers.get("HX-Trigger") != "stepup-required" and "data-stepup-page" not in r.text, path


@pytest.mark.parametrize("path", SHORT)
def test_the_most_sensitive_ask_for_a_tap_from_the_last_five_minutes(signed, path):
    _age_tap(minutes=10)                                  # inside the 12 h default, outside 5 minutes
    r = signed["client"].post(path, headers=H)
    assert r.status_code == 403 and r.headers["HX-Trigger"] == "stepup-required"
    assert signed["client"].post("/admin/users/1/clear-email", headers=H).headers.get("HX-Trigger") != "stepup-required"


def test_a_passkey_reconfirm_makes_the_blocked_request_work_afterwards(signed):
    c = signed["client"]
    _age_tap(hours=13)
    path = f"/admin/users/{signed['id']}/clear-email"
    assert c.post(path, headers=H).headers["HX-Trigger"] == "stepup-required"
    opts = c.post("/account/security/passkey/options")                               # what the modal runs
    assert c.post("/account/security/passkey/verify", json=signed["key"].get(opts.text, signed["id"])).status_code == 200
    r = c.post(path, headers=H, follow_redirects=False)
    assert r.headers.get("HX-Trigger") != "stepup-required" and r.status_code == 303


def test_the_window_is_a_setting_and_defaults_to_12_hours(signed, monkeypatch):
    assert config.WEB_STEPUP_HOURS == 12 and access.stepup_seconds() == 12 * 3600
    _age_tap(hours=5)
    assert signed["client"].post("/admin/users/1/disable", headers=H).headers.get("HX-Trigger") != "stepup-required"
    monkeypatch.setattr(config, "WEB_STEPUP_HOURS", 4)
    assert signed["client"].post("/admin/users/1/disable", headers=H).headers["HX-Trigger"] == "stepup-required"


def test_a_token_never_passes_even_beside_a_fresh_session_cookie(signed):
    r = signed["client"].post("/x/external-agent/token", headers={**H, "Authorization": "Bearer cbs_whatever"})
    assert r.status_code == 403 and "HX-Trigger" not in r.headers and "token can't give one" in r.json()["error"]


def _req(web_user=None, bearer=False):
    return SimpleNamespace(headers={"authorization": "Bearer x"} if bearer else {}, state=SimpleNamespace(web_user=web_user))


def test_is_fresh_for_every_kind_of_caller(monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH", "required")
    local_u = {"user_id": 5, "tap_age": 100.0, "auth_age": 10 ** 6}
    assert access.is_fresh(_req(local_u)) and access.is_fresh(_req(local_u), 300)
    assert not access.is_fresh(_req({**local_u, "tap_age": 13 * 3600.0}))
    assert not access.is_fresh(_req({**local_u, "tap_age": None}))
    assert not access.is_fresh(_req({**local_u, "enrolment_only": True}))
    assert not access.is_fresh(_req(None)) and not access.is_fresh(_req(local_u, bearer=True))      # no session; a token
    oidc = {"user_id": None, "tap_age": None, "auth_age": 100.0}                    # Microsoft: recent auth_time counts, nothing else does
    assert access.is_fresh(_req(oidc)) and access.is_fresh(_req(oidc), 300)
    assert access.is_fresh(_req({**oidc, "auth_age": 4000.0})) and not access.is_fresh(_req({**oidc, "auth_age": 4000.0}), 300)
    assert not access.is_fresh(_req({**oidc, "auth_age": 13 * 3600.0})) and not access.is_fresh(_req({**oidc, "auth_age": None}))
    assert not access.is_fresh(_req(oidc, bearer=True))
    monkeypatch.setattr(config, "WEB_AUTH", "off")
    assert access.is_fresh(_req(None))                                              # sign-in off: nobody to ask
    assert not access.is_fresh(_req(None, bearer=True))                             # ...but a token is still never a person
    with pytest.raises(access.StepUpRequired) as e:
        access.require_fresh()(_req(None, bearer=True))
    assert e.value.token


def test_require_fresh_raises_for_oidc_with_a_message_not_a_passkey_modal(monkeypatch):
    monkeypatch.setattr(config, "WEB_AUTH", "required")
    with pytest.raises(access.StepUpRequired) as e:
        access.require_fresh(300)(_req({"user_id": None, "tap_age": None, "auth_age": 4000.0}))
    assert e.value.oidc and not e.value.token


def test_the_shared_modal_is_on_every_page_and_reuses_the_passkey_flow(signed):
    page = signed["client"].get("/").text
    assert "js/stepup.js" in page
    js = open("app/static/js/stepup.js", encoding="utf-8").read()
    assert "stepup-required" in js and "CrewbossPasskey.confirm" in js and "htmx.trigger" in js
    pk = open("app/static/js/passkey.js", encoding="utf-8").read()
    assert "CrewbossPasskey" in pk and "/account/security/passkey/options" in pk and "/account/security/passkey/verify" in pk


# --- admin Reset sign-in -----------------------------------------------------------------------------------------------------------------------

def _person(lc, owner_key):
    """A second person who has finished enrolment."""
    user, pw = users.create_user("dee", "designer", "dee@example.com")
    c = _tc(lc.app)
    assert _temp(c, pw, "dee").status_code == 303
    codes, r = _mfa_enrol(c, user["id"])
    assert r.status_code == 303
    key = SoftKey()
    assert _register(c, key).status_code == 200
    return {"id": user["id"], "key": key, "codes": codes, "client": c}


def test_admin_reset_signin_removes_passkeys_and_codes_ends_sessions_and_issues_a_new_temp_password(signed):
    dee = _person(signed["client"], signed["key"])
    assert la.passkey_count(dee["id"]) == 1 and dee["client"].get("/", follow_redirects=False).status_code == 200
    admin = signed["client"]
    r = admin.post(f"/admin/users/{dee['id']}/reset-signin", headers=H)
    assert r.status_code == 200 and "New temporary password" in r.text
    pw = re.search(r"data-temp-password-value>([^<]+)<", r.text).group(1)
    assert pw not in admin.get("/admin/users").text                                       # shown once
    assert la.passkey_count(dee["id"]) == 0 and la.recovery_codes_left(dee["id"]) == 0
    assert dee["client"].get("/", follow_redirects=False).status_code == 302              # every session ended
    c = _tc(admin.app)
    assert _temp(c, pw, "dee").headers["location"] == "/auth/enrol"                       # the new password opens enrolment only
    assert c.get("/", follow_redirects=False).headers["location"] == "/auth/enrol"
    again = _tc(admin.app)
    assert _passkey_signin(again, dee["key"], dee["id"], "dee").status_code == 401        # the old passkey is gone
    assert any("Reset the sign-in of dee" in a["summary"] for a in access.recent_audit())


def test_admin_reset_needs_a_fresh_tap_and_the_owner_cannot_be_reset(signed):
    dee = _person(signed["client"], signed["key"])
    _age_tap(minutes=10)
    r = signed["client"].post(f"/admin/users/{dee['id']}/reset-signin", headers=H)
    assert r.status_code == 403 and r.headers["HX-Trigger"] == "stepup-required"
    assert la.passkey_count(dee["id"]) == 1
    with get_cursor() as cur:
        cur.execute("UPDATE web_sessions SET second_factor_at = now() WHERE user_id = %s", (signed["id"],))
    r = signed["client"].post(f"/admin/users/{signed['id']}/reset-signin", headers=H)
    assert r.status_code == 400 and "owner account" in r.text
    assert la.passkey_count(signed["id"]) == 1 and la.recovery_codes_left(signed["id"]) == 10
    with pytest.raises(Exception):
        users.reset_signin(signed["id"])
    assert dee["client"].post(f"/admin/users/{signed['id']}/reset-signin", headers=H).status_code == 404     # not an admin


# --- the owner's way back: recovery codes or the server console ----------------------------------------------------------------------------------

def test_reset_owner_cli_prints_a_temporary_password_and_wipes_the_way_in(signed, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", SimpleNamespace(isatty=lambda: False))
    with pytest.raises(SystemExit) as e:
        cli.main(["reset-owner"])                                                          # no terminal, no --yes: refused
    assert "server's shell" in str(e.value) and la.passkey_count(signed["id"]) == 1
    assert cli.main(["reset-owner", "--yes"]) == 0
    out = capsys.readouterr().out
    pw = re.search(r"shown once\): (\S+)", out).group(1)
    assert la.passkey_count(signed["id"]) == 0 and la.recovery_codes_left(signed["id"]) == 0
    assert signed["client"].get("/", follow_redirects=False).status_code == 302
    c = _tc(signed["client"].app)
    assert _temp(c, pw).headers["location"] == "/auth/enrol"
    assert _temp(_tc(c.app), pw).status_code == 401                                        # single use


def test_reset_owner_with_no_owner_is_a_clear_error(monkeypatch):
    with pytest.raises(SystemExit) as e:
        cli.main(["reset-owner", "--yes"])
    assert "no owner" in str(e.value)


def test_there_is_no_web_route_to_reset_the_owner():
    from app.main import app
    paths = [getattr(r, "path", "") for r in app.routes]
    assert not [p for p in paths if "reset-owner" in p or "reset_owner" in p]
    import app.routers.admin_pages as ap, app.routers.local_auth_pages as lp
    assert "reset_owner" not in open(ap.__file__).read() + open(lp.__file__).read()


def test_the_owner_can_get_back_with_a_recovery_code(signed):
    c = signed["second"]
    _identify(c)
    assert c.post("/auth/login/id/recovery", data={"code": signed["codes"][3]}, follow_redirects=False).headers["location"] == "/auth/enrol"


def test_crewboss_script_has_reset_owner_and_no_password_flags():
    src = open("crewboss", encoding="utf-8").read()
    assert "reset-owner" in src and "--owner-password-stdin" not in src and '"reset-owner", "--yes"' in src


# --- "Sign-in lasts": the instance-wide length ---------------------------------------------------------------------------------------------------

def _set_length(c, minutes, headers=H):
    return c.post("/admin/settings/session-length", data={"minutes": str(minutes)}, headers=headers, follow_redirects=False)


def test_only_the_listed_lengths_are_accepted(signed):
    assert [m for m, _ in web_auth.SESSION_CHOICES] == [60, 480, 1440, 10080, 43200, 129600]
    for bad in (0, 45, 100000, -5):
        assert _set_length(signed["client"], bad).status_code == 400
    assert web_auth.session_minutes() == 43200
    with pytest.raises(ValueError):
        web_auth.set_session_minutes(61, None)
    page = signed["client"].get("/admin/users").text
    for label in ("1 hour", "8 hours", "1 day", "7 days", "30 days", "90 days"):
        assert label in page
    assert re.search(r'<option value="43200" selected>30 days', page)


def test_setting_the_length_needs_a_fresh_passkey_tap(signed):
    _age_tap(minutes=10)
    r = _set_length(signed["client"], 60)
    assert r.status_code == 403 and r.headers["HX-Trigger"] == "stepup-required" and web_auth.session_minutes() == 43200
    assert _set_length(_person(signed["client"], signed["key"])["client"], 60).status_code in (403, 404)       # not an admin


def test_shortening_ends_older_sessions_at_once(signed):
    c = signed["client"]
    other = _tc(c.app)
    _passkey_signin(other, signed["key"], signed["id"])
    with get_cursor() as cur:       # `other` is 2 hours old, `c` is brand new
        cur.execute("UPDATE web_sessions SET created_at = now() - interval '2 hours' WHERE id = (SELECT max(id) FROM web_sessions)")
    assert other.get("/", follow_redirects=False).status_code == 200
    r = _set_length(c, 60)
    assert r.status_code == 303 and web_auth.session_minutes() == 60
    assert other.get("/", follow_redirects=False).status_code == 302                       # older than the new length: out
    assert c.get("/", follow_redirects=False).status_code == 200                           # younger: stays
    assert web_auth.idle_minutes() == 60                                                    # idle follows the length
    with get_cursor() as cur:       # and even without the cleanup the check itself ends it
        cur.execute("UPDATE web_sessions SET created_at = now() - interval '2 hours' WHERE user_id = %s", (signed["id"],))
    assert c.get("/", follow_redirects=False).status_code == 302


def test_a_one_hour_setting_really_is_one_hour_for_a_new_sign_in(signed):
    _set_length(signed["client"], 60)
    c = _tc(signed["client"].app)
    r = _passkey_signin(c, signed["key"], signed["id"])
    assert "Max-Age=3600" in r.headers["set-cookie"]
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT expires_at < now() + interval '61 minutes' AS ok FROM web_sessions ORDER BY id DESC LIMIT 1")
        assert cur.fetchone()["ok"]
    with get_cursor() as cur:
        cur.execute("UPDATE web_sessions SET last_seen_at = now() - interval '61 minutes' WHERE id = (SELECT max(id) FROM web_sessions)")
    assert c.get("/", follow_redirects=False).status_code == 302                           # idle > 1 hour


def test_lengthening_never_extends_a_session_already_issued(signed):
    c = signed["client"]
    _set_length(c, 60)
    with get_cursor() as cur:
        cur.execute("UPDATE web_sessions SET created_at = now() - interval '30 minutes', expires_at = now() + interval '30 minutes' "
                    "WHERE user_id = %s", (signed["id"],))
        cur.execute("SELECT expires_at FROM web_sessions WHERE user_id = %s", (signed["id"],))
        before = cur.fetchone()["expires_at"]
    assert _set_length(c, 129600).status_code == 303                                       # 90 days
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT expires_at FROM web_sessions WHERE user_id = %s", (signed["id"],))
        assert cur.fetchone()["expires_at"] == before                                       # not moved
    with get_cursor() as cur:
        cur.execute("UPDATE web_sessions SET expires_at = now() - interval '1 second' WHERE user_id = %s", (signed["id"],))
    assert c.get("/", follow_redirects=False).status_code == 302                           # its own old expiry still ends it
    new = _tc(c.app)
    _passkey_signin(new, signed["key"], signed["id"])
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT expires_at > now() + interval '89 days' AS long FROM web_sessions ORDER BY id DESC LIMIT 1")
        assert cur.fetchone()["long"]                                                       # only NEW sign-ins get the longer one


def test_the_change_is_logged_with_who_and_old_and_new(signed):
    _set_length(signed["client"], 480)
    texts = [a["summary"] for a in access.recent_audit()]
    assert "Sign-in lasts 8 hours (was 30 days)" in texts
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT updated_by FROM instance_settings WHERE key = 'session_minutes'")
        assert cur.fetchone()["updated_by"] == signed["id"]
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT actor_user_id FROM instance_audit ORDER BY id DESC LIMIT 1")
        assert cur.fetchone()["actor_user_id"] == signed["id"]


def test_the_default_stays_thirty_days_without_a_setting(lc):
    with get_cursor() as cur:
        cur.execute("DELETE FROM instance_settings WHERE key = 'session_minutes'")
    assert web_auth.session_minutes() == 43200 == config.WEB_SESSION_MINUTES


# ---- Fable review #809 follow-ups ----

def test_identify_never_answers_429_from_a_per_person_counter(client):
    """A locked username must not make its EMAIL answer differently at the identifier step (an email↔username oracle)."""
    from app import local_auth as la
    import inspect
    from app.routers import local_auth_pages as lap
    src = inspect.getsource(lap.identify)
    assert "_limits(request, None)" in src and "user_ip_key" not in src and "user_key" not in src


def test_finishing_enrolment_needs_codes_made_in_this_session():
    """After a recovery-code sign-in the OLD codes must not satisfy "I've saved them": only codes made since this session began."""
    import inspect
    from app.routers import local_auth_pages as lap
    from app import local_auth as la
    assert "fresh_recovery_codes_since(s[\"user_id\"], s[\"id\"])" in inspect.getsource(lap.enrol_complete)
    assert "r.created_at >= s.created_at" in inspect.getsource(la.fresh_recovery_codes_since)


def test_account_security_keeps_the_header_and_menu(signed):
    """Owner 11-16: the page used the chrome-less sign-in shell, so the only way back was the browser's Back button."""
    html = signed["client"].get("/account/security").text
    assert 'id="nav-drawer"' in html and 'id="tabbar"' in html and "data-account-security" in html
    assert "Back to Crewboss" not in html
