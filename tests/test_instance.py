"""Instance config (app/instance.py, $CREWBOSS_HOME/instance.yaml): the values in tests/home/instance.yaml must come back
EXACTLY through app/config.py; an environment variable still overrides; with no file the values are neutral.
Each case runs `python -m app.config dump` in a clean subprocess, because config reads the instance once at import."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app import instance

ROOT = Path(__file__).resolve().parent.parent

# What tests/home/instance.yaml (neutral values) must give back EXACTLY through app/config.py.
GOLDEN = {
    "ORIGIN_HOSTS": ["crewboss.example.com", "crewboss.example.net", "crewboss.internal.example.com",
                     "127.0.0.1:8931", "localhost:8931"],
    "PUBLIC_URL": "https://crewboss.example.com",
    "ERP_URL": "https://erp.example.com/",
    "KNOWN_SERVERS": ["app01.example.com", "dev01.example.com", "web-1.example.com"],
    "VAPID_SUB": "mailto:admin@example.com",
    "ZABBIX_GROUPIDS": ["42"],
    "DEFAULT_PROJECT": "acme-erp",
    "HEALTH_PROJECT": "acme-erp",
    "OVERSEER_PROJECT": "mallen-dashboard",
    "LEGACY_TOKEN_PROJECT": "acme-erp",
    "EXPORT_PROJECT": "acme-erp",
    "FALLBACK_SESSION_SLUG": "boot-acct",
    "SLUG_PREFIX": "acme-",
    "SERVER_HEALTH_KEYS": {"reboot": "acme.health[reboot.required]", "sec_updates": "acme.health[updates.security]",
                           "updates": "acme.health[updates.total]"},
    "DASHBOARD_PROJECT": "mallen-dashboard",
    "PLATFORM_KEY_STORES": {"acme-erp": {"name": "Acme App", "url": "https://keys.example.com",
                                         "key_id": "dash-keys-1", "key_file": "/run/secrets/dash-keys-1",
                                         "server": "app01"}},
}
HOME = ROOT / "tests" / "home"


def _env(home=None, **env):
    """A clean environment: no CREWBOSS_* from the test run, optionally another CREWBOSS_HOME."""
    e = {k: v for k, v in os.environ.items() if not k.startswith("CREWBOSS_")}
    e["CREWBOSS_HOME"] = str(HOME if home is None else home)
    e.update(env)
    return e


def _dump(home=None, **env) -> dict:
    out = subprocess.run([sys.executable, "-m", "app.config", "dump"], cwd=ROOT, env=_env(home, **env),
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def _run(home, **env):
    return subprocess.run([sys.executable, "-m", "app.config", "dump"], cwd=ROOT, env=_env(home, **env),
                          capture_output=True, text=True)


def test_the_instance_folder_gives_back_its_values():
    cfg = _dump()
    for key, want in GOLDEN.items():
        assert cfg[key] == want, key


def test_env_vars_still_override_the_instance():
    cfg = _dump(CREWBOSS_PUBLIC_URL="https://other.test/", CREWBOSS_ERP_URL="https://erp.other.test", CREWBOSS_ORIGIN_HOSTS="A.test",
                CREWBOSS_KNOWN_SERVERS="s1,s2", CREWBOSS_ZABBIX_GROUPIDS="1,2", CREWBOSS_VAPID_SUB="mailto:x@y.test",
                CREWBOSS_OVERSEER_PROJECT="", CREWBOSS_HEALTH_PROJECT="h", CREWBOSS_PLATFORM_KEYS_URL="https://k.test/",
                CREWBOSS_PLATFORM_KEYS_FILE="/k")
    assert cfg["PUBLIC_URL"] == "https://other.test"
    assert cfg["ERP_URL"] == "https://erp.other.test/"
    assert cfg["ORIGIN_HOSTS"] == ["a.test"]
    assert cfg["KNOWN_SERVERS"] == ["s1", "s2"]
    assert cfg["ZABBIX_GROUPIDS"] == ["1", "2"]
    assert cfg["VAPID_SUB"] == "mailto:x@y.test"
    assert cfg["OVERSEER_PROJECT"] == ""          # set-but-empty is "no overseer", as before
    assert cfg["HEALTH_PROJECT"] == "h"
    store = cfg["PLATFORM_KEY_STORES"]["acme-erp"]
    assert store["url"] == "https://k.test" and store["key_file"] == "/k"


def test_no_instance_file_gives_neutral_defaults(tmp_path):
    cfg = _dump(home=tmp_path)
    for key in ("DEFAULT_PROJECT", "HEALTH_PROJECT", "OVERSEER_PROJECT", "LEGACY_TOKEN_PROJECT", "EXPORT_PROJECT",
                "DASHBOARD_PROJECT", "ERP_URL"):
        assert cfg[key] == "", key
    assert cfg["PLATFORM_KEY_STORES"] == {} and cfg["KNOWN_SERVERS"] == [] and cfg["ZABBIX_GROUPIDS"] == []
    assert cfg["ORIGIN_HOSTS"] == ["127.0.0.1:8931", "localhost:8931"]
    moved = {k: cfg[k] for k in GOLDEN}
    assert "mallen" not in json.dumps(moved).lower()


def test_dump_redacts_secrets():
    cfg = _dump()
    assert "test@" not in cfg["DATABASE_URL"] and ":***@" in cfg["DATABASE_URL"]
    raw = subprocess.run([sys.executable, "-m", "app.config", "dump"], cwd=ROOT, capture_output=True, text=True,
                         env={**os.environ, "DATABASE_URL": "postgresql://u:pw123@h/d"})
    assert "pw123" not in raw.stdout


def test_load_rules(tmp_path):
    assert instance.load(str(tmp_path / "nope.yaml")) == {}            # a fresh install: no file
    empty = tmp_path / "empty.yaml"
    empty.write_text("")
    assert instance.load(str(empty)) == {}
    bad = tmp_path / "bad.yaml"
    bad.write_text("- a\n- b\n")
    with pytest.raises(ValueError):
        instance.load(str(bad))
    data = {"roles": {"overseer": "x"}, "servers": "one", "n": None}
    assert instance.get("roles.overseer", data=data) == "x"
    assert instance.get("roles.missing", "d", data=data) == "d"
    assert instance.get("n", "d", data=data) == "d"


def test_scanner_repos_and_catalog_default_come_from_the_instance():
    from app import catalog_import
    from scanner import scan
    assert scan.PROJECT_REPOS == {"acme-erp": "/srv/acme-erp"} == dict(instance.get_map("project_repos"))
    assert catalog_import.DEFAULT_PROJECT == "acme-erp"


def test_a_missing_or_empty_instance_file_is_a_fresh_install_that_still_starts(tmp_path):
    r = _run(tmp_path)                                    # no instance.yaml in the folder
    assert r.returncode == 0 and json.loads(r.stdout)["OVERSEER_PROJECT"] == ""
    (tmp_path / "instance.yaml").write_text("# nothing\n")
    r = _run(tmp_path)
    assert r.returncode == 0 and json.loads(r.stdout)["PUBLIC_URL"] == "http://localhost:8931"


def test_crewboss_home_is_read_from_the_environment_and_defaults_to_dot_crewboss(tmp_path):
    out = subprocess.run([sys.executable, "-c", "from app import instance; print(instance.HOME)"], cwd=ROOT,
                         env=_env(tmp_path), capture_output=True, text=True, check=True)
    assert out.stdout.strip() == str(tmp_path)
    e = {k: v for k, v in os.environ.items() if k != "CREWBOSS_HOME"}
    e["HOME"] = str(tmp_path)
    out = subprocess.run([sys.executable, "-c", "from app import instance; print(instance.HOME)"], cwd=ROOT, env=e,
                         capture_output=True, text=True, check=True)
    assert out.stdout.strip() == str(tmp_path / ".crewboss")


def test_the_example_instance_file_is_loadable_and_names_no_real_install():
    data = instance.load(str(ROOT / "instance.example.yaml"))
    assert data["roles"]["overseer"] and "mallen" not in json.dumps(data).lower()


CHECK_HOME = os.environ.get("CREWBOSS_CHECK_HOME")


@pytest.mark.skipif(not CHECK_HOME, reason="set CREWBOSS_CHECK_HOME to a real instance folder to check it loads")
def test_a_real_instance_folder_loads_through_the_same_loader():
    """Deploy check: CREWBOSS_CHECK_HOME=<folder> pytest tests/test_instance.py -k real_instance"""
    cfg = _dump(home=CHECK_HOME)
    assert cfg["OVERSEER_PROJECT"] and cfg["DEFAULT_PROJECT"] and cfg["PUBLIC_URL"].startswith("http")
    assert instance.load(str(Path(CHECK_HOME) / "instance.yaml"))["name"]


@pytest.mark.parametrize("yaml_text,key", [
    ("zabbix_groupids: 42\n", "zabbix_groupids"),                  # a bare int where a list is expected
    ("zabbix_groupids: [42]\n", "zabbix_groupids"),                # ints inside the list
    ("zabbix_groupids: {a: b}\n", "zabbix_groupids"),
    ("roles: {overseer: 5}\n", "roles.overseer"),
    ("roles: {overseer: true}\n", "roles.overseer"),
    ("roles: {overseer: [a]}\n", "roles.overseer"),
    ("roles: {health: {a: b}}\n", "roles.health"),
    ("public_url: 8000\n", "public_url"),
    ("platform_key_stores: [a]\n", "platform_key_stores"),
])
def test_wrong_yaml_types_raise_naming_file_and_key(tmp_path, yaml_text, key):
    (tmp_path / "instance.yaml").write_text(yaml_text)
    r = _run(tmp_path)
    assert r.returncode != 0
    assert "ValueError" in r.stderr and str(tmp_path / "instance.yaml") in r.stderr and repr(key) in r.stderr


def test_null_for_an_optional_value_is_unset(tmp_path):
    (tmp_path / "instance.yaml").write_text("roles: {overseer: ~, health: ~}\nerp_url: ~\nzabbix_groupids: ~\n")
    r = _run(tmp_path)
    assert r.returncode == 0
    cfg = json.loads(r.stdout)
    assert cfg["OVERSEER_PROJECT"] == "" and cfg["HEALTH_PROJECT"] == "" and cfg["ERP_URL"] == ""


def test_get_bool_takes_a_yaml_bool_or_a_yes_no_string(monkeypatch):
    """runner_repo_private: true (a YAML bool) once crashed the app at start, as get_str refused it (11-16)."""
    for raw, want in ((True, True), (False, False), ("yes", True), ("no", False), ("On", True), (None, False)):
        monkeypatch.setattr(instance, "DATA", {"runner_repo_private": raw})
        assert instance.get_bool("runner_repo_private") is want
    monkeypatch.setattr(instance, "DATA", {"runner_repo_private": "maybe"})
    with pytest.raises(ValueError):
        instance.get_bool("runner_repo_private")
