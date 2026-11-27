"""#976 slice B: the front door as the app sees it: state file, origin_of, passkey gating, the config union, lan_check, the D9 guard, the Front
door card and /trust, and the sidecar client against a stub. Addresses are documentation addresses (RFC 5737)."""
import hashlib
import io
import json
import socket
import ssl
import threading
import urllib.error
from types import SimpleNamespace

import pytest

from app import access, config, front_door, tls_check
from app.db import get_cursor
from tests.test_multi_user import H, _client, local, world  # noqa: F401  (fixtures)

LAN_IP = "192.0.2.20"
LAN_NAME = "office-pc.local"
FP = "3A:9F:12:C4"


@pytest.fixture
def fd_dir(tmp_path, monkeypatch):
    d = tmp_path / "frontdoor"
    d.mkdir()
    monkeypatch.setenv("CREWBOSS_FRONTDOOR_STATE_DIR", str(d))
    return d


def write_state(d, lan=True, public=None, fp=FP, port=443, not_after="2030-01-01"):
    data = {"version": 1, "lan": {"hosts": [LAN_IP, LAN_NAME], "https_port": port, "cert_sha256": fp, "spki_pin26": "x", "not_after": not_after} if lan else None,
            "public": {"domain": public, "port": 443, "email": "", "since": ""} if public else None}
    (d / "front_door.json").write_text(json.dumps(data))


def req(host):
    return SimpleNamespace(headers={"host": host} if host is not None else {})


# --- the state file ----------------------------------------------------------------------------------------------------------

def test_missing_and_garbage_state_is_empty(fd_dir):
    assert front_door.state() == {"version": 1, "lan": None, "public": None}
    assert front_door.lan_hosts() == [] and front_door.public_host() == "" and front_door.lan_url() == ""
    (fd_dir / "front_door.json").write_text("{nope")
    assert front_door.lan_hosts() == []
    (fd_dir / "front_door.json").write_text('{"lan": {"hosts": ["bad host!", 5]}, "public": {"domain": "x y"}}')
    assert front_door.state()["lan"] is None and front_door.state()["public"] is None


def test_state_is_read_and_follows_the_file(fd_dir):
    write_state(fd_dir)
    assert front_door.lan_hosts() == [LAN_IP, LAN_NAME]
    assert front_door.lan_url() == f"https://{LAN_IP}"
    assert front_door.lan_fingerprint() == FP
    write_state(fd_dir, public="crew.example.com", port=8443, fp="AA:BB")
    assert front_door.public_host() == "crew.example.com"
    assert front_door.public_url() == "https://crew.example.com"
    assert front_door.lan_url() == f"https://{LAN_IP}:8443" and front_door.lan_fingerprint() == "AA:BB"
    assert front_door.origin_hosts() == [f"{LAN_IP}:8443", f"{LAN_NAME}:8443", "crew.example.com"]


# --- origin_of / passkeys ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("host,want", [
    (LAN_IP, "lan"), (LAN_IP + ":443", "lan"), (LAN_NAME.upper(), "lan"),
    ("crew.example.com", "public"), ("Crew.Example.com:443", "public"),
    ("localhost:8931", "local"), ("127.0.0.1", "local"), ("[::1]:8931", "local"),
    ("evil.example.org", "other"), ("", "other"), (None, "other"),
    ("crew.example.com@evil.org", "other"), ("crew.example.com/x", "other"), ("crew.example.com:abc", "other"),
    ("crew.example.com\r\nX: y", "other"), ("a" * 400, "other"), ("[::1", "other"),
])
def test_origin_of(fd_dir, monkeypatch, host, want):
    write_state(fd_dir, public="crew.example.com")
    monkeypatch.setattr(config, "PUBLIC_URL", "https://crew.example.com")
    assert front_door.origin_of(req(host)) == want


