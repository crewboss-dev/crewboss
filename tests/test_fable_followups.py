"""Small items from the second Fable re-review of the OSS branch (R1, R3, R4, R5, R6). Addresses are documentation ranges."""
import importlib.machinery
import importlib.util
import json
import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

from app import changes, config, jobs, local_auth, proxy_check, repo, runner_ws
from app.db import get_cursor

from test_multi_user import H, ORIGIN, _client, _user, local  # noqa: F401  (local sign-in mode, real sessions)
from test_server_access import _commissioned, est  # noqa: F401
from test_server_session_passes import _switch

_loader = importlib.machinery.SourceFileLoader("crewboss_cli_f", str(Path(__file__).resolve().parent.parent / "crewboss"))
_spec = importlib.util.spec_from_loader("crewboss_cli_f", _loader)
cb = importlib.util.module_from_spec(_spec)
_loader.exec_module(cb)


def _req(peer, xff=""):
    return SimpleNamespace(client=SimpleNamespace(host=peer), headers={"x-forwarded-for": xff} if xff else {})


# --- R1: the untrusted-proxy warning ---------------------------------------------------------------------------------------

def test_r1_untrusted_proxy_peer_with_xff_warns_once_and_shows_a_banner(caplog):
    proxy_check.reset()
    with caplog.at_level(logging.WARNING, logger="crewboss.proxy_check"):
        proxy_check.observe("127.0.0.1", "198.51.100.7")            # loopback: trusted by default, nothing
        proxy_check.observe("192.0.2.9", "")                        # no header: a direct client, nothing
        assert proxy_check.banner() is None
        proxy_check.observe("192.0.2.9", "198.51.100.7")
        proxy_check.observe("192.0.2.10", "198.51.100.8")          # once per process
    assert [r.getMessage().count("192.0.2.9") for r in caplog.records] == [1] and len(caplog.records) == 1
    assert "192.0.2.9" in proxy_check.banner() and "trusted_proxies" in proxy_check.banner()
    # the advice is conditional: only YOUR proxy is added; otherwise investigate (a client may be reaching the app directly)
    assert "only if it is YOUR reverse proxy" in proxy_check.banner() and "investigate" in proxy_check.banner()
    assert "Add that address to trusted_proxies in instance.yaml and restart." not in proxy_check.banner()
    assert "YOUR reverse proxy" in caplog.records[0].getMessage() and "investigate" in caplog.records[0].getMessage()
    proxy_check.reset()


def test_r1_the_banner_escapes_the_peer_text(monkeypatch):
    from app.templating import templates
    monkeypatch.setattr(proxy_check, "_peer", "<script>x</script>")
    html = templates.env.from_string("{{ p }}").render(p=proxy_check.banner())
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_r1_a_page_renders_with_no_banner_when_the_global_is_absent(monkeypatch):
    from app.templating import templates
    monkeypatch.delitem(templates.env.globals, "proxy_banner")
    html = templates.env.from_string('{% set p = proxy_banner() if proxy_banner is defined else None %}[{{ p }}]').render()
    assert html == "[None]"


def test_r1_a_trusted_peer_never_warns(monkeypatch):
    proxy_check.reset()
    monkeypatch.setattr(config, "TRUSTED_PROXIES", ("192.0.2.0/24",))
    proxy_check.observe("192.0.2.9", "198.51.100.7")
    assert proxy_check.banner() is None


def test_r1_the_first_request_through_the_app_trips_it(monkeypatch):
    from app.main import app
    proxy_check.reset()
    c = TestClient(app, base_url=ORIGIN, client=("192.0.2.77", 50000))
    c.get("/healthz", headers={"X-Forwarded-For": "198.51.100.3"})
    assert "192.0.2.77" in (proxy_check.banner() or "")
    proxy_check.reset()


