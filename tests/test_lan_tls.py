"""The LAN certificate (#976 D2): shape, SANs, 825 days, key reuse on renewal, fingerprints, and that Python's ssl accepts it as its own
trust anchor (spike S0 finding 7)."""
import datetime
import socket
import ssl
import threading

import pytest
from cryptography import x509
from cryptography.x509.oid import ExtendedKeyUsageOID

from app import lan_tls

HOSTS = ["192.0.2.20", "office-pc.local"]


def make(hosts=HOSTS, **kw):
    crt, key = lan_tls.generate(hosts, **kw)
    return lan_tls.load_cert(crt), crt, key


def test_extensions_and_lifetime():
    c, _, _ = make()
    bc = c.extensions.get_extension_for_class(x509.BasicConstraints)
    assert bc.critical and bc.value.ca is False
    ku = c.extensions.get_extension_for_class(x509.KeyUsage)
    assert ku.critical and ku.value.digital_signature and not ku.value.key_cert_sign
    assert ExtendedKeyUsageOID.SERVER_AUTH in c.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    assert (c.not_valid_after_utc - c.not_valid_before_utc).days in (825, 826)
    assert c.issuer == c.subject
    assert c.public_key().curve.name == "secp256r1"


def test_san_has_ip_and_dns():
    c, _, _ = make()
    san = c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert [str(i) for i in san.get_values_for_type(x509.IPAddress)] == ["192.0.2.20"]
    assert san.get_values_for_type(x509.DNSName) == ["office-pc.local"]


@pytest.mark.parametrize("bad", ["", "a b", "x\nhttps://evil", "a{b}", "x;y", "-bad.example", "a..b", " 192.0.2.1", "fe80::1%eth0", "fe80::1%x\n}", "2001:db8::1", "::1"])
def test_bad_hosts_refused(bad):
    assert not lan_tls.valid_lan_host(bad)
    with pytest.raises(ValueError):
        lan_tls.generate([bad])


def test_renewal_keeps_the_key_and_the_pin_but_not_the_fingerprint():
    c1, crt1, key = make()
    crt2 = lan_tls.renew(crt1, key)
    c2 = lan_tls.load_cert(crt2)
    assert lan_tls.spki_sha256(c1) == lan_tls.spki_sha256(c2)
    assert lan_tls.pin26(c1) == lan_tls.pin26(c2)
    assert lan_tls.fingerprint(c1) != lan_tls.fingerprint(c2)
    assert lan_tls.hosts_of(c2) == lan_tls.hosts_of(c1)


def test_fingerprint_and_pin_formats():
    c, _, _ = make()
    fp = lan_tls.fingerprint(c)
    assert len(fp) == 95 and fp.count(":") == 31 and fp == fp.upper()
    pin = lan_tls.pin26(c)
    assert len(pin) == 26 and pin == pin.lower() and set(pin) <= set("abcdefghijklmnopqrstuvwxyz234567")


def test_days_left_and_due():
    now = datetime.datetime.now(datetime.timezone.utc)
    c, _, _ = make(now=now - datetime.timedelta(days=800))
    assert 20 < lan_tls.days_left(c, now) < 30
    assert lan_tls.days_left(c, now) <= lan_tls.RENEW_DAYS


def _serve(crt, key, tmp_path):
    (tmp_path / "c.pem").write_bytes(crt)
    (tmp_path / "k.pem").write_bytes(key)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(tmp_path / "c.pem", tmp_path / "k.pem")
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)

    def run():
        while True:
            try:
                s, _ = srv.accept()
                try:
                    ctx.wrap_socket(s, server_side=True).recv(1)
                except OSError:
                    pass
            except OSError:
                return
    threading.Thread(target=run, daemon=True).start()
    return srv


def _connect(port, pem, host):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(cadata=pem.decode())
    with socket.create_connection(("127.0.0.1", port)) as s, ctx.wrap_socket(s, server_hostname=host):
        return True


def test_python_ssl_trusts_the_leaf_as_its_own_anchor_and_refuses_others(tmp_path):
    crt, key = lan_tls.generate(["127.0.0.1", "office-pc.local"])
    other, _ = lan_tls.generate(["127.0.0.1"])
    srv = _serve(crt, key, tmp_path)
    port = srv.getsockname()[1]
    try:
        assert _connect(port, crt, "127.0.0.1") and _connect(port, crt, "office-pc.local")
        with pytest.raises(ssl.SSLCertVerificationError):
            _connect(port, crt, "other.local")                 # hostname must be in the SAN
        with pytest.raises(ssl.SSLCertVerificationError):
            _connect(port, other, "127.0.0.1")                 # another self-signed leaf is not trusted
    finally:
        srv.close()
