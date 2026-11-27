"""The `crewboss` install CLI (task #707 slice A): argument parsing, port pick, refusal rules, env file, never touching another
project; the generic compose; the public-certificate check (app/tls_check.py) with a mocked socket. No docker is started."""
import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
_loader = importlib.machinery.SourceFileLoader("crewboss_cli", str(ROOT / "crewboss"))
_spec = importlib.util.spec_from_loader("crewboss_cli", _loader)
cb = importlib.util.module_from_spec(_spec)
_loader.exec_module(cb)


def parse(*argv):
    return cb.build_parser().parse_args(list(argv))


# --- arguments ---------------------------------------------------------------------------------------------------------------

def test_script_is_executable_stdlib_with_main_guard():
    src = (ROOT / "crewboss").read_text()
    assert os.access(ROOT / "crewboss", os.X_OK)
    assert src.startswith("#!/usr/bin/env python3")
    assert 'if __name__ == "__main__":' in src
    imported = set(re.findall(r"^(?:import|from) (\w+)", src, re.M))
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}


def test_install_defaults_and_flags():
    a = parse("install")
    assert (a.project, a.home, a.port, a.mode, a.yes, a.skip_dns_check) == ("crewboss", "~/.crewboss", None, None, False, False)
    a = parse("install", "--mode", "network", "--domain", "x.example.com", "--proxy", "bundled", "--port", "9000",
              "--project", "p2", "--home", "/tmp/h", "--yes", "--skip-checks", "--skip-dns-check")
    assert (a.mode, a.domain, a.proxy, a.port, a.project, a.home) == ("network", "x.example.com", "bundled", 9000, "p2", "/tmp/h")
    assert a.yes and a.skip_checks and a.skip_dns_check


def test_bad_choices_rejected():
    with pytest.raises(SystemExit):
        parse("install", "--mode", "cloud")
    with pytest.raises(SystemExit):
        parse("down", "--bogus")


def test_down_delete_data_needs_flag_and_name(tmp_path, monkeypatch):
    home = _installed(tmp_path)
    monkeypatch.setattr(cb, "run", lambda *a, **k: pytest.fail("must not run docker"))
    with pytest.raises(cb.Fail):
        cb.cmd_down(parse("down", "--home", str(home), "--delete-data", "--confirm-project", "wrong"))
    calls = []
    monkeypatch.setattr(cb, "run", lambda cmd, **k: calls.append(cmd))
    cb.cmd_down(parse("down", "--home", str(home)))
    assert "-v" not in calls[0] and calls[0][-1] == "down"
    cb.cmd_down(parse("down", "--home", str(home), "--delete-data", "--confirm-project", "mine"))
    assert calls[1][-2:] == ["down", "-v"]


# --- ports ------------------------------------------------------------------------------------------------------------------

def test_pick_port_next_free():
    taken = {8931, 8932, 8933}
    assert cb.pick_port(8931, is_free=lambda p: p not in taken) == 8934
    assert cb.pick_port(9000, is_free=lambda p: True) == 9000


def test_pick_port_real_socket():
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        busy = s.getsockname()[1]
        assert not cb.port_is_free(busy)
        assert cb.pick_port(busy) != busy


# --- domain -----------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name,ok", [("crewboss.example.com", True), ("a.b.duckdns.org", True), ("localhost", False),
                                     ("10.0.0.5", False), ("foo", False), ("a_b.example.com", False), ("x.localhost", False)])
def test_valid_domain(name, ok):
    assert cb.valid_domain(name) is ok


def test_domain_findings():
    assert cb.domain_findings("x.example.com", local={"1.2.3.4"}, resolver=lambda d: {"1.2.3.4"}) == ([], [])
    errs, warns = cb.domain_findings("x.example.com", local={"1.2.3.4"}, resolver=lambda d: {"9.9.9.9"})
    assert not errs and "9.9.9.9" in warns[0]
    assert cb.domain_findings("x.example.com", local={"1.2.3.4"}, resolver=lambda d: {"9.9.9.9"}, extra=["9.9.9.9"]) == ([], [])

    def boom(d):
        raise OSError("no")
    errs, _ = cb.domain_findings("x.example.com", local=set(), resolver=boom)
    assert "does not resolve" in errs[0]


def test_network_without_domain_prints_duckdns(monkeypatch):
    with pytest.raises(cb.Fail) as e:
        cb.collect_answers(parse("install", "--mode", "network", "--yes"))
    assert "duckdns.org" in e.value.message and e.value.code == 2
    with pytest.raises(cb.Fail):
        cb.collect_answers(parse("install", "--mode", "network", "--yes", "--domain", "10.1.1.1"))


def test_plan_proxy_choice():
    p = cb.collect_answers(parse("install", "--mode", "network", "--yes", "--domain", "x.example.com"))
    assert p == {"mode": "network", "domain": "x.example.com", "proxy": "caddy"}
    p = cb.collect_answers(parse("install", "--mode", "network", "--yes", "--domain", "x.example.com", "--proxy", "existing"))
    assert p["proxy"] == "existing"
    assert cb.collect_answers(parse("install", "--yes"))["proxy"] == "none"


# --- env file, instance, proxy files ---------------------------------------------------------------------------------------

def test_env_file_contents_and_roundtrip(tmp_path):
    home = tmp_path / "h"
    text = cb.render_env(project="mine", port=8950, home=home, password="pw123", proxy="caddy", mode="network")
    env = cb.parse_env(text)
    assert env["COMPOSE_PROJECT_NAME"] == "mine" and env["APP_PORT"] == "8950"
    assert env["CREWBOSS_DB_VOLUME"] == "mine_db-data"
    assert env["CREWBOSS_HOME"] == str(home) and env["CREWBOSS_SECRETS_DIR"] == str(home / "secrets")
    assert env["POSTGRES_PASSWORD"] == "pw123" and env["CREWBOSS_HTTP_PORT"] == "80"
    assert "CREWBOSS_HTTP_PORT" not in cb.parse_env(cb.render_env(project="m", port=1, home=home, password="x", proxy="none", mode="localhost"))
    spaced = cb.parse_env(cb.render_env(project="m", port=1, home=Path("/a b"), password="x", proxy="none", mode="localhost"))
    assert spaced["CREWBOSS_HOME"] == "/a b"


def test_unsafe_home_path_refused():
    for bad in ("/a:b", "/a$b", "/a'b", "/a\nb"):
        with pytest.raises(cb.Fail):
            cb.check_path(Path(bad))
    cb.check_path(Path("/home/me/.crewboss"))


def test_instance_yaml_is_valid_and_has_the_keys():
    text = cb.render_instance(name="My: Crew", public_url="https://x.example.com", domain="x.example.com", port=8931,
                              tls_check="enforce", tls_connect="caddy:443")
    d = yaml.safe_load(text)
    assert d["name"] == "My: Crew" and d["auth"] == "local" and d["tls_check"] == "enforce"
    assert "x.example.com" in d["origin_hosts"] and "127.0.0.1:8931" in d["origin_hosts"]
    assert d["tls_check_connect"] == "caddy:443" and d["mcp_allowed_hosts"] == ["x.example.com"]
    local = yaml.safe_load(cb.render_instance(name="C", public_url="http://localhost:8931", domain=None, port=8931,
                                              tls_check=None, tls_connect=None))
    assert "tls_check" not in local and local["public_url"] == "http://localhost:8931"


