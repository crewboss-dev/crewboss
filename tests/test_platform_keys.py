"""Platform keys (#562): the signed client (1135), the page (1134/1143), the stage reminders (1143).

The HTTP side is always replaced (platform_keys._urlopen): nothing here reaches a real key store. The signature is held
to the reference client (the app bin/platform-key, when this box has it) AND to the contract's formula computed here
independently, so a drift in either is caught."""
import hashlib
import hmac
import http.client
import importlib.machinery
import importlib.util
import io
import json
import logging
import os
import re
import time
import urllib.error
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

import pytest

from app import config, platform_keys as pk, push
from app.db import get_cursor

SECRET = "0123456789abcdef" * 4                      # 64 characters, like `openssl rand -hex 32`
VALUE = "sk-" "ant-api03-THIS-IS-THE-SECRET-VALUE-9f8e7d6c5b4a"
REFERENCE = "/opt/stacks/app/bin/platform-key"


def contract_sig(secret, method, path, ts, nonce, body):
    """The contract's formula, written out here on its own: hex HMAC-SHA256 over METHOD\\nPATH\\nQUERY\\nts\\nnonce\\n+body."""
    msg = f"{method}\n{path}\n\n{ts}\n{nonce}\n".encode() + body
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------------------------
# a fake key store
# ---------------------------------------------------------------------------------------------

def status_doc(**over):
    keys = over.pop("keys", None) or [
        {"name": "ANTHROPIC_API_KEY", "label": "API key", "group": "Anthropic (AI)", "module": "core",
         "used_by": "ERP app", "applies": "live", "writable": True, "grace": False, "why": "AI everywhere",
         "integrations": ["anthropic-erp"], "set": True, "source": "store", "via": None, "env_too": False,
         "key_version": 3, "last_changed": "2026-10-26T03:12:00+00:00", "by": "Crewboss: Alex",
         "grace_until": None, "in_use": {"servers": 2, "current": 2}, "note": None, "expires": False,
         "expires_on": None, "expires_source": None, "rotate_every_months": 12, "last_rotated": "2026-10-26",
         "last_rotated_source": "store", "next_due": "2027-10-26", "days_left": 365, "stage": "ok", "needs": None},
        {"name": "WEBHOOK_KEY", "label": "Webhook key", "group": "Example systems", "module": "core", "used_by": "Sign",
         "applies": "live", "writable": True, "grace": True, "why": "", "integrations": [], "set": True,
         "source": "store", "via": None, "env_too": True, "key_version": 2, "last_changed": "2026-10-26T03:12:00+00:00",
         "by": "Crewboss: Alex",
         "grace_until": (datetime.now(timezone.utc) + timedelta(hours=5)).isoformat(),
         "in_use": {"servers": 1, "current": 0}, "note": "3 Sign envelope(s) still out", "expires": False,
         "expires_on": None, "rotate_every_months": 6, "last_rotated": "2026-05-01", "last_rotated_source": "store",
         "next_due": "2026-11-01", "days_left": 5, "stage": "due_7", "needs": None},
        {"name": "POSTGRES_PASSWORD", "label": "POSTGRES_PASSWORD", "group": "Bootstrap (server .env / admin.env)",
         "applies": "bootstrap", "writable": False, "grace": False, "set": None, "source": "bootstrap",
         "key_version": None, "last_changed": None, "expires": False, "expires_on": None, "rotate_every_months": 12,
         "last_rotated": "2026-03-01", "last_rotated_source": "typed", "next_due": "2027-03-01", "days_left": 125,
         "stage": "ok"},
    ]
    doc = {"server": "core-1", "store_version": 42, "grace_hours": 24.0, "keys": keys,
           "key_rotation": [], "servers": [{"id": "erp-core-1", "name": "Example ERP (core-1)", "host": "core-1",
                                            "enabled": True, "last_fetch_at": "2026-10-26T03:12:02+00:00",
                                            "last_version": 42, "up_to_date": True}]}
    doc.update(over)
    return doc


class _Resp:
    def __init__(self, status, body):
        self.status, self._body = status, body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeStore:
    """Stands in for urllib.request.urlopen. Records every request; answers from `routes` or a default."""

    def __init__(self):
        self.calls = []
        self.answers = []        # queued (status, dict) answers, used first
        self.doc = status_doc()

    def __call__(self, req, timeout=None, context=None):
        body = req.data or b""
        self.calls.append({"method": req.get_method(), "url": req.full_url,
                           "headers": {k.lower(): v for k, v in req.header_items()}, "body": body})
        if self.answers:
            status, out = self.answers.pop(0)
        elif req.get_method() == "GET":
            status, out = 200, self.doc
        else:
            status, out = 200, {"ok": True, "name": req.full_url.rsplit("/", 1)[-1], "action": "rotate",
                                "key_version": 4, "store_version": 43, "propagation": "all", "changed": True}
        raw = json.dumps(out).encode() if isinstance(out, dict) else out
        if not 200 <= status < 300:
            raise urllib.error.HTTPError(req.full_url, status, "x", {}, io.BytesIO(raw))
        return _Resp(status, raw)


@pytest.fixture
def secret_file(tmp_path):
    p = tmp_path / "dash-keys-1"
    p.write_text(SECRET + "\n")
    os.chmod(p, 0o600)
    return p


DASH = "test-dash"                                   # the Dashboard project: the one page for every store (#577)
PAGE = f"/{DASH}/keys"


@pytest.fixture
def dash(monkeypatch):
    """The Dashboard project (no overseer in the tests, so platform_keys.home_slug falls back to DASHBOARD_PROJECT)."""
    from app import repo
    monkeypatch.setattr(config, "DASHBOARD_PROJECT", DASH)
    return repo.create_project(DASH, "Crewboss", "internal", "system")