def _install(tmp_path, proxy="existing", yaml_text=None):
    (tmp_path / "instance.yaml").write_text(cb.INSTANCE_MARKER + "\nname: C\n" if yaml_text is None else yaml_text)
    return cb.Install("crewboss-x", tmp_path, env={"CREWBOSS_INSTALL_PROXY": proxy, "APP_PORT": "8931"})


def _fake_docker(monkeypatch, calls, gateway="198.51.100.1"):
    insp = json.dumps([{"NetworkSettings": {"Networks": {"n": {"IPAddress": "198.51.100.4", "Gateway": gateway}}}}])

    def fake(cmd, **k):
        calls.append(cmd)
        out = "abc123\n" if "ps" in cmd else insp if cmd[:2] == ["docker", "inspect"] else ""
        return subprocess.CompletedProcess(cmd, 0, out, "")
    monkeypatch.setattr(cb, "run", fake)
    monkeypatch.setattr(cb, "wait_healthy", lambda *a, **k: True)


def test_r1_cmd_up_refreshes_trusted_proxies_with_one_restart_only_when_changed(tmp_path, monkeypatch):
    inst = _install(tmp_path)
    calls = []
    _fake_docker(monkeypatch, calls)
    monkeypatch.setattr(cb.Install, "load", classmethod(lambda cls, *a, **k: inst))
    args = SimpleNamespace(home=str(tmp_path), project=None)
    assert cb.cmd_up(args) == 0
    assert cb.current_trusted_proxies(tmp_path) == ["127.0.0.1/32", "198.51.100.1/32"]
    assert sum(1 for c in calls if "restart" in c) == 1
    calls.clear()
    cb.cmd_up(args)                                                  # already right: no second restart
    assert not any("restart" in c for c in calls)
    assert (tmp_path / "instance.yaml").read_text().count("trusted_proxies:") == 1


def test_r1_cmd_up_never_touches_an_instance_yaml_without_the_marker_or_with_a_hand_written_list(tmp_path, monkeypatch):
    calls = []
    _fake_docker(monkeypatch, calls)
    foreign = "name: Live\n"
    inst = _install(tmp_path, yaml_text=foreign)
    assert cb.refresh_trusted_proxies(inst) is False
    assert (tmp_path / "instance.yaml").read_text() == foreign and not any("restart" in c for c in calls)
    mine = cb.INSTANCE_MARKER + "\nname: C\ntrusted_proxies:\n  - 192.0.2.0/24\n"      # marker, but the list is the owner's own
    inst = _install(tmp_path, yaml_text=mine)
    assert cb.refresh_trusted_proxies(inst) is False
    assert (tmp_path / "instance.yaml").read_text() == mine and not any("restart" in c for c in calls)


_BLOCK_HEAD = ("# The reverse proxy whose X-Forwarded-For is believed (the sign-in rate limits key on the client address). Only this\n"
               "# address: if your proxy reaches the app from another one, list that here.\ntrusted_proxies:\n")


def _up_with_block(tmp_path, monkeypatch, entries):
    text = cb.INSTANCE_MARKER + "\nname: C\n" + _BLOCK_HEAD + entries
    inst = _install(tmp_path, yaml_text=text)
    calls = []
    _fake_docker(monkeypatch, calls)
    return inst, calls, text


def test_r1_up_keeps_an_address_the_owner_added_to_the_block(tmp_path, monkeypatch):
    inst, calls, _ = _up_with_block(tmp_path, monkeypatch, '  - "127.0.0.1/32"\n  - "198.51.100.1/32"\n  - "203.0.113.5/32"\n')
    assert cb.refresh_trusted_proxies(inst) is False                 # the docker address is already there: nothing to add, no restart
    assert not any("restart" in c for c in calls)
    assert "203.0.113.5/32" in (tmp_path / "instance.yaml").read_text()
    # the docker gateway changed AND the owner has an entry: the new address is added, the owner's stays
    inst, calls, _ = _up_with_block(tmp_path, monkeypatch, '  - "127.0.0.1/32"\n  - "203.0.113.5/32"\n')
    assert cb.refresh_trusted_proxies(inst) is True
    assert cb.current_trusted_proxies(tmp_path) == ["127.0.0.1/32", "203.0.113.5/32", "198.51.100.1/32"]
    assert sum(1 for c in calls if "restart" in c) == 1
    assert (tmp_path / "instance.yaml").read_text().count("trusted_proxies:") == 1