def test_caddyfile_and_snippets_strip_front_door_headers():
    cf = cb.render_caddyfile("x.example.com", "me@example.com")
    assert "x.example.com {" in cf and "reverse_proxy app:8000" in cf and "email me@example.com" in cf
    assert "request_header -X-Crewboss-Front-Door" in cf and "request_header -X-MDD-Front-Door" in cf
    assert "Strict-Transport-Security" in cf
    assert "email" not in cb.render_caddyfile("x.example.com")
    s = cb.render_snippets("x.example.com", 8931)
    assert "reverse_proxy 127.0.0.1:8931" in s["caddy.txt"] and "request_header -X-MDD-Front-Door" in s["caddy.txt"]
    assert 'proxy_set_header X-Crewboss-Front-Door "";' in s["nginx.txt"] and 'proxy_set_header X-MDD-Front-Door "";' in s["nginx.txt"]
    assert "proxy_pass http://127.0.0.1:8931" in s["nginx.txt"] and "$connection_upgrade" in s["nginx.txt"]


# --- refusal rules ----------------------------------------------------------------------------------------------------------

@pytest.fixture
def docker_idle(monkeypatch):
    """No project exists; records every docker call."""
    calls = []

    def fake(cmd, **k):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1 if cmd[:3] == ["docker", "volume", "inspect"] else 0, "", "")
    monkeypatch.setattr(cb, "run", fake)
    return calls


def test_no_force_flag_exists():
    with pytest.raises(SystemExit):
        parse("install", "--force")


def test_refuse_foreign_instance_yaml_whatever_the_flags(tmp_path, docker_idle):
    """A folder with someone else's instance.yaml (e.g. a live install) is never accepted, and there is no --force."""
    (tmp_path / "instance.yaml").write_text("name: x\n")
    with pytest.raises(cb.Fail) as e:
        cb.refuse_rules(tmp_path, "crewboss")
    assert "not empty" in e.value.message
    (tmp_path / "compose.env").write_text("COMPOSE_PROJECT_NAME=crewboss\n")     # even with a matching env file: no marker
    with pytest.raises(cb.Fail) as e:
        cb.refuse_rules(tmp_path, "crewboss")
    assert "install marker" in e.value.message


def test_rerun_of_the_same_install_is_accepted(tmp_path, docker_idle):
    (tmp_path / "compose.env").write_text("COMPOSE_PROJECT_NAME=crewboss\n")
    (tmp_path / "instance.yaml").write_text(cb.render_instance(name="C", public_url="http://localhost:1", domain=None, port=1,
                                                               tls_check=None, tls_connect=None))
    cb.refuse_rules(tmp_path, "crewboss")


def test_refuse_other_projects_compose_env(tmp_path, docker_idle):
    (tmp_path / "compose.env").write_text("COMPOSE_PROJECT_NAME=other\n")
    with pytest.raises(cb.Fail):
        cb.refuse_rules(tmp_path, "crewboss")


def test_refuse_nonempty_folder_without_env(tmp_path, docker_idle):
    (tmp_path / ".env").write_text("SECRET=1\n")
    with pytest.raises(cb.Fail) as e:
        cb.refuse_rules(tmp_path, "crewboss")
    assert "not empty" in e.value.message


def test_empty_or_missing_folder_accepted(tmp_path, docker_idle):
    cb.refuse_rules(tmp_path, "crewboss")
    cb.refuse_rules(tmp_path / "new", "crewboss")


def test_refuse_the_checkout_itself_and_its_parent(docker_idle):
    with pytest.raises(cb.Fail) as e:
        cb.refuse_rules(ROOT, "crewboss")
    assert "git checkout" in e.value.message
    with pytest.raises(cb.Fail):
        cb.refuse_rules(ROOT.parent, "crewboss")


def test_refuse_a_folder_with_a_git_dir(tmp_path, docker_idle):
    (tmp_path / ".git").mkdir()
    with pytest.raises(cb.Fail) as e:
        cb.refuse_rules(tmp_path, "crewboss")
    assert "git checkout" in e.value.message


def test_refuse_symlink(tmp_path, docker_idle):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(cb.Fail) as e:
        cb.refuse_rules(link, "crewboss")
    assert "symlink" in e.value.message
    dangling = tmp_path / "dangling"
    dangling.symlink_to(tmp_path / "nowhere")
    with pytest.raises(cb.Fail):
        cb.refuse_rules(dangling, "crewboss")


def test_refuse_project_in_use(tmp_path, monkeypatch):
    def fake(cmd, **k):
        out = "abc123\n" if cmd[:3] == ["docker", "ps", "-a"] else ""
        return subprocess.CompletedProcess(cmd, 1 if cmd[:3] == ["docker", "volume", "inspect"] else 0, out, "")
    monkeypatch.setattr(cb, "run", fake)
    with pytest.raises(cb.Fail) as e:
        cb.refuse_rules(tmp_path / "h", "crewboss")
    assert "already has containers" in e.value.message

    def vol(cmd, **k):
        return subprocess.CompletedProcess(cmd, 0, "", "")      # volume inspect succeeds = exists
    monkeypatch.setattr(cb, "run", vol)
    with pytest.raises(cb.Fail) as e:
        cb.refuse_rules(tmp_path / "h", "crewboss")
    assert "crewboss_db-data already exists" in e.value.message


def test_bad_project_name(tmp_path, docker_idle):
    with pytest.raises(cb.Fail):
        cb.refuse_rules(tmp_path, "Bad Name")


# --- never another project ----------------------------------------------------------------------------------------------------

def _installed(tmp_path, project="mine", proxy="none"):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / "compose.env").write_text(cb.render_env(project=project, port=8950, home=home, password="pw", proxy=proxy, mode="localhost"))
    return home


def test_compose_command_is_pinned_to_project_and_env_file(tmp_path):
    home = _installed(tmp_path)
    inst = cb.Install.load(home)
    cmd = inst.compose("ps")
    assert cmd[:6] == ["docker", "compose", "-p", "mine", "--env-file", str(home / "compose.env")]
    assert cmd[cmd.index("--project-directory") + 1] == str(ROOT)
    assert cmd[cmd.index("-f") + 1] == str(ROOT / "docker-compose.yml")
    assert "--profile" not in cmd
    assert "--profile" in cb.Install.load(_installed(tmp_path, proxy="caddy")).compose("ps")


def test_install_override_file_is_a_second_compose_file_only_when_present(tmp_path):
    home = _installed(tmp_path)
    assert cb.Install.load(home).compose("ps").count("-f") == 1
    (home / "compose.override.yml").write_text("services: {}\n")
    cmd = cb.Install.load(home).compose("ps")
    files = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-f"]
    assert files == [str(ROOT / "docker-compose.yml"), str(home / "compose.override.yml")]


def test_project_flag_must_match_install(tmp_path):
    home = _installed(tmp_path)
    with pytest.raises(cb.Fail):
        cb.Install.load(home, "other")
    with pytest.raises(cb.Fail):
        cb.Install.load(tmp_path / "nowhere")


