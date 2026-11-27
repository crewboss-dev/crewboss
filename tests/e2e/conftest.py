"""End-to-end smoke suite (tests/e2e/README.md): the real UI in headless Chromium against a private test database.

Everything here is skipped unless asked for: `pytest -m e2e` or CREWBOSS_E2E=1. The server boot pattern is the one tests/test_full_pages_browser.py uses
(uvicorn on a free loopback port in a thread, the throwaway Postgres from tests/conftest.py); the CA's /ca/* side is stubbed in the browser, because it needs
a signed-in person and a runner, and the stub is what lets a test drive the drawer deterministically.
"""
import glob
import json
import os
import re
import socket
import threading
import time
from urllib.parse import urlsplit

import pytest

pw = pytest.importorskip("playwright.sync_api")
uvicorn = pytest.importorskip("uvicorn")

CHROME = (glob.glob(os.path.expanduser("~/.cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell"))
          or [None])[0]
SHOTS = os.environ.get("CREWBOSS_E2E_SHOTS", "/tmp/crewboss-e2e-shots")

DEVICES = {
    "desktop": {"viewport": {"width": 1440, "height": 900}},
    "phone": {"viewport": {"width": 390, "height": 844}, "is_mobile": True, "has_touch": True},
}

# Failed same-origin responses and console errors that are expected, each with the reason. Anything else fails the test that caused it.
ALLOWED = [
    # (pattern on the URL / message, why it is expected). Empty on purpose: add an entry only with its reason.
]

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "app", "static")
BUILD_OUTPUTS = ("css/app.css", "js/htmx.min.js", "js/alpine.min.js")


def pytest_collection_modifyitems(config, items):
    on = "e2e" in (config.getoption("-m") or "") or os.environ.get("CREWBOSS_E2E") == "1"
    skip = pytest.mark.skip(reason="end-to-end browser suite: run with `pytest -m e2e` (or CREWBOSS_E2E=1); see tests/e2e/README.md")
    for item in items:
        if "/tests/e2e/" in str(item.fspath).replace("\\", "/"):
            item.add_marker(pytest.mark.e2e)
            if not on:
                item.add_marker(skip)
            elif not CHROME:
                item.add_marker(pytest.mark.skip(reason="no Chromium (chromium_headless_shell) for Playwright"))


def _free_port():
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        return sk.getsockname()[1]


@pytest.fixture(scope="session")
def server():
    missing = [f for f in BUILD_OUTPUTS if not os.path.exists(os.path.join(STATIC, f))]
    assert not missing, f"app/static/{missing} are build outputs (gitignored) and are missing: see tests/e2e/README.md, 'Before the first run'"
    port = _free_port()
    from app import config
    config.ORIGIN_HOSTS = tuple(config.ORIGIN_HOSTS) + (f"127.0.0.1:{port}",)
    from app.main import app
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", lifespan="off"))
    th = threading.Thread(target=srv.run, daemon=True)
    th.start()
    for _ in range(200):
        if srv.started:
            break
        time.sleep(0.05)
    assert srv.started, "the test server did not start"
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    th.join(5)


@pytest.fixture(scope="session")
def browser():
    with pw.sync_playwright() as p:
        b = p.chromium.launch(executable_path=CHROME)
        yield b
        b.close()


@pytest.fixture
def seed():
    """Two projects, three tasks in the first (one doing, one ready, one backlog) and one in the second. Returns a plain dict."""
    from app import repo
    a = repo.create_project("test-proj", "Test Project", "internal", "system")
    b = repo.create_project("other-proj", "Other Project", "internal", "system")
    t1 = repo.create_task(a["id"], "Task Alpha", "body a", "must", "bug", [], {}, "owner", assignee="owner", status="doing")
    t2 = repo.create_task(a["id"], "Task Beta", "body b", "should", "feature", [], {}, "owner", assignee="owner", status="ready")
    t3 = repo.create_task(a["id"], "Task Gamma", "body c", "could", "tweak", [], {}, "owner", status="backlog")
    t4 = repo.create_task(b["id"], "Task Delta", "body d", "must", "feature", [], {}, "owner", status="backlog")
    return {"a": a, "b": b, "t1": t1, "t2": t2, "t3": t3, "t4": t4}


class Watch:
    """What the browser reported on one page: console errors, uncaught exceptions and same-origin responses >= 400."""

    def __init__(self, origin):
        self.origin = origin
        self.problems = []
        self.muted = False                       # a test that provokes errors on purpose (the watcher check) sets this

    def _allowed(self, text):
        return any(rx.search(text) for rx, _why in ALLOWED)

    def attach(self, page):
        def console(msg):
            if msg.type == "error":
                loc = msg.location.get("url", "") if msg.location else ""
                if loc and urlsplit(loc).netloc != urlsplit(self.origin).netloc:
                    return
                text = f"console error: {msg.text} ({loc})"
                if not self._allowed(text):
                    self.problems.append(text)

        def response(r):
            if r.status >= 400 and urlsplit(r.url).netloc == urlsplit(self.origin).netloc and not self._allowed(r.url):
                self.problems.append(f"HTTP {r.status} {r.request.method} {r.url}")

        def failed(req):
            err = (req.failure or "")
            if urlsplit(req.url).netloc == urlsplit(self.origin).netloc and "ERR_ABORTED" not in str(err) and not self._allowed(req.url):
                self.problems.append(f"request failed {req.method} {req.url}: {err}")

        page.on("console", console)
        page.on("pageerror", lambda e: self.problems.append(f"uncaught: {e}"))
        page.on("response", response)
        page.on("requestfailed", failed)