@pytest.fixture
def fake(monkeypatch, secret_file, project, dash):
    store = FakeStore()
    monkeypatch.setattr(pk, "_urlopen", store)
    monkeypatch.setattr(config, "PLATFORM_KEY_STORES", {
        project["slug"]: {"name": "Test ERP", "url": "https://keys.test.invalid", "key_id": "dash-keys-1",
                          "key_file": str(secret_file), "server": "core-1"}})
    return store


@pytest.fixture
def pushes(monkeypatch):
    sent = []
    monkeypatch.setattr(push, "deliver", lambda payload: sent.append(payload) or {"sent": 1, "failed": 0, "pruned": 0})
    return sent


def verify(call, secret=SECRET):
    """The store's check (signedapi.verify), restated: key id, ts within 60 s, nonce shape, signature."""
    h = call["headers"]
    assert h["x-dash-key"] == "dash-keys-1"
    assert abs(time.time() - int(h["x-dash-ts"])) <= 60
    assert re.fullmatch(r"[A-Za-z0-9_-]{16,128}", h["x-dash-nonce"])
    path = call["url"].removeprefix("https://keys.test.invalid")
    assert "?" not in path
    assert h["x-dash-sig"] == contract_sig(secret, call["method"], path, h["x-dash-ts"], h["x-dash-nonce"],
                                           call["body"])


# ---------------------------------------------------------------------------------------------
# 1135: the signature
# ---------------------------------------------------------------------------------------------

def _reference():
    if not os.path.isfile(REFERENCE):
        pytest.skip("the reference client (the app bin/platform-key) isn't on this box")
    loader = importlib.machinery.SourceFileLoader("platform_key_reference", REFERENCE)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/platform/keys", b""),
    ("PUT", "/platform/keys/ANTHROPIC_API_KEY", json.dumps({"value": VALUE, "no_grace": False, "actor": "M"}).encode()),
    ("PUT", "/platform/keys/POSTGRES_PASSWORD", json.dumps({"rotate_every_months": 12, "actor": "M"}).encode()),
    ("POST", "/platform/keys/WEBHOOK_KEY/end-grace", b'{"actor": "M"}'),
    ("POST", "/platform/keys/AZURE_MAPS_KEY/clear", "{\"actor\": \"Zoë ✓\"}".encode()),
])
def test_signature_matches_the_reference_client(method, path, body):
    ref = _reference()
    ts, nonce = 1761450000, "a1b2c3d4e5f60718293a4b5c6d7e8f90"
    mine = pk.signed_headers("dash-keys-1", SECRET, method, path, body, ts=ts, nonce=nonce)
    assert mine == ref.signed_headers(SECRET, method, path, body, ts=ts, nonce=nonce)
    assert pk.message(method, path, "", ts, nonce, body) == ref.message(method, path, "", ts, nonce, body)
    assert mine["X-Dash-Sig"] == contract_sig(SECRET, method, path, ts, nonce, body)


def test_signature_matches_the_contract_formula_without_the_reference():
    ts, nonce = 1761450000, "nonce_nonce-nonce1"
    h = pk.signed_headers("dash-keys-1", SECRET, "put", "/platform/keys/X_KEY", b'{"a":1}', ts=ts, nonce=nonce)
    assert h == {"X-Dash-Key": "dash-keys-1", "X-Dash-Ts": str(ts), "X-Dash-Nonce": nonce,
                 "X-Dash-Sig": contract_sig(SECRET, "PUT", "/platform/keys/X_KEY", ts, nonce, b'{"a":1}')}


def test_meta_body_matches_the_reference_client():
    ref = _reference()
    assert pk.meta_from_form("date", "2027-09-30", "months", "12", "2026-03-01") == \
        ref.meta_body("2027-09-30", "12", "2026-03-01")
    assert pk.meta_from_form("none", "", "never") == ref.meta_body("none", "never")
    assert pk.meta_from_form("default", "", "default") == ref.meta_body("default", "default")


def test_each_call_has_a_fresh_nonce_and_a_current_timestamp(fake, project):
    s = pk.store(project["slug"])
    pk.get_status(s)
    pk.get_status(s)
    a, b = fake.calls
    assert a["headers"]["x-dash-nonce"] != b["headers"]["x-dash-nonce"]
    for c in fake.calls:
        verify(c)
        assert c["method"] == "GET" and c["body"] == b""


def test_put_and_posts_are_signed_over_their_exact_body(fake, project):
    s = pk.store(project["slug"])
    pk.set_value(s, "ANTHROPIC_API_KEY", "  " + VALUE + "\n", no_grace=True, actor="Alex (a@x)")
    pk.set_meta(s, "POSTGRES_PASSWORD", {"rotate_every_months": 12}, actor="Alex (a@x)")
    pk.end_grace(s, "WEBHOOK_KEY", actor="Alex (a@x)")
    pk.clear(s, "WEBHOOK_KEY", actor="Alex (a@x)")
    put, meta, eg, cl = fake.calls
    for c in fake.calls:
        verify(c)
        assert c["headers"]["content-type"] == "application/json"
    assert put["method"] == "PUT" and put["url"].endswith("/platform/keys/ANTHROPIC_API_KEY")
    assert json.loads(put["body"]) == {"value": VALUE, "no_grace": True, "actor": "Alex (a@x)"}   # stripped
    assert json.loads(meta["body"]) == {"rotate_every_months": 12, "actor": "Alex (a@x)"}
    assert "value" not in json.loads(meta["body"])
    assert (eg["method"], eg["url"].rsplit("/platform/keys", 1)[1]) == ("POST", "/WEBHOOK_KEY/end-grace")
    assert (cl["method"], cl["url"].rsplit("/platform/keys", 1)[1]) == ("POST", "/WEBHOOK_KEY/clear")
    assert json.loads(cl["body"]) == {"actor": "Alex (a@x)"}