def test_clean_env_drops_compose_and_crewboss_vars():
    env = cb.clean_env({"PATH": "/bin", "HOME": "/h", "COMPOSE_FILE": "x", "COMPOSE_PROJECT_NAME": "live", "CREWBOSS_HOME": "/live",
                        "POSTGRES_PASSWORD": "p", "APP_PORT": "8931", "DOCKER_HOST": "unix:///x"})
    assert env == {"PATH": "/bin", "HOME": "/h", "DOCKER_HOST": "unix:///x"}


def test_commands_never_use_repo_dot_env(tmp_path):
    src = (ROOT / "crewboss").read_text()
    assert '".env"' not in src and "'.env'" not in src


def test_secrets_not_overwritten(tmp_path, monkeypatch):
    home = _installed(tmp_path)
    (home / "secrets").mkdir()
    for f in ("ssh-ca-enc-key", "vapid-private.pem", "runner-identity-key"):
        (home / "secrets" / f).write_text("keep")
    monkeypatch.setattr(cb, "run", lambda *a, **k: pytest.fail("no docker needed when the secrets exist"))
    monkeypatch.setattr(cb, "image_name", lambda inst: "img")
    cb.make_secrets(cb.Install.load(home))
    assert all((home / "secrets" / f).read_text() == "keep" for f in ("ssh-ca-enc-key", "vapid-private.pem"))


def test_generated_ssh_ca_key_is_a_valid_fernet_key(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet
    home = _installed(tmp_path)
    (home / "secrets").mkdir()
    (home / "secrets" / "vapid-private.pem").write_text("x")
    (home / "secrets" / "runner-identity-key").write_text("x")
    monkeypatch.setattr(cb, "image_name", lambda inst: "img")
    cb.make_secrets(cb.Install.load(home))
    f = home / "secrets" / "ssh-ca-enc-key"
    assert oct(f.stat().st_mode & 0o777) == "0o600"
    Fernet(f.read_text().strip().encode())


def test_write_private_is_0600_and_no_overwrite(tmp_path):
    f = tmp_path / "s"
    assert cb.write_private(f, "a") and not cb.write_private(f, "b")
    assert f.read_text() == "a" and oct(f.stat().st_mode & 0o777) == "0o600"


def test_wait_healthy():
    class R:
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
    assert cb.wait_healthy("http://x", 5, opener=lambda u, timeout: R(), sleep=lambda s: None)
    def bad(u, timeout):
        raise OSError("down")
    assert not cb.wait_healthy("http://x", 0.01, opener=bad, sleep=lambda s: time.sleep(0.02))


# --- compose files ------------------------------------------------------------------------------------------------------------

def _compose_config(files, env, tmp_path):
    envf = tmp_path / "t.env"
    env = {"CREWBOSS_IMAGE": "t-app", **env}
    envf.write_text("".join(f"{k}={v}\n" for k, v in env.items()))
    cmd = ["docker", "compose", "--project-directory", str(ROOT), "--env-file", str(envf)]
    for f in files:
        cmd += ["-f", str(ROOT / f)]
    res = subprocess.run(cmd + ["config", "--format", "json"], capture_output=True, text=True, env={"PATH": os.environ["PATH"], "HOME": "/nonexistent-home"})
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


needs_compose = pytest.mark.skipif(not shutil.which("docker") or subprocess.run(["docker", "compose", "version"], capture_output=True).returncode != 0,
                                   reason="docker compose v2 not available")


@needs_compose
def test_base_compose_renders_with_only_the_install_env(tmp_path):
    env = {"POSTGRES_PASSWORD": "x" * 20, "CREWBOSS_HOME": "/h", "CREWBOSS_SECRETS_DIR": "/h/secrets",
           "CREWBOSS_MATERIALS_HOST_DIR": "/h/materials", "CREWBOSS_CA_UPLOADS_HOST_DIR": "/h/ca-uploads"}
    cfg = _compose_config(["docker-compose.yml"], env, tmp_path)
    assert set(cfg["services"]) == {"db", "app"}         # caddy is behind its profile
    sources = {v["source"] for v in cfg["services"]["app"]["volumes"] if v["type"] == "bind"}
    assert sources == {"/h", "/h/materials", "/h/ca-uploads", str(ROOT / "exports"), "/h/secrets/ssh-ca-enc-key", "/h/secrets/vapid-private.pem",
                       "/h/secrets/runner-identity-key"}
    blob = json.dumps(cfg).replace(str(ROOT), "<checkout>").lower()
    for word in ("zabbix-api-token", "erp-health", "entra-client-secret", "dash-keys", "drift.git", "mallen"):
        assert word not in blob, word
    assert not [v for v in cfg["services"]["app"]["volumes"] if v["target"] == "/data/estate"]


@needs_compose
def test_base_compose_has_no_default_database_password(tmp_path):
    envf = tmp_path / "t.env"
    envf.write_text("CREWBOSS_HOME=/h\n")
    res = subprocess.run(["docker", "compose", "--project-directory", str(ROOT), "--env-file", str(envf), "-f", str(ROOT / "docker-compose.yml"), "config"],
                         capture_output=True, text=True, env={"PATH": os.environ["PATH"], "HOME": "/x"})
    assert res.returncode != 0 and "POSTGRES_PASSWORD" in res.stderr


@needs_compose
def test_caddy_profile_publishes_80_and_443_only_when_on(tmp_path):
    env = {"CREWBOSS_IMAGE": "t-app", "POSTGRES_PASSWORD": "x" * 20, "CREWBOSS_HOME": "/h"}
    base = _compose_config(["docker-compose.yml"], env, tmp_path)
    assert "caddy" not in base["services"]
    envf = tmp_path / "t.env"
    envf.write_text("".join(f"{k}={v}\n" for k, v in env.items()))
    res = subprocess.run(["docker", "compose", "--project-directory", str(ROOT), "--env-file", str(envf), "-f", str(ROOT / "docker-compose.yml"),
                          "--profile", "caddy", "config", "--format", "json"], capture_output=True, text=True,
                         env={"PATH": os.environ["PATH"], "HOME": "/x"})
    cfg = json.loads(res.stdout)
    assert {p["published"] for p in cfg["services"]["caddy"]["ports"]} == {"80", "443"}
    assert cfg["services"]["caddy"]["image"].startswith("caddy:")
    assert "caddy-data" in cfg["volumes"]


@needs_compose
def test_addons_example_adds_the_mounts_back(tmp_path):
    env = {"POSTGRES_PASSWORD": "x" * 20, "CREWBOSS_HOME": "/h", "CREWBOSS_SECRETS_DIR": "/s", "CREWBOSS_ESTATE_SRC": "/e",
           "CREWBOSS_DRIFT_GIT_SRC": "/d", "CREWBOSS_DRIFT_REPO": "/srv/x"}
    for override in ("docker-compose.addons.example.yml",):
        cfg = _compose_config(["docker-compose.yml", override], env, tmp_path)
        targets = {v["target"] for v in cfg["services"]["app"]["volumes"]}
        assert {"/run/secrets/zabbix-api-token", "/run/secrets/erp-health-read-token", "/run/secrets/erp-health-resolve-token",
                "/run/secrets/entra-client-secret", "/run/secrets/dash-keys-1", "/data/estate", "/srv/x/.git"} <= targets


def test_compose_text_has_no_instance_specifics():
    assert not (ROOT / "deploy").exists()
    for f in ("docker-compose.yml", "docker-compose.addons.example.yml"):
        txt = (ROOT / f).read_text().lower()
        assert not re.search(r"\bmallen\.(tech|au|services)\b|dev-box|\.mallen", txt), f
    assert "POSTGRES_PASSWORD:?" in (ROOT / "docker-compose.yml").read_text()
    assert ":-dashboard}" not in (ROOT / "docker-compose.yml").read_text().replace("POSTGRES_DB:-dashboard", "").replace("POSTGRES_USER:-dashboard", "")


# --- YAML safety, exports ---------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["off", "on", "yes", "no", "null", "enforce", "warn", "123", "a: b", "# x", "true"])
def test_every_generated_scalar_survives_yaml(value):
    text = cb.render_instance(name=value, public_url=value, domain="x.example.com", port=1, tls_check=value, tls_connect=value)
    d = yaml.safe_load(text)
    assert d["name"] == value and d["public_url"] == value and d["tls_check"] == value and d["tls_check_connect"] == value
    assert cb.INSTANCE_MARKER in text


def test_unquoted_off_in_instance_yaml_means_off_not_a_crash(tmp_path):
    """YAML reads a bare `off` as False; config must accept it as 'off' (an old or hand-written file), not raise."""
    home = tmp_path / "h"
    home.mkdir()
    (home / "instance.yaml").write_text("tls_check: off\n")
    code = "from app import config; print(config.TLS_CHECK)"
    env = {k: v for k, v in os.environ.items() if not k.startswith("CREWBOSS_")}
    env["CREWBOSS_HOME"] = str(home)
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr[-300:]
    assert out.stdout.split()[0] == "off"
    (home / "instance.yaml").write_text("tls_check: true\n")
    assert subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True).returncode != 0