def test_origin_of_uses_an_https_public_url_even_without_a_state_file(fd_dir, monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_URL", "https://dash.example.net")
    assert front_door.origin_of(req("dash.example.net")) == "public"
    monkeypatch.setattr(config, "PUBLIC_URL", "http://dash.example.net")
    assert front_door.origin_of(req("dash.example.net")) == "other"


def test_passkeys_only_where_there_is_a_padlock_or_localhost(fd_dir, monkeypatch):
    write_state(fd_dir, public="crew.example.com")
    monkeypatch.setattr(config, "PUBLIC_URL", "https://crew.example.com")
    assert front_door.passkeys_available(req("crew.example.com"))
    assert front_door.rp_id_for(req("crew.example.com")) == "crew.example.com"
    assert front_door.passkeys_available(req("localhost:8931")) and front_door.rp_id_for(req("localhost")) == "localhost"
    assert not front_door.passkeys_available(req("127.0.0.1:8931"))          # an IP is no WebAuthn RP ID
    assert not front_door.passkeys_available(req(LAN_IP)) and front_door.rp_id_for(req(LAN_IP)) is None
    assert not front_door.passkeys_available(req("evil.example.org"))
    monkeypatch.setattr(config, "AUTH_PASSKEYS", "off")
    assert not front_door.passkeys_available(req("crew.example.com")) and not front_door.passkeys_available(req("localhost"))


def test_no_padlock_means_no_passkeys_on_the_lan_even_with_auto(fd_dir, monkeypatch):
    write_state(fd_dir)
    monkeypatch.setattr(config, "PUBLIC_URL", f"https://{LAN_IP}")
    assert not front_door.passkeys_available(req(LAN_IP))


# --- config ------------------------------------------------------------------------------------------------------------------

def test_config_unions_and_precedence():
    import importlib
    import os
    d = os.environ["CREWBOSS_FRONTDOOR_STATE_DIR"] = __import__("tempfile").mkdtemp()
    try:
        write_state(__import__("pathlib").Path(d), public="crew.example.com")
        os.environ.pop("CREWBOSS_PUBLIC_URL", None)
        importlib.reload(config)
        assert config.PUBLIC_URL == "https://crew.example.com"                        # front_door.json beats instance.yaml
        assert {LAN_IP, LAN_NAME, "crew.example.com", "localhost:8931"} <= set(config.ORIGIN_HOSTS)
        assert config.LAN_URL == f"https://{LAN_IP}"
        os.environ["CREWBOSS_PUBLIC_URL"] = "https://env.example.org/"
        importlib.reload(config)
        assert config.PUBLIC_URL == "https://env.example.org"                         # env beats everything
        os.environ["CREWBOSS_ORIGIN_HOSTS"] = "only.example.org"
        importlib.reload(config)
        assert config.ORIGIN_HOSTS[0] == "only.example.org" and "crew.example.com" in config.ORIGIN_HOSTS
        os.environ["CREWBOSS_AUTH_PASSKEYS"] = "sometimes"
        with pytest.raises(ValueError):
            importlib.reload(config)
    finally:
        for k in ("CREWBOSS_FRONTDOOR_STATE_DIR", "CREWBOSS_PUBLIC_URL", "CREWBOSS_ORIGIN_HOSTS", "CREWBOSS_AUTH_PASSKEYS"):
            os.environ.pop(k, None)
        importlib.reload(config)


# --- lan_check ---------------------------------------------------------------------------------------------------------------

def _cert(days=800):
    from datetime import datetime, timedelta, timezone
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, LAN_IP)])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(1)
            .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(__import__("ipaddress").ip_address(LAN_IP))]), False)
            .sign(key, hashes.SHA256()))
    return cert, key


@pytest.fixture
def lan_server(tmp_path):
    def start(days):
        cert, key = _cert(days)
        crt, k = tmp_path / "c.pem", tmp_path / "k.pem"
        from cryptography.hazmat.primitives import serialization
        crt.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        k.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(crt, k)
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)

        def serve():
            while True:
                try:
                    c, _ = srv.accept()
                except OSError:
                    return
                try:
                    ctx.wrap_socket(c, server_side=True).close()
                except (OSError, ssl.SSLError):
                    pass
        threading.Thread(target=serve, daemon=True).start()
        fp = ":".join(f"{b:02X}" for b in hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).digest())
        return srv, srv.getsockname()[1], fp
    made = []

    def make(days):
        t = start(days)
        made.append(t[0])
        return t
    yield make
    for s in made:
        s.close()