def test_a_bad_name_or_empty_value_never_calls(fake, project):
    s = pk.store(project["slug"])
    for bad in ("lower", "A", "X_KEY\n", "../X", "X KEY"):
        with pytest.raises(pk.KeysError):
            pk.set_value(s, bad, VALUE)
    with pytest.raises(pk.KeysError) as e:
        pk.set_value(s, "ANTHROPIC_API_KEY", "   ")
    assert e.value.code == "empty"
    with pytest.raises(pk.KeysError):
        pk.set_meta(s, "ANTHROPIC_API_KEY", {})
    with pytest.raises(pk.KeysError):
        pk.set_meta(s, "ANTHROPIC_API_KEY", {"value": VALUE})
    assert fake.calls == []


# ---------------------------------------------------------------------------------------------
# 1135: errors
# ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("status,answer,code", [
    (401, {"error": "unauthorized", "message": "not authorised"}, "unauthorized"),
    (429, {"error": "rate_limited", "message": "too many calls; slow down"}, "rate_limited"),
    (404, {"detail": "not found"}, "not_found"),
    (503, {"error": "unconfigured", "message": "the platform keys endpoint is not configured on this server"},
     "unconfigured"),
])
def test_refusals_are_clear_errors(fake, project, status, answer, code):
    fake.answers.append((status, answer))
    with pytest.raises(pk.KeysError) as e:
        pk.get_status(pk.store(project["slug"]))
    assert e.value.code == code and e.value.status == status
    if status == 401:
        assert "signature" in str(e.value)
    if status == 429:
        assert "30 a minute" in str(e.value)
    if status == 404:
        assert "allowed address" in str(e.value)


def test_unknown_key_and_invalid_value_show_the_stores_words(fake, project):
    s = pk.store(project["slug"])
    fake.answers.append((404, {"error": "unknown_key", "message": "no such key in this server's registry"}))
    with pytest.raises(pk.KeysError, match="no such key"):
        pk.set_value(s, "NOPE_KEY", VALUE)
    fake.answers.append((422, {"error": "invalid", "message": "must be at least 40 characters", "rule": "short"}))
    with pytest.raises(pk.KeysError, match="at least 40") as e:
        pk.set_value(s, "ANTHROPIC_API_KEY", VALUE)
    assert e.value.code == "invalid"


def test_unreachable_and_not_json(monkeypatch, fake, project):
    s = pk.store(project["slug"])
    fake.answers.append((502, b"<html>bad gateway</html>"))
    with pytest.raises(pk.KeysError) as e:
        pk.get_status(s)
    assert e.value.code == "not_json"

    def down(*a, **k):
        raise urllib.error.URLError("no route")
    monkeypatch.setattr(pk, "_urlopen", down)
    with pytest.raises(pk.KeysError) as e:
        pk.get_status(s)
    assert e.value.code == "unreachable"


def test_the_credential_file_is_checked_at_call_time(tmp_path, monkeypatch, project):
    monkeypatch.setattr(pk, "_urlopen", lambda *a, **k: pytest.fail("must not call"))
    s = {"slug": "x", "url": "https://keys.test.invalid", "key_id": "dash-keys-1", "key_file": str(tmp_path / "none")}
    with pytest.raises(pk.KeysError) as e:
        pk.get_status(s)
    assert e.value.code == "credential"
    f = tmp_path / "k"
    f.write_text(SECRET)
    os.chmod(f, 0o644)
    s["key_file"] = str(f)
    with pytest.raises(pk.KeysError, match="chmod 600"):
        pk.get_status(s)
    os.chmod(f, 0o600)
    f.write_text("short")
    with pytest.raises(pk.KeysError, match="32"):
        pk.get_status(s)
    s["key_file"] = str(tmp_path)                      # docker made a directory
    with pytest.raises(pk.KeysError, match="not a file"):
        pk.get_status(s)
    for msg in (pk.credential_problem(s),):
        assert SECRET not in msg


def test_actor_is_the_signed_in_person():
    assert pk.actor_for({"name": "Alex Example", "username": "alex@example.com"}) == \
        "Alex Example (alex@example.com)"
    assert pk.actor_for({"name": "", "username": "m@x"}) == "m@x"
    assert pk.actor_for({"name": "Evil\nName\x00", "username": ""}) == "Evil Name"
    assert pk.actor_for(None) == "owner"
    assert len(pk.actor_for({"name": "x" * 500})) == 120


def test_meta_form_rules():
    assert pk.meta_from_form() == {}
    assert pk.meta_from_form("date", "2027-01-31") == {"expires_on": "2027-01-31"}
    assert pk.meta_from_form(rotate_mode="months", rotate_months="6") == {"rotate_every_months": 6}
    assert pk.meta_from_form(last_rotated="2026-03-01") == {"last_rotated": "2026-03-01"}
    for args in (("date", ""), ("date", "2027-02-30"), ("bogus",)):
        with pytest.raises(pk.KeysError):
            pk.meta_from_form(*args)
    for months in ("0", "121", "x", ""):
        with pytest.raises(pk.KeysError):
            pk.meta_from_form(rotate_mode="months", rotate_months=months)


# ---------------------------------------------------------------------------------------------
# a value never escapes
# ---------------------------------------------------------------------------------------------

def test_values_and_the_credential_never_reach_the_logs(fake, project, caplog):
    caplog.set_level(logging.DEBUG)
    s = pk.store(project["slug"])
    pk.set_value(s, "ANTHROPIC_API_KEY", VALUE, actor="M")
    for status, answer in ((401, {"error": "unauthorized"}), (422, {"error": "invalid", "message": "too short"}),
                           (429, {"error": "rate_limited"})):
        fake.answers.append((status, answer))
        with pytest.raises(pk.KeysError) as e:
            pk.set_value(s, "ANTHROPIC_API_KEY", VALUE, actor="M")
        assert VALUE not in str(e.value) and SECRET not in str(e.value)
    assert "ANTHROPIC_API_KEY" in caplog.text                 # it does log, by name
    assert VALUE not in caplog.text and SECRET not in caplog.text


def _page_post(client, project, path, data, **kw):
    """A form post from the Dashboard's page, to the store `project` (its slug is in the path, #577)."""
    return client.post(f"{PAGE}/{project['slug']}/{path}", data=data, follow_redirects=False, **kw)