STATE = {"online": True, "runners": [], "conversations": [], "first_name": "M", "speech": False, "unread": 0, "notices": []}

FAKE_EVENT_SOURCE = """
(function () {
  var Real = window.EventSource, all = [];
  function Fake(url) {
    if (String(url).indexOf('/ca/stream') < 0) return new Real(url);
    var l = {}; this.url = url; this.readyState = 1; all.push(this);
    this.addEventListener = function (n, f) { (l[n] = l[n] || []).push(f); };
    this.close = function () {};
    this._emit = function (n, d) { (l[n] || []).forEach(function (f) { f({ data: JSON.stringify(d) }); }); };
  }
  window.EventSource = Fake;
  window.__ca_emit = function (name, data) { all.forEach(function (s) { s._emit(name, data); }); };
})();
"""


def stub_ca(page):
    """The CA's server side, in the browser: no runner needed, the SSE stream replaced by a handle (window.__ca_emit)."""
    ok = lambda body: dict(status=200, content_type="application/json", body=json.dumps(body))   # noqa: E731
    page.add_init_script(FAKE_EVENT_SOURCE)
    page.route("**/ca/state", lambda r: r.fulfill(**ok(STATE)))
    page.route("**/ca/notices/read", lambda r: r.fulfill(**ok({})))
    page.route("**/ca/items/**", lambda r: r.fulfill(status=200, content_type="text/html", body="<div data-stub-discussions></div>"))
    page.route("**/ca/stream", lambda r: r.fulfill(status=200, content_type="text/event-stream", body=": hi\n\n"))
    # a task's Discussion panel (#830): the e2e person is not a CA user, so the real endpoint answers 403; an empty discussion they may post in
    page.route("**/ca/tasks/*/discussion", lambda r: r.fulfill(**ok({"task": {"id": int(r.request.url.split("/ca/tasks/")[1].split("/")[0])},
                                                                      "can_post": True, "items": []})))


def stub_tour(page):
    """The first-run tour (#938) starts by itself on a person's first page, so every journey that is not about it sees it as already finished (GET only:
    a POST goes to the real route)."""
    body = json.dumps({"status": "finished", "step": "", "steps": []})
    page.route("**/all/tour", lambda r: r.fulfill(status=200, content_type="application/json", body=body) if r.request.method == "GET" else r.continue_())


@pytest.fixture
def new_page(browser, server):
    """new_page("desktop" | "phone") -> a page in a fresh context with the CA stubbed, a problem watcher attached and a screenshot helper
    (page.shot("name")); tour=True lets the first-run tour run for real (tests/e2e/test_e2e_tour.py). A test that finishes with unexplained console errors / failed requests fails: that is the broad net (journey 8)."""
    made = []

    def make(device="desktop", stub=True, tour=False):
        ctx = browser.new_context(**DEVICES[device])
        page = ctx.new_page()
        page.set_default_timeout(8000)
        w = Watch(server)
        w.attach(page)
        page.watch, page.device = w, device
        if stub:
            stub_ca(page)
        if not tour:
            stub_tour(page)
        os.makedirs(SHOTS, exist_ok=True)
        counter = {"n": 0}
        test = os.environ.get("PYTEST_CURRENT_TEST", "t").split("::")[-1].split(" ")[0]
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", test).strip("-")

        def shot(name):
            counter["n"] += 1
            page.screenshot(path=os.path.join(SHOTS, f"{slug}-{counter['n']:02d}-{name}.png"))
        page.shot = shot
        made.append((ctx, page))
        return page

    yield make
    problems = []
    for ctx, page in made:
        problems += [] if page.watch.muted else page.watch.problems
        ctx.close()
    assert not problems, "the browser reported problems:\n" + "\n".join(problems)


@pytest.fixture(params=["desktop", "phone"])
def device(request):
    return request.param


@pytest.fixture
def local_signin(monkeypatch, server, tmp_path):
    """Sign-in ON in local (passkey) mode for the in-process server; restored when the test ends."""
    from app import config
    monkeypatch.setattr(config, "WEB_AUTH", "required")
    monkeypatch.setattr(config, "AUTH_SETTING", "local")
    monkeypatch.setattr(config, "OIDC_CLIENT_ID", "")
    monkeypatch.setattr(config, "PUBLIC_URL", server)
    monkeypatch.setattr(config, "LOCAL_AUTH_KEY_FILE", str(tmp_path / "secrets" / "local-auth.key"))


@pytest.fixture
def sign_in(local_signin, server):
    """sign_in(page): make the owner and give the page's browser context a local session, the way tests/test_local_auth.py fakes a finished sign-in (password + authenticator is the default since #976, so the amr is "mfa")
    (the browser's own passkey prompt cannot be driven headless). The pages then see a real signed-in person, so what needs one (Runners'
    enrolment, the CA) behaves as it does live."""
    from app import local_auth, web_auth
    from app.db import get_cursor

    def do(page):
        with get_cursor(commit=False) as cur:
            cur.execute("SELECT * FROM users WHERE is_owner")
            row = cur.fetchone()
        if row is None:
            uid, _temp = local_auth.create_owner("owner")
            with get_cursor(commit=False) as cur:
                cur.execute("SELECT * FROM users WHERE id = %s", (uid,))
                row = cur.fetchone()
        token = web_auth.create_local_session(dict(row), "mfa", ip="127.0.0.1", user_agent="e2e")
        page.context.add_cookies([{"name": web_auth.session_cookie_name(), "value": token, "url": server}])
    return do
