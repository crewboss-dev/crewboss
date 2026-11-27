"""#976 slice D: the runner's pinned TLS (runner/crewboss_runner/tlspin.py, enrol.py, link.py, spawn.py)."""
import asyncio
import base64
import datetime
import hashlib
import json
import ssl
import struct
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import NameOID

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runner"))

from crewboss_runner import enrol, link, spawn, store, tlspin  # noqa: E402


def make_cert(names=("127.0.0.1", "office-pc.local")):
    """A self-signed CA:FALSE leaf, the same shape app/lan_tls.py makes. Returns (pem, der, key)."""
    import ipaddress
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "crewboss-lan")])
    sans = []
    for n in names:
        try:
            sans.append(x509.IPAddress(ipaddress.ip_address(n)))
        except ValueError:
            sans.append(x509.DNSName(n))
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=825))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False, data_encipherment=False,
                                         key_agreement=False, key_cert_sign=False, crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .sign(key, hashes.SHA256()))
    pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    return pem, cert.public_bytes(serialization.Encoding.DER), key


def key_pem(key) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


class TlsServer:
    """A real HTTPS server with a given certificate. Counts the application bytes it was sent (zero after a refused pin)."""

    def __init__(self, tmp_path, pem, key, ident_pub_b64=""):
        self.app_bytes = 0
        self.requests: list[tuple[str, str]] = []
        outer = self
        files = (tmp_path / f"c{id(self)}.pem", tmp_path / f"k{id(self)}.pem")
        files[0].write_text(pem)
        files[1].write_bytes(key_pem(key))

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def handle(self):
                try:
                    if self.rfile.peek(1):   # the client sent application data after the handshake
                        outer.app_bytes += 1
                except OSError:
                    pass
                super().handle()

            def _reply(self, doc):
                raw = json.dumps(doc).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                outer.requests.append(("GET", self.path))
                self._reply({"public_key": ident_pub_b64})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(n)
                outer.requests.append(("POST", self.path))
                self._reply({"device_id": 7, "device_token": "cbd_x", "mode": "subscription", "expires_at": "2027-01-01"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(files[0], files[1])
        self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def hostport(self):
        return f"127.0.0.1:{self.port}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class FakeSecrets:
    def __init__(self):
        self.d = {}

    def set(self, k, v):
        self.d[k] = v


class FakeConfig:
    def __init__(self):
        self.cfg = None

    def save(self, c):
        self.cfg = c


def identity():
    k = Ed25519PrivateKey.generate()
    raw = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return k, base64.b64encode(raw).decode(), enrol.fp8_of(raw)


# ---- the code -----------------------------------------------------------------------------------------------------------

def test_parse_old_and_new_codes():
    old = tlspin.parse_code("host.example:443#abcd2345-sec_ret-x")
    assert (old.hostport, old.fp8, old.pin, old.secret) == ("host.example:443", "abcd2345", None, "sec_ret-x")
    pin = "a" * 26
    new = tlspin.parse_code(f"192.0.2.20:443#abcd2345.{pin}-sec_ret-x")
    assert (new.hostport, new.fp8, new.pin, new.secret) == ("192.0.2.20:443", "abcd2345", pin, "sec_ret-x")
    assert enrol.parse_code(f"192.0.2.20:443#abcd2345.{pin}-s") == ("192.0.2.20:443", "abcd2345", "s")   # the old 3-tuple API still works


@pytest.mark.parametrize("code", ["nohash", "h:1#short-s", "h:1#abcd2345.tooshort-s", "h:1#abcd2345." + "A" * 26 + "-s", "h:1#abcd2345.", "h:1#abcd2345-",
                                  "h#abcd2345-s", "h:1#abcd2345." + "a" * 27 + "-s"])
def test_bad_codes_are_refused(code):
    with pytest.raises(enrol.EnrolError):
        enrol.parse_code(code)


def test_an_old_runner_check_fails_closed_on_a_pinned_code():
    fp = "abcd2345." + "a" * 26
    assert len(fp) != 8   # the old parse_code's `len(fp) != 8` rejects it


# ---- the pin check runs before any byte is sent --------------------------------------------------------------------------

def test_wrong_pin_sends_nothing(tmp_path):
    pem, _der, key = make_cert()
    srv = TlsServer(tmp_path, pem, key)
    try:
        _k, pub, fp8 = identity()
        with pytest.raises(enrol.EnrolError, match="not the one in the code"):
            enrol.enrol(f"{srv.hostport}#{fp8}.{'a' * 26}-secret", "pc", "linux", FakeSecrets(), FakeConfig(), state=tmp_path)
        assert srv.requests == [] and srv.app_bytes == 0   # the handshake only: no identity GET, no secret, no byte
    finally:
        srv.close()


def test_another_self_signed_certificate_is_refused(tmp_path):
    real_pem, real_der, _ = make_cert()
    other_pem, _o, other_key = make_cert()
    srv = TlsServer(tmp_path, other_pem, other_key)   # an impostor serving ITS certificate
    try:
        _k, pub, fp8 = identity()
        pin_of_real = tlspin.pin26_of(real_der)
        with pytest.raises(enrol.EnrolError):
            enrol.enrol(f"{srv.hostport}#{fp8}.{pin_of_real}-secret", "pc", "linux", FakeSecrets(), FakeConfig(), state=tmp_path)
        assert srv.requests == [] and srv.app_bytes == 0
        # and a connection after enrolment (cadata = the real certificate) refuses the impostor
        ctx = tlspin.context_for([real_pem])
        import socket
        with pytest.raises(ssl.SSLError), socket.create_connection(("127.0.0.1", srv.port), timeout=5) as raw, ctx.wrap_socket(raw, server_hostname="127.0.0.1"):
            pass
    finally:
        srv.close()


def test_a_good_pin_enrols_stores_the_certificate_and_later_requests_trust_only_it(tmp_path):
    pem, der, key = make_cert()
    idk, pub, fp8 = identity()
    srv = TlsServer(tmp_path, pem, key, ident_pub_b64=pub)
    try:
        pin = tlspin.pin26_of(der)
        sec, cfgs = FakeSecrets(), FakeConfig()
        cfg = enrol.enrol(f"{srv.hostport}#{fp8}.{pin}-secret", "pc", "linux", sec, cfgs, state=tmp_path)
        assert srv.requests == [("GET", "/runner/identity"), ("POST", "/runner/enrol")] and srv.app_bytes >= 2
        assert cfg["tls_pin"] == pin and len(cfg["tls_certs"]) == 1 and cfgs.cfg == cfg
        assert tlspin.pem_to_der(cfg["tls_certs"][0]) == der
        assert (tmp_path / "server-cert.pem").read_text().strip() == cfg["tls_certs"][0].strip()
        assert tlspin.context_from_cfg(cfg) is not None
    finally:
        srv.close()


def test_a_pinned_code_needs_https(tmp_path):
    _k, _pub, fp8 = identity()
    with pytest.raises(enrol.EnrolError, match="https"):
        enrol.enrol(f"127.0.0.1:1#{fp8}.{'a' * 26}-s", "pc", "linux", FakeSecrets(), FakeConfig(), scheme="http", state=tmp_path)


def test_an_old_format_code_enrols_with_no_pin_and_the_system_store(tmp_path):
    idk, pub, fp8 = identity()
    calls = []

    def http(method, url, body=None):
        calls.append(url)
        return (200, {"public_key": pub}) if method == "GET" else (200, {"device_id": 1, "device_token": "t", "mode": "api"})
    cfg = enrol.enrol(f"host.example:443#{fp8}-secret", "pc", "linux", FakeSecrets(), FakeConfig(), http=http, state=tmp_path)
    assert "tls_pin" not in cfg and "tls_certs" not in cfg and not (tmp_path / "server-cert.pem").exists()
    assert tlspin.context_from_cfg(cfg) is None   # no pin: the connection uses the system store


# ---- agents -------------------------------------------------------------------------------------------------------------

def _profile(tmp_path, monkeypatch, cfg):
    monkeypatch.setenv("CREWBOSS_RUNNER_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("CREWBOSS_RUNNER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("CREWBOSS_RUNNER_PROFILE", raising=False)
    store.Config(profile="default").save(cfg)
    return store.profile_state_dir("default")


def test_agent_env_has_node_extra_ca_certs_only_when_pinned(tmp_path, monkeypatch):
    pem, _d, _k = make_cert()
    state = _profile(tmp_path, monkeypatch, {"server": "h:443", "tls_pin": "a" * 26, "tls_certs": [pem]})
    store.mark_dir(state)
    tlspin.write_cert_file(state, [pem])
    env = spawn.agent_env("subscription", {"PATH": "/bin", "SSL_CERT_FILE": "/etc/x"})
    assert env["NODE_EXTRA_CA_CERTS"] == str(state / "server-cert.pem")
    assert env["SSL_CERT_FILE"] == "/etc/x"   # untouched: never replaced
    assert spawn.gemini_server("https://h", "tok", 1, str(tmp_path))["env"]["CB_SERVER_CA_FILE"] == str(state / "server-cert.pem")


def test_agent_env_without_a_pin_is_unchanged(tmp_path, monkeypatch):
    _profile(tmp_path, monkeypatch, {"server": "h:443"})
    env = spawn.agent_env("subscription", {"PATH": "/bin"})
    assert "NODE_EXTRA_CA_CERTS" not in env
    assert "CB_SERVER_CA_FILE" not in spawn.gemini_server("https://h", "tok", 1, str(tmp_path))["env"]


# ---- tls_rotate ---------------------------------------------------------------------------------------------------------

class FakeWs:
    def __init__(self):
        self.sent, self.closed = [], None

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def close(self, code=1000, reason=""):
        self.closed = code


def _link(pem, idk):
    pub = base64.b64encode(idk.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    der = tlspin.pem_to_der(pem)
    cfg = {"server": "office-pc.local:443", "scheme": "https", "device_id": 5, "server_public_key": pub, "tls_pin": tlspin.pin26_of(der), "tls_certs": [pem]}
    lk = link.Link(cfg, Ed25519PrivateKey.generate(), "tok")
    lk.persisted = 0
    lk.on_tls = lambda: setattr(lk, "persisted", lk.persisted + 1)
    return lk, der


def _sign(idk, new_der, old_der, host="office-pc.local", label=tlspin.ROTATE_LABEL):
    msg = (label + tlspin.spki_sha256(new_der) + hashlib.sha256(new_der).digest() + tlspin.spki_sha256(old_der)
           + struct.pack(">H", len(host)) + host.encode())
    return base64.b64encode(idk.sign(msg)).decode()


def _rotate(lk, ws, **frame):
    asyncio.run(lk._handle(ws, json.dumps({"type": "tls_rotate", "id": "ab12", **frame}), b"n" * 32))


def test_tls_rotate_good_signature_keeps_both_and_acks():
    idk = Ed25519PrivateKey.generate()
    old_pem, old_der, _ = make_cert()
    new_pem, new_der, _ = make_cert()
    lk, _ = _link(old_pem, idk)
    lk._proven = True
    ws = FakeWs()
    _rotate(lk, ws, cert_pem=new_pem, sig=_sign(idk, new_der, old_der))
    assert ws.sent == [{"type": "tls_rotate_result", "id": "ab12", "ok": True, "fp": tlspin.pin26_of(new_der)}]
    assert [tlspin.pem_to_der(p) for p in lk.cfg["tls_certs"]] == [old_der, new_der] and lk.persisted == 1


def test_tls_rotate_bad_signature_wrong_label_wrong_host_change_nothing():
    idk, other = Ed25519PrivateKey.generate(), Ed25519PrivateKey.generate()
    old_pem, old_der, _ = make_cert()
    new_pem, new_der, _ = make_cert()
    for sig in (_sign(other, new_der, old_der),                                             # signed by another key (a stolen TLS key, say)
                _sign(idk, new_der, old_der, label=b"crewboss-runner-ws/v1/server\0"),     # the challenge label: no cross-protocol replay
                _sign(idk, new_der, old_der, host="elsewhere.example"),                     # for another address
                _sign(idk, old_der, old_der),                                              # a signature over a different certificate
                "!!notbase64"):
        lk, _ = _link(old_pem, idk)
        lk._proven = True
        ws = FakeWs()
        _rotate(lk, ws, cert_pem=new_pem, sig=sig)
        assert ws.sent[0]["ok"] is False and "fp" not in ws.sent[0]
        assert lk.cfg["tls_certs"] == [old_pem] and lk.persisted == 0


def test_tls_rotate_for_a_certificate_that_does_not_name_this_server_is_refused():
    idk = Ed25519PrivateKey.generate()
    old_pem, old_der, _ = make_cert()
    new_pem, new_der, _ = make_cert(names=("elsewhere.example",))
    lk, _ = _link(old_pem, idk)
    lk._proven = True
    ws = FakeWs()
    _rotate(lk, ws, cert_pem=new_pem, sig=_sign(idk, new_der, old_der))
    assert ws.sent[0]["ok"] is False and lk.cfg["tls_certs"] == [old_pem]


def test_tls_rotate_before_the_server_proved_is_not_acted_on():
    idk = Ed25519PrivateKey.generate()
    old_pem, old_der, _ = make_cert()
    new_pem, new_der, _ = make_cert()
    lk, _ = _link(old_pem, idk)
    assert lk._proven is False
    ws = FakeWs()
    with pytest.raises(link.PinFailure):
        _rotate(lk, ws, cert_pem=new_pem, sig=_sign(idk, new_der, old_der))   # even a VALID signature
    assert ws.sent == [] and ws.closed == 1008 and lk.cfg["tls_certs"] == [old_pem] and lk.persisted == 0


def test_an_unpinned_runner_ignores_a_rotation():
    idk = Ed25519PrivateKey.generate()
    old_pem, old_der, _ = make_cert()
    new_pem, new_der, _ = make_cert()
    lk, _ = _link(old_pem, idk)
    lk.cfg.pop("tls_pin"), lk.cfg.pop("tls_certs")
    lk._proven = True
    ws = FakeWs()
    _rotate(lk, ws, cert_pem=new_pem, sig=_sign(idk, new_der, old_der))
    assert ws.sent[0]["ok"] is False and "tls_certs" not in lk.cfg


def test_the_servers_message_matches_the_runners():
    """app/runner_ws.py builds the bytes the runner verifies: same label, same layout."""
    from app import runner_ws
    _p, new_der, _ = make_cert()
    _q, old_der, _ = make_cert()
    assert runner_ws.tls_rotate_message(new_der, tlspin.spki_sha256(old_der), "Office-PC.local") == \
        tlspin.rotate_message(new_der, tlspin.spki_sha256(old_der), "office-pc.local")
    assert runner_ws.pin26(new_der) == tlspin.pin26_of(new_der)


def test_old_certificate_is_dropped_once_a_session_is_ready_on_the_newest():
    idk = Ed25519PrivateKey.generate()
    old_pem, old_der, _ = make_cert()
    new_pem, new_der, _ = make_cert()
    lk, _ = _link(old_pem, idk)
    lk.cfg["tls_certs"] = [old_pem, new_pem]
    lk._peer_der = staticmethod(lambda ws: new_der)
    lk._drop_old_certs(FakeWs())
    assert lk.cfg["tls_certs"] == [new_pem] and lk.cfg["tls_pin"] == tlspin.pin26_of(new_der) and lk.persisted == 1
    lk._peer_der = staticmethod(lambda ws: old_der)   # connected on the OLD one: nothing dropped
    lk.cfg["tls_certs"] = [old_pem, new_pem]
    lk._drop_old_certs(FakeWs())
    assert len(lk.cfg["tls_certs"]) == 2


# --- trust the KEY, not one exact certificate (a same-key renewal just works) ---------------------------------------------------

def _renew(key, names=("127.0.0.1", "office-pc.local")):
    import ipaddress
    now = datetime.datetime.now(datetime.timezone.utc)
    sans = [x509.IPAddress(ipaddress.ip_address(n)) if n[0].isdigit() else x509.DNSName(n) for n in names]
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "crewboss-lan")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=825))
            .add_extension(x509.SubjectAlternativeName(sans), critical=False).sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.DER)