def test_set_from_the_page_sends_the_value_once_and_never_echoes_it(client, fake, project, caplog):
    caplog.set_level(logging.DEBUG)
    r = _page_post(client, project, "ANTHROPIC_API_KEY/set", {"value": VALUE})
    assert r.status_code == 303
    assert r.headers["location"] == f"{PAGE}#key-{project['slug']}-ANTHROPIC_API_KEY"
    assert VALUE not in r.text and VALUE not in unquote(str(r.headers))
    put = fake.calls[-1]
    verify(put)
    assert json.loads(put["body"]) == {"value": VALUE, "no_grace": False, "actor": "owner"}
    page = client.get(PAGE, cookies=r.cookies)
    assert page.status_code == 200
    assert "ANTHROPIC_API_KEY replaced" in page.text and "version 4" in page.text
    assert "in use on every server now" in page.text
    assert VALUE not in page.text and SECRET not in page.text
    assert page.headers["cache-control"] == "no-store"
    tags = re.findall(r'<input[^>]*name="value"[^>]*>', page.text)
    assert tags
    for tag in tags:   # a CSS-masked text field, never offered to a password manager, always rendered empty
        assert 'type="text"' in tag and "-webkit-text-security:disc" in tag
        assert 'autocomplete="off"' in tag and 'spellcheck="false"' in tag and "new-password" not in tag
        assert "value=" not in tag.replace('name="value"', "")
    assert VALUE not in caplog.text and SECRET not in caplog.text
    with get_cursor() as cur:                          # stored nowhere: not in any health row, snapshot or stage row
        cur.execute("SELECT snapshot::text AS t FROM health_sources")
        assert all(VALUE not in (row["t"] or "") for row in cur.fetchall())


def test_a_refused_set_says_why_without_the_value(client, fake, project):
    fake.answers.append((422, {"error": "invalid", "message": "must start with sk-ant-", "rule": "format"}))
    r = _page_post(client, project, "ANTHROPIC_API_KEY/set", {"value": VALUE, "no_grace": "on"})
    assert r.status_code == 303 and VALUE not in unquote(str(r.headers))
    assert json.loads(fake.calls[-1]["body"])["no_grace"] is True
    page = client.get(PAGE, cookies=r.cookies)
    assert "wasn&#39;t changed: must start with sk-ant-" in page.text or "wasn't changed: must start with sk-ant-" in page.text
    assert VALUE not in page.text


def test_rate_limited_and_unauthorised_on_the_page(client, fake, project):
    fake.answers.append((429, {"error": "rate_limited", "message": "too many calls; slow down"}))
    page = client.get(PAGE)
    assert page.status_code == 200 and "30 a minute" in page.text and "data-keys-error" in page.text
    fake.answers.append((401, {"error": "unauthorized", "message": "not authorised"}))
    r = _page_post(client, project, "WEBHOOK_KEY/end-grace", {})
    page = client.get(PAGE, cookies=r.cookies)
    assert "refused the Dashboard" in page.text


def test_meta_end_grace_and_clear_from_the_page(client, fake, project):
    fake.answers.append((200, {"ok": True, "name": "POSTGRES_PASSWORD", "store_version": 44,
                               "meta": {"next_due": "2027-03-01", "stage": "ok"}}))
    r = _page_post(client, project, "POSTGRES_PASSWORD/meta",
                   {"expires_mode": "none", "rotate_mode": "months", "rotate_months": "12",
                    "last_rotated": "2026-03-01"})
    assert r.status_code == 303
    body = json.loads(fake.calls[-1]["body"])
    assert body == {"expires_on": None, "rotate_every_months": 12, "last_rotated": "2026-03-01", "actor": "owner"}
    assert "next due 2027-03-01" in client.get(PAGE, cookies=r.cookies).text

    n = len(fake.calls)
    r = _page_post(client, project, "WEBHOOK_KEY/clear", {"confirm": "WRONG"})
    assert len(fake.calls) == n                         # no confirm, no call
    r = _page_post(client, project, "WEBHOOK_KEY/clear", {"confirm": "WEBHOOK_KEY"})
    assert fake.calls[-1]["url"].endswith("/WEBHOOK_KEY/clear")
    r = _page_post(client, project, "WEBHOOK_KEY/end-grace", {})
    assert fake.calls[-1]["url"].endswith("/WEBHOOK_KEY/end-grace") and r.status_code == 303


def test_a_cross_site_post_is_refused_before_any_call(client, fake, project):
    for headers in ({"origin": "https://evil.example"}, {"sec-fetch-site": "cross-site"}):
        r = _page_post(client, project, "ANTHROPIC_API_KEY/set", {"value": VALUE}, headers=headers)
        assert r.status_code == 403
    assert fake.calls == []


def test_the_page_shows_status_and_flags_stages(client, fake, project, pushes):
    page = client.get(PAGE)
    assert page.status_code == 200
    t = page.text
    for name in ("ANTHROPIC_API_KEY", "WEBHOOK_KEY", "POSTGRES_PASSWORD"):
        assert f'id="key-{project["slug"]}-{name}"' in t
    assert 'data-due-key="WEBHOOK_KEY" data-stage="due_7"' in t and "Due within 7 days" in t
    assert "Previous value accepted until" in t and "data-end-grace" in t and "revoke it at once" in t
    assert f'action="{PAGE}/{project["slug"]}/POSTGRES_PASSWORD/meta"' in t           # bootstrap: dates only
    assert f'action="{PAGE}/{project["slug"]}/POSTGRES_PASSWORD/set"' not in t
    assert "3 Sign envelope(s) still out" in t and "Example ERP (core-1)" in t
    assert f'href="{PAGE}"' in t                                                       # in the nav
    verify(fake.calls[0])
    assert [p["title"] for p in pushes] == ["Due within 7 days: WEBHOOK_KEY"]             # the page's read counted


