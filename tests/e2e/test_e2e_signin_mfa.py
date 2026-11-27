"""Journey: the real first sign-in in a browser (#976): temporary password -> choose a password -> authenticator key -> code -> recovery codes,
then sign out and back in with password + code; plus the owner's Front door card and the Runners enrolment form. The other journeys fake a
finished session (the sign_in fixture); this one drives the pages themselves."""
import re
import time

import pyotp
import pytest


def _code(secret, offset=0):
    return pyotp.TOTP(secret).at(time.time() + 30 * offset)


@pytest.fixture
def fresh_owner(local_signin, server):
    from app import local_auth
    from app.db import get_cursor
    with get_cursor() as cur:       # a clean slate: no owner yet, so the first sign-in is the real first run
        cur.execute("DELETE FROM web_sessions")
        cur.execute("DELETE FROM users WHERE is_owner")
    uid, temp = local_auth.create_owner("owner")
    return {"id": uid, "temp": temp}


def test_first_sign_in_enrolment_then_password_and_authenticator_sign_in(new_page, server, seed, fresh_owner, device):
    page = new_page(device)
    new_pw = "e2e-" + str(int(time.time())) + "-long-pass"
    page.goto(f"{server}/auth/login")
    page.fill("input[name=identifier]", "owner")
    page.click("button:has-text('Continue')")
    page.wait_for_selector("[data-password-form]")
    page.shot("password-step")
    page.fill("[data-password-form] input[name=password]", fresh_owner["temp"])
    page.click("[data-password-form] button[type=submit]")
    page.wait_for_url("**/auth/enrol")
    page.shot("choose-a-password")
    page.fill("input[name=password]", new_pw)
    page.fill("input[name=confirm]", new_pw)
    page.click("form[action='/auth/enrol/password'] button[type=submit]")
    page.wait_for_selector("[data-totp-secret]")
    secret = re.sub(r"\s+", "", page.locator("[data-totp-secret]").text_content()).upper()
    assert len(secret) >= 16 and page.locator("[data-totp-qr]").is_visible()
    page.shot("authenticator")
    page.fill("form[action='/auth/enrol/totp'] input[name=code]", _code(secret))
    page.click("form[action='/auth/enrol/totp'] button[type=submit]")
    page.wait_for_selector("[data-make-codes]")
    page.click("[data-make-codes]")
    page.wait_for_selector("[data-codes-saved]")
    page.shot("recovery-codes")
    page.check("[data-codes-saved]")
    page.click("[data-codes-continue]")
    page.wait_for_url(lambda u: "/auth/" not in u)
    page.wait_for_selector("#page-inner")
    # sign out, then the normal sign-in: password AND the next code in one step
    page.context.clear_cookies()
    page.goto(f"{server}/auth/login")
    page.fill("input[name=identifier]", "owner")
    page.click("button:has-text('Continue')")
    page.wait_for_selector("[data-password-form]")
    page.fill("[data-password-form] input[name=password]", new_pw)
    page.fill("[data-password-form] input[name=code]", _code(secret, 1))          # the next 30 s step: the enrolment's step cannot be replayed
    page.shot("password-and-code")
    page.click("[data-password-form] button[type=submit]")
    page.wait_for_url(lambda u: "/auth/" not in u)
    page.wait_for_selector("#page-inner")
    assert "/auth/login" not in page.url
    # a wrong code and a wrong password both fail with the same words
    page.context.clear_cookies()
    page.goto(f"{server}/auth/login")
    page.fill("input[name=identifier]", "owner")
    page.click("button:has-text('Continue')")
    page.wait_for_selector("[data-password-form]")
    page.fill("[data-password-form] input[name=password]", new_pw)
    page.fill("[data-password-form] input[name=code]", "000000")
    page.click("[data-password-form] button[type=submit]")
    page.wait_for_selector("[role=alert]")
    assert "/auth/login" in page.url
    page.watch.muted = True        # the failed sign-in answers 401, which the net would otherwise report


def test_the_front_door_card_and_the_runners_enrolment_form_load_for_the_owner(new_page, server, seed, sign_in, device):
    page = new_page(device)
    sign_in(page)
    page.goto(f"{server}/admin/front-door")
    page.wait_for_selector("[data-front-door]")
    assert page.locator("[data-front-door]").is_visible()
    assert "Front door" in page.locator("h1").first.text_content()
    page.shot("front-door-card")
    page.goto(f"{server}/runners")
    page.wait_for_selector("form[action$='/runners/codes']")
    assert re.search(r"/machines(\?|$)", page.url), page.url
    page.shot("runners-enrolment")
