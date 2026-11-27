"""The front-door sidecar (#976 D4-D6): golden Caddyfiles, injection refusal, the ACME log to plain-words mapping, the apply /
rollback state machine against a fake Caddy /load, the checks, the HTTP API and its token, init and renewal. No docker, no network."""
import datetime
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from app import frontdoor_agent as fa
from app import lan_tls

GOLDEN = Path(__file__).parent / "golden" / "frontdoor"
LAN = {"hosts": ["192.0.2.20", "office-pc.local"], "https_port": 443, "http_port": 80, "cert_sha256": "AA", "spki_pin26": "x", "not_after": "2029-01-01T00:00:00Z"}
STAGING = "https://acme-staging.example/directory"


def golden(name, text):
    f = GOLDEN / name
    if not f.exists():
        f.write_text(text)
    assert text == f.read_text(), f"{name} changed: delete the golden file to regenerate after review"


def test_golden_lan():
    golden("lan.Caddyfile", fa.render_caddyfile({"lan": LAN, "public": None}))


def test_golden_lan_other_port():
    golden("lan-8443.Caddyfile", fa.render_caddyfile({"lan": dict(LAN, https_port=8443, hosts=["192.0.2.20"]), "public": None}))


def test_golden_lan_plus_public():
    pub = {"domain": "crew.example.com", "port": 443, "email": "me@example.com"}
    golden("lan-public.Caddyfile", fa.render_caddyfile({"lan": LAN, "public": pub}))


def test_golden_public_only():
    golden("public.Caddyfile", fa.render_caddyfile({"lan": None, "public": {"domain": "crew.example.com", "port": 443, "email": ""}}))


def test_golden_dry_run_http_only_and_alpn_only():
    st = {"lan": LAN, "public": None}
    golden("dry-http.Caddyfile", fa.render_caddyfile(st, {"domain": "crew.example.com", "disable": "tlsalpn", "dir": STAGING}))
    golden("dry-alpn.Caddyfile", fa.render_caddyfile(st, {"domain": "crew.example.com", "disable": "http", "dir": STAGING}))


def test_lan_never_gets_hsts_and_public_does():
    text = fa.render_caddyfile({"lan": LAN, "public": {"domain": "crew.example.com", "port": 443, "email": ""}})
    lan_part, public_part = text.split("crew.example.com {")
    assert "Strict-Transport" not in lan_part and 'max-age=31536000"' in public_part and "includeSubDomains" not in text


def test_admin_socket_log_and_front_door_headers_stripped():
    text = fa.render_caddyfile({"lan": LAN, "public": None})
    assert "admin unix//run/caddy-admin/caddy.sock|0666" in text and "default_sni 192.0.2.20" in text
    assert text.count("request_header -X-Crewboss-Front-Door") == 1 and "request_header -X-MDD-Front-Door" in text
    assert "format json" in text and "mode 0644" in text


@pytest.mark.parametrize("bad", ["a.com\nroot /etc", "a.com {", "a.com }\nfile_server", "evil.com tls internal", "a b.com", "x", "192.0.2.5", "a.com;", ""])
def test_domain_injection_refused(bad):
    with pytest.raises(fa.Refused):
        fa.render_caddyfile({"lan": None, "public": {"domain": bad, "port": 443, "email": ""}})
    with pytest.raises(fa.Refused):
        fa.render_caddyfile({"lan": LAN, "public": None}, {"domain": bad, "disable": "http", "dir": STAGING})


@pytest.mark.parametrize("bad", ["a@b.com\nfile_server", "a@b.com {", "no at sign", "a b@c.com"])
def test_email_injection_refused(bad):
    with pytest.raises(fa.Refused):
        fa.render_caddyfile({"lan": None, "public": {"domain": "crew.example.com", "port": 443, "email": bad}})


def test_lan_host_injection_refused():
    with pytest.raises(fa.Refused):
        fa.render_caddyfile({"lan": dict(LAN, hosts=["1.2.3.4\nfile_server"]), "public": None})