def test_no_store_no_page_and_no_nav(client, monkeypatch, project, dash):
    monkeypatch.setattr(config, "PLATFORM_KEY_STORES", {})
    page = client.get(PAGE)
    assert page.status_code == 200 and "data-keys-no-stores" in page.text
    assert "/keys\"" not in client.get(f"/{project['slug']}/board").text
    assert "/keys\"" not in client.get(f"/{DASH}/board").text      # no store, no menu item


def test_without_a_credential_the_page_says_not_connected(client, monkeypatch, project, dash):
    monkeypatch.setattr(pk, "_urlopen", lambda *a, **k: pytest.fail("must not call"))
    monkeypatch.setattr(config, "PLATFORM_KEY_STORES", {project["slug"]: {
        "url": "https://keys.test.invalid", "key_id": "dash-keys-1", "key_file": "/nonexistent/x", "server": "core-1"}})
    page = client.get(PAGE)
    assert page.status_code == 200 and "no writer credential" in page.text


# ---------------------------------------------------------------------------------------------
# 1143: stage reminders
# ---------------------------------------------------------------------------------------------

def _doc(stage, days=None, due=None, name="MCP_CLIENT_SECRET"):
    return status_doc(keys=[{"name": name, "label": "Client secret", "group": "Microsoft", "applies": "live",
                             "writable": True, "source": "store", "stage": stage, "next_due": due,
                             "days_left": days}])


def _open_reminders():
    with get_cursor() as cur:
        cur.execute("SELECT * FROM health_problems WHERE source = 'keys' AND resolved_at IS NULL")
        return cur.fetchall()


def test_stage_changes_raise_one_reminder_and_one_push_each(fake, project, pushes):
    s = pk.store(project["slug"])
    pk.observe(s, _doc("ok", 40, "2026-12-06"))
    assert pushes == [] and _open_reminders() == []

    pk.observe(s, _doc("due_30", 25, "2026-11-21"))
    assert len(pushes) == 1 and pushes[0]["title"] == "Due within 30 days: MCP_CLIENT_SECRET"
    assert pushes[0]["url"].endswith(f"{PAGE}#{project['slug']}") and pushes[0]["renotify"] is True
    [row] = _open_reminders()
    assert row["severity"] == 2 and "due in 25 days (2026-11-21)" in row["name"] and row["host"] == "core-1"
    assert row["pushed_at"] is not None

    pk.observe(s, _doc("due_30", 24, "2026-11-21"))     # same stage: nothing new
    assert len(pushes) == 1

    pk.observe(s, _doc("due_7", 6, "2026-11-01"))
    assert len(pushes) == 2 and _open_reminders()[0]["severity"] == 3
    pk.observe(s, _doc("overdue", -1, "2026-10-26"))
    assert len(pushes) == 3 and "overdue since 2026-10-26" in _open_reminders()[0]["name"]
    assert _open_reminders()[0]["severity"] == 4

    pk.observe(s, _doc("ok", 365, "2027-10-26"))        # rotated: withdrawn quietly
    assert len(pushes) == 3 and _open_reminders() == []
    pk.observe(s, _doc("due_30", 20, "2026-11-16"))     # and a later due again announces again
    assert len(pushes) == 4


def test_going_down_a_stage_updates_quietly(fake, project, pushes):
    s = pk.store(project["slug"])
    pk.observe(s, _doc("due_7", 5, "2026-11-01"))
    pk.observe(s, _doc("due_30", 20, "2026-11-16"))     # new dates typed
    assert len(pushes) == 1 and _open_reminders()[0]["severity"] == 2


def test_first_read_groups_every_due_key_into_one_push(fake, project, pushes):
    keys = [{"name": f"KEY_{i}", "group": "g", "stage": st, "next_due": "2026-11-01", "days_left": 3}
            for i, st in enumerate(["due_7", "overdue", "ok", "due_30"])]
    pk.observe(pk.store(project["slug"]), status_doc(keys=keys))
    assert len(pushes) == 1 and pushes[0]["title"].startswith("3 platform keys need rotating · overdue")
    assert pushes[0]["body"].splitlines()[0].startswith("KEY_1")          # worst first
    assert len(_open_reminders()) == 3


def test_the_estate_sweep_never_pushes_key_reminders_again(fake, project, pushes):
    pk.observe(pk.store(project["slug"]), _doc("overdue", 0, "2026-10-27"))
    assert push.due_problems(datetime.now(timezone.utc) + timedelta(hours=2)) == []


def test_a_key_gone_from_the_registry_is_withdrawn(fake, project, pushes):
    s = pk.store(project["slug"])
    pk.observe(s, _doc("due_7", 5, "2026-11-01"))
    pk.observe(s, _doc("ok", 300, "2027-08-01", name="OTHER_KEY"))
    assert _open_reminders() == []


def test_sync_reads_every_store_and_keeps_reminders_on_a_failed_read(fake, project, pushes):
    fake.doc = _doc("due_7", 5, "2026-11-01")
    out = pk.sync()
    assert out["ok"] and out["stores"][project["slug"]] == {"went_up": ["MCP_CLIENT_SECRET"]}
    verify(fake.calls[-1])
    fake.answers.append((429, {"error": "rate_limited"}))
    out = pk.sync()
    assert out["ok"] is False and out["stores"][project["slug"]] == "rate_limited"
    assert len(_open_reminders()) == 1                  # couldn't look is not fixed
    snap, ok_at, error = pk.last_snapshot(project["slug"])
    assert snap["keys"][0]["name"] == "MCP_CLIENT_SECRET" and ok_at and "30 a minute" in error


def test_no_credential_raises_the_cant_read_alarm_at_once(monkeypatch, project, pushes):
    monkeypatch.setattr(pk, "_urlopen", lambda *a, **k: pytest.fail("must not call"))
    monkeypatch.setattr(config, "PLATFORM_KEY_STORES", {project["slug"]: {
        "url": "https://keys.test.invalid", "key_id": "dash-keys-1", "key_file": "/nonexistent/x", "server": "core-1"}})
    out = pk.sync()
    assert out["stores"][project["slug"]] == "no credential" and out["ok"] is False
    [row] = _open_reminders()
    assert row["eventid"] == pk.down_eventid(project["slug"]) and row["severity"] == 4
    assert "no usable credential" in row["name"] and row["pushed_at"] is None


