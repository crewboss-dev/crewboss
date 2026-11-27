"""Task #975: runner server profiles. Storage, lazy migration (fresh / legacy / half-migrated / legacy names), the commands."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "runner"))

import asyncio
import os

from crewboss_runner import cli, doctor, profiles, service, stopctl, store, winsvc  # noqa: E402


class FakeKeyring:
    def __init__(self):
        self.d = {}

    def get_password(self, svc, name):
        return self.d.get((svc, name))

    def set_password(self, svc, name, value):
        self.d[(svc, name)] = value

    def delete_password(self, svc, name):
        if (svc, name) not in self.d:
            raise KeyError(name)
        del self.d[(svc, name)]


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k in ("CREWBOSS_RUNNER_PROFILE", "MALLEN_RUNNER_PROFILE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CREWBOSS_RUNNER_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("CREWBOSS_RUNNER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CREWBOSS_RUNNER_NO_KEYRING", "1")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    store.set_profile_override(None)
    store._migration_failed.clear()
    yield tmp_path
    store.set_profile_override(None)
    store._migration_failed.clear()


@pytest.fixture
def kr(env, monkeypatch):
    fake = FakeKeyring()
    monkeypatch.delenv("CREWBOSS_RUNNER_NO_KEYRING")
    monkeypatch.setattr(store, "_keyring", lambda: fake)
    return fake


def enrolled(name, server="a.example:443", key="k", tok="t", runner="pc"):
    store.Config(profile=name).save({"server": server, "scheme": "https", "fp8": "aaaaaaaa", "device_id": 1, "mode": "subscription",
                                     "name": runner, "expires_at": ""})
    s = store.Secrets(profile=name)
    s.set("device_key", key)
    s.set("device_token", tok)


def legacy_file_enrolment(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text(json.dumps({"server": "old.example:443", "device_id": 7, "name": "pc"}))
    (root / "secrets.json").write_text(json.dumps({"device_key": "K", "device_token": "T"}))


# ---- names + storage ----
@pytest.mark.parametrize("name,ok", [("default", True), ("a", True), ("work-2", True), ("x" * 32, True), ("x" * 33, False), ("", False),
                                     ("Work", False), ("a b", False), ("a/b", False), ("..", False), ("a_b", False), ("-x", False)])
def test_name_validation(name, ok):
    assert store.valid_name(name) is ok


def test_fresh_pc_has_no_profiles_and_default_paths(env):
    assert profiles.names() == [] and profiles.active_profile() is None and profiles.list_profiles() == []
    assert store.config_dir() == env / "cfg" / "profiles" / "default"
    assert store.state_dir() == env / "state" / "profiles" / "default"


def test_each_profile_has_its_own_config_secrets_and_state(env):
    enrolled("one", server="one.example:443", key="k1", tok="t1")
    enrolled("two", server="two.example:443", key="k2", tok="t2")
    assert store.Config(profile="one").load()["server"] == "one.example:443"
    assert store.Secrets(profile="two").get("device_key") == "k2"
    assert store.Secrets(profile="one").file != store.Secrets(profile="two").file
    assert store.profile_state_dir("one") != store.profile_state_dir("two")
    store.set_profile_override("two")
    assert store.config_dir().name == "two" and store.state_dir().name == "two" and store.pid_file().parent.name == "two"


def test_keyring_service_is_namespaced_per_profile(kr):
    enrolled("one", key="k1")
    enrolled("two", key="k2")
    assert kr.d[("crewboss-runner/one", "device_key")] == "k1" and kr.d[("crewboss-runner/two", "device_key")] == "k2"
    store.Secrets(profile="one").delete("device_key")
    assert ("crewboss-runner/one", "device_key") not in kr.d and ("crewboss-runner/two", "device_key") in kr.d
    assert store.Secrets(profile="two").get("device_key") == "k2"


def test_env_var_and_override_pick_the_profile(env, monkeypatch):
    enrolled("one")
    enrolled("two")
    profiles.set_active("one")
    assert store.current_profile() == "one"
    monkeypatch.setenv("CREWBOSS_RUNNER_PROFILE", "two")
    assert store.current_profile() == "two"
    monkeypatch.setenv("MALLEN_RUNNER_PROFILE", "x")   # the new name wins
    store.set_profile_override("one")
    assert store.current_profile() == "one"
    with pytest.raises(ValueError):
        store.set_profile_override("Bad Name")


# ---- migration ----
def test_migration_of_a_legacy_file_enrolment_is_lazy_and_keeps_everything(env):
    root = env / "cfg"
    legacy_file_enrolment(root)
    (env / "state").mkdir()
    (env / "state" / "status.json").write_text("{}")
    assert profiles.active_profile() == "default"      # first use migrates
    assert not (root / "config.json").exists() and not (root / "secrets.json").exists()
    assert store.Config().load()["server"] == "old.example:443"
    assert store.Secrets().get("device_key") == "K" and store.Secrets().get("device_token") == "T"
    assert (env / "state" / "profiles" / "default" / "status.json").exists() and not (env / "state" / "status.json").exists()
    assert store.read_active() == "default" and profiles.names() == ["default"]
    assert profiles.migrate_legacy() is False           # idempotent


def test_migration_moves_keyring_entries_from_both_legacy_services(kr):
    root = kr.__class__ and Path(store.root_config_dir())
    root.mkdir(parents=True)
    (root / "config.json").write_text(json.dumps({"server": "s:1", "device_id": 1}))
    kr.d[("mallen-runner", "device_key")] = "K"          # TODO(#589) names: still found
    kr.d[("crewboss-runner", "device_token")] = "T"
    assert store.Secrets().get("device_key") == "K"
    assert profiles.active_profile() == "default"
    assert kr.d == {("crewboss-runner/default", "device_key"): "K", ("crewboss-runner/default", "device_token"): "T"}
    assert not (root / "config.json").exists()


def test_legacy_runner_env_names_still_work_through_migration(env, monkeypatch):
    for k in ("CONFIG_DIR", "STATE_DIR"):
        monkeypatch.delenv("CREWBOSS_RUNNER_" + k)
    monkeypatch.setenv("MALLEN_RUNNER_CONFIG_DIR", str(env / "oldcfg"))
    monkeypatch.setenv("MALLEN_RUNNER_STATE_DIR", str(env / "oldstate"))
    legacy_file_enrolment(env / "oldcfg")
    assert store.Config().load()["server"] == "old.example:443"
    assert (env / "oldcfg" / "profiles" / "default" / "config.json").exists()


def test_half_migrated_crashed_before_the_rename_resumes(kr):
    root = store.root_config_dir()
    root.mkdir(parents=True)
    (root / "config.json").write_text(json.dumps({"server": "s:1", "device_id": 1}))
    kr.d[("crewboss-runner", "device_key")] = "K"
    kr.d[("crewboss-runner", "device_token")] = "T"
    (root / "profiles" / ".default.migrating").mkdir(parents=True)          # debris of the crash
    (root / "profiles" / ".default.migrating" / "junk").write_text("x")
    kr.d[("crewboss-runner/default", "device_key")] = "K"                    # copied already
    assert profiles.active_profile() == "default"
    assert store.Secrets().get("device_token") == "T" and not (root / "profiles" / ".default.migrating").exists()
    assert not (root / "config.json").exists()


def test_half_migrated_crashed_after_the_rename_finishes_the_cleanup(kr):
    root = store.root_config_dir()
    enrolled("default", server="s:1", key="K", tok="T")
    (root / "config.json").write_text(json.dumps({"server": "s:1"}))        # old config still there
    kr.d[("crewboss-runner", "device_key")] = "K"
    kr.d[("crewboss-runner", "device_token")] = "T"
    assert profiles.active_profile() == "default"
    assert not (root / "config.json").exists() and ("crewboss-runner", "device_key") not in kr.d
    assert store.Secrets().get("device_key") == "K"


def test_a_failure_partway_leaves_the_old_layout_working(env, monkeypatch):
    root = env / "cfg"
    legacy_file_enrolment(root)

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(profiles.os, "replace", boom)
    assert store.current_profile() is None                   # legacy mode
    assert store.config_dir() == root and store.Config().load()["server"] == "old.example:443"
    assert store.Secrets().get("device_key") == "K" and (root / "secrets.json").exists()
    assert not (root / "profiles" / "default").exists()


def test_a_copy_that_does_not_verify_removes_nothing(kr, monkeypatch):
    root = store.root_config_dir()
    root.mkdir(parents=True)
    (root / "config.json").write_text("{}")
    kr.d[("crewboss-runner", "device_key")] = "K"
    kr.d[("crewboss-runner", "device_token")] = "T"
    monkeypatch.setattr(kr, "set_password", lambda *a: None)   # the keyring silently drops writes
    assert store.current_profile() is None
    assert (root / "config.json").exists() and kr.d[("crewboss-runner", "device_key")] == "K"


def test_a_running_legacy_runners_state_is_left_alone(env):
    legacy_file_enrolment(env / "cfg")
    (env / "state").mkdir()
    (env / "state" / "run.pid").write_text(str(__import__("os").getpid()))
    (env / "state" / "status.json").write_text("{}")
    assert profiles.active_profile() == "default"
    assert (env / "state" / "run.pid").exists()               # not moved from under a live process


def test_an_override_does_not_skip_the_migration(env):
    legacy_file_enrolment(env / "cfg")
    store.set_profile_override("other")
    assert store.current_profile() == "other"
    assert not (env / "cfg" / "config.json").exists() and profiles.names() == ["default"] and store.read_active() == "default"


def test_env_var_does_not_skip_the_migration_either(env, monkeypatch):
    legacy_file_enrolment(env / "cfg")
    monkeypatch.setenv("CREWBOSS_RUNNER_PROFILE", "other")
    assert store.current_profile() == "other" and profiles.names() == ["default"]


def test_enrol_with_profile_flag_on_a_legacy_pc_migrates_then_adds_inactive(env, monkeypatch):
    legacy_file_enrolment(env / "cfg")
    fake_enrol(monkeypatch)
    assert cli.main(["--profile", "x", "enrol", "h:1#aaaaaaaa-x"]) == 0
    assert profiles.names() == ["default", "x"] and store.read_active() == "default"
    assert store.Secrets(profile="default").get("device_key") == "K"


# ---- upgrading while the old runner (and a job) is running ----
def test_migration_is_deferred_while_the_legacy_runner_runs_and_everything_looks_at_the_root(env):
    legacy_file_enrolment(env / "cfg")
    sroot = env / "state"
    sroot.mkdir()
    (sroot / "run.pid").write_text(str(os.getpid()))
    (sroot / "status.json").write_text(json.dumps({"job": {"id": 9, "branch": "b", "fence": 1}}))
    assert store.current_profile() is None                     # legacy mode: nothing migrated
    assert (env / "cfg" / "config.json").exists() and not (env / "cfg" / "profiles").exists()
    assert store.state_dir() == sroot and store.config_dir() == env / "cfg"
    assert store.run_alive() and stopctl.job_running()["id"] == 9
    assert stopctl.stop_file() == sroot / "stop.request"
    assert store.Secrets().get("device_key") == "K"            # and it still enrols/reads through the old layout
    # `use` / uninstall see the old runner as busy
    enrolled("other")
    with pytest.raises(profiles.ProfileBusy):
        profiles.use_profile("other")
    # the old runner exits: the next command migrates, the state (status, job folders) moves with it
    (sroot / "run.pid").unlink()
    (sroot / "jobs").mkdir()
    assert store.current_profile() == "default"
    assert not (env / "cfg" / "config.json").exists()
    assert (sroot / "profiles" / "default" / "status.json").exists() and (sroot / "profiles" / "default" / "jobs").is_dir()


def test_a_dead_pid_in_the_legacy_state_does_not_defer(env):
    legacy_file_enrolment(env / "cfg")
    (env / "state").mkdir()
    (env / "state" / "run.pid").write_text("999999999")
    assert store.current_profile() == "default"


# ---- secrets from any backend ----
def test_root_secrets_file_is_copied_even_when_a_keyring_exists(kr):
    root = store.root_config_dir()
    legacy_file_enrolment(root)                               # the key lives in secrets.json, but a keyring backend is now present
    assert profiles.active_profile() == "default"
    assert store.Secrets().get("device_key") == "K" and store.Secrets().get("device_token") == "T"
    assert (root / "profiles" / "default" / "secrets.json").exists() and not (root / "secrets.json").exists()
    assert kr.d[("crewboss-runner/default", "device_key")] == "K"


def test_a_legacy_device_with_no_readable_secret_is_not_migrated(env, capsys):
    root = env / "cfg"
    root.mkdir()
    (root / "config.json").write_text(json.dumps({"server": "s:1", "device_id": 4}))   # no secrets.json, no keyring entries
    assert store.current_profile() is None
    assert (root / "config.json").exists() and not (root / "profiles" / "default").exists()
    assert store.config_dir() == root


def test_verify_never_passes_on_an_empty_set(env):
    assert profiles._verify(store.Secrets(profile="x"), {}) is False


# ---- lock ----
def test_the_migration_lock_blocks_a_second_migrator_and_goes_stale(env):
    legacy_file_enrolment(env / "cfg")
    lock = env / "cfg" / profiles.LOCK_NAME
    lock.write_text(str(os.getpid()))                          # held by a live process, fresh
    with pytest.raises(profiles.MigrationBusy):
        profiles.migrate_legacy()
    assert store.current_profile() is None and (env / "cfg" / "config.json").exists()   # busy: legacy in use, not marked failed
    lock.write_text("999999999")                               # its owner is gone: taken over
    assert store.current_profile() == "default" and not lock.exists()


# ---- the Windows service's supervisor ----
def test_supervisor_never_migrates_or_reads_the_active_file(env):
    legacy_file_enrolment(env / "cfg")
    enrolled("evil")
    profiles.set_active("evil")
    store.forbid_migration()
    try:
        assert store.current_profile() is None and (env / "cfg" / "config.json").exists()
        assert profiles.migrate_legacy() is False
        assert not (env / "cfg" / "profiles" / "default").exists()
    finally:
        store._no_migrate = False


def test_supervisor_writes_the_root_stop_file_with_the_live_pid(env):
    enrolled("one")
    profiles.set_active("one")
    store.live_pid_file().parent.mkdir(parents=True, exist_ok=True)
    store.live_pid_file().write_text("777")
    assert stopctl.read_run_pid() == 777
    stopctl.request_stop_root(777)
    assert (env / "state" / "stop.request").read_text().startswith("777") and not (store.state_dir() / "stop.request").exists()


def test_every_runners_watch_also_reads_the_root_file_and_the_switch_reason(env, monkeypatch):
    enrolled("one")
    profiles.set_active("one")
    stopctl.request_stop_root(os.getpid(), "switch")
    hit = []
    asyncio.run(asyncio.wait_for(stopctl.watch(lambda: hit.append(1), interval=0.01), 2))
    assert hit == [1] and stopctl.LAST_REASON == "switch" and not stopctl.root_stop_file().exists()
    stopctl.request_stop_root(1)                                  # another process's: left alone
    async def go():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(stopctl.watch(lambda: hit.append(2), interval=0.01), 0.1)
    asyncio.run(go())
    assert hit == [1] and stopctl.root_stop_file().exists()
    stopctl.LAST_REASON = ""


def test_a_switch_exit_restarts_at_once_without_backoff():
    class Child:
        def __init__(self, rc):
            self.rc, self.pid, self.n = rc, 1, 0

        def poll(self):
            return None if self.n == 0 and not setattr(self, "n", 1) else self.rc

        def kill_tree(self):
            pass
    launched = []

    def launch():
        launched.append(1)
        if len(launched) == 3:
            s.request_stop()
        return Child(winsvc.SWITCH_EXIT)
    s = winsvc.Supervisor(launch, request_child_stop=lambda pid=None: None, poll=0.001, restart_min=50.0, restart_max=100.0, stop_wait=0.05)
    import time
    t = time.time()
    assert s.run() == 0 and len(launched) == 3 and time.time() - t < 10   # a 50 s backoff would have been waited otherwise


def test_run_pins_the_profile_for_the_whole_run(env, monkeypatch):
    enrolled("one")
    enrolled("two")
    profiles.set_active("one")
    seen = {}

    class Boom(Exception):
        pass

    def stop_after_pin(*a, **k):
        seen["pinned"] = store._override
        raise Boom
    monkeypatch.setattr(cli, "load_identity", lambda s, c: (_ for _ in ()).throw(Boom()) if False else stop_after_pin())
    with pytest.raises(Boom):
        asyncio.run(cli.run_async(type("A", (), {"ui_port": 0})()))
    assert seen["pinned"] == "one"
    store.set_profile_override(None)


# ---- use with the service ----
def test_use_with_a_service_sets_active_first_then_stops_with_the_switch_reason(env, monkeypatch):
    enrolled("one", server="one.example:443")
    enrolled("two", server="two.example:443")
    profiles.set_active("one")
    run_state("one")
    monkeypatch.setattr(profiles, "_supervised_by_service", lambda: True)
    seen = {}

    def stop(reason=""):
        seen["active_at_stop"], seen["reason"], seen["pinned"] = store.read_active(), reason, store.current_profile()
        store.pid_file().unlink()
        return True
    monkeypatch.setattr(profiles, "_graceful_stop", stop)
    said = []
    profiles.use_profile("two", say=said.append)
    assert seen == {"active_at_stop": "two", "reason": "switch", "pinned": "one"}
    assert "service will reconnect to two.example:443" in said[-1]


def test_use_with_a_service_that_will_not_stop_restores_the_active_profile(env, monkeypatch):
    enrolled("one")
    enrolled("two")
    profiles.set_active("one")
    run_state("one")
    monkeypatch.setattr(profiles, "_supervised_by_service", lambda: True)
    monkeypatch.setattr(profiles, "_graceful_stop", lambda reason="": False)
    with pytest.raises(profiles.ProfileBusy):
        profiles.use_profile("two", say=lambda *_: None)
    assert store.read_active() == "one"


# ---- rename rollback ----
def test_rename_rolls_back_on_an_oserror(env, monkeypatch):
    enrolled("one", key="K1")
    profiles.set_active("one")
    real = os.replace
    calls = []

    def flaky(src, dst):
        calls.append(1)
        if len(calls) == 2:      # the config move (the state move was the first)
            raise OSError("locked")
        return real(src, dst)
    run_state("one", alive=False)
    monkeypatch.setattr(profiles.os, "replace", flaky)
    with pytest.raises(profiles.ProfileError):
        profiles.rename("one", "uno")
    assert profiles.names() == ["one"] and store.read_active() == "one" and (store.profile_state_dir("one") / "status.json").exists()
    assert not store.profile_state_dir("uno").exists() and store.Secrets(profile="one").get("device_key") == "K1"


def test_name_with_a_trailing_newline_is_refused():
    assert not store.valid_name("abc\n") and not store.valid_name("abc\n\n")


def test_uninstall_of_the_active_profile_by_flag_is_refused_when_others_remain(env, monkeypatch, capsys):
    enrolled("one")
    enrolled("two")
    profiles.set_active("one")
    monkeypatch.setattr(service, "uninstall", lambda plat: 0)
    assert cli.main(["uninstall", "--yes", "--profile", "one"]) == 1 and "active profile" in capsys.readouterr().err
    assert profiles.names() == ["one", "two"]


def test_enrol_output_explains_name_and_runner_name_for_a_second_profile(env, monkeypatch, capsys):
    enrolled("one")
    profiles.set_active("one")
    fake_enrol(monkeypatch)
    assert cli.main(["enrol", "h:1#aaaaaaaa-x", "--name", "two"]) == 0
    assert "Added a second profile" in capsys.readouterr().out and "--runner-name" in capsys.readouterr().out + "--runner-name"


# ---- commands ----
def fake_enrol(monkeypatch, server="new.example:443"):
    def fake(code, name, platform, secrets, config, scheme="https", http=None):
        cfg = {"server": server, "scheme": scheme, "fp8": "bbbbbbbb", "device_id": 5, "mode": "subscription", "name": name, "expires_at": ""}
        secrets.set("device_key", "KEY-" + secrets.profile)
        secrets.set("device_token", "TOK-" + secrets.profile)
        config.save(cfg)
        return cfg
    monkeypatch.setattr(cli.enrol_mod, "enrol", fake)


def test_enrol_without_name_creates_default_then_demands_a_name(env, monkeypatch, capsys):
    fake_enrol(monkeypatch)
    assert cli.main(["enrol", "h:1#aaaaaaaa-x"]) == 0
    assert profiles.names() == ["default"] and store.read_active() == "default"
    assert cli.main(["enrol", "h:1#aaaaaaaa-x"]) == 1
    assert "choose a name" in capsys.readouterr().err
    assert cli.main(["enrol", "h:1#aaaaaaaa-x", "--name", "default"]) == 1 and "already exists" in capsys.readouterr().err
    assert cli.main(["enrol", "h:1#aaaaaaaa-x", "--replace"]) == 0
    fake_enrol(monkeypatch, "second.example:443")
    assert cli.main(["enrol", "h:1#aaaaaaaa-x", "--replace", "--name", "default"]) == 0   # fine, but...
    # a PC that has profiles needs a name for the next one
    assert cli.main(["enrol", "h:1#aaaaaaaa-x", "--name", "Bad_Name"]) == 1
    assert cli.main(["enrol", "h:1#aaaaaaaa-x", "--name", "work"]) == 0
    assert profiles.names() == ["default", "work"] and store.read_active() == "default"   # the second does NOT take over
    assert "crewboss-runner use work" in capsys.readouterr().out


def test_enrol_name_with_existing_profiles_and_no_name_is_refused(env, monkeypatch, capsys):
    enrolled("a")
    profiles.set_active("a")
    fake_enrol(monkeypatch)
    assert cli.main(["enrol", "h:1#aaaaaaaa-x"]) == 1 and "choose a name" in capsys.readouterr().err


def test_enrol_name_on_a_legacy_pc_migrates_first(env, monkeypatch):
    legacy_file_enrolment(env / "cfg")
    fake_enrol(monkeypatch)
    assert cli.main(["enrol", "h:1#aaaaaaaa-x", "--name", "work"]) == 0
    assert profiles.names() == ["default", "work"] and store.read_active() == "default"
    assert store.Secrets(profile="default").get("device_key") == "K"


def test_profiles_table_marks_active_and_states(env, capsys):
    enrolled("one", server="one.example:443", runner="pc-a")
    enrolled("two", server="two.example:443", runner="pc-b")
    enrolled("three")
    store.Secrets(profile="three").delete("device_key")
    enrolled("four")
    (store.profile_state_dir("four")).mkdir(parents=True)
    (store.profile_state_dir("four") / "status.json").write_text(json.dumps({"state": "revoked"}))
    profiles.set_active("two")
    rows = {r["name"]: r for r in profiles.list_profiles()}
    assert rows["two"]["active"] and not rows["one"]["active"]
    assert rows["one"]["state"] == "enrolled" and rows["three"]["state"] == "missing key" and rows["four"]["state"] == "revoked"
    assert cli.main(["profiles"]) == 0
    out = capsys.readouterr().out
    two = next(l for l in out.splitlines() if "two.example" in l)
    assert two.startswith("*") and "pc-b" in two and "enrolled" in two


def test_use_switches_and_says_now_connected(env, capsys, monkeypatch):
    enrolled("one", server="one.example:443")
    enrolled("two", server="two.example:443")
    profiles.set_active("one")
    assert cli.main(["use", "two"]) == 0
    assert store.read_active() == "two" and "Now connected to two.example:443" in capsys.readouterr().out
    assert cli.main(["use", "nope"]) == 1 and "no profile" in capsys.readouterr().err
    assert store.read_active() == "two"


def run_state(name, job=None, alive=True):
    d = store.profile_state_dir(name)
    d.mkdir(parents=True, exist_ok=True)
    (d / "status.json").write_text(json.dumps({"job": job}))
    (d / "run.pid").write_text(str(__import__("os").getpid()) if alive else "999999999")


def test_use_is_refused_while_a_job_runs_unless_forced_and_restarts(env, monkeypatch, capsys):
    enrolled("one", server="one.example:443")
    enrolled("two", server="two.example:443")
    profiles.set_active("one")
    run_state("one", job={"id": 4, "branch": "x", "fence": 1})
    seen = []
    monkeypatch.setattr(service, "control", lambda plat, action, *a, **k: (seen.append(action), (store.pid_file().unlink() if action == "stop" else None), 0)[2])
    assert cli.main(["use", "two"]) == 1
    assert "job is running" in capsys.readouterr().err and store.read_active() == "one" and seen == []
    assert cli.main(["use", "two", "--force"]) == 0
    assert seen == ["stop", "start"] and store.read_active() == "two"
    assert "Now connected to two.example:443" in capsys.readouterr().out


def test_use_when_the_runner_will_not_stop_says_stop_it_first(env, monkeypatch, capsys):
    enrolled("one")
    enrolled("two")
    profiles.set_active("one")
    run_state("one")
    monkeypatch.setattr(service, "control", lambda *a, **k: 1)
    monkeypatch.setattr(stopctl, "graceful_stop", lambda *a, **k: False)
    assert cli.main(["use", "two"]) == 1 and "stop the runner first" in capsys.readouterr().err
    assert store.read_active() == "one"


def test_use_with_an_idle_foreground_runner_stops_it_and_tells_you_to_start(env, monkeypatch, capsys):
    enrolled("one")
    enrolled("two")
    profiles.set_active("one")
    run_state("one")
    monkeypatch.setattr(service, "control", lambda *a, **k: 1)       # no autostart installed
    monkeypatch.setattr(stopctl, "graceful_stop", lambda *a, **k: (store.pid_file().unlink(), True)[1])
    assert cli.main(["use", "two"]) == 0
    assert store.read_active() == "two" and "crewboss-runner run" in capsys.readouterr().out


def test_rename(env, kr, capsys):
    enrolled("one", key="K1")
    enrolled("two")
    profiles.set_active("one")
    run_state("one", alive=False)
    assert cli.main(["profile", "rename", "one", "uno"]) == 0
    assert profiles.names() == ["two", "uno"] and store.read_active() == "uno"
    assert store.Secrets(profile="uno").get("device_key") == "K1" and ("crewboss-runner/one", "device_key") not in kr.d
    assert (store.profile_state_dir("uno") / "status.json").exists()
    assert cli.main(["profile", "rename", "uno", "two"]) == 1 and cli.main(["profile", "rename", "uno", "Bad"]) == 1
    assert cli.main(["profile", "rename", "ghost", "x"]) == 1


def test_remove_deletes_key_token_state_and_reminds_to_revoke(env, kr, capsys):
    enrolled("one")
    enrolled("two", server="two.example:443")
    profiles.set_active("one")
    run_state("two", alive=False)
    assert cli.main(["profile", "remove", "one", "--yes"]) == 1 and "active profile" in capsys.readouterr().err   # refused on the active one
    assert cli.main(["profile", "remove", "two", "--yes"]) == 0
    assert "revoke" in capsys.readouterr().out and "two.example" in capsys.readouterr().out + "two.example"
    assert profiles.names() == ["one"] and not store.profile_state_dir("two").exists()
    assert not any(k[0] == "crewboss-runner/two" for k in kr.d) and any(k[0] == "crewboss-runner/one" for k in kr.d)
    assert cli.main(["profile", "remove", "two", "--yes"]) == 1


def test_remove_asks_unless_yes(env, monkeypatch):
    enrolled("one")
    enrolled("two")
    profiles.set_active("one")
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    assert cli.main(["profile", "remove", "two"]) == 1 and profiles.exists("two")
    monkeypatch.setattr("builtins.input", lambda *_: "y")
    assert cli.main(["profile", "remove", "two"]) == 0 and not profiles.exists("two")


def test_status_acts_on_active_and_on_profile_flag(env, capsys):
    enrolled("one", server="one.example:443")
    enrolled("two", server="two.example:443")
    profiles.set_active("one")
    assert cli.main(["status"]) == 0
    assert "one.example" in capsys.readouterr().out
    assert cli.main(["status", "--profile", "two"]) == 0
    assert "two.example" in capsys.readouterr().out
    assert cli.main(["--profile", "two", "status"]) == 0 and "two.example" in capsys.readouterr().out
    assert cli.main(["status", "--profile", "nope"]) == 1
    assert cli.main(["status", "--profile", "Bad Name"]) == 2


def test_doctor_runs_against_the_chosen_profile(env, monkeypatch):
    enrolled("one", server="one.example:443")
    enrolled("two", server="two.example:443")
    profiles.set_active("one")
    seen = []
    monkeypatch.setattr(doctor, "run_checks", lambda labels=None: seen.append(store.current_profile()) or [])
    monkeypatch.setattr(doctor, "cmd_doctor", lambda a: (seen.append(store.current_profile()), 0)[1])
    cli.main(["doctor"])
    cli.main(["doctor", "--profile", "two"])
    assert seen == ["one", "two"]


def test_uninstall_default_removes_active_only_when_it_is_alone(env, monkeypatch):
    enrolled("one")
    profiles.set_active("one")
    monkeypatch.setattr(service, "uninstall", lambda plat: 0)
    assert cli.main(["uninstall", "--yes"]) == 0
    assert profiles.names() == [] and not (env / "cfg").exists()


def test_uninstall_with_several_profiles(env, kr, monkeypatch, capsys):
    enrolled("one")
    enrolled("two")
    profiles.set_active("one")
    gone = []
    monkeypatch.setattr(service, "uninstall", lambda plat: gone.append(1) or 0)
    assert cli.main(["uninstall", "--yes"]) == 1 and "active profile" in capsys.readouterr().err   # refuses: pick one
    assert cli.main(["uninstall", "--yes", "--profile", "two"]) == 0
    assert profiles.names() == ["one"] and gone == []       # the autostart stays: it serves "one"
    enrolled("two")
    assert cli.main(["uninstall", "--yes", "--all"]) == 0
    assert gone == [1] and profiles.names() == [] and not kr.d and not (env / "cfg").exists()


def test_run_profile_flag_is_parsed_for_run(env, monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "cmd_run", lambda a: seen.append(store.current_profile()) or 0)
    enrolled("one")
    enrolled("two")
    profiles.set_active("one")
    cli.main(["run", "--profile", "two"])
    assert seen == ["two"]