def test_pebble_test_env_points_production_block_at_the_test_acme():
    text = fa.render_caddyfile({"lan": None, "public": {"domain": "crew.example.com", "port": 443, "email": ""}},
                               acme={"dir": "https://pebble:14000/dir", "root": "/frontdoor/pebble-roots.pem"})
    assert "dir https://pebble:14000/dir" in text and "trusted_roots /frontdoor/pebble-roots.pem" in text


# --- the ACME log -> plain words ---------------------------------------------------------------------------------------------

def _log(tmp_path, *recs):
    f = tmp_path / "caddy.log"
    f.write_text("".join(json.dumps(r) + "\n" for r in recs))
    return f


def _fail(kind, detail="x"):
    return {"msg": "challenge failed", "identifier": "crew.example.com", "challenge_type": "http-01",
            "problem": {"type": f"urn:ietf:params:acme:error:{kind}", "detail": detail}}


@pytest.mark.parametrize("kind,words", [("connection", "could not connect"), ("dns", "cannot find"), ("unauthorized", "not this computer"),
                                       ("rateLimited", "wait")])
def test_acme_problem_becomes_a_plain_sentence(tmp_path, kind, words):
    ok, why = fa.watch_log(_log(tmp_path, _fail(kind)), 0, "crew.example.com", "http-01", 80, timeout=2, sleep=lambda s: None)
    assert ok is False and words in why
    for jargon in ("ACME", "challenge", "HTTP-01", "TLS-ALPN", "urn:"):
        assert jargon not in why


def test_success_line_and_other_names_ignored(tmp_path):
    f = _log(tmp_path, {"msg": "challenge failed", "identifier": "other.example.com", "problem": {"type": "x"}},
             {"msg": "certificate obtained successfully", "identifier": "crew.example.com"})
    assert fa.watch_log(f, 0, "crew.example.com", "", 80, timeout=2, sleep=lambda s: None) == (True, "")


def test_log_timeout_is_a_plain_sentence(tmp_path):
    t = iter(range(0, 1000, 10))
    ok, why = fa.watch_log(tmp_path / "none.log", 0, "crew.example.com", "", 80, timeout=30, sleep=lambda s: None, clock=lambda: next(t))
    assert ok is False and "took too long" in why


def test_stale_when_caddy_never_starts(tmp_path):
    t = iter(range(0, 1000, 3))
    r = fa.watch_log(tmp_path / "none.log", 0, "crew.example.com", "", 80, timeout=90, sleep=lambda s: None, clock=lambda: next(t), stale_after=6)
    assert r[0] is None


# --- the agent: apply, rollback, checks ------------------------------------------------------------------------------------

class FakeCaddy:
    def __init__(self, fail_on=None):
        self.loads, self.fail_on = [], fail_on

    def __call__(self, text):
        self.loads.append(text)
        if self.fail_on and self.fail_on in text:
            return False, "boom"
        return True, ""


def make_agent(tmp_path, caddy=None, **kw):
    base = tmp_path / "fd"
    fa.init(base, ["192.0.2.20"], "T")
    (base / "logs").mkdir(exist_ok=True)
    caddy = caddy or FakeCaddy()
    kw.setdefault("resolver", lambda d: ["8.8.4.4"])
    kw.setdefault("tls_probe", lambda *a, **k: True)
    kw.setdefault("health", lambda: True)
    a = fa.Agent(base, "t" * 40, loader=caddy, sleep=lambda s: None, background=False, log_wait=1, **kw)
    return a, caddy


def script_log(agent, monkeypatch, outcomes):
    """watch_log answers from a list instead of reading a file."""
    it = iter(outcomes)
    monkeypatch.setattr(fa, "watch_log", lambda *a, **k: next(it))