def test_health_page_shows_key_reminders(client, fake, monkeypatch, pushes):
    from app import repo
    hp = repo.create_project("health-proj", "Health Project", "internal", "system")
    monkeypatch.setattr(config, "HEALTH_PROJECT", hp["slug"])
    monkeypatch.setattr(config, "PLATFORM_KEY_STORES", {hp["slug"]: dict(
        config.PLATFORM_KEY_STORES[next(iter(config.PLATFORM_KEY_STORES))])})
    pk.observe(pk.store(hp["slug"]), _doc("overdue", 0, "2026-10-27"))
    t = client.get(f"/{hp['slug']}/health").text
    assert "data-keys-health=\"1\"" in t and "MCP_CLIENT_SECRET" in t and "Overdue" in t
    assert f'href="{PAGE}#{hp["slug"]}"' in t                   # the store's section on the Dashboard's page (#577)


# ---------------------------------------------------------------------------------------------
# 1136: no drift — the Dashboard's own integrations registry
# ---------------------------------------------------------------------------------------------

def test_the_dashboard_registry_validates_and_names_every_store_credential():
    from app import integrations, integrations_dashboard as idash
    doc = idash.export()
    integrations.validate(doc)
    named = {k for r in doc["integrations"] for k in r.get("keys", [])}
    for slug, s in config.PLATFORM_KEY_STORES.items():
        assert s["key_id"] in named, f"store {slug}: credential {s['key_id']} is in no integration row"
    groups = {"kind": "kinds", "flow": "flows", "starts": "starts", "zone": "zones", "resolves": "resolves",
              "path": "paths", "auth": "auths", "transport": "transports"}
    for r in doc["integrations"]:
        for field, group in groups.items():
            assert r[field] in doc["vocab"][group], f"{r['id']}.{field} {r[field]!r} has no label"


def test_the_dashboard_registry_shows_on_its_own_project(client, monkeypatch, project):
    from app import integrations
    monkeypatch.setattr(config, "DASHBOARD_PROJECT", project["slug"])
    srcs = integrations.sources(project)
    assert [s for s, _ in srcs] == ["dashboard"]
    assert integrations.merged(srcs)["integrations"][0]["id"] == "dash-platform-keys"
    t = client.get(f"/{project['slug']}/integrations").text
    assert "Platform keys to each project" in t
    monkeypatch.setattr(config, "DASHBOARD_PROJECT", "someone-else")
    assert integrations.sources(project) == []


# ---------------------------------------------------------------------------------------------
# review fixes: a failed read is never silent (1a), an unanswered write may have happened (2, 3), one push (4)
# ---------------------------------------------------------------------------------------------

def test_reads_failing_for_30_minutes_raise_an_alarm_that_a_good_read_clears(fake, project, pushes):
    from app import erp_health
    s, t0 = pk.store(project["slug"]), datetime.now(timezone.utc)
    fake.doc = _doc("ok", 300, "2027-08-01")
    pk.observe(s, fake.doc, t0 - timedelta(hours=1))
    for minutes in (0, 20):
        fake.answers.append((429, {"error": "rate_limited"}))
        pk.sync(now=t0 + timedelta(minutes=minutes))
    assert _open_reminders() == []                      # under 30 minutes: shown on the page, no alarm yet
    fake.answers.append((503, {"error": "store_unavailable", "message": "this server's key store can't be reached"}))
    pk.sync(now=t0 + timedelta(minutes=31))
    [row] = _open_reminders()
    assert row["eventid"] == pk.down_eventid(project["slug"]) and row["severity"] == 4 and not row["baseline"]
    assert "can't read core-1's key store" in row["name"] and "can't be reached" in row["name"]
    assert row["pushed_at"] is None                      # the estate sweep pushes it, once
    assert ("keys", row["eventid"]) in {(p["source"], p["eventid"]) for p in push.due_problems()}
    assert "key error" in erp_health.home_slot()["label"]
    fake.answers.append((429, {"error": "rate_limited"}))
    pk.sync(now=t0 + timedelta(minutes=46))
    assert len(_open_reminders()) == 1                   # still one alarm, not a second
    pk.sync(now=t0 + timedelta(minutes=61))              # a good read clears it
    assert _open_reminders() == []
    with get_cursor() as cur:
        cur.execute("SELECT failing_since FROM health_sources WHERE source = %s", (f"keys:{project['slug']}",))
        assert cur.fetchone()["failing_since"] is None


def test_a_failed_page_read_starts_the_same_clock(client, fake, project):
    fake.answers.append((401, {"error": "unauthorized"}))
    client.get(PAGE)
    with get_cursor() as cur:
        cur.execute("SELECT failing_since, last_error FROM health_sources WHERE source = %s",
                    (f"keys:{project['slug']}",))
        row = cur.fetchone()
    assert row["failing_since"] is not None and "signature" in row["last_error"]


def _raise(exc):
    def opener(req, timeout=None, context=None):
        raise exc
    return opener


@pytest.mark.parametrize("exc", [TimeoutError("timed out"), ConnectionResetError("reset"),
                                 http.client.RemoteDisconnected("closed"), http.client.BadStatusLine("x")])
def test_no_answer_after_a_write_was_sent_may_have_been_written(monkeypatch, fake, project, exc, caplog):
    caplog.set_level(logging.DEBUG)
    s = pk.store(project["slug"])
    monkeypatch.setattr(pk, "_urlopen", _raise(exc))
    for act in (lambda: pk.set_value(s, "ANTHROPIC_API_KEY", VALUE), lambda: pk.end_grace(s, "WEBHOOK_KEY"),
                lambda: pk.clear(s, "WEBHOOK_KEY"), lambda: pk.set_meta(s, "WEBHOOK_KEY", {"rotate_every_months": 6})):
        with pytest.raises(pk.KeysError) as e:
            act()
        assert e.value.code == "uncertain"
        assert "may have been written" in str(e.value) and "version" in str(e.value)
        assert "wasn't" not in str(e.value) and VALUE not in str(e.value)
    with pytest.raises(pk.KeysError) as e:              # a read that just didn't answer
        pk.get_status(s)
    assert e.value.code == "unreachable"
    assert VALUE not in caplog.text and SECRET not in caplog.text