def test_r1_up_leaves_a_block_it_cannot_read_with_certainty_alone(tmp_path, monkeypatch):
    for entries in ('- "127.0.0.1/32"\n- "203.0.113.5/32"\n',                              # unindented list items
                    '  - "127.0.0.1/32"\n  - "203.0.113.5/32"   # my tunnel\n',               # a trailing comment
                    '  - "127.0.0.1/32"\n  - 203.0.113.5/32 # mine\n'):
        inst, calls, text = _up_with_block(tmp_path, monkeypatch, entries)
        assert cb.current_trusted_proxies(tmp_path) is None
        assert cb.refresh_trusted_proxies(inst) is False
        assert (tmp_path / "instance.yaml").read_text() == text and not any("restart" in c for c in calls)
    inst, calls, text = _up_with_block(tmp_path, monkeypatch, '  - 127.0.0.1/32\n  - \'203.0.113.5/32\'\n')      # bare and single-quoted are fine
    assert cb.current_trusted_proxies(tmp_path) == ["127.0.0.1/32", "203.0.113.5/32"]


_C1, _C2 = cb.TP_COMMENT


def _refuse_cases():
    entry = '  - "203.0.113.5/32"\n'
    head = cb.INSTANCE_MARKER + "\nname: C\n"
    return {
        "blank line between the key and its entries": head + "\n".join([_C1, _C2, "trusted_proxies:", "", entry]),
        "a column-0 comment between the key and its entries": head + "\n".join([_C1, _C2, "trusted_proxies:", "# mine", entry]),
        "another key between the installer comment and the key": head + f"{_C1}\n{_C2}\ntls_check: enforce\ntrusted_proxies:\n" + entry,
        "the whole comment appearing twice": head + _BLOCK_HEAD + entry + "\n" + _BLOCK_HEAD + entry,
        "a comment line inside the entries": head + _BLOCK_HEAD + entry + "# note\n" + entry,
        "an entry indented four spaces": head + _BLOCK_HEAD + '    - "203.0.113.5/32"\n',
    }


@pytest.mark.parametrize("why", list(_refuse_cases()))
def test_r1_a_block_that_is_not_exactly_the_installers_is_left_byte_identical(tmp_path, monkeypatch, why):
    text = _refuse_cases()[why]
    inst = _install(tmp_path, yaml_text=text)
    calls = []
    _fake_docker(monkeypatch, calls)
    assert cb.refresh_trusted_proxies(inst) is False
    assert (tmp_path / "instance.yaml").read_bytes() == text.encode() and not any("restart" in c for c in calls)


def test_r1_the_comment_text_appearing_earlier_deletes_nothing_between(tmp_path, monkeypatch):
    text = cb.INSTANCE_MARKER + f"\nname: C\n{_C1}\nsign_in: local\n" + _BLOCK_HEAD + '  - "127.0.0.1/32"\n'
    inst = _install(tmp_path, yaml_text=text)
    _fake_docker(monkeypatch, [])
    assert cb.refresh_trusted_proxies(inst) is True
    new = (tmp_path / "instance.yaml").read_text()
    assert new.startswith(text.split(_BLOCK_HEAD)[0]) and yaml.safe_load(new)["sign_in"] == "local"      # the stray line and key stay
    assert yaml.safe_load(new)["trusted_proxies"] == ["127.0.0.1/32", "198.51.100.1/32"]


