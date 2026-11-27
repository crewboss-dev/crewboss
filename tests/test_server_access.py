"""Server access (task #367): per-account SSH CA, the paste line, Check access (ssh mocked), session passes over
/api/servers, and the Servers page. Accounts stay partitioned (task #368): account B's token never gets a pass for, or
even sees, account A's server; the overseer lists but never signs. The CA's private key never leaves app/ssh_ca.py.

Uses the real ssh-keygen (CA creation, signing, `ssh-keygen -L`); ssh itself is mocked via server_access._run."""
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.fernet import Fernet

from app import config, db, server_access, ssh_ca, tree, repo
from app import servers as srv
from app.auth import issue_token
from app.db import get_cursor
from app.repo import BadRequest

OWN = {"Origin": "https://crewboss.example.com", "Sec-Fetch-Site": "same-origin"}
HOST_KEY_BLOB = "AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
OTHER_KEY_BLOB = "AAAAC3NzaC1lZDI1NTE5AAAAIPpQhh2Ptg5VnR9hnQ0aTDQh5jJx4z1Bq8RgS3m3W1sX"
OS_RELEASE = 'PRETTY_NAME="Debian GNU/Linux 13 (trixie)"\nNAME="Debian GNU/Linux"\nVERSION_ID="13"\nID=debian\n'


def _project(slug, short, roles, parent=None, kind="project"):
    p = repo.create_project(slug, slug.replace("-", " ").title(), "internal", "owner", short_name=short,
                            session_roles=roles)
    if parent is not None:
        tree.set_place(p["id"], "owner", None if kind == "account" or parent["slug"] == config.OVERSEER_PROJECT else parent["id"], kind=kind)   # no project root (change #76)
    return repo.get_project(p["id"])


@pytest.fixture
def est(client, monkeypatch, tmp_path):
    with db.connect() as conn:
        conn.execute("TRUNCATE projects RESTART IDENTITY CASCADE")
        conn.commit()
    keyfile = tmp_path / "ssh-ca-enc-key"
    keyfile.write_text(Fernet.generate_key().decode())
    monkeypatch.setattr(config, "SSH_CA_KEY_FILE", str(keyfile))
    monkeypatch.setattr(config, "OVERSEER_PROJECT", "mallen-dashboard")
    # No servers.yml servers for these accounts, and never a Zabbix call.
    (tmp_path / "estate").mkdir()
    (tmp_path / "estate" / "servers.yml").write_text("defaults: {account: elsewhere}\nservers: []\n")
    monkeypatch.setattr(config, "ESTATE_DIR", str(tmp_path / "estate"))
    monkeypatch.setattr(srv, "_fetch_live", lambda h: pytest.fail("no Zabbix call"))
    dash = _project("mallen-dashboard", "dash", ["all"])
    acme = _project("acme", "acme", ["all"], dash, kind="account")
    acme_app = _project("acme-app", "acmeapp", ["all"], acme)
    beta = _project("beta", "beta", ["all"], dash, kind="account")
    loose = _project("loose", "loose", ["all"], dash)
    return {"dash": dash, "acme": acme, "acme_app": acme_app, "beta": beta, "loose": loose, "keyfile": keyfile}


def _h(project, stream="all"):
    return {"Authorization": f"Bearer {issue_token(stream, f't-{project['short_name']}', project_id=project['id'])}"}


def _pubkey(tmp_path, name="k"):
    path = tmp_path / name
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)], check=True)
    return (tmp_path / f"{name}.pub").read_text().strip()


def _ca_private(account_id, keyfile) -> bytes:
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT private_key_enc FROM ssh_cas WHERE account_id = %s", (account_id,))
        enc = cur.fetchone()["private_key_enc"]
    return Fernet(keyfile.read_text().strip().encode()).decrypt(enc.encode())


def _assert_no_ca_private(text, account_id, keyfile):
    assert "PRIVATE KEY" not in text
    for line in _ca_private(account_id, keyfile).decode().splitlines():
        if line and "PRIVATE KEY" not in line:
            assert line not in text


def _cert_info(cert_text, tmp_path) -> str:
    path = tmp_path / "c-cert.pub"
    path.write_text(cert_text + "\n")
    return subprocess.run(["ssh-keygen", "-L", "-f", str(path)], check=True, capture_output=True, text=True,
                          env={**os.environ, "TZ": "UTC"}).stdout