def test_a_same_key_renewal_is_trusted_by_pin_and_stored():
    pem, der, key = make_cert()
    cfg = {"server": "127.0.0.1:443", "tls_pin": tlspin.pin26_of(der), "tls_certs": [pem]}
    assert tlspin.trust_by_pin(cfg, fetch=lambda hp: der) is False          # the stored certificate: nothing to do
    new = _renew(key)
    assert tlspin.trust_by_pin(cfg, fetch=lambda hp: new) is True
    assert cfg["tls_certs"][-1] == tlspin.der_to_pem(new) and len(cfg["tls_certs"]) == 2 and cfg["tls_pin"] == tlspin.pin26_of(der)
    assert tlspin.trust_by_pin(cfg, fetch=lambda hp: new) is False


def test_another_key_or_another_host_is_refused_before_anything_is_sent():
    pem, der, key = make_cert()
    cfg = {"server": "127.0.0.1:443", "tls_pin": tlspin.pin26_of(der), "tls_certs": [pem]}
    _p2, other, other_key = make_cert()
    with pytest.raises(tlspin.PinError, match="different key"):
        tlspin.trust_by_pin(cfg, fetch=lambda hp: other)
    with pytest.raises(tlspin.PinError, match="does not name"):
        tlspin.trust_by_pin(cfg, fetch=lambda hp: _renew(key, names=("198.51.100.9",)))
    assert cfg["tls_certs"] == [pem]
    assert tlspin.trust_by_pin({"server": "x:1"}, fetch=lambda hp: pytest.fail("an unpinned profile must not be touched")) is False


def test_the_users_own_node_extra_ca_certs_is_kept_inside_a_combined_file(tmp_path, monkeypatch):
    pem, _d, _k = make_cert()
    state = _profile(tmp_path, monkeypatch, {"server": "h:443", "tls_pin": "a" * 26, "tls_certs": [pem]})
    store.mark_dir(state)
    tlspin.write_cert_file(state, [pem])
    mine = tmp_path / "corp.pem"
    mine.write_text("-----BEGIN CERTIFICATE-----\nCORP\n-----END CERTIFICATE-----\n")
    env = spawn.agent_env("subscription", {"PATH": "/bin", "NODE_EXTRA_CA_CERTS": str(mine)})
    combined = Path(env["NODE_EXTRA_CA_CERTS"])
    assert combined != mine and combined.name == "server-cert-combined.pem"
    text = combined.read_text()
    assert "CORP" in text and pem.strip() in text and mine.read_text().count("CORP") == 1      # theirs is untouched