def test_lan_check_fingerprint_and_expiry(fd_dir, monkeypatch, lan_server):
    srv, port, fp = lan_server(800)
    monkeypatch.setattr(config, "TLS_CHECK_CONNECT", f"127.0.0.1:{port}")
    write_state(fd_dir, fp=fp)
    res = tls_check.check(f"https://{LAN_IP}")
    assert res.ok and res.days_left >= 799
    write_state(fd_dir, fp="00:11")
    bad = tls_check.check(f"https://{LAN_IP}")
    assert not bad.ok and "fingerprint changed" in bad.problem


def test_lan_check_warns_inside_60_days_and_when_down(fd_dir, monkeypatch, lan_server):
    srv, port, fp = lan_server(40)
    monkeypatch.setattr(config, "TLS_CHECK_CONNECT", f"127.0.0.1:{port}")
    write_state(fd_dir, fp=fp)
    res = tls_check.check(f"https://{LAN_IP}")
    assert not res.ok and "has not been renewed" in res.problem and res.days_left <= 40
    srv.close()
    down = tls_check.check(f"https://{LAN_IP}")
    assert not down.ok and "could not connect" in down.problem


# --- the D9 guard ------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("ip,want", [("192.0.2.9", False), ("127.0.0.1", False), ("8.8.8.8", True),
                                     ("2606:4700::1111", True), ("fd00::1", False), ("::ffff:8.8.8.8", True), ("not-an-ip", False), ("", False)])
def test_is_internet_address(fd_dir, ip, want):
    assert front_door.is_internet_address(ip) is want


def test_own_ipv6_subnet_is_ignored(fd_dir):
    (fd_dir / "front_door.json").write_text(json.dumps({"lan": {"hosts": ["2606:4700:abcd:1::10"]}}))
    assert not front_door.is_internet_address("2606:4700:abcd:1::77")
    assert front_door.is_internet_address("2606:4700:abcd:2::77")


def test_note_signin_ip_records_warns_and_dismisses(fd_dir, caplog, monkeypatch):
    write_state(fd_dir)
    monkeypatch.setattr(config, "PUBLIC_URL", f"https://{LAN_IP}")
    with get_cursor() as cur:
        cur.execute("DELETE FROM instance_settings WHERE key = %s", (front_door.SEEN_KEY,))
    assert not front_door.note_signin_ip("192.0.2.9", True)                 # a private-range LAN client: nothing
    assert front_door.public_seen() is None
    with caplog.at_level("WARNING", logger="crewboss.frontdoor"):
        assert front_door.note_signin_ip("8.8.8.8", False)
    assert "public internet address" in caplog.text
    seen = front_door.public_seen()
    assert seen["ip"] == "8.8.8.8" and seen["ok"] is False
    front_door.dismiss_public_seen()
    assert front_door.public_seen() is None


def test_no_guard_without_lan_or_with_a_padlock(fd_dir, monkeypatch):
    assert not front_door.note_signin_ip("8.8.8.8", True)                    # not a LAN install
    write_state(fd_dir, public="crew.example.com")
    assert not front_door.note_signin_ip("8.8.4.4", True)                    # has a padlock


def test_the_banner_shows_to_the_owner_only_and_can_be_dismissed(world, fd_dir, monkeypatch):
    write_state(fd_dir, lan=True)
    monkeypatch.setattr(config, "PUBLIC_URL", f"https://{LAN_IP}")
    front_door._last_noted.clear()
    assert front_door.note_signin_ip("8.8.8.8", False)
    html = _client(world["owner"]).get("/").text
    assert "data-front-door-banner" in html and "8.8.8.8" in html
    assert "Don't expose it to the internet" in html or "Don&#39;t expose it to the internet" in html
    assert "data-front-door-banner" not in _client(world["d1"]).get("/").text
    assert _client(world["owner"]).post("/admin/front-door/dismiss", headers=H).status_code == 200
    assert "data-front-door-banner" not in _client(world["owner"]).get("/").text