class FakeSsh:
    """Stands in for server_access._run: records each argv and what the known_hosts file held, answers like sshd."""

    def __init__(self, host_key_blob=HOST_KEY_BLOB, returncode=0, stderr="", sudo="yes"):
        self.host_key_blob, self.returncode, self.stderr, self.sudo = host_key_blob, returncode, stderr, sudo
        self.calls = []

    def __call__(self, argv, timeout):
        opts = dict(argv[i + 1].split("=", 1) for i, a in enumerate(argv) if a == "-o")
        kh = opts["UserKnownHostsFile"]
        before = open(kh).read()
        self.calls.append({"argv": argv, "opts": opts, "known_hosts": before})
        if opts["StrictHostKeyChecking"] == "accept-new" and not before.strip():
            with open(kh, "a") as fh:
                fh.write(f"{opts['HostKeyAlias']} ssh-ed25519 {self.host_key_blob}\n")
        elif opts["StrictHostKeyChecking"] == "yes" and self.host_key_blob not in before:
            return subprocess.CompletedProcess(argv, 255, "", "Host key verification failed.\n")
        if self.returncode:
            return subprocess.CompletedProcess(argv, self.returncode, "", self.stderr)
        out = f"@@host\nweb02\n@@os\n{OS_RELEASE}@@kernel\n6.12.0-1-amd64\n@@sudo\n{self.sudo}\n@@end\n"
        return subprocess.CompletedProcess(argv, 0, out, "")


def _allow_sessions(*ids, on=True):
    """The per-server switch (F3): AI sessions may ask for their own pass here. OFF by default."""
    with get_cursor() as cur:
        cur.execute("UPDATE commissioned_servers SET sessions_may_request_passes = %s WHERE id = ANY(%s)", (on, list(ids)))


def _commissioned(est, monkeypatch, account="acme", name="web02", sessions=True):
    row = server_access.add(est[account], name, "203.0.113.10", 22, "crewboss", "owner")
    monkeypatch.setattr(server_access, "_run", FakeSsh())
    out = server_access.check(est[account], row["id"])
    if sessions:
        _allow_sessions(out["id"])
        out = server_access.get(est[account]["id"], out["id"])
    return out


# --- validation -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["-oProxyCommand=sh", "-p", "a b", "host;rm", "$(id)", "h`x`", "web 02", "",
                                 "host:22", "user@host", "10.0.0.1/24", "a..b", "-host.example.com", "1234"])
def test_address_refuses_anything_ssh_could_read_as_an_option(bad):
    with pytest.raises(BadRequest):
        server_access.clean_address(bad)


@pytest.mark.parametrize("bad", ["-oProxyCommand=sh", "-l", "Root", "root", "a b", "a;b", "$(id)", "", "x" * 33])
def test_login_user_must_be_a_plain_posix_name_and_not_root(bad):
    with pytest.raises(BadRequest):
        server_access.clean_user(bad)


@pytest.mark.parametrize("bad", ["-x", "a b", "12", "a/b", "", "x;y"])
def test_name_is_plain_and_never_only_digits(bad):
    with pytest.raises(BadRequest):
        server_access.clean_name(bad)


def test_good_values_are_normalised():
    assert server_access.clean_address("Web02.Example.COM.") == "web02.example.com"
    assert server_access.clean_address("2001:DB8::0:1") == "2001:db8::1"
    assert server_access.clean_address("203.0.113.10") == "203.0.113.10"
    assert server_access.clean_user("crewboss") == "crewboss" and server_access.clean_port("") == 22
    with pytest.raises(BadRequest):
        server_access.clean_port("70000")


def test_ssh_argv_puts_the_address_after_double_dash():
    row = {"id": 7, "address": "203.0.113.10", "port": 2222, "login_user": "crewboss"}
    argv = server_access.ssh_argv(row, "/k", "/k-cert.pub", "/kh", True, "true")
    assert argv[-3:] == ["--", "203.0.113.10", "true"]
    assert argv[argv.index("-l") + 1] == "crewboss" and argv[argv.index("-p") + 1] == "2222"
    assert "StrictHostKeyChecking=yes" in argv and "HostKeyAlias=crewboss-srv-7" in argv
    assert "BatchMode=yes" in argv and "ForwardAgent=no" in argv


def test_only_ed25519_public_keys_are_signed(tmp_path):
    good = _pubkey(tmp_path)
    assert ssh_ca.parse_ed25519(good + "\n") == " ".join(good.split()[:2])
    for bad in ["ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQ", "ssh-ed25519 notbase64!!", "ssh-ed25519 AAAA",
                good + "\nssh-ed25519 " + good.split()[1], "-oProxyCommand=x", "", "ssh-ed25519 " + "A" * 500]:
        with pytest.raises(BadRequest):
            ssh_ca.parse_ed25519(bad)


# --- the CA, the paste line and the pass ----------------------------------------------------------------------------