def passed_check(agent, monkeypatch, domain="crew.example.com", p80=(True, ""), p443=(True, "")):
    script_log(agent, monkeypatch, [p80, p443])
    cid = agent.start_check(domain)["check_id"]
    return cid, agent.get_check(cid)


def test_init_writes_the_files_with_modes(tmp_path):
    a, _ = make_agent(tmp_path)
    base = a.base
    assert (base / "lan.key").stat().st_mode & 0o777 == 0o600
    st = json.loads((base / "front_door.json").read_text())
    assert st["version"] == 1 and st["lan"]["hosts"] == ["192.0.2.20"] and st["public"] is None
    assert set(st["lan"]) >= {"hosts", "https_port", "cert_sha256", "spki_pin26", "not_after"}
    assert (base / "Caddyfile").read_text().startswith(fa.MARKER) and a.managed()


def test_reissue_keeps_key_and_public(tmp_path):
    a, _ = make_agent(tmp_path)
    key1 = (a.base / "lan.key").read_bytes()
    a._commit(dict(a.state(), public={"domain": "crew.example.com", "port": 443, "email": "", "since": "x"}))
    fa.init(a.base, ["192.0.2.30"], "T", reissue=True)
    assert (a.base / "lan.key").read_bytes() == key1
    assert a.state()["lan"]["hosts"] == ["192.0.2.30"] and a.state()["public"]["domain"] == "crew.example.com"


def test_check_passes_and_switch_commits_caddyfile_then_state(tmp_path, monkeypatch):
    a, caddy = make_agent(tmp_path)
    cid, res = passed_check(a, monkeypatch)
    assert res["done"] and res["dns"]["ok"] and res["port80"]["ok"] and res["port443"]["ok"]
    assert "crew.example.com" in caddy.loads[0] and "disable_tlsalpn_challenge" in caddy.loads[0]
    assert "disable_http_challenge" in caddy.loads[1]
    assert "crew.example.com" not in caddy.loads[-1]                     # the active (LAN-only) config is restored after the dry runs
    script_log(a, monkeypatch, [(True, "")])
    assert a.switch("crew.example.com", "me@example.com", cid, False) == {"ok": True}
    assert "crew.example.com {" in (a.base / "Caddyfile").read_text()
    assert a.state()["public"]["domain"] == "crew.example.com" and (a.base / "Caddyfile.prev").is_file() and (a.base / "front_door.json.prev").is_file()


def test_switch_failure_changes_nothing_and_reloads_the_active_config(tmp_path, monkeypatch):
    a, caddy = make_agent(tmp_path)
    cid, _ = passed_check(a, monkeypatch)
    before = (a.base / "Caddyfile").read_text(), (a.base / "front_door.json").read_text()
    script_log(a, monkeypatch, [(False, "Let's Encrypt could not connect.")])
    res = a.switch("crew.example.com", None, cid, False)
    assert res["ok"] is False and "Nothing was changed" in res["problem"]
    assert (a.base / "Caddyfile").read_text(), (a.base / "front_door.json").read_text() == before
    assert caddy.loads[-1] == before[0]


def test_caddy_refusing_the_config_rolls_back(tmp_path, monkeypatch):
    a, caddy = make_agent(tmp_path)
    cid, _ = passed_check(a, monkeypatch)
    caddy.fail_on = "Strict-Transport-Security"        # only the real public block has it
    res = a.switch("crew.example.com", None, cid, False)
    assert res["ok"] is False and a.state()["public"] is None


def test_unserved_certificate_rolls_back(tmp_path, monkeypatch):
    a, _ = make_agent(tmp_path, tls_probe=lambda *x, **k: False)
    cid, _ = passed_check(a, monkeypatch)
    script_log(a, monkeypatch, [(True, "")])
    assert a.switch("crew.example.com", None, cid, False)["ok"] is False and a.state()["public"] is None