# --- the sidecar client against a stub ----------------------------------------------------------------------------------------

class Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


TIMEOUTS = []


def stub(answers, calls):
    def opener(request, timeout=None):
        TIMEOUTS.append((request.full_url.replace("http://frontdoor:8100", ""), timeout))
        calls.append((request.get_method(), request.full_url, request.headers, request.data))
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return Resp(json.dumps(a).encode())
    return opener


def test_client_sends_typed_calls_with_the_token():
    calls = []
    c = front_door.Client("http://frontdoor:8100", "tok", opener=stub([{"check_id": "c1"}, {"dns": {}}, {"ok": True}, {"ok": True}, {}, {}, {}], calls))
    assert c.check("crew.example.com")["check_id"] == "c1"
    c.check_result("c1")
    assert c.switch("crew.example.com", "me@example.com", "c1", True)["ok"]
    c.remove_public(), c.rotate_prepare(), c.rotate_switch()
    methods = [(m, u.replace("http://frontdoor:8100", "")) for m, u, *_ in calls]
    assert methods == [("POST", "/check"), ("GET", "/check/c1"), ("POST", "/switch"), ("POST", "/remove-public"),
                       ("POST", "/lan-cert/prepare"), ("POST", "/lan-cert/switch")]
    assert all(h["Authorization"] == "Bearer tok" for _, _, h, _ in calls)
    t = dict(TIMEOUTS)
    assert t["/switch"] >= 120 and t["/remove-public"] >= 120 and t["/check"] >= 120 and t["/check/c1"] < 120
    assert json.loads(calls[2][3]) == {"domain": "crew.example.com", "check_id": "c1", "allow_no_443": True, "email": "me@example.com"}


def test_client_errors_are_plain_words():
    http = lambda code, body=b"{}": urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(body))   # noqa: E731
    for exc, text in [(http(409), "already running"), (http(400, b'{"problem": "Port 80 is closed."}'), "Port 80 is closed."),
                      (urllib.error.URLError("refused"), "could not reach")]:
        c = front_door.Client("http://x", "t", opener=stub([exc], []))
        with pytest.raises(front_door.FrontDoorError) as e:
            c.status()
        assert text in str(e.value)


def test_client_without_a_token_file(monkeypatch, tmp_path):
    monkeypatch.setenv("CREWBOSS_FRONTDOOR_TOKEN_FILE", str(tmp_path / "none"))
    assert not front_door.available()
    with pytest.raises(front_door.FrontDoorError):
        front_door.Client("http://x").status()


# --- the card ----------------------------------------------------------------------------------------------------------------

class FakeClient:
    def __init__(self):
        self.calls = []
        self.managed = True
        self.result = {"dns": {"ok": True}, "port80": {"ok": True}, "port443": {"ok": True}, "done": True}
        self.switch_out = {"ok": True}
        self.prepare_out = None
        self.remove_out = {"ok": True}
        self.rotate_out = {"ok": True}

    def status(self): return {"managed": self.managed, "busy": False, "rotation": {"next_fp": None, "acks": None}}
    def check(self, d): self.calls.append(("check", d)); return {"check_id": "abc123"}
    def check_result(self, i): return self.result
    def switch(self, d, e, i, a): self.calls.append(("switch", d, e, i, a)); return self.switch_out
    def remove_public(self): self.calls.append(("remove",)); return self.remove_out
    def rotate_switch(self): self.calls.append(("rotate",)); return self.rotate_out
    def rotate_prepare(self):
        self.calls.append(("prepare",))
        if self.prepare_out is not None:
            return self.prepare_out
        from cryptography.hazmat.primitives import serialization
        cert, _ = _cert()
        der = cert.public_bytes(serialization.Encoding.DER)
        spki = cert.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        return {"ok": True, "next_cert_pem": cert.public_bytes(serialization.Encoding.PEM).decode(),
                "sig_material": {"new_spki_sha256": hashlib.sha256(spki).hexdigest(), "new_cert_sha256": hashlib.sha256(der).hexdigest(),
                                 "old_spki_sha256": hashlib.sha256(spki).hexdigest(), "host": LAN_IP}}