def test_generated_off_roundtrips_through_config(tmp_path):
    home = tmp_path / "h"
    home.mkdir()
    (home / "instance.yaml").write_text(cb.render_instance(name="C", public_url="https://x.example.com", domain="x.example.com",
                                                           port=1, tls_check="off", tls_connect=None))
    env = {k: v for k, v in os.environ.items() if not k.startswith("CREWBOSS_")}
    env["CREWBOSS_HOME"] = str(home)
    out = subprocess.run([sys.executable, "-c", "from app import config; print(config.TLS_CHECK)"], cwd=ROOT, env=env,
                         capture_output=True, text=True)
    assert out.stdout.strip() == "off", out.stderr[-300:]


def test_exports_folder_is_per_install():
    env = cb.parse_env(cb.render_env(project="m", port=1, home=Path("/h"), password="x", proxy="none", mode="localhost"))
    assert env["CREWBOSS_EXPORTS_HOST_DIR"] == "/h/exports"
    assert "${CREWBOSS_EXPORTS_HOST_DIR:-./exports}:/data/exports" in (ROOT / "docker-compose.yml").read_text()


@needs_compose
def test_exports_default_stays_the_checkout_folder(tmp_path):
    cfg = _compose_config(["docker-compose.yml"], {"POSTGRES_PASSWORD": "x" * 20, "CREWBOSS_HOME": "/h"}, tmp_path)
    assert str(ROOT / "exports") in {v["source"] for v in cfg["services"]["app"]["volumes"] if v["type"] == "bind"}
    cfg = _compose_config(["docker-compose.yml"], {"POSTGRES_PASSWORD": "x" * 20, "CREWBOSS_HOME": "/h",
                                                    "CREWBOSS_EXPORTS_HOST_DIR": "/h/exports"}, tmp_path)
    assert "/h/exports" in {v["source"] for v in cfg["services"]["app"]["volumes"] if v["type"] == "bind"}


# --- the certificate check --------------------------------------------------------------------------------------------------

from app import config, tls_check  # noqa: E402


@pytest.mark.parametrize("url,expected", [
    ("https://crewboss.example.com", ("crewboss.example.com", 443)), ("https://x.example.com:8443/a", ("x.example.com", 8443)),
    ("http://crewboss.example.com", None), ("http://localhost:8931", None), ("https://localhost", None),
    ("https://127.0.0.1:8931", None), ("https://[::1]/", None), ("", None)])
def test_tls_target(url, expected):
    assert tls_check.target(url) == expected


class _FakeTLS:
    def __init__(self, cert=None, exc=None):
        self.cert, self.exc = cert, exc

    def wrap_socket(self, sock, server_hostname=None):
        if self.exc:
            raise self.exc
        self.hostname = server_hostname
        return self

    def __enter__(self): return self
    def __exit__(self, *a): return False
    def getpeercert(self): return self.cert


class _Raw:
    def __enter__(self): return self
    def __exit__(self, *a): return False


def _patch(monkeypatch, cert=None, exc=None, connect_exc=None):
    seen = {}

    def conn(addr, timeout=None):
        seen["addr"] = addr
        if connect_exc:
            raise connect_exc
        return _Raw()
    monkeypatch.setattr(tls_check.socket, "create_connection", conn)
    ctx = _FakeTLS(cert, exc)
    monkeypatch.setattr(tls_check.ssl, "create_default_context", lambda: ctx)
    return seen, ctx


def _cert(days):
    return {"notAfter": time.strftime("%b %d %H:%M:%S %Y GMT", time.gmtime(time.time() + days * 86400 + 3600))}


def test_check_good_certificate(monkeypatch):
    seen, ctx = _patch(monkeypatch, cert=_cert(60))
    r = tls_check.check("https://x.example.com")
    assert r.ok and 58 <= r.days_left <= 60 and seen["addr"] == ("x.example.com", 443) and ctx.hostname == "x.example.com"


def test_check_connect_override_still_verifies_public_name(monkeypatch):
    seen, ctx = _patch(monkeypatch, cert=_cert(60))
    monkeypatch.setattr(config, "TLS_CHECK_CONNECT", "caddy:443")
    assert tls_check.check("https://x.example.com").ok
    assert seen["addr"] == ("caddy", 443) and ctx.hostname == "x.example.com"


def test_check_untrusted_and_name_mismatch(monkeypatch):
    e = ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] x")
    e.verify_message = "Hostname mismatch"
    _patch(monkeypatch, exc=e)
    r = tls_check.check("https://x.example.com")
    assert not r.ok and "not trusted" in r.problem and "Hostname mismatch" in r.problem


def test_check_expired_and_soon(monkeypatch):
    _patch(monkeypatch, cert=_cert(-3))
    assert "expired" in tls_check.check("https://x.example.com").problem
    _patch(monkeypatch, cert=_cert(3))
    r = tls_check.check("https://x.example.com")
    assert not r.ok and "expires in" in r.problem
    _patch(monkeypatch, cert=_cert(8))
    assert tls_check.check("https://x.example.com").ok


def test_check_unreachable(monkeypatch):
    _patch(monkeypatch, connect_exc=TimeoutError())
    r = tls_check.check("https://x.example.com")
    assert not r.ok and "could not connect" in r.problem