def test_app_not_coming_back_restores_the_previous_files(tmp_path, monkeypatch):
    t = [0.0]
    a, caddy = make_agent(tmp_path, health=lambda: False, now=lambda: t.__setitem__(0, t[0] + 50) or t[0])
    cid, _ = passed_check(a, monkeypatch)
    script_log(a, monkeypatch, [(True, "")])
    a.switch("crew.example.com", None, cid, False)
    assert a.state()["public"] is None and "crew.example.com" not in (a.base / "Caddyfile").read_text()
    assert "previous settings were restored" in a.last_result["problem"]


def test_switch_needs_a_passed_fresh_matching_check(tmp_path, monkeypatch):
    a, _ = make_agent(tmp_path)
    cid, _ = passed_check(a, monkeypatch)
    with pytest.raises(fa.Refused):
        a.switch("other.example.com", None, cid, False)
    with pytest.raises(fa.Refused):
        a.switch("crew.example.com", None, "0" * 16, False)
    a.now = lambda: __import__("time").time() + 16 * 60
    with pytest.raises(fa.Refused):
        a.switch("crew.example.com", None, cid, False)


def test_failed_port_80_blocks_switch_and_443_can_be_waived(tmp_path, monkeypatch):
    a, _ = make_agent(tmp_path)
    cid, res = passed_check(a, monkeypatch, p80=(False, "nope"))
    assert res["port80"]["ok"] is False and res["port443"]["ok"] is False
    with pytest.raises(fa.Refused):
        a.switch("crew.example.com", None, cid, True)
    a2, _ = make_agent(tmp_path / "b")
    cid, res = passed_check(a2, monkeypatch, p443=(False, "closed"))
    with pytest.raises(fa.Refused):
        a2.switch("crew.example.com", None, cid, False)
    script_log(a2, monkeypatch, [(True, "")])
    assert a2.switch("crew.example.com", None, cid, True)["ok"]


def test_dns_problems(tmp_path):
    a, _ = make_agent(tmp_path, resolver=lambda d: [])
    cid = a.start_check("crew.example.com")["check_id"]
    assert a.get_check(cid)["dns"]["ok"] is False and a.get_check(cid)["done"]
    a, _ = make_agent(tmp_path / "p", resolver=lambda d: ["fd00::1", "::1"])
    cid = a.start_check("crew.example.com")["check_id"]
    assert "inside your own network" in a.get_check(cid)["dns"]["problem"]


def test_busy_and_unmanaged_and_bad_domain(tmp_path):
    a, _ = make_agent(tmp_path)
    with pytest.raises(fa.Refused):
        a.start_check("not a domain")
    a.lock.acquire()
    with pytest.raises(fa.Refused, match="busy"):
        a.start_check("crew.example.com")
    a.lock.release()
    (a.base / "Caddyfile").write_text("example.com {\n}\n")           # hand-edited: no marker
    assert a.status()["managed"] is False
    with pytest.raises(fa.Refused):
        a.start_check("crew.example.com")


def test_remove_public(tmp_path):
    a, caddy = make_agent(tmp_path)
    a._commit(dict(a.state(), public={"domain": "crew.example.com", "port": 443, "email": "", "since": "x"}))
    assert a.remove_public() == {"ok": True} and a.state()["public"] is None
    assert "crew.example.com" not in (a.base / "Caddyfile").read_text()


def test_lan_rotation_prepare_then_switch_keeps_the_key(tmp_path):
    a, caddy = make_agent(tmp_path)
    old_fp = a.state()["lan"]["cert_sha256"]
    prep = a.lan_prepare()
    sm = prep["sig_material"]
    assert set(sm) == {"new_spki_sha256", "new_cert_sha256", "old_spki_sha256", "host"} and sm["new_spki_sha256"] == sm["old_spki_sha256"]
    assert a.status()["rotation"]["next_fp"]
    assert a.lan_switch() == {"ok": True}
    assert a.state()["lan"]["cert_sha256"] != old_fp and not (a.base / "lan-next.crt").exists()
    assert a.status()["rotation"]["next_fp"] is None