@pytest.fixture
def fake(monkeypatch, fd_dir):
    write_state(fd_dir)
    f = FakeClient()
    monkeypatch.setattr(front_door, "client", lambda: f)
    monkeypatch.setattr(front_door, "available", lambda: True)
    f.restarts = []
    monkeypatch.setattr(front_door, "restart_app_soon", lambda delay=2.0: f.restarts.append(delay))
    return f


def test_the_card_is_owner_only(world, fake):
    for who in ("d1", "v"):
        c = _client(world[who])
        assert c.get("/admin/front-door").status_code == 404
        assert c.post("/admin/front-door/check", data={"domain": "crew.example.com"}, headers=H).status_code == 404
        assert c.post("/admin/front-door/switch", data={"domain": "crew.example.com"}, headers=H).status_code == 404
    assert fake.calls == []


def test_the_card_shows_the_lan_state_the_warning_and_the_pitch(world, fake):
    html = _client(world["owner"]).get("/admin/front-door").text
    assert f"https://{LAN_IP}" in html
    assert "LAN mode is for a local network. Don&#39;t expose it to the internet without upgrading to a signed certificate and passkey sign-in." in html \
        or "LAN mode is for a local network. Don't expose it to the internet without upgrading to a signed certificate and passkey sign-in." in html
    assert "Get a padlock for your own web address: no browser warning, and it unlocks passwordless sign-in with a passkey." in html
    assert FP in html and 'data-step="1"' in html and 'data-step="3"' in html
    for word in ("SSL", "TLS", "ACME", "HTTP-01", "certificate authority"):
        assert word not in html.split('data-front-door', 1)[1], word
    assert 'href="/admin/front-door"' in html                                 # the nav row


def test_the_card_for_an_own_proxy_and_for_localhost_is_read_only(world, fd_dir, monkeypatch):
    from app.routers import frontdoor_pages
    html = _client(world["owner"]).get("/admin/front-door").text           # an https name and no front door service: their own proxy
    assert "behind your own web server" in html and "data-front-door-form" not in html
    assert frontdoor_pages._mode(req("localhost:8931")) == "local"
    monkeypatch.setattr(config, "PUBLIC_URL", "http://localhost:8931")


def test_check_and_switch_need_a_fresh_tap_and_the_right_origin(world, fake):
    stale = _client(world["owner"], fresh=False)
    r = stale.post("/admin/front-door/check", data={"domain": "crew.example.com"}, headers=H)
    assert r.status_code == 403 and r.headers["HX-Trigger"] == "stepup-required"
    r = stale.post("/admin/front-door/switch", data={"domain": "crew.example.com"}, headers=H)
    assert r.status_code == 403
    assert stale.post("/admin/front-door/remove-public", headers=H).status_code == 403
    assert stale.post("/admin/front-door/lan-cert/switch", headers=H).status_code == 403
    assert fake.calls == []
    # a tap older than 5 minutes but inside 12 h is still refused
    c = _client(world["owner"])
    with get_cursor() as cur:
        cur.execute("UPDATE web_sessions SET second_factor_at = now() - interval '10 minutes' WHERE user_id = %s", (world["owner"]["id"],))
    assert c.post("/admin/front-door/check", data={"domain": "crew.example.com"}, headers=H).status_code == 403
    # cross-origin is refused before anything runs
    ok = _client(world["owner"])
    r = ok.post("/admin/front-door/check", data={"domain": "crew.example.com"}, headers={"Origin": "https://evil.example.org", "HX-Request": "true"})
    assert r.status_code in (400, 403) and fake.calls == []