def test_check_localhost_is_exempt(monkeypatch):
    _patch(monkeypatch, connect_exc=AssertionError("must not connect"))
    assert tls_check.check("http://localhost:8931").ok and tls_check.check("https://localhost").ok


def test_default_is_warn_so_a_live_box_cannot_be_bricked():
    """An instance.yaml with no tls_check key (the live Mallen instance) gets `warn`; the installer's own key can say enforce."""
    code = "from app import config; print(config.TLS_CHECK, config.TLS_CHECK_CONNECT)"
    env = {k: v for k, v in os.environ.items() if not k.startswith("CREWBOSS_")}
    env["CREWBOSS_HOME"] = str(ROOT / "tests" / "home")
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, check=True).stdout
    assert out.split()[0] == "warn"
    env["CREWBOSS_TLS_CHECK"] = "bogus"
    assert subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True).returncode != 0
    assert tls_check.config.TLS_MIN_DAYS == 7


def test_banner_follows_last_result(monkeypatch):
    monkeypatch.setattr(config, "TLS_CHECK", "warn")
    monkeypatch.setattr(config, "PUBLIC_URL", "https://x.example.com")
    _patch(monkeypatch, cert=_cert(-1))
    tls_check.run_once()
    assert "expired" in tls_check.banner()
    monkeypatch.setattr(config, "TLS_CHECK", "off")
    assert tls_check.banner() is None
    monkeypatch.setattr(config, "TLS_CHECK", "warn")
    _patch(monkeypatch, cert=_cert(60))
    tls_check.run_once()
    assert tls_check.banner() is None


def test_startup_enforce_refuses_and_warn_does_not(monkeypatch):
    import asyncio
    monkeypatch.setattr(config, "PUBLIC_URL", "https://x.example.com")
    monkeypatch.setattr(config, "TLS_CHECK_GRACE_SECONDS", 0)
    _patch(monkeypatch, cert=_cert(-1))
    monkeypatch.setattr(config, "TLS_CHECK", "enforce")
    with pytest.raises(RuntimeError, match="refusing to start"):
        asyncio.run(tls_check.startup())
    monkeypatch.setattr(config, "TLS_CHECK", "warn")
    monkeypatch.setattr(config, "TLS_CHECK_INTERVAL_SECONDS", 0)

    async def go():
        t = await tls_check.startup()
        await asyncio.sleep(0.05)
        return t
    assert asyncio.run(go()) is None                 # warn: starts, no loop task with interval 0