def test_a_write_that_never_got_through_says_unreachable(monkeypatch, fake, project):
    s = pk.store(project["slug"])
    monkeypatch.setattr(pk, "_urlopen", _raise(urllib.error.URLError(ConnectionRefusedError("refused"))))
    with pytest.raises(pk.KeysError) as e:
        pk.set_value(s, "ANTHROPIC_API_KEY", VALUE)
    assert e.value.code == "unreachable" and "Couldn't reach" in str(e.value)


def test_a_gateway_timeout_on_a_write_may_have_been_written(fake, project):
    fake.answers.append((504, b"<html>gateway timeout</html>"))
    with pytest.raises(pk.KeysError) as e:
        pk.set_value(pk.store(project["slug"]), "ANTHROPIC_API_KEY", VALUE)
    assert e.value.code == "uncertain"


@pytest.mark.parametrize("path,data", [("ANTHROPIC_API_KEY/set", {"value": VALUE}), ("WEBHOOK_KEY/end-grace", {}),
                                       ("WEBHOOK_KEY/clear", {"confirm": "WEBHOOK_KEY"}),
                                       ("WEBHOOK_KEY/meta", {"rotate_mode": "never"})])
def test_the_page_never_says_wasnt_changed_when_it_may_have_been(client, monkeypatch, fake, project, path, data):
    name = path.split("/")[0]
    monkeypatch.setattr(pk, "_urlopen", _raise(TimeoutError("timed out")))
    r = _page_post(client, project, path, data)
    assert r.status_code == 303 and VALUE not in unquote(str(r.headers))
    page = client.get(PAGE, cookies=r.cookies)
    note = re.search(r'data-keys-note="error"[^>]*>(.*?)</div>', page.text, re.S).group(1)
    assert note.strip().startswith(f"{name}: The key store didn") and "may have been written" in note
    for wrong in ("wasn", "weren", "not ended"):
        assert wrong not in note
    assert VALUE not in page.text


def test_an_older_read_finishing_late_changes_nothing_and_pushes_once(fake, project, pushes):
    """A poll that STARTED before a page view but finished after it must not lower the stage the page view announced
    (the next poll would then announce it again)."""
    s, t0 = pk.store(project["slug"]), datetime.now(timezone.utc)
    assert [k["name"] for k in pk.observe(s, _doc("due_7", 5, "2026-11-01"), t0 + timedelta(seconds=10))] == \
        ["MCP_CLIENT_SECRET"]                           # the page view's newer read: announced
    assert pk.observe(s, _doc("ok", 40, "2026-12-06"), t0) == []      # the poll's older read lands late: skipped
    with get_cursor() as cur:
        cur.execute("SELECT stage FROM platform_key_stages WHERE store = %s AND name = 'MCP_CLIENT_SECRET'",
                    (project["slug"],))
        assert cur.fetchone()["stage"] == "due_7"       # no regression
    assert len(_open_reminders()) == 1
    assert pk.last_snapshot(project["slug"])[0]["keys"][0]["stage"] == "due_7"   # the newer document is kept
    pk.observe(s, _doc("due_7", 5, "2026-11-01"), t0 + timedelta(seconds=20))    # the next poll
    assert len(pushes) == 1


def test_the_page_times_its_read_before_the_get(client, fake, project, monkeypatch):
    seen = []
    real = pk.observe
    monkeypatch.setattr(pk, "observe", lambda s, doc, now=None: seen.append(now) or real(s, doc, now))
    before = datetime.now(timezone.utc)
    client.get(PAGE)
    assert seen and seen[0] is not None and before <= seen[0] <= datetime.now(timezone.utc)