def test_renew_if_due(tmp_path):
    a, _ = make_agent(tmp_path)
    assert a.renew_if_due() == "not due"
    now = datetime.datetime.now(datetime.timezone.utc)
    crt, key = lan_tls.generate(["192.0.2.20"], key=lan_tls.load_key((a.base / "lan.key").read_bytes()), now=now - datetime.timedelta(days=780))
    (a.base / "lan.crt").write_bytes(crt)
    assert a.renew_if_due() == "prepared" and a.renew_if_due() == "waiting"
    crt, _ = lan_tls.generate(["192.0.2.20"], key=lan_tls.load_key((a.base / "lan.key").read_bytes()), now=now - datetime.timedelta(days=800))
    (a.base / "lan.crt").write_bytes(crt)
    assert a.renew_if_due() == "waiting"                 # the sidecar never switches by itself: the app and the owner do
    assert (a.base / "lan-next.crt").is_file()


# --- the HTTP API and its token ---------------------------------------------------------------------------------------------

@pytest.fixture()
def server(tmp_path, monkeypatch):
    a, caddy = make_agent(tmp_path)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), fa.make_handler(a))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield a, f"http://127.0.0.1:{srv.server_address[1]}", monkeypatch
    srv.shutdown()


def call(url, path, token="t" * 40, data=None, method=None):
    req = urllib.request.Request(url + path, data=json.dumps(data).encode() if data is not None else None, method=method or ("POST" if data is not None else "GET"))
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_token_required_everywhere(server):
    a, url, _ = server
    for path, data in (("/status", None), ("/check", {"domain": "crew.example.com"}), ("/remove-public", {}), ("/lan-cert/switch", {})):
        assert call(url, path, token=None, data=data)[0] == 401
        assert call(url, path, token="wrong" * 10, data=data)[0] == 401
    assert call(url, "/status")[0] == 200


def test_status_shape_for_the_app(server):
    _, url, _ = server
    code, body = call(url, "/status")
    assert code == 200 and set(body) >= {"lan", "public", "busy", "last_result", "rotation"} and set(body["rotation"]) >= {"next_fp", "acks"}


def test_check_api_shape_errors_and_busy(server):
    a, url, mp = server
    script_log(a, mp, [(True, ""), (True, "")])
    code, body = call(url, "/check", data={"domain": "crew.example.com"})
    assert code == 200
    code, res = call(url, f"/check/{body['check_id']}")
    assert res["done"] is True and {"dns", "port80", "port443"} <= set(res)
    assert call(url, "/check", data={"domain": "bad domain {"}) [0] == 400
    assert "problem" in call(url, "/check", data={"domain": "bad domain {"})[1]
    a.lock.acquire()
    assert call(url, "/check", data={"domain": "crew.example.com"})[0] == 409
    a.lock.release()
    assert call(url, "/check/" + "0" * 16)[0] == 404
    assert call(url, "/nope")[0] == 404


def test_switch_api_refuses_without_check(server):
    _, url, _ = server
    code, body = call(url, "/switch", data={"domain": "crew.example.com", "check_id": "0" * 16, "allow_no_443": False})
    assert code == 400 and "problem" in body


def test_no_endpoint_takes_config_text(server):
    """Unknown fields are ignored; the Caddyfile only ever comes from render_caddyfile."""
    a, url, _ = server
    before = (a.base / "Caddyfile").read_text()
    call(url, "/switch", data={"domain": "crew.example.com", "check_id": "0" * 16, "caddyfile": "evil.com {\n file_server\n}"})
    assert (a.base / "Caddyfile").read_text() == before


def test_body_limit(server):
    _, url, _ = server
    req = urllib.request.Request(url + "/check", data=b"x" * 5000, method="POST", headers={"Authorization": "Bearer " + "t" * 40, "Content-Length": "5000"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 400