def test_enforce_retries_within_grace_then_succeeds(monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_URL", "https://x.example.com")
    results = iter([tls_check.Result(False, "nope"), tls_check.Result(False, "nope"), tls_check.Result(True, days_left=80, host="x")])
    monkeypatch.setattr(tls_check, "run_once", lambda: next(results))
    assert tls_check.wait_for_good_certificate(grace=3600, sleep=lambda s: None).ok


def test_banner_renders_in_pages(client, monkeypatch):
    monkeypatch.setattr(tls_check, "_last", tls_check.Result(False, "the certificate for x is not trusted: test"))
    monkeypatch.setattr(config, "TLS_CHECK", "warn")
    assert "Certificate problem" in client.get("/").text
    monkeypatch.setattr(tls_check, "_last", tls_check.Result(True, days_left=60, host="x"))
    assert "Certificate problem" not in client.get("/").text


def test_custom_https_port_lands_in_origin_hosts():
    data = yaml.safe_load(cb.render_instance(name="C", public_url="https://x.example.com:8443", domain="x.example.com", port=8931,
                                             tls_check="enforce", tls_connect=None))
    assert "x.example.com:8443" in data["origin_hosts"] and "x.example.com" in data["origin_hosts"]
    plain = yaml.safe_load(cb.render_instance(name="C", public_url="https://x.example.com", domain="x.example.com", port=8931,
                                              tls_check="enforce", tls_connect=None))
    assert not any(h.startswith("x.example.com:") for h in plain["origin_hosts"])


def test_public_port_is_separate_from_the_bound_port():
    a = parse("install", "--mode", "network", "--domain", "x.example.com", "--yes", "--https-port", "8443")
    assert cb.choose_public_port(a, "caddy", "x.example.com") == 443            # --yes: behind a router that forwards 443
    a = parse("install", "--mode", "network", "--domain", "x.example.com", "--yes", "--https-port", "8443", "--public-port", "8443")
    assert cb.choose_public_port(a, "caddy", "x.example.com") == 8443
    assert "browsers use https://x.example.com" in cb.http01_requirements("x.example.com", 8443)


def test_interactive_https_port_asks_which_address_browsers_use(monkeypatch):
    a = parse("install", "--mode", "network", "--domain", "x.example.com", "--https-port", "8443")
    seen = []
    monkeypatch.setattr(cb, "ask_yes", lambda q, d, args=None: (seen.append(q), False)[1])
    assert cb.choose_public_port(a, "caddy", "x.example.com") == 8443
    assert "https://x.example.com:8443" in seen[0]


def test_render_env_keeps_every_host_path_inside_the_home(tmp_path):
    env = cb.parse_env(cb.render_env(project="p", port=1, home=tmp_path / "h", password="x", proxy="none", mode="localhost"))
    for k in ("CREWBOSS_HOME", "CREWBOSS_SECRETS_DIR", "CREWBOSS_MATERIALS_HOST_DIR", "CREWBOSS_EXPORTS_HOST_DIR", "CREWBOSS_CA_UPLOADS_HOST_DIR"):
        assert env[k].startswith(str(tmp_path / "h")), k
    compose = (ROOT / "docker-compose.yml").read_text()
    for var in re.findall(r"\$\{(CREWBOSS_[A-Z_]*(?:DIR|HOME))[:?-]", compose):
        if var.startswith("CREWBOSS_ACME_"):           # test-only knobs (Pebble): empty by default, not host paths
            continue
        assert var in env, f"docker-compose.yml has a host-path default for {var} that render_env does not set"


def test_mount_problems_catches_paths_and_volumes_outside_the_install(tmp_path):
    home = tmp_path / "h"
    home.mkdir()
    good = {"services": {"app": {"volumes": [{"type": "bind", "source": str(home / "secrets")}, {"type": "volume", "source": "x"}]}},
            "volumes": {"db-data": {"name": "p_db-data"}, "local": {"name": "p_local"}}}
    assert cb.mount_problems(good, home, "p") == []
    bad = {"services": {"app": {"volumes": [{"type": "bind", "source": str(tmp_path / "other" / "ca-uploads")}]}},
           "volumes": {"db-data": {"name": "crewboss_db-data"}}}
    found = cb.mount_problems(bad, home, "p")
    assert len(found) == 2 and "outside" in found[0] and "crewboss_db-data" in found[1]
    sibling = {"services": {"app": {"volumes": [{"type": "bind", "source": str(tmp_path / "h-evil")}]}}}
    assert cb.mount_problems(sibling, home, "p")                    # a folder that merely starts with the same text is outside


@pytest.mark.skipif(not shutil.which("docker"), reason="no docker")
@pytest.mark.parametrize("proxy,mode", [("none", "localhost"), ("caddy", "network")])
def test_the_real_compose_file_renders_only_inside_the_home(tmp_path, proxy, mode):
    """`docker compose config` only renders (starts nothing): every bind source is under the instance folder, every volume is the project's."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "compose.env").write_text(cb.render_env(project="crewboss-trial-test", port=45999, home=home, password="x", proxy=proxy, mode=mode))
    inst = cb.Install("crewboss-trial-test", home, ROOT, cb.parse_env((home / "compose.env").read_text()))
    res = subprocess.run(inst.compose("config", "--format", "json"), capture_output=True, text=True, env=cb.clean_env())
    if res.returncode != 0:
        pytest.skip("docker compose config unavailable: " + res.stderr[-200:])
    assert cb.mount_problems(json.loads(res.stdout), home, "crewboss-trial-test") == []


def test_a_failed_build_shows_the_last_40_lines_and_the_log_path(tmp_path):
    log = "\n".join(f"#{n} step" for n in range(100)) + "\nERROR: failed to solve"
    msg = cb.build_failure(tmp_path, log)
    assert "ERROR: failed to solve" in msg and "#61 step" in msg and "#10 step" not in msg and str(tmp_path / "build.log") in msg


def test_mount_problems_also_refuses_outside_secrets_configs_and_devices(tmp_path):
    home = tmp_path / "h"
    home.mkdir()
    cfg = {"services": {"app": {"devices": ["/dev/mem"]}},
           "secrets": {"a": {"file": "/etc/shadow"}, "b": {"file": str(home / "secrets" / "k")}},
           "configs": {"c": {"file": str(tmp_path / "x")}}}
    found = cb.mount_problems(cfg, home, "p")
    assert len(found) == 3 and any("devices" in f for f in found) and any("secret a" in f for f in found) and any("config c" in f for f in found)


@pytest.mark.parametrize("bad", ["0", "65536", "-1", "abc", ""])
def test_public_port_must_be_a_real_port(bad):
    with pytest.raises(SystemExit):
        parse("install", "--mode", "network", "--domain", "x.example.com", "--public-port", bad)


def test_public_port_accepts_the_range_and_works_with_an_existing_proxy():
    assert parse("install", "--public-port", "65535").public_port == 65535
    a = parse("install", "--mode", "network", "--domain", "x.example.com", "--proxy", "existing", "--public-port", "8443", "--yes")
    assert cb.choose_public_port(a, "existing", "x.example.com") == 8443


# --- trusted_proxies (OSS review N3): only the actual proxy ---------------------------------------------------------------------

def test_the_default_trusts_loopback_only_not_every_private_range():
    from app import config
    assert config.TRUSTED_PROXIES == ("127.0.0.0/8", "::1/128")        # no 10/8, 172.16/12, 192.168/16, fc00::/7


def test_the_installer_writes_the_actual_proxy_address_only(tmp_path):
    caddy = json.dumps([{"NetworkSettings": {"Networks": {"p_default": {"IPAddress": "198.51.100.4", "Gateway": "198.51.100.1"}}}}])
    assert cb.proxy_peers(caddy, "caddy") == ["127.0.0.1/32", "198.51.100.4/32"]          # the bundled proxy's own address
    assert cb.proxy_peers(caddy, "existing") == ["127.0.0.1/32", "198.51.100.1/32"]       # a host proxy reaches the port from the gateway
    assert cb.proxy_peers("not json", "caddy") == ["127.0.0.1/32"]                      # unknown: loopback only, never a wide range
    assert cb.proxy_peers(json.dumps([{"NetworkSettings": {"Networks": {"n": {"IPAddress": ""}}}}]), "caddy") == ["127.0.0.1/32"]
    text = cb.render_instance(name="C", public_url="https://x.example.com", domain="x.example.com", port=8931, tls_check=None,
                              tls_connect=None, trusted_proxies=["127.0.0.1/32", "198.51.100.4/32"])
    assert yaml.safe_load(text)["trusted_proxies"] == ["127.0.0.1/32", "198.51.100.4/32"]
    assert "trusted_proxies" not in yaml.safe_load(cb.render_instance(name="C", public_url="http://localhost:1", domain=None, port=1,
                                                                       tls_check=None, tls_connect=None))
    (tmp_path / "instance.yaml").write_text(text)
    cb.set_trusted_proxies(tmp_path, ["127.0.0.1/32", "198.51.100.9/32"])                 # a re-run replaces the block, never doubles it
    got = yaml.safe_load((tmp_path / "instance.yaml").read_text())
    assert got["trusted_proxies"] == ["127.0.0.1/32", "198.51.100.9/32"] and got["name"] == "C"
    assert (tmp_path / "instance.yaml").read_text().count("trusted_proxies:") == 1


# --- #976 slice C: LAN mode ----------------------------------------------------------------------------------------------------

WARNING = "LAN mode is for a local network. Don't expose it to the internet without upgrading to a signed certificate and passkey sign-in."


def test_mode_question_has_three_plain_choices():
    q = cb.MODE_QUESTION
    assert q.startswith("How will people open Crewboss?")
    assert "1) Just on this computer" in q and "2) On my home or office network" in q and "[recommended]" in q
    assert "3) On the internet with my own web address" in q
    for jargon in ("SSL", "TLS", "ACME", "HTTP-01", "certificate authority"):
        assert jargon not in q


@pytest.mark.parametrize("answer,mode", [("1", "localhost"), ("2", "lan"), ("3", "network"), ("", "lan")])
def test_ask_mode(monkeypatch, answer, mode):
    monkeypatch.setattr("builtins.input", lambda *a: answer)
    assert cb.ask_mode(parse("install")) == mode


def test_ask_mode_reasks_on_nonsense(monkeypatch):
    seq = iter(["x", "9", "3"])
    monkeypatch.setattr("builtins.input", lambda *a: next(seq))
    assert cb.ask_mode(parse("install")) == "network"


def test_yes_without_mode_stays_localhost():
    assert cb.collect_answers(parse("install", "--yes"))["mode"] == "localhost"


def test_lan_mode_flags_and_plan():
    a = parse("install", "--mode", "lan", "--lan-host", "192.0.2.20", "--lan-host", "Office-PC.local", "--yes")
    assert a.mode == "lan"
    plan = cb.collect_answers(a)
    assert plan == {"mode": "lan", "domain": None, "proxy": "caddy", "lan_hosts": ["192.0.2.20", "office-pc.local"]}


@pytest.mark.parametrize("bad", ["a b", "x\ny", "evil.com {", "a..b", "fe80::1%eth0", "2001:db8::1"])
def test_lan_host_refused(bad):
    with pytest.raises(cb.Fail):
        cb.collect_answers(parse("install", "--mode", "lan", "--lan-host", bad, "--yes"))


def test_lan_without_detectable_address_under_yes_asks_for_the_flag(monkeypatch):
    monkeypatch.setattr(cb, "lan_address", lambda *a, **k: None)
    with pytest.raises(cb.Fail) as e:
        cb.collect_answers(parse("install", "--mode", "lan", "--yes"))
    assert "--lan-host" in e.value.message


def test_lan_interactive_confirms_the_address_and_offers_the_name(monkeypatch, capsys):
    monkeypatch.setattr(cb, "lan_address", lambda *a, **k: "192.0.2.20")
    monkeypatch.setattr(cb, "mdns_name", lambda: "office-pc.local")
    answers = iter(["", "y"])          # "Is that right?" Enter = yes; then yes to the name
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    assert cb.collect_answers(parse("install", "--mode", "lan"))["lan_hosts"] == ["192.0.2.20", "office-pc.local"]
    out = capsys.readouterr().out
    assert "Other computers will open Crewboss at https://192.0.2.20" in out and "DHCP reservation" in out


def test_lan_interactive_wrong_address_can_be_typed(monkeypatch):
    monkeypatch.setattr(cb, "lan_address", lambda *a, **k: "192.0.2.20")
    monkeypatch.setattr(cb, "mdns_name", lambda: None)
    answers = iter(["n", "192.0.2.77"])
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    assert cb.collect_answers(parse("install", "--mode", "lan"))["lan_hosts"] == ["192.0.2.77"]


ROUTE = "1.1.1.1 via 192.0.2.1 dev eth0 src 192.0.2.20 uid 1000 \n    cache \n"


def test_parse_route_src():
    assert cb.parse_route_src(ROUTE) == "192.0.2.20"
    assert cb.parse_route_src("RTNETLINK answers: Network is unreachable") is None
    assert cb.parse_route_src("") is None


def test_lan_address_prefers_route_then_udp_and_refuses_loopback():
    class R:
        stdout = ROUTE
    assert cb.lan_address(runner=lambda: R(), udp=lambda: "198.51.100.9") == "192.0.2.20"
    class Bad:
        stdout = "nothing"
    assert cb.lan_address(runner=lambda: Bad(), udp=lambda: "198.51.100.9") == "198.51.100.9"
    assert cb.lan_address(runner=lambda: Bad(), udp=lambda: "127.0.0.1") is None
    def boom():
        raise OSError("no ip command")
    assert cb.lan_address(runner=boom, udp=lambda: "198.51.100.9") == "198.51.100.9"


def test_env_carries_frontdoor_dir_and_uid(tmp_path):
    env = cb.parse_env(cb.render_env(project="p", port=1, home=tmp_path / "h", password="x", proxy="caddy", mode="lan", uid=1234, gid=5678))
    assert env["CREWBOSS_FRONTDOOR_DIR"] == str(tmp_path / "h" / "frontdoor") and env["CREWBOSS_UID"] == "1234" and env["CREWBOSS_GID"] == "5678"
    assert env["CREWBOSS_INSTALL_MODE"] == "lan" and env["CREWBOSS_INSTALL_PROXY"] == "caddy" and env["CREWBOSS_HTTPS_PORT"] == "443"


def test_instance_for_lan(tmp_path):
    data = yaml.safe_load(cb.render_instance(name="C", public_url="https://192.0.2.20:8443", domain=None, port=8931, tls_check="warn",
                                             tls_connect="caddy:443", lan_hosts=["192.0.2.20", "office-pc.local"], https_port=8443))
    assert data["public_url"] == "https://192.0.2.20:8443" and data["tls_check"] == "warn" and data["tls_check_connect"] == "caddy:443"
    assert {"192.0.2.20", "192.0.2.20:8443", "office-pc.local", "office-pc.local:8443", "127.0.0.1:8931"} <= set(data["origin_hosts"])
    assert data["mcp_allowed_hosts"] == ["192.0.2.20", "office-pc.local"] and data["auth"] == "local"


def test_lan_card_has_address_fingerprint_and_click_through_words():
    card = cb.lan_card("https://192.0.2.20", "3A:9F:12")
    assert "Crewboss is ready:  https://192.0.2.20" in card and "3A:9F:12" in card and "http://192.0.2.20/trust" in card
    assert "Your browser will warn you once. That's expected on your own network: click Advanced, then Continue." in card
    assert cb.LAN_WARNING == WARNING


def test_install_parser_accepts_lan_and_subcommand():
    assert parse("lan-address", "192.0.2.5").ip == "192.0.2.5"
    assert parse("lan-address").ip is None
    assert parse("install", "--mode", "lan").lan_host == []


def test_lan_install_end_to_end_with_docker_stubbed(tmp_path, monkeypatch, capsys):
    """do_install in LAN mode: writes the env/instance, makes the front door files (stub), prints the card and the warning."""
    home = tmp_path / "home"
    calls = []
    monkeypatch.setattr(cb, "preflight", lambda *a, **k: None)
    monkeypatch.setattr(cb, "refuse_rules", lambda *a, **k: None)
    monkeypatch.setattr(cb, "port_is_listening", lambda *a, **k: False)
    monkeypatch.setattr(cb, "port_is_free", lambda *a, **k: True)
    monkeypatch.setattr(cb, "check_mounts", lambda inst: None)
    monkeypatch.setattr(cb, "make_secrets", lambda inst: None)
    monkeypatch.setattr(cb, "wait_healthy", lambda *a, **k: True)
    monkeypatch.setattr(cb, "refresh_trusted_proxies", lambda *a, **k: False)
    monkeypatch.setattr(cb, "create_owner", lambda *a, **k: print("temporary password: abcd-efgh") or 0)
    monkeypatch.setattr(cb, "make_frontdoor", lambda inst, **kw: (calls.append(kw), {"lan": {"cert_sha256": "AB:CD"}})[1])

    class Res:
        returncode, stdout, stderr = 0, "", ""
    monkeypatch.setattr(cb, "run", lambda *a, **k: Res())
    args = parse("install", "--mode", "lan", "--lan-host", "192.0.2.20", "--yes", "--home", str(home), "--name", "Acme")
    assert cb.do_install(args) == 0
    out = capsys.readouterr().out
    assert calls[0]["lan_hosts"] == ["192.0.2.20"] and calls[0]["domain"] is None
    assert "Crewboss is ready:  https://192.0.2.20" in out and "AB:CD" in out and "temporary password: abcd-efgh" in out
    assert out.rstrip().endswith("") and f"{WARNING} (Administration → Front door)" in out
    assert out.index("AB:CD") < out.index("temporary password") < out.index(WARNING)
    env = cb.parse_env((home / "compose.env").read_text())
    assert env["CREWBOSS_INSTALL_PROXY"] == "caddy" and env["CREWBOSS_INSTALL_MODE"] == "lan"
    inst = yaml.safe_load((home / "instance.yaml").read_text())
    assert inst["public_url"] == "https://192.0.2.20" and inst["tls_check"] == "warn"
    assert not (home / "Caddyfile").exists()


def _legacy(tmp_path):
    """An install from before #976: its compose.env has no frontdoor keys."""
    home = _installed(tmp_path, proxy="caddy")
    f = home / "compose.env"
    f.write_text("".join(l + "\n" for l in f.read_text().splitlines() if not l.startswith(("CREWBOSS_FRONTDOOR_DIR", "CREWBOSS_UID", "CREWBOSS_GID"))))
    return home


def test_ensure_frontdoor_moves_an_installer_caddyfile(tmp_path, monkeypatch):
    home = _legacy(tmp_path)
    (home / "Caddyfile").write_text(cb.render_caddyfile("crew.example.com", "me@example.com"))
    inst = cb.Install.load(home)
    got = {}
    monkeypatch.setattr(cb, "run", lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})())
    def fake_make(i, **kw):
        got.update(kw)
        (kw["out_dir"] / "logs").mkdir(parents=True)
        (kw["out_dir"] / "Caddyfile").write_text("rendered by the sidecar")
        return {}
    monkeypatch.setattr(cb, "make_frontdoor", fake_make)
    assert cb.ensure_frontdoor(inst) == "done"
    assert got["domain"] == "crew.example.com" and got["email"] == "me@example.com"
    env = cb.parse_env((home / "compose.env").read_text())
    assert env["CREWBOSS_FRONTDOOR_DIR"] == str(home / "frontdoor") and "CREWBOSS_UID" in env
    assert (home / "frontdoor" / "logs").is_dir()
    assert cb.ensure_frontdoor(cb.Install.load(home)) == "not-needed"        # idempotent


