#!/usr/bin/env python3
"""Browser checks for `./crewboss deploy-test` (task #589). Needs Playwright (python) and a Chromium.

    python deploy_test_browser.py BASE_URL USERNAME TEMP_PASSWORD

Prints one `PASS|FAIL|SKIP <name>[: detail]` line per check; exit 0 when none failed. It signs the owner in with the temporary
password, chooses a password, sets up the authenticator app (the key is read off the page), saves the recovery codes, steps up with a fresh code and makes a project.
"""
import glob
import os
import re
import sys

from playwright.sync_api import sync_playwright

CHROME = (glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell"))
          or [None])[0]
failed = []


def result(ok: bool, name: str, detail: str = "") -> bool:
    print(("PASS " if ok else "FAIL ") + name + (f": {detail}" if detail else ""), flush=True)
    if not ok:
        failed.append(name)
    return ok


def tour_walkthrough(page, base: str) -> None:
    """Walk the first-run tour (#938) on the fresh trial: a new owner with no projects. It starts by itself; step 2 is walked with
    its "Make a project now" button (the real + and the real form), then every later step by Next to Done. Asserts no off-site
    navigation and no 4xx/5xx answer to the app's own requests."""
    seen, bad = [], []
    page.on("framenavigated", lambda f: seen.append(f.url) if f == page.main_frame else None)
    page.on("response", lambda r: bad.append(f"{r.status} {r.url.replace(base, '')}") if r.url.startswith(base) and r.status >= 400 else None)
    page.wait_for_selector("[data-tour-root]:not([hidden])", timeout=15000)
    title = lambda: page.locator("[data-tour-title]").text_content().strip()      # noqa: E731
    count = lambda: page.locator("[data-tour-count]").text_content().strip()      # noqa: E731
    first = title()
    result(first.startswith("Welcome"), "tour: starts by itself on the first page", first)
    total = int(count().split(" of ")[1])
    page.click("[data-tour-act=next]")
    page.wait_for_function("() => document.querySelector('[data-tour-count]').textContent.includes('Step 2 ')", timeout=10000)
    page.wait_for_selector("[data-tour-act=do]:visible", timeout=10000)
    result(page.locator("[data-tour-act=do]").text_content().strip() == "Make a project now", "tour: step 2 offers 'Make a project now'", title())
    page.click("[data-tour-act=do]")
    form = page.locator("form[action='/projects']")
    form.wait_for(state="visible", timeout=10000)
    form.locator("input[name=name]").fill("Trial project")
    form.locator("input[name=slug]").fill("trial-project")
    sn = form.locator("input[name=short_name]")
    if sn.count() and sn.is_visible():
        sn.fill("trial")
    form.locator("button[type=submit]").click()
    page.wait_for_url("**/trial-project**", timeout=15000)
    result(True, "browser: project created through the tour", page.url.replace(base, ""))
    page.wait_for_selector("[data-tour-root]:not([hidden])", timeout=15000)
    titles = [first, "Make your first project"]
    for _ in range(total + 2):
        page.wait_for_function("() => document.querySelector('[data-tour-title]').textContent.trim() !== ''", timeout=10000)
        t = title()
        if t not in titles:
            titles.append(t)
        btn = page.locator("[data-tour-act=next]")
        if btn.text_content().strip() == "Done":
            break
        n = int(count().split(" ")[1])
        btn.click()
        try:
            page.wait_for_function("(n) => { const c = document.querySelector('[data-tour-count]'); return c && !c.textContent.includes('Step ' + n + ' '); }",
                                   arg=n, timeout=15000)
        except Exception:  # noqa: BLE001 - a step that navigates reloads the page; the next page's tour resumes at the next step
            page.wait_for_selector("[data-tour-root]:not([hidden])", timeout=15000)
    result(len(titles) == total and titles[-1].startswith("You"), f"tour: walked every step to Done ({len(titles)} of {total})", " > ".join(titles))
    page.click("[data-tour-act=next]")
    page.wait_for_selector("[data-tour-root]", state="hidden", timeout=10000)
    st = {}
    for _ in range(40):
        st = page.evaluate("fetch('/all/tour', {headers: {'HX-Request': 'true'}}).then(r => r.json())")
        if st.get("status") == "finished":
            break
        page.wait_for_timeout(250)
    result(st.get("status") == "finished", "tour: finished is saved on the server", str(st.get("status")))
    off = [u for u in seen if not (u == base or u.startswith(base + "/"))]
    result(not off, "tour: no off-site navigation", f"{len(seen)} page loads" if not off else ", ".join(off[:3]))
    result(not bad, "tour: no 4xx/5xx while walking it", f"{len(seen)} page loads" if not bad else ", ".join(bad[:5]))