def test_one_ca_per_account_and_the_paste_line(est):
    a = server_access.add(est["acme"], "web02", "203.0.113.10", 22, "crewboss", "owner")
    b = server_access.add(est["beta"], "db01", "198.51.100.5", 2222, "ops", "owner")
    ca_a, ca_b = ssh_ca.get_ca(est["acme"]["id"]), ssh_ca.get_ca(est["beta"]["id"])
    assert ca_a and ca_b and ca_a["public_key"] != ca_b["public_key"]
    assert "private_key_enc" not in ca_a
    model = server_access.page_model(est["acme"])
    card = model["servers"][0]
    kind, blob = ca_a["public_key"].split()[:2]
    assert card["line"] == f'cert-authority,principals="srv-{a["id"]}" {kind} {blob} crewboss:acme'
    assert {f["key"] for f in card["setup"]} == {"debian", "rhel", "alpine"}
    for f in card["setup"]:
        assert f"'{card['line']}'" in f["script"]                       # the exact line, single-quoted
        assert "visudo -cf /etc/sudoers.d/crewboss" in f["script"] and "chmod 600" in f["script"]
        assert "install -d -m 700 -o crewboss" in f["script"]
    assert [c["id"] for c in server_access.page_model(est["beta"])["servers"]] == [b["id"]]


def test_the_pass_opens_one_server_for_an_hour_without_agent_or_x11_forwarding(est, tmp_path):
    row = server_access.add(est["acme"], "web02", "203.0.113.10", 22, "crewboss", "owner")
    before = datetime.now(timezone.utc).replace(tzinfo=None)
    signed = ssh_ca.sign(est["acme"], row["id"], _pubkey(tmp_path), "claude:all/t-acme")
    info = _cert_info(signed["cert"], tmp_path)
    assert re.search(rf"Principals:\s*\n\s*srv-{row['id']}\s*\n\s*Critical", info)
    m = re.search(r"Valid: from (\S+) to (\S+)", info)
    start, end = (datetime.fromisoformat(x) for x in m.groups())
    assert timedelta(minutes=64) <= end - start <= timedelta(minutes=66)
    assert abs((end - before) - timedelta(hours=1)) < timedelta(minutes=2)
    exts = info.split("Extensions:")[1]
    assert "permit-pty" in exts and "permit-port-forwarding" in exts
    assert "agent-forwarding" not in exts and "X11" not in exts and "user-rc" not in exts
    assert f'Key ID: "acme:claude:all/t-acme:srv-{row["id"]}:{signed["serial"]}"' in info
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT * FROM ssh_cert_issues")
        audit = cur.fetchall()
    assert len(audit) == 1 and audit[0]["principal"] == f"srv-{row['id']}" and audit[0]["actor"] == "claude:all/t-acme"
    assert audit[0]["serial"] == signed["serial"] and audit[0]["account_id"] == est["acme"]["id"]