def test_r1_a_key_after_the_block_and_the_block_at_the_end_are_both_kept_intact(tmp_path, monkeypatch):
    for tail in ("", "tls_check: warn\n", "tls_check: warn"):
        text = cb.INSTANCE_MARKER + "\nname: C\n" + _BLOCK_HEAD + '  - "127.0.0.1/32"\n  - "203.0.113.5/32"\n' + tail
        inst = _install(tmp_path, yaml_text=text)
        _fake_docker(monkeypatch, [])
        assert cb.refresh_trusted_proxies(inst) is True
        new = (tmp_path / "instance.yaml").read_text()
        got = yaml.safe_load(new)
        assert got["trusted_proxies"] == ["127.0.0.1/32", "203.0.113.5/32", "198.51.100.1/32"] and got["name"] == "C"
        assert ("tls_check" in got) == bool(tail) and new.count("trusted_proxies:") == 1


def test_r1_the_write_keeps_the_files_mode_and_leaves_no_temp_file(tmp_path, monkeypatch):
    inst = _install(tmp_path, yaml_text=cb.INSTANCE_MARKER + "\nname: C\n")
    (tmp_path / "instance.yaml").chmod(0o640)
    _fake_docker(monkeypatch, [])
    assert cb.refresh_trusted_proxies(inst) is True
    assert (tmp_path / "instance.yaml").stat().st_mode & 0o777 == 0o640
    assert sorted(p.name for p in tmp_path.iterdir()) == ["instance.yaml"]


@pytest.mark.parametrize("bad_quote", ["{", "'not-the-address'"])         # unparseable YAML / a list that is not the merged one
def test_r1_the_verify_after_write_guard_refuses_a_result_that_does_not_read_back(tmp_path, monkeypatch, capsys, bad_quote):
    text = cb.INSTANCE_MARKER + "\nname: C\n" + _BLOCK_HEAD + '  - "127.0.0.1/32"\n'
    (tmp_path / "instance.yaml").write_text(text)
    monkeypatch.setattr(cb, "q", lambda a: bad_quote)
    assert cb.set_trusted_proxies(tmp_path, ["127.0.0.1/32", "203.0.113.5/32"]) is False
    assert (tmp_path / "instance.yaml").read_text() == text
    assert "by hand" in capsys.readouterr().err and sorted(p.name for p in tmp_path.iterdir()) == ["instance.yaml"]


def test_r1_the_verify_after_write_guard_refuses_a_change_to_another_key(tmp_path, monkeypatch):
    text = cb.INSTANCE_MARKER + "\nname: C\n" + _BLOCK_HEAD + '  - "127.0.0.1/32"\n'
    (tmp_path / "instance.yaml").write_text(text)
    real = yaml.safe_load
    seq = iter([real(text), {**real(text), "name": "other", "trusted_proxies": ["127.0.0.1/32", "203.0.113.5/32"]}])
    monkeypatch.setattr(yaml, "safe_load", lambda s: next(seq))
    assert cb.set_trusted_proxies(tmp_path, ["127.0.0.1/32", "203.0.113.5/32"]) is False
    assert (tmp_path / "instance.yaml").read_text() == text


@pytest.fixture
def no_yaml(monkeypatch):
    """PyYAML missing on the host, at call time (`import yaml` inside the function raises ImportError)."""
    monkeypatch.setitem(sys.modules, "yaml", None)


@pytest.mark.parametrize("text", [cb.INSTANCE_MARKER + "\nname: C\n...\n",              # a YAML document-end line
                                  cb.INSTANCE_MARKER + "\n{name: C}\n",                  # a root flow mapping
                                  cb.INSTANCE_MARKER + "\nnote: |\n  a block scalar\n",  # indented last line
                                  cb.INSTANCE_MARKER + "\nname: [C,\n  D]\n"])
def test_r1_without_pyyaml_a_block_is_not_appended_after_a_last_line_that_is_not_a_plain_key(tmp_path, capsys, no_yaml, text):
    (tmp_path / "instance.yaml").write_text(text)
    assert cb.set_trusted_proxies(tmp_path, ["127.0.0.1/32"]) is False
    assert (tmp_path / "instance.yaml").read_text() == text and "by hand" in capsys.readouterr().err