def test_ensure_frontdoor_leaves_a_hand_edited_caddyfile_alone(tmp_path, monkeypatch):
    home = _legacy(tmp_path)
    mine = "my.example.com {\n\treverse_proxy app:8000\n\tfile_server /x\n}\n"
    (home / "Caddyfile").write_text(mine)
    (home / "secrets").mkdir()
    monkeypatch.setattr(cb, "make_frontdoor", lambda *a, **k: pytest.fail("must not re-render a hand-edited Caddyfile"))
    assert cb.ensure_frontdoor(cb.Install.load(home)) == "kept-hand-edited"
    assert (home / "frontdoor" / "Caddyfile").read_text() == mine and not (home / "frontdoor" / "front_door.json").exists()
    assert (home / "secrets" / "frontdoor-token").stat().st_mode & 0o777 == 0o600


def test_ensure_frontdoor_keeps_a_caddyfile_that_only_starts_like_the_installers(tmp_path, monkeypatch):
    home = _legacy(tmp_path)
    mine = cb.render_caddyfile("crew.example.com", "me@example.com").replace("reverse_proxy app:8000", "reverse_proxy app:8000\n\tfile_server /x")
    assert mine.startswith(cb.INSTALLER_CADDYFILE)
    (home / "Caddyfile").write_text(mine)
    (home / "secrets").mkdir()
    monkeypatch.setattr(cb, "make_frontdoor", lambda *a, **k: pytest.fail("a hand-edited Caddyfile must not be re-rendered"))
    assert cb.ensure_frontdoor(cb.Install.load(home)) == "kept-hand-edited"
    assert (home / "frontdoor" / "Caddyfile").read_text() == mine