def test_no_key_file_means_no_signing_but_adding_still_works(est, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SSH_CA_KEY_FILE", str(tmp_path / "missing"))
    row = server_access.add(est["acme"], "web02", "203.0.113.10", 22, "crewboss", "owner")
    assert ssh_ca.get_ca(est["acme"]["id"]) is None
    model = server_access.page_model(est["acme"])
    assert model["servers"][0]["line"] == "" and "key file" in model["ca_error"]
    checked = server_access.check(est["acme"], row["id"])
    assert checked["status"] == "failed" and "key file" in checked["last_check_error"]


# --- check access (ssh mocked) --------------------------------------------------------------------------------------

def test_first_check_pins_the_host_key_and_records_the_server(est, monkeypatch):
    row = server_access.add(est["acme"], "web02", "203.0.113.10", 22, "crewboss", "owner")
    fake = FakeSsh()
    monkeypatch.setattr(server_access, "_run", fake)
    got = server_access.check(est["acme"], row["id"])
    assert got["status"] == "commissioned" and got["last_check_error"] is None and got["commissioned_at"]
    assert (got["hostname"], got["os_name"], got["os_version"], got["kernel"]) == \
        ("web02", "Debian GNU/Linux", "13", "6.12.0-1-amd64")
    assert got["sudo_nopasswd"] is True
    assert got["host_key"] == f"crewboss-srv-{row['id']} ssh-ed25519 {HOST_KEY_BLOB}"
    assert got["host_key_fingerprint"].startswith("SHA256:") and got["host_key_fingerprint"].endswith("(ED25519)")
    call = fake.calls[0]
    assert call["opts"]["StrictHostKeyChecking"] == "accept-new" and call["opts"]["BatchMode"] == "yes"
    assert call["argv"][-3:-1] == ["--", "203.0.113.10"]
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT actor, principal FROM ssh_cert_issues")
        assert cur.fetchall() == [{"actor": "dashboard-check", "principal": f"srv-{row['id']}"}]

    # Every later check uses the pinned key, strictly.
    server_access.check(est["acme"], row["id"])
    assert fake.calls[1]["opts"]["StrictHostKeyChecking"] == "yes"
    assert HOST_KEY_BLOB in fake.calls[1]["known_hosts"]


def test_a_changed_host_key_fails_loudly_and_stops_passes(client, est, monkeypatch, tmp_path):
    got = _commissioned(est, monkeypatch)
    pinned = got["host_key"]
    monkeypatch.setattr(server_access, "_run", FakeSsh(host_key_blob=OTHER_KEY_BLOB))
    bad = server_access.check(est["acme"], got["id"])
    assert bad["status"] == "failed" and bad["last_check_error"] == server_access.HOST_KEY_CHANGED
    assert "host key changed — not connecting" in bad["last_check_error"]
    assert bad["host_key"] == pinned                                     # never re-pinned by a failed check
    r = client.post(f"/api/servers/{got['id']}/ssh-cert", json={"public_key": _pubkey(tmp_path)}, headers=_h(est["acme"]))
    assert r.status_code == 409


def test_refused_and_unreachable_are_explained(est, monkeypatch):
    row = server_access.add(est["acme"], "web02", "203.0.113.10", 22, "crewboss", "owner")
    monkeypatch.setattr(server_access, "_run", FakeSsh(returncode=255, stderr="crewboss@203.0.113.10: Permission denied (publickey).\n"))
    assert "authorized_keys" in server_access.check(est["acme"], row["id"])["last_check_error"]
    monkeypatch.setattr(server_access, "_run", FakeSsh(returncode=255, stderr="ssh: connect to host 203.0.113.10 port 22: Connection timed out\n"))
    got = server_access.check(est["acme"], row["id"])
    assert got["status"] == "failed" and "can't reach 203.0.113.10:22" in got["last_check_error"]
    assert got["host_key"] is None                                       # nothing pinned without a good check

    def slow(argv, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)
    monkeypatch.setattr(server_access, "_run", slow)
    assert "no answer" in server_access.check(est["acme"], row["id"])["last_check_error"]


def test_sudo_with_a_password_is_recorded(est, monkeypatch):
    row = server_access.add(est["acme"], "web02", "203.0.113.10", 22, "crewboss", "owner")
    monkeypatch.setattr(server_access, "_run", FakeSsh(sudo="no"))
    got = server_access.check(est["acme"], row["id"])
    assert got["status"] == "commissioned" and got["sudo_nopasswd"] is False


# --- the session API ------------------------------------------------------------------------------------------------

def test_no_token_is_a_401(client, est):
    assert client.get("/api/servers").status_code == 401
    assert client.post("/api/servers/1/ssh-cert", json={"public_key": "x"}).status_code == 401


def test_a_session_gets_a_pass_for_its_own_accounts_commissioned_server(client, est, monkeypatch, tmp_path):
    got = _commissioned(est, monkeypatch)
    for who in ("acme", "acme_app"):                                     # the account session, or a project's in it
        r = client.post(f"/api/servers/{got['id']}/ssh-cert", json={"public_key": _pubkey(tmp_path, who)},
                        headers=_h(est[who]))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["principal"] == f"srv-{got['id']}" and body["known_hosts_line"] == got["host_key"]
        assert (body["address"], body["port"], body["user"]) == ("203.0.113.10", 22, "crewboss")
        assert body["host_key_alias"] == f"crewboss-srv-{got['id']}"
        assert f"srv-{got['id']}" in _cert_info(body["cert"], tmp_path)
        _assert_no_ca_private(r.text, est["acme"]["id"], est["keyfile"])
    listed = client.get("/api/servers", headers=_h(est["acme"])).json()
    assert [s["name"] for s in listed] == ["web02"] and listed[0]["status"] == "commissioned"
    assert "host_key" not in listed[0] and "known_hosts_line" not in listed[0]
    _assert_no_ca_private(str(listed), est["acme"]["id"], est["keyfile"])


def test_another_accounts_token_never_gets_or_sees_the_server(client, est, monkeypatch, tmp_path):
    got = _commissioned(est, monkeypatch)
    missing = client.post("/api/servers/9999/ssh-cert", json={"public_key": _pubkey(tmp_path)}, headers=_h(est["beta"]))
    for who in ("beta", "loose", "dash"):                                # another account, no account, the overseer
        r = client.post(f"/api/servers/{got['id']}/ssh-cert", json={"public_key": _pubkey(tmp_path, who)},
                        headers=_h(est[who]))
        assert r.status_code == 404 and r.json()["detail"].replace(str(got["id"]), "9999") == missing.json()["detail"]
    assert client.get("/api/servers", headers=_h(est["beta"])).json() == []
    assert client.get("/api/servers", headers=_h(est["loose"])).json() == []
    # The overseer READS across accounts (task #368), but never signs.
    assert [s["account"] for s in client.get("/api/servers", headers=_h(est["dash"])).json()] == ["acme"]
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT count(*) AS n FROM ssh_cert_issues WHERE actor <> 'dashboard-check'")
        assert cur.fetchone()["n"] == 0


def test_a_server_not_yet_commissioned_gets_no_pass(client, est, tmp_path):
    row = server_access.add(est["acme"], "web02", "203.0.113.10", 22, "crewboss", "owner")
    _allow_sessions(row["id"])
    r = client.post(f"/api/servers/{row['id']}/ssh-cert", json={"public_key": _pubkey(tmp_path)}, headers=_h(est["acme"]))
    assert r.status_code == 409 and "isn't commissioned" in r.json()["detail"]


def test_a_bad_public_key_is_a_400(client, est, monkeypatch):
    got = _commissioned(est, monkeypatch)
    for key in ("ssh-rsa AAAAB3NzaC1yc2E", "-oProxyCommand=sh", "ssh-ed25519 AAAA"):
        r = client.post(f"/api/servers/{got['id']}/ssh-cert", json={"public_key": key}, headers=_h(est["acme"]))
        assert r.status_code == 400, key


def test_a_role_token_is_refused(client, est):
    assert client.get("/api/servers", headers={"Authorization": f"Bearer {issue_token('scanner', 's')}"}).status_code == 403


# --- the Servers page -----------------------------------------------------------------------------------------------

def test_empty_account_offers_add_a_server(client, est):
    html = client.get("/beta/servers").text
    assert "data-servers-empty" in html and "data-add-server" in html and 'action="/beta/servers"' in html


def test_owner_adds_checks_and_removes_a_server_from_the_page(client, est, monkeypatch):
    r = client.post("/acme/servers", data={"name": "web02", "address": "203.0.113.10", "port": "22",
                                           "login_user": "crewboss"}, headers=OWN, follow_redirects=False)
    assert r.status_code == 303
    sid = int(re.search(r"added=(\d+)", r.headers["location"]).group(1))
    row = server_access.get(est["acme"]["id"], sid)
    assert row["account_id"] == est["acme"]["id"] and row["status"] == "pending" and row["created_by"] == "owner"
    html = client.get(f"/acme/servers?added={sid}").text
    assert f'data-commissioned="web02"' in html and f'principals=&#34;srv-{sid}&#34;' in html
    assert "data-paste-line" in html and 'data-setup="alpine"' in html and "data-servers-empty" not in html
    # a pending server also gets the no-sudo append command (owner, 11-15: a bare line pasted into bash does nothing)
    assert "data-append-cmd" in html and "install -d -m 700 ~/.ssh" in html and "chmod go-w ~" in html
    _assert_no_ca_private(html, est["acme"]["id"], est["keyfile"])

    monkeypatch.setattr(server_access, "_run", FakeSsh())
    r = client.post(f"/acme/servers/{sid}/check", headers=OWN, follow_redirects=False)
    assert r.status_code == 303
    html = client.get("/acme/servers").text
    assert 'data-status="commissioned"' in html and "Debian GNU/Linux 13" in html and "sudo ok" in html
    assert "data-next-steps" not in html                       # monitoring next steps are Tech Assist's now (#810)
    assert "data-actions" in html and "Check again" in html     # Check access lives inside Access and setup
    _assert_no_ca_private(html, est["acme"]["id"], est["keyfile"])

    r = client.post(f"/acme/servers/{sid}/remove", headers=OWN, follow_redirects=False)
    assert r.status_code == 303 and "authorized_keys" in r.cookies.get("crewboss_flash", "") + r.headers.get("set-cookie", "")
    assert server_access.list_for([est["acme"]["id"]]) == []


def test_the_page_never_shows_another_accounts_servers_or_acts_on_them(client, est, monkeypatch):
    got = _commissioned(est, monkeypatch, account="acme", name="acme-web")
    html = client.get("/beta/servers").text
    assert "acme-web" not in html and "data-servers-empty" in html
    assert client.post(f"/beta/servers/{got['id']}/check", headers=OWN, follow_redirects=False).status_code in (303, 404)
    assert server_access.get(est["acme"]["id"], got["id"])["last_check_at"] == got["last_check_at"]  # untouched
    client.post(f"/beta/servers/{got['id']}/remove", headers=OWN, follow_redirects=False)
    assert server_access.get(est["acme"]["id"], got["id"])                                          # still there
    assert client.get("/acme-app/servers").status_code == 404


def test_bad_input_on_the_page_is_a_400_with_the_reason(client, est):
    r = client.post("/acme/servers", data={"name": "web02", "address": "-oProxyCommand=sh", "port": "22",
                                           "login_user": "crewboss"}, headers=OWN)
    assert r.status_code == 400 and "data-add-error" in r.text and "Address" in r.text
    assert server_access.list_for([est["acme"]["id"]]) == []
    r = client.post("/acme/servers", data={"name": "web02", "address": "203.0.113.10", "port": "22",
                                           "login_user": "crewboss"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_names_are_unique_per_account(est):
    server_access.add(est["acme"], "web02", "203.0.113.10", 22, "crewboss", "owner")
    server_access.add(est["beta"], "web02", "203.0.113.11", 22, "crewboss", "owner")    # another account: fine
    with pytest.raises(repo.Conflict):
        server_access.add(est["acme"], "WEB02", "203.0.113.12", 22, "crewboss", "owner")


# --- jump host / "Connect via" (task #580) --------------------------------------------------------------------------

def _via_pair(est, monkeypatch, account="acme"):
    """core-1 (commissioned, direct) and mesh-1 (192.0.2.8, via core-1), both in the account."""
    core = _commissioned(est, monkeypatch, account, "core-1")
    mesh = server_access.add(est[account], "mesh-1", "192.0.2.8", 22, "crewboss", "owner", core["id"])
    _allow_sessions(mesh["id"])
    return core, mesh


def test_via_refusals(est, monkeypatch):
    core = _commissioned(est, monkeypatch, "acme", "core-1")
    other = _commissioned(est, monkeypatch, "beta", "betabox")
    pending = server_access.add(est["acme"], "pend", "203.0.113.20", 22, "crewboss", "owner")
    for bad, why in ((other["id"], "this account"), (pending["id"], "isn't commissioned"), (99999, "this account"),
                     ("abc", "this account")):
        with pytest.raises(BadRequest, match=why):
            server_access.add(est["acme"], "x1", "192.0.2.8", 22, "crewboss", "owner", bad)
    with pytest.raises(BadRequest, match="itself"):
        server_access.set_via(est["acme"]["id"], core["id"], core["id"])
    mesh = server_access.add(est["acme"], "mesh-1", "192.0.2.8", 22, "crewboss", "owner", core["id"])
    assert mesh["via_server_id"] == core["id"] and server_access.clean_address("192.0.2.8") == "192.0.2.8"
    # a chain: mesh-1 has a via, so nothing may use it as one; and core-1 (used as a via) can't gain one
    monkeypatch.setattr(server_access, "_run", FakeSsh())
    server_access.check(est["acme"], mesh["id"])
    with pytest.raises(BadRequest, match="chain"):
        server_access.add(est["acme"], "x2", "192.0.2.9", 22, "crewboss", "owner", mesh["id"])
    with pytest.raises(BadRequest, match="chain"):
        server_access.set_via(est["acme"]["id"], core["id"], mesh["id"])
    # removing the via server while mesh-1 uses it is refused
    with pytest.raises(server_access.Conflict, match="mesh-1"):
        server_access.remove(est["acme"]["id"], core["id"])
    # clearing the route makes it direct again
    assert server_access.set_via(est["acme"]["id"], mesh["id"], "")["via_server_id"] is None


def test_via_check_hops_through_the_via_server_strictly_with_two_principals(est, monkeypatch):
    core, mesh = _via_pair(est, monkeypatch)
    seen = {}

    class Fake(FakeSsh):
        def __call__(self, argv, timeout):
            cfg = open(argv[argv.index("-F") + 1]).read()
            seen.update(text=cfg, via_kh=open(re.search(r"UserKnownHostsFile (\S+)", cfg).group(1)).read())
            return super().__call__(argv, timeout)
    fake = Fake()
    monkeypatch.setattr(server_access, "_run", fake)
    got = server_access.check(est["acme"], mesh["id"])
    assert got["status"] == "commissioned" and got["host_key"].startswith(f"crewboss-srv-{mesh['id']} ")
    argv, opts = fake.calls[0]["argv"], fake.calls[0]["opts"]
    assert argv[argv.index("-J") + 1] == f"crewboss-srv-{core['id']}" and argv[argv.index("-F") + 1] != "/dev/null"
    assert argv[-3:-1] == ["--", "192.0.2.8"]
    assert opts["StrictHostKeyChecking"] == "accept-new" and opts["HostKeyAlias"] == f"crewboss-srv-{mesh['id']}"
    t = seen["text"]
    assert t.startswith(f"Host crewboss-srv-{core['id']}\n") and "StrictHostKeyChecking yes" in t
    assert "HostName 203.0.113.10" in t and "Port 22" in t and "User crewboss" in t
    assert f"HostKeyAlias crewboss-srv-{core['id']}" in t and "ForwardAgent no" in t
    assert seen["via_kh"].strip() == core["host_key"]
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT server_id, principal FROM ssh_cert_issues WHERE actor = 'dashboard-check' "
                    "ORDER BY id DESC LIMIT 1")
        assert cur.fetchone() == {"server_id": mesh["id"], "principal": f"srv-{mesh['id']},srv-{core['id']}"}
    server_access.check(est["acme"], mesh["id"])                         # later checks: strict on the target too
    assert fake.calls[1]["opts"]["StrictHostKeyChecking"] == "yes"


def test_the_jump_config_parses_with_openssh_and_a_via_less_check_needs_a_commissioned_via(est, monkeypatch, tmp_path):
    core = _commissioned(est, monkeypatch, "acme", "core-1")
    cfg = tmp_path / "cfg"
    cfg.write_text(server_access.jump_config(core, "/k", "/k-cert.pub", "/kh"))
    out = subprocess.run(["ssh", "-G", "-F", str(cfg), f"crewboss-srv-{core['id']}"], capture_output=True,
                         text=True, check=True).stdout
    assert "hostname 203.0.113.10" in out and "stricthostkeychecking true" in out and "user crewboss" in out
    assert "userknownhostsfile /kh" in out and "certificatefile /k-cert.pub" in out
    mesh = server_access.add(est["acme"], "mesh-1", "192.0.2.8", 22, "crewboss", "owner", core["id"])
    with get_cursor() as cur:
        cur.execute("UPDATE commissioned_servers SET status = 'failed' WHERE id = %s", (core["id"],))
    monkeypatch.setattr(server_access, "_run", lambda *a: pytest.fail("no ssh"))
    got = server_access.check(est["acme"], mesh["id"])
    assert got["status"] == "failed" and "core-1" in got["last_check_error"]


def test_direct_server_argv_and_pass_are_unchanged(est, monkeypatch, tmp_path):
    core = _commissioned(est, monkeypatch, "acme", "core-1")
    argv = server_access.ssh_argv(core, "/k", "/k-cert.pub", "/kh", True, "true")
    assert "-J" not in argv and argv[argv.index("-F") + 1] == "/dev/null"
    signed = ssh_ca.sign(est["acme"], core["id"], _pubkey(tmp_path), "t")
    assert signed["principal"] == f"srv-{core['id']}"
    assert "via" not in server_access.issue_pass(est["acme"], core["id"], _pubkey(tmp_path, "k2"), "t")
    with pytest.raises(BadRequest):
        server_access.ssh_argv(core, "/k", "/c", "/kh", True, "true", core, None)


def test_via_pass_has_both_principals_and_the_jump_details(client, est, monkeypatch, tmp_path):
    core, mesh = _via_pair(est, monkeypatch)
    monkeypatch.setattr(server_access, "_run", FakeSsh())
    server_access.check(est["acme"], mesh["id"])
    r = client.post(f"/api/servers/{mesh['id']}/ssh-cert", json={"public_key": _pubkey(tmp_path)},
                    headers=_h(est["acme"]))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["principal"] == f"srv-{mesh['id']},srv-{core['id']}"
    info = _cert_info(body["cert"], tmp_path)
    assert re.search(rf"Principals:\s*\n\s*srv-{mesh['id']}\s*\n\s*srv-{core['id']}\s*\n\s*Critical", info)
    assert body["via"] == {"id": core["id"], "name": "core-1", "address": "203.0.113.10", "port": 22,
                           "user": "crewboss", "host_key_alias": f"crewboss-srv-{core['id']}",
                           "known_hosts_line": core["host_key"]}
    listed = {s["name"]: s for s in client.get("/api/servers", headers=_h(est["acme"])).json()}
    assert listed["mesh-1"]["via"] == "core-1" and listed["core-1"]["via"] is None
    with get_cursor() as cur:                                            # via server no longer commissioned
        cur.execute("UPDATE commissioned_servers SET status = 'failed' WHERE id = %s", (core["id"],))
    r = client.post(f"/api/servers/{mesh['id']}/ssh-cert", json={"public_key": _pubkey(tmp_path, "b")},
                    headers=_h(est["acme"]))
    assert r.status_code == 409


def test_page_shows_via_and_owner_sets_it(client, est, monkeypatch):
    core = _commissioned(est, monkeypatch, "acme", "core-1")
    r = client.post("/acme/servers", data={"name": "mesh-1", "address": "192.0.2.8", "port": "22",
                                           "login_user": "crewboss", "via_server_id": str(core["id"])},
                    headers=OWN, follow_redirects=False)
    assert r.status_code == 303
    sid = int(re.search(r"added=(\d+)", r.headers["location"]).group(1))
    html = client.get(f"/acme/servers?added={sid}").text
    assert "data-via>via core-1<" in html and "data-via-note" in html and "must already trust its own line" in html
    assert f'principals=&#34;srv-{sid}&#34;' in html and "data-via-form" in html
    r = client.post(f"/acme/servers/{sid}/via", data={"via_server_id": ""}, headers=OWN, follow_redirects=False)
    assert r.status_code == 303 and server_access.get(est["acme"]["id"], sid)["via_server_id"] is None
    r = client.post(f"/acme/servers/{sid}/via", data={"via_server_id": str(sid)}, headers=OWN)
    assert r.status_code == 400 and "itself" in r.text
    other = _commissioned(est, monkeypatch, "beta", "betabox")           # another account's server as via
    r = client.post(f"/acme/servers/{sid}/via", data={"via_server_id": str(other["id"])}, headers=OWN)
    assert r.status_code == 400


def test_via_pass_audit_row_records_the_via_server_and_direct_passes_do_not(est, monkeypatch, tmp_path):
    core, mesh = _via_pair(est, monkeypatch)
    ssh_ca.sign(est["acme"], mesh["id"], _pubkey(tmp_path, "a"), "t", core["id"])
    ssh_ca.sign(est["acme"], core["id"], _pubkey(tmp_path, "b"), "t")
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT server_id, via_server_id FROM ssh_cert_issues WHERE actor = 't' ORDER BY id")
        assert cur.fetchall() == [{"server_id": mesh["id"], "via_server_id": core["id"]},
                                  {"server_id": core["id"], "via_server_id": None}]
        cur.execute("SELECT count(*) AS n FROM ssh_cert_issues WHERE actor = 't' AND (server_id = %s OR via_server_id = %s)",
                    (core["id"], core["id"]))
        assert cur.fetchone()["n"] == 2                                  # both passes opened core-1


def test_racing_set_via_cannot_form_a_cycle(est, monkeypatch):
    import threading
    x = _commissioned(est, monkeypatch, "acme", "xbox")
    y = _commissioned(est, monkeypatch, "acme", "ybox")
    for _ in range(5):
        with get_cursor() as cur:
            cur.execute("UPDATE commissioned_servers SET via_server_id = NULL WHERE id IN (%s, %s)", (x["id"], y["id"]))
        gate, results = threading.Barrier(2), []

        def go(a, b):
            gate.wait()
            try:
                server_access.set_via(est["acme"]["id"], a, b)
                results.append("ok")
            except Exception as exc:                                     # BadRequest, or the DB backstop
                results.append(type(exc).__name__)
        ts = [threading.Thread(target=go, args=(x["id"], y["id"])), threading.Thread(target=go, args=(y["id"], x["id"]))]
        [t.start() for t in ts]
        [t.join() for t in ts]
        with get_cursor(commit=False) as cur:
            cur.execute("SELECT count(*) AS n FROM commissioned_servers WHERE via_server_id IS NOT NULL "
                        "AND id IN (%s, %s)", (x["id"], y["id"]))
            assert cur.fetchone()["n"] == 1, results                     # exactly one landed, never both


def test_db_trigger_refuses_a_via_that_has_a_via_or_is_used_as_one(est, monkeypatch):
    import psycopg
    core, mesh = _via_pair(est, monkeypatch)
    third = server_access.add(est["acme"], "third", "192.0.2.9", 22, "crewboss", "owner")
    with pytest.raises(psycopg.errors.RaiseException, match="chain"):
        with get_cursor() as cur:
            cur.execute("UPDATE commissioned_servers SET via_server_id = %s WHERE id = %s", (mesh["id"], third["id"]))
    with pytest.raises(psycopg.errors.RaiseException, match="used as a via"):
        with get_cursor() as cur:
            cur.execute("UPDATE commissioned_servers SET via_server_id = %s WHERE id = %s", (third["id"], core["id"]))


def test_a_host_key_pinned_before_the_rename_is_filed_under_the_current_alias():
    from app import server_access as sa
    row = {"id": 7, "host_key": "mallen-dash-srv-7 ssh-ed25519 AAAAkey\nmallen-dash-srv-7 ssh-rsa BBBBkey"}
    assert sa.alias(7) == "crewboss-srv-7"
    assert sa.known_hosts(row) == "crewboss-srv-7 ssh-ed25519 AAAAkey\ncrewboss-srv-7 ssh-rsa BBBBkey"
    assert sa.known_hosts({"id": 7, "host_key": "crewboss-srv-7 ssh-ed25519 AAAAkey"}) == "crewboss-srv-7 ssh-ed25519 AAAAkey"