def test_r1_without_pyyaml_a_plain_last_line_still_gets_the_block_and_it_reads_back(tmp_path, no_yaml):
    (tmp_path / "instance.yaml").write_text(cb.INSTANCE_MARKER + "\nname: C\ntls_check: warn")        # no trailing newline either
    assert cb.set_trusted_proxies(tmp_path, ["127.0.0.1/32", "203.0.113.5/32"]) is True
    got = yaml.safe_load((tmp_path / "instance.yaml").read_text())
    assert got == {"name": "C", "tls_check": "warn", "trusted_proxies": ["127.0.0.1/32", "203.0.113.5/32"]}


def test_r1_without_pyyaml_the_text_check_still_refuses_a_block_that_does_not_read_back(tmp_path, monkeypatch, no_yaml):
    text = cb.INSTANCE_MARKER + "\nname: C\n" + _BLOCK_HEAD + '  - "127.0.0.1/32"\n'
    (tmp_path / "instance.yaml").write_text(text)
    monkeypatch.setattr(cb, "q", lambda a: '"tampered"')
    assert cb.set_trusted_proxies(tmp_path, ["127.0.0.1/32", "203.0.113.5/32"]) is False
    assert (tmp_path / "instance.yaml").read_text() == text


def test_r1_a_symlinked_instance_yaml_stays_a_link_and_the_target_changes(tmp_path):
    real_dir, home = tmp_path / "real", tmp_path / "home"
    real_dir.mkdir()
    home.mkdir()
    target = real_dir / "conf.yaml"
    target.write_text(cb.INSTANCE_MARKER + "\nname: C\n")
    target.chmod(0o640)
    (home / "instance.yaml").symlink_to(target)
    assert cb.set_trusted_proxies(home, ["127.0.0.1/32", "203.0.113.5/32"]) is True
    assert (home / "instance.yaml").is_symlink() and (home / "instance.yaml").resolve() == target.resolve()
    assert yaml.safe_load(target.read_text())["trusted_proxies"] == ["127.0.0.1/32", "203.0.113.5/32"]
    assert target.stat().st_mode & 0o777 == 0o640
    assert sorted(p.name for p in real_dir.iterdir()) == ["conf.yaml"] and sorted(p.name for p in home.iterdir()) == ["instance.yaml"]


def test_r1_up_says_so_when_it_leaves_a_hand_written_list_alone(tmp_path, monkeypatch, capsys):
    text = cb.INSTANCE_MARKER + "\nname: C\ntrusted_proxies:\n- 203.0.113.5/32\n"
    inst = _install(tmp_path, yaml_text=text)
    _fake_docker(monkeypatch, [])
    assert cb.refresh_trusted_proxies(inst) is False
    err = capsys.readouterr().err
    assert "by hand" in err and "instance.yaml" in err and (tmp_path / "instance.yaml").read_text() == text


def test_r1_upgrading_note_is_in_the_docs():
    text = (Path(__file__).resolve().parent.parent / "docs" / "configuration.md").read_text()
    assert "## Upgrading" in text and "trusted_proxies" in text.split("## Upgrading", 1)[1] and "./crewboss up" in text


# --- R3: acknowledging a decision needs the project's Admin/Owner and names the real person -------------------------------------