def totp(secret: str, offset_steps: int = 0) -> str:
    """RFC 6238 (SHA-1, 30 s, 6 digits) with the standard library only. `offset_steps` = 1 gives the NEXT window's code: the server accepts
    one step either side but never a step it has already used, so a step-up right after enrolment uses the next one instead of waiting."""
    import base64
    import hashlib
    import hmac
    import struct
    import time
    key = base64.b32decode(secret.replace(" ", "").upper() + "=" * (-len(secret.replace(" ", "")) % 8))
    mac = hmac.new(key, struct.pack(">Q", int(time.time() // 30) + offset_steps), hashlib.sha1).digest()
    o = mac[-1] & 15
    return f"{(struct.unpack('>I', mac[o:o + 4])[0] & 0x7fffffff) % 10**6:06d}"


def main(base: str, user: str, password: str) -> int:
    """Owner's first sign-in (#976): temporary password -> choose a password -> authenticator key ("Can't scan it?") -> code -> recovery
    codes -> a step-up with a FRESH code -> the tour. DEPLOY_TEST_IGNORE_HTTPS=1 trusts a LAN trial's self-signed certificate."""
    new_password = "trial-" + os.urandom(6).hex() + "-pass"
    with sync_playwright() as p:
        # DEPLOY_TEST_HOSTMAP="name=127.0.0.1": reach a trial's test hostname without DNS, and trust its throwaway certificate
        hostmap = os.environ.get("DEPLOY_TEST_HOSTMAP", "")
        args = [f"--host-resolver-rules=MAP {hostmap.split('=')[0]} {hostmap.split('=')[1]}"] if "=" in hostmap else []
        browser = p.chromium.launch(**({"executable_path": CHROME} if CHROME else {}), args=args)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, ignore_https_errors=bool(args) or os.environ.get("DEPLOY_TEST_IGNORE_HTTPS") == "1")
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        try:
            page.goto(base + "/auth/login")
            result(page.locator("input[name=identifier]").count() == 1, "browser: sign-in page shows the username box")
            page.fill("input[name=identifier]", user)
            page.click("button:has-text('Continue')")
            page.wait_for_selector("[data-password-form]")
            page.fill("[data-password-form] input[name=password]", password)
            page.click("[data-password-form] button[type=submit]")
            page.wait_for_url("**/auth/enrol", timeout=15000)
            result(True, "browser: temporary password accepted, goes to Choose a password")
            page.fill("input[name=password]", new_password)
            page.fill("input[name=confirm]", new_password)
            page.click("form[action='/auth/enrol/password'] button[type=submit]")
            page.wait_for_selector("[data-totp-secret]", timeout=15000)
            secret = page.locator("[data-totp-secret]").text_content().strip()
            result(len(secret) >= 16, "browser: authenticator key shown under Can't scan it?", f"{len(secret)} characters")
            page.fill("form[action='/auth/enrol/totp'] input[name=code]", totp(secret))
            page.click("form[action='/auth/enrol/totp'] button[type=submit]")
            page.wait_for_selector("[data-make-codes]", timeout=15000)
            result(True, "browser: authenticator code accepted")
            page.click("[data-make-codes]")
            page.wait_for_selector("[data-codes-saved]", timeout=10000)
            page.check("[data-codes-saved]")
            page.click("[data-codes-continue]")
            page.wait_for_url(lambda u: "/auth/" not in u, timeout=15000)
            result(True, "browser: recovery codes saved, signed in", page.url.replace(base, ""))
            # Step-up: the code just used is refused, the next window's is accepted.
            r = page.evaluate("(c) => fetch('/account/security/reauth', {method: 'POST', credentials: 'same-origin', headers: {'Accept': 'application/json', "
                              "'Content-Type': 'application/x-www-form-urlencoded'}, body: 'code=' + c}).then(r => r.status)", totp(secret))
            result(r == 401, "browser: step-up refuses the code that was just used", f"HTTP {r}")
            r = page.evaluate("(c) => fetch('/account/security/reauth', {method: 'POST', credentials: 'same-origin', headers: {'Accept': 'application/json', "
                              "'Content-Type': 'application/x-www-form-urlencoded'}, body: 'code=' + c}).then(r => r.status)", totp(secret, 1))
            result(r == 200, "browser: step-up accepts a fresh code", f"HTTP {r}")

            page.wait_for_selector("#nav-burger", timeout=15000)
            tour_walkthrough(page, base)                    # also makes the project (its step 2)
            r = page.goto(base + "/trial-project")
            result(r.status == 200, "browser: the new project's page loads", f"HTTP {r.status}")
            out = os.environ.get("DEPLOY_TEST_CODE_OUT")
            if out:     # an enrolment code for the runner step (Machines > New enrolment code); written to a 0600 file, never printed
                page.goto(base + "/runners")      # the old link redirects to the account's Machines > Runners tab
                form = page.locator("form[action$='/runners/codes']").first          # the page's own New enrolment code form
                action = form.get_attribute("action") if form.count() else "/trial-project/runners/codes"
                html = page.evaluate("(a) => fetch(a, {method: 'POST', credentials: 'same-origin', headers: "
                                     "{'Content-Type': 'application/x-www-form-urlencoded'}, body: 'mode=subscription&where=auto'}).then(r => r.text().then(t => r.status + ' ' + t))", action)
                m = re.search(r"data-code>([^<]+)<", html)
                if m:
                    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(fd, "w") as fh:
                        fh.write(m.group(1).strip())
                result(bool(m), "browser: enrolment code made (Machines > New enrolment code)", "" if m else re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))[:200])
        except Exception as exc:  # noqa: BLE001 - one line per failure, the caller prints it
            result(False, "browser checks stopped", f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]} (at {page.url.replace(base, '')})")
            try:
                shots = os.environ.get("DEPLOY_TEST_SHOTS", os.getcwd())
                page.screenshot(path=os.path.join(shots, "deploy-test-failure.png"))
                with open(os.path.join(shots, "deploy-test-failure.html"), "w") as fh:
                    fh.write(page.content())
            except Exception:  # noqa: BLE001
                pass
        if errors:
            result(False, "browser: no uncaught page errors", errors[0][:200])
        else:
            result(True, "browser: no uncaught page errors")
        browser.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:4]))