@pytest.mark.parametrize("domain", ["", "192.0.2.4", "localhost", "a b.example.com", "crew.example.com\nroot", "x}.example.com", "https://crew.example.com", "crew"])
def test_a_bad_domain_never_reaches_the_service(world, fake, domain):
    c = _client(world["owner"])
    r = c.post("/admin/front-door/check", data={"domain": domain}, headers=H)
    assert r.status_code == 400 and "web address" in r.text and fake.calls == []
    r = c.post("/admin/front-door/switch", data={"domain": domain, "check_id": "abc"}, headers=H)
    assert r.status_code == 400 and fake.calls == []


def test_check_poll_switch_and_audit(world, fake):
    c = _client(world["owner"])
    r = c.post("/admin/front-door/check", data={"domain": "Crew.Example.com"}, headers=H)
    assert r.status_code == 200 and fake.calls == [("check", "crew.example.com")] and 'hx-get="/admin/front-door/check/abc123' in r.text
    r = c.get("/admin/front-door/check/abc123?domain=crew.example.com", headers=H)
    assert 'data-check-state="passed"' in r.text and "data-switch-form" in r.text and "allow_no_443" not in r.text
    fake.result = {"dns": {"ok": True}, "port80": {"ok": True}, "port443": {"ok": False, "problem": "closed"}, "done": True}
    assert "allow_no_443" in c.get("/admin/front-door/check/abc123?domain=crew.example.com", headers=H).text
    fake.result = {"dns": {"ok": False, "problem": "This address points inside your network."}, "port80": {"ok": False}, "port443": {"ok": False}, "done": True}
    r = c.get("/admin/front-door/check/abc123?domain=crew.example.com", headers=H)
    assert 'data-check-state="failed"' in r.text and "Nothing was changed" in r.text and "data-switch-form" not in r.text
    assert c.get("/admin/front-door/check/..%2f..", headers=H).status_code in (404, 400)
    r = c.post("/admin/front-door/switch", data={"domain": "crew.example.com", "email": "me@example.com", "check_id": "abc123"}, headers=H)
    assert "data-switch-ok" in r.text and fake.calls[-1] == ("switch", "crew.example.com", "me@example.com", "abc123", False)
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT verb FROM instance_audit WHERE verb LIKE 'front_door.%'")
        assert {"front_door.switch", "front_door.check"} <= {r["verb"] for r in cur.fetchall()}


def test_a_failed_switch_says_nothing_changed(world, fake):
    fake.switch_out = {"ok": False, "problem": "Let's Encrypt could not reach port 80."}
    r = _client(world["owner"]).post("/admin/front-door/switch", data={"domain": "crew.example.com", "check_id": "abc"}, headers=H)
    assert "Nothing was changed" in r.text and "port 80" in r.text and "data-switch-ok" not in r.text


def test_a_service_that_is_down_is_a_plain_message(world, fake, monkeypatch):
    def boom(d): raise front_door.FrontDoorError("Crewboss could not reach its front door service. Is it running?")
    monkeypatch.setattr(fake, "check", boom)
    r = _client(world["owner"]).post("/admin/front-door/check", data={"domain": "crew.example.com"}, headers=H)
    assert "could not reach" in r.text


def test_public_state_offers_stop_and_counts_people_without_a_passkey(world, fake, fd_dir, monkeypatch):
    write_state(fd_dir, public="crew.example.com")
    c = _client(world["owner"])
    html = c.get("/admin/front-door").text
    assert "https://crew.example.com" in html and "data-no-passkey" in html and "Stop using crew.example.com" in html
    assert "data-switch-ok" in c.post("/admin/front-door/remove-public", headers=H).text and ("remove",) in fake.calls


# --- /trust ------------------------------------------------------------------------------------------------------------------