def test_r3_acknowledge_names_the_real_person_and_needs_project_admin(client, monkeypatch):
    owner, admin, member = _user("own", "owner"), _user("adm", "designer"), _user("mem", "designer")
    p = repo.create_project("ack-proj", "Ack", "internal", "owner", short_name="ack", session_roles=[], owner_user_id=owner["id"])
    with get_cursor() as cur:
        for u, role in ((admin, "admin"), (member, "viewer")):
            cur.execute("INSERT INTO project_members (project_id, user_id, role) VALUES (%s, %s, %s)", (p["id"], u["id"], role))
    c = changes.recommend(p["id"], "Chat call", "why", "claude:features")
    changes.decide(c["id"], "approved", "owner")
    with get_cursor() as cur:
        cur.execute("UPDATE change_log SET decided_via = 'session', decided_by = 'claude:all', owner_words = 'yes', "
                    "words_verified = true WHERE id = %s", (c["id"],))
    with pytest.raises(repo.Forbidden):
        changes.acknowledge(c["id"], repo.person_actor(member["id"]))
    r = _client(member).post(f"/ack-proj/changes/{c['id']}/acknowledge", headers=H)
    assert r.status_code in (403, 404) and changes.get(c["id"])["acknowledged_at"] is None
    r = _client(admin).post(f"/ack-proj/changes/{c['id']}/acknowledge", headers=H)
    assert r.status_code == 200 and changes.get(c["id"])["acknowledged_at"] is not None
    with get_cursor(commit=False) as cur:
        cur.execute("SELECT actor FROM activity WHERE verb = 'acknowledged' AND entity_id = %s", (c["id"],))
        assert [x["actor"] for x in cur.fetchall()] == [f"user:{admin['id']}"]          # not 'owner'
        cur.execute("SELECT summary FROM activity WHERE verb = 'acknowledged' AND entity_id = %s", (c["id"],))
        said = cur.fetchone()["summary"]
        assert "acknowledged by adm" in said and "by the owner" not in said               # names the Admin


# --- R4: a tapped answer is the person's, not "unconfirmed (assistant)" ---------------------------------------------------------

def test_r4_the_comment_for_a_tapped_answer_is_authored_by_the_person_and_not_marked_unconfirmed():
    assert jobs._brief_comment({"author": "user:5", "body": "Answer: Yes"})["body"] == "Answer: Yes"
    assert jobs._brief_comment({"author": "ca:5", "body": "I think so"})["body"].startswith("[unconfirmed (assistant)] ")   # CA's own text stays marked


# --- R5: the enrol rate limit believes X-Forwarded-For only from a trusted proxy -------------------------------------------------

def test_r5_runner_client_ip_ignores_xff_from_an_untrusted_peer(monkeypatch):
    assert runner_ws.client_ip(_req("192.0.2.50", "198.51.100.1, 203.0.113.9")) == "192.0.2.50"     # a client cannot pick its own bucket
    assert runner_ws.client_ip(_req("127.0.0.1", "198.51.100.1, 203.0.113.9")) == "203.0.113.9"
    monkeypatch.setattr(config, "TRUSTED_PROXIES", ("192.0.2.0/24",))
    assert runner_ws.client_ip(_req("192.0.2.50", "203.0.113.9")) == "203.0.113.9"
    assert runner_ws.client_ip(_req("192.0.2.50", "203.0.113.9")) == local_auth.client_ip(_req("192.0.2.50", "203.0.113.9"))


# --- R6: turning the session-pass switch ON needs a tap in the last five minutes -------------------------------------------------

def test_r6_session_passes_on_needs_a_tap_within_five_minutes(client, est, monkeypatch):
    got = _commissioned(est, monkeypatch, sessions=False)
    url = f"/acme/servers/{got['id']}/session-passes"
    owner = _user("boss6", "owner")
    c = _client(owner)
    with get_cursor() as cur:                                   # tapped an hour ago: inside the 12 h window, outside the 5 minutes
        cur.execute("UPDATE web_sessions SET second_factor_at = now() - interval '1 hour' WHERE user_id = %s", (owner["id"],))
    r = c.post(url, data={"allow": "1"}, headers=H)
    assert r.status_code == 403 and r.headers["HX-Trigger"] == "stepup-required" and _switch(got["id"]) is False
    with get_cursor() as cur:
        cur.execute("UPDATE web_sessions SET second_factor_at = now() WHERE user_id = %s", (owner["id"],))
    assert c.post(url, data={"allow": "1"}, headers=H, follow_redirects=False).status_code == 303 and _switch(got["id"]) is True