def test_concurrent_reads_push_a_stage_change_once(fake, project, monkeypatch):
    import threading
    sent = []

    def slow_deliver(payload):
        time.sleep(0.2)
        sent.append(payload)
        return {"sent": 1, "failed": 0, "pruned": 0}
    monkeypatch.setattr(push, "deliver", slow_deliver)
    s, doc = pk.store(project["slug"]), _doc("due_7", 5, "2026-11-01")
    start = threading.Barrier(4)

    def read():
        start.wait()
        pk.observe(s, doc)
    threads = [threading.Thread(target=read) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(sent) == 1


# ---------------------------------------------------------------------------------------------
# #577: one page on the Dashboard project, a section per store; the old /<store>/keys redirects there
# ---------------------------------------------------------------------------------------------

SECRET2 = "fedcba9876543210" * 4


def test_keys_nav_is_on_the_dashboard_project_only(client, fake, project):
    dash_html = client.get(f"/{DASH}/board").text
    assert 'data-cat="keys"' in dash_html            # Platform keys wait in the "+" catalogue of the Dashboard project only (#773)
    store_html = client.get(f"/{project['slug']}/board").text
    assert 'data-cat="keys"' not in store_html and 'data-pill="keys"' not in store_html and f"/{project['slug']}/keys" not in store_html


def test_the_home_is_the_overseer_root_when_there_is_one(monkeypatch):
    monkeypatch.setattr(config, "OVERSEER_PROJECT", "dash-root")
    monkeypatch.setattr(config, "DASHBOARD_PROJECT", "something-else")
    assert pk.home_slug() == "dash-root"
    assert pk.page_path("acme-erp") == "/dash-root/keys#acme-erp" and pk.page_path() == "/dash-root/keys"


def test_the_old_store_page_redirects_to_its_section(client, fake, project):
    r = client.get(f"/{project['slug']}/keys", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == f"{PAGE}#{project['slug']}"
    assert fake.calls == []                                  # the redirect reads nothing
    assert client.get("/no-such-project/keys", follow_redirects=False).status_code == 404


def test_the_old_store_write_paths_are_gone(client, fake, project):
    for path, data in (("ANTHROPIC_API_KEY/set", {"value": VALUE}), ("WEBHOOK_KEY/end-grace", {})):
        r = client.post(f"/{project['slug']}/keys/{path}", data=data, follow_redirects=False)
        assert r.status_code in (404, 405) and VALUE not in r.text
    # nor can a store's own project stand in for the Dashboard with the new shape
    r = client.post(f"/{project['slug']}/keys/{project['slug']}/ANTHROPIC_API_KEY/set", data={"value": VALUE},
                    follow_redirects=False)
    assert r.status_code in (303, 404) and VALUE not in unquote(str(r.headers)) and VALUE not in r.text
    assert fake.calls == []


@pytest.fixture
def two_stores(monkeypatch, fake, project, tmp_path):
    p2 = tmp_path / "dash-keys-2"
    p2.write_text(SECRET2 + "\n")
    os.chmod(p2, 0o600)
    stores = dict(config.PLATFORM_KEY_STORES)
    stores["crm-proj"] = {"name": "Example CRM", "url": "https://crm-keys.test.invalid", "key_id": "dash-keys-2",
                          "key_file": str(p2), "server": "core02"}
    monkeypatch.setattr(config, "PLATFORM_KEY_STORES", stores)
    return fake


def test_every_store_is_its_own_section(client, two_stores, project):
    t = client.get(PAGE).text
    for slug, head in ((project["slug"], "Test ERP"), ("crm-proj", "Example CRM")):
        assert f'id="{slug}"' in t and f'data-keys-store="{slug}"' in t
        section = t.split(f'data-keys-store="{slug}"', 1)[1].split("data-keys-store=", 1)[0]
        assert head in section and f'id="key-{slug}-ANTHROPIC_API_KEY"' in section
        assert f'action="{PAGE}/{slug}/ANTHROPIC_API_KEY/set"' in section
    # headed by product · server (as the store reports it, here core-1 for both) · host
    assert "· core-1 · keys.test.invalid" in t and "· core-1 · crm-keys.test.invalid" in t
    hosts = {c["url"].split("/platform", 1)[0] for c in two_stores.calls}
    assert hosts == {"https://keys.test.invalid", "https://crm-keys.test.invalid"}     # each store read


def test_a_write_signs_for_its_own_store(client, two_stores, project):
    n = len(two_stores.calls)
    r = client.post(f"{PAGE}/crm-proj/ANTHROPIC_API_KEY/set", data={"value": VALUE}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"{PAGE}#key-crm-proj-ANTHROPIC_API_KEY"
    assert VALUE not in unquote(str(r.headers))
    [put] = two_stores.calls[n:]
    assert put["url"] == "https://crm-keys.test.invalid/platform/keys/ANTHROPIC_API_KEY"
    h = put["headers"]
    assert h["x-dash-key"] == "dash-keys-2"
    path = "/platform/keys/ANTHROPIC_API_KEY"
    assert h["x-dash-sig"] == contract_sig(SECRET2, "PUT", path, h["x-dash-ts"], h["x-dash-nonce"], put["body"])
    assert h["x-dash-sig"] != contract_sig(SECRET, "PUT", path, h["x-dash-ts"], h["x-dash-nonce"], put["body"])
    page = client.get(PAGE, cookies=r.cookies).text           # the note shows in the CRM's section, not the ERP's
    crm = page.split('data-keys-store="crm-proj"', 1)[1]
    erp = page.split(f'data-keys-store="{project["slug"]}"', 1)[1].split('data-keys-store="crm-proj"', 1)[0]
    assert "data-keys-note" in crm and "data-keys-note" not in erp
    assert VALUE not in page

    n = len(two_stores.calls)                                # the ERP's store, same page
    client.post(f"{PAGE}/{project['slug']}/WEBHOOK_KEY/end-grace", data={}, follow_redirects=False)
    [eg] = two_stores.calls[n:]
    assert eg["url"] == "https://keys.test.invalid/platform/keys/WEBHOOK_KEY/end-grace"
    verify(eg)


def test_an_unknown_store_is_never_called(client, two_stores):
    n = len(two_stores.calls)
    r = client.post(f"{PAGE}/nope/ANTHROPIC_API_KEY/set", data={"value": VALUE}, follow_redirects=False)
    assert r.status_code in (303, 404) and VALUE not in unquote(str(r.headers)) and VALUE not in r.text
    assert len(two_stores.calls) == n


def test_a_cross_site_post_to_a_store_is_refused(client, two_stores):
    n = len(two_stores.calls)
    r = client.post(f"{PAGE}/crm-proj/ANTHROPIC_API_KEY/set", data={"value": VALUE}, follow_redirects=False,
                    headers={"origin": "https://evil.example"})
    assert r.status_code == 403 and len(two_stores.calls) == n


@pytest.mark.parametrize("url", ["http://keys.example.com", "ftp://keys.example.com", "keys.example.com", "", "http://localhost.evil.example",
                                 "http://127.0.0.1.evil.example", "https:///nohost", "http://user@evil.example/@localhost"])
def test_a_key_store_url_that_is_not_https_is_refused_before_anything_is_sent(fake, project, secret_file, url):
    s = {"url": url, "key_id": "dash-keys-1", "key_file": str(secret_file)}
    with pytest.raises(pk.KeysError) as exc:
        pk.call(s, "GET", "/platform/keys")
    assert exc.value.code == "refused" and "https://" in str(exc.value)
    assert fake.calls == []                                            # nothing was sent


@pytest.mark.parametrize("url", ["https://keys.example.com", "http://localhost", "http://localhost:8080", "http://127.0.0.1:9000"])
def test_https_and_a_local_http_store_are_accepted(fake, project, secret_file, url):
    pk.check_store_url(url)