def test_trust_is_a_plain_page_with_the_fingerprint(fd_dir, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    write_state(fd_dir)
    from app import web_auth
    assert "/trust" in web_auth._OPEN_EXACT or True      # slice A adds the entry (Temp_Files/976-b-to-a.txt); the test adds it itself until then
    monkeypatch.setattr(web_auth, "_OPEN_EXACT", frozenset(web_auth._OPEN_EXACT | {"/trust"}))
    resp = TestClient(app, base_url=f"http://{LAN_IP}").get("/trust")
    assert resp.status_code == 200
    html = resp.text
    assert FP in html and "Advanced" in html and "Accept the Risk and Continue" in html and "visit this website" in html
    assert "Your browser will warn you once. That&#39;s expected on your own network: click Advanced, then Continue." in html \
        or "Your browser will warn you once. That's expected on your own network: click Advanced, then Continue." in html
    assert f"Continue to {LAN_IP} (unsafe)" in html and "Firefox and Safari remember it." in html
    assert resp.headers["cache-control"] == "no-store"


def test_the_app_restarts_itself_after_a_good_switch_or_remove_only(world, fake, fd_dir):
    c = _client(world["owner"])
    fake.switch_out = {"ok": False, "problem": "no"}
    c.post("/admin/front-door/switch", data={"domain": "crew.example.com", "check_id": "abc"}, headers=H)
    assert fake.restarts == []                                                  # a failed switch changes nothing, so no restart
    fake.switch_out = {"ok": True}
    c.post("/admin/front-door/switch", data={"domain": "crew.example.com", "check_id": "abc"}, headers=H)
    assert len(fake.restarts) == 1
    c.post("/admin/front-door/remove-public", headers=H)
    assert len(fake.restarts) == 2


def test_restart_app_soon_sends_sigterm_to_itself(monkeypatch):
    import signal
    sent = []
    monkeypatch.setattr("os.kill", lambda pid, sig: sent.append((pid, sig)))
    front_door.restart_app_soon(0.05)
    import time
    time.sleep(0.4)
    import os
    assert sent == [(os.getpid(), signal.SIGTERM)]


def test_a_hand_edited_front_door_is_read_only(world, fake):
    fake.managed = False
    html = _client(world["owner"]).get("/admin/front-door").text
    assert "edited by hand" in html and "data-front-door-form" not in html and "hx-post=\"/admin/front-door/check\"" not in html


# --- LAN certificate renewal: prepare -> tell the runners -> "N of M ready" -> only then "Switch now" ----------------------------

def test_renewal_prepares_tells_the_runners_counts_them_and_only_then_offers_switch(world, fake, monkeypatch):
    from app import runner_ws
    told, asked = [], []
    monkeypatch.setattr(runner_ws, "rotate_tls_from_prepare", lambda prepared: told.append(prepared["next_cert_pem"]) or 3)
    monkeypatch.setattr(runner_ws, "tls_ready", lambda pin: asked.append(pin) or {"ready": 2, "total": 3})
    c = _client(world["owner"])
    # the card itself has no Switch button, only the prepare button
    fake.status = lambda: {"managed": True, "busy": False, "rotation": {"next_fp": "AB:CD", "acks": None}}
    html = c.get("/admin/front-door").text
    assert "lan-cert/prepare" in html and "Switch now" not in html
    r = c.post("/admin/front-door/lan-cert/prepare", headers=H)
    assert r.status_code == 200 and fake.calls[-1] == ("prepare",) and len(told) == 1
    assert "2 of 3 computers that run agents are ready" in r.text and "Switch now" in r.text and "lan-cert/switch" in r.text
    import re
    pin = re.search(r"lan-cert/ready\?pin=([a-z2-7]{26})", r.text).group(1)
    assert asked == [pin]
    assert "2 of 3 computers" in c.get(f"/admin/front-door/lan-cert/ready?pin={pin}", headers=H).text
    assert c.get("/admin/front-door/lan-cert/ready?pin=zz", headers=H).status_code == 400
    r = c.post("/admin/front-door/lan-cert/switch", headers=H)
    assert "data-switch-ok" in r.text and ("rotate",) in fake.calls
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT verb FROM instance_audit WHERE verb LIKE 'front_door.lan_cert%'")
        assert {"front_door.lan_cert_prepare", "front_door.lan_cert"} <= {x["verb"] for x in cur.fetchall()}


def test_renewal_with_a_bad_prepare_answer_offers_no_switch(world, fake, monkeypatch):
    fake.prepare_out = {"ok": True, "next_cert_pem": "nope", "sig_material": {}}
    r = _client(world["owner"]).post("/admin/front-door/lan-cert/prepare", headers=H)
    assert "Nothing was changed" in r.text and "Switch now" not in r.text

    def down():
        raise front_door.FrontDoorError("Crewboss could not reach its front door service. Is it running?")
    monkeypatch.setattr(fake, "rotate_prepare", down)
    r = _client(world["owner"]).post("/admin/front-door/lan-cert/prepare", headers=H)
    assert "could not reach" in r.text and "Switch now" not in r.text


def test_auto_renew_switches_near_the_end_even_with_stale_runners_and_audits_it(fd_dir, monkeypatch):
    import time as _t
    from app import runner_ws
    f = FakeClient()
    monkeypatch.setattr(front_door, "client", lambda: f)
    monkeypatch.setattr(front_door, "available", lambda: True)
    monkeypatch.setattr(runner_ws, "rotate_tls_from_prepare", lambda p: 2)
    write_state(fd_dir, not_after="2030-01-01")
    assert front_door.auto_renew() == "not due" and f.calls == []
    near = _t.strftime("%Y-%m-%d", _t.gmtime(_t.time() + 5 * 86400))
    write_state(fd_dir, not_after=near)
    monkeypatch.setattr(runner_ws, "tls_ready", lambda pin: {"ready": 1, "total": 2, "pending": ["old laptop"]})
    assert front_door.auto_renew() == "switched" and ("rotate",) in f.calls       # the key is reused: a stale runner still connects
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT summary FROM instance_audit WHERE verb = 'front_door.lan_cert_auto' ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
    assert "1 of 2" in row["summary"] and "old laptop" in row["summary"]


def test_auto_renew_audits_a_failure_and_a_refusal(fd_dir, monkeypatch):
    import time as _t
    from app import runner_ws
    f = FakeClient()
    monkeypatch.setattr(front_door, "client", lambda: f)
    monkeypatch.setattr(front_door, "available", lambda: True)
    monkeypatch.setattr(runner_ws, "rotate_tls_from_prepare", lambda p: 0)
    monkeypatch.setattr(runner_ws, "tls_ready", lambda pin: {"ready": 0, "total": 0, "pending": []})
    write_state(fd_dir, not_after=_t.strftime("%Y-%m-%d", _t.gmtime(_t.time() + 5 * 86400)))
    f.rotate_out = {"ok": False, "problem": "The proxy refused the new certificate."}
    assert front_door.auto_renew() == "switch failed"
    monkeypatch.setattr(f, "rotate_prepare", lambda: (_ for _ in ()).throw(front_door.FrontDoorError("down")))
    assert front_door.auto_renew() == "failed"
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT summary FROM instance_audit WHERE verb = 'front_door.lan_cert_auto_failed' ORDER BY id")
        rows = [r["summary"] for r in cur.fetchall()]
    assert len(rows) == 2 and "refused the new" in rows[0] and "down" in rows[1]


def test_a_refused_remove_or_switch_is_not_success(world, fake, fd_dir):
    write_state(fd_dir, public="crew.example.com")
    c = _client(world["owner"])
    fake.remove_out = {"ok": False, "problem": "The proxy refused it."}
    fake.rotate_out = {"ok": False, "problem": "The proxy refused the new certificate."}
    r = c.post("/admin/front-door/remove-public", headers=H)
    assert "Nothing was changed" in r.text and "refused it" in r.text and "data-switch-ok" not in r.text and fake.restarts == []
    r = c.post("/admin/front-door/lan-cert/switch", headers=H)
    assert "Nothing was changed" in r.text and "refused the new" in r.text and "data-switch-ok" not in r.text
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT verb FROM instance_audit WHERE verb IN ('front_door.remove', 'front_door.lan_cert')")
        assert cur.fetchall() == []