def test_ensure_frontdoor_is_transactional_and_a_rerun_retries(tmp_path, monkeypatch):
    home = _legacy(tmp_path)
    (home / "Caddyfile").write_text(cb.render_caddyfile("crew.example.com"))
    monkeypatch.setattr(cb, "run", lambda *a, **k: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})())
    monkeypatch.setattr(cb, "make_frontdoor", lambda *a, **k: (_ for _ in ()).throw(cb.Fail("docker build failed")))
    with pytest.raises(cb.Fail):
        cb.ensure_frontdoor(cb.Install.load(home))
    assert "CREWBOSS_FRONTDOOR_DIR" not in cb.parse_env((home / "compose.env").read_text()) and not (home / "frontdoor").exists()
    monkeypatch.setattr(cb, "make_frontdoor", lambda i, **kw: (kw["out_dir"] / "logs").mkdir(parents=True) or (kw["out_dir"] / "Caddyfile").write_text("x") or {})
    assert cb.ensure_frontdoor(cb.Install.load(home)) == "done"
    assert "CREWBOSS_FRONTDOOR_DIR" in cb.parse_env((home / "compose.env").read_text())
    # an init that produced no Caddyfile is not migrated either
    (tmp_path / "two").mkdir()
    home2 = _legacy(tmp_path / "two")
    (home2 / "Caddyfile").write_text(cb.render_caddyfile("crew.example.com"))
    monkeypatch.setattr(cb, "make_frontdoor", lambda i, **kw: (kw["out_dir"] / "logs").mkdir(parents=True) or {})
    with pytest.raises(cb.Fail):
        cb.ensure_frontdoor(cb.Install.load(home2))
    assert "CREWBOSS_FRONTDOOR_DIR" not in cb.parse_env((home2 / "compose.env").read_text())


def test_ensure_frontdoor_without_a_caddyfile_changes_nothing(tmp_path):
    home = _legacy(tmp_path)
    with pytest.raises(cb.Fail):
        cb.ensure_frontdoor(cb.Install.load(home))
    assert "CREWBOSS_FRONTDOOR_DIR" not in cb.parse_env((home / "compose.env").read_text())


def test_ensure_frontdoor_ignores_localhost_and_existing_proxy_installs(tmp_path):
    assert cb.ensure_frontdoor(cb.Install.load(_installed(tmp_path))) == "not-needed"


def test_cmd_up_runs_ensure_frontdoor_first(tmp_path, monkeypatch):
    home = _installed(tmp_path)
    order = []
    monkeypatch.setattr(cb, "ensure_frontdoor", lambda inst: order.append("ensure"))
    monkeypatch.setattr(cb, "run", lambda *a, **k: order.append("compose"))
    monkeypatch.setattr(cb, "refresh_trusted_proxies", lambda *a, **k: False)
    cb.cmd_up(parse("up", "--home", str(home)))
    assert order[:2] == ["ensure", "compose"]


def test_compose_sidecar_is_locked_down():
    c = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
    fd = c["frontdoor"]
    assert fd["read_only"] is True and fd["cap_drop"] == ["ALL"] and "no-new-privileges:true" in fd["security_opt"]
    assert "CREWBOSS_UID" in fd["user"] and fd["profiles"] == ["caddy"] and "ports" not in fd
    assert fd["command"][:3] == ["python", "-m", "app.frontdoor_agent"]
    assert not any("docker.sock" in v for v in fd["volumes"]) and not any("docker.sock" in v for v in c["app"]["volumes"])
    assert all("caddy-admin" not in v for v in c["app"]["volumes"])           # the app never sees Caddy's admin socket
    assert c["caddy"]["command"].startswith("caddy run --config /frontdoor/Caddyfile")
    assert any(v.startswith("caddy-admin:") for v in c["caddy"]["volumes"]) and any(v.startswith("caddy-admin:") for v in fd["volumes"])


def test_every_install_names_its_own_image_and_compose_requires_it(tmp_path):
    home = _installed(tmp_path)
    assert cb.parse_env((home / "compose.env").read_text())["CREWBOSS_IMAGE"] == "mine-app"
    compose = (ROOT / "docker-compose.yml").read_text()
    assert compose.count("image: ${CREWBOSS_IMAGE:?") == 2 and "COMPOSE_PROJECT_NAME:-crewboss" not in compose
    # an install from before: the key is added on load, never changed
    f = home / "compose.env"
    f.write_text("".join(l + "\n" for l in f.read_text().splitlines() if not l.startswith("CREWBOSS_IMAGE")))
    assert cb.Install.load(home).env["CREWBOSS_IMAGE"] == "mine-app" and "CREWBOSS_IMAGE=mine-app" in f.read_text()
