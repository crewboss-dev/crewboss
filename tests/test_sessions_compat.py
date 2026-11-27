"""The session scripts take their settings from CREWBOSS_* environment and $CREWBOSS_HOME/sessions.env, and still understand the
pre-rename MALLEN_* variables that sessions started before task #589 carry (TODO(#589 compat): delete the legacy tests here with
the shim in sessions/streams.sh once every session was restarted)."""
import json
import os
import socket
import subprocess
from pathlib import Path

import pytest

from tests.test_session_scripts import StubDashboard

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def stub():
    s = StubDashboard()
    yield s
    s.close()


def _env(**kw):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("MALLEN_", "CREWBOSS_DASH", "CREWBOSS_STREAM", "CREWBOSS_PROJECT", "CREWBOSS_SESSION")) and k != "TMUX"}
    env.update(kw)
    return env


def _inbox(env):
    return subprocess.run(["bash", str(ROOT / "sessions/hooks/inbox.sh")], input="{}", capture_output=True, text=True,
                          timeout=20, env=env)


def test_a_session_with_the_new_variable_names_is_served(stub):
    out = _inbox(_env(CREWBOSS_STREAM="bugs", CREWBOSS_DASH_TOKEN="tok", CREWBOSS_DASH_URL=stub.url))
    assert "Owner commented on task #12" in out.stdout and stub.log[0].endswith("Bearer tok")


def test_a_session_started_before_the_rename_is_still_served(stub):
    out = _inbox(_env(MALLEN_STREAM="bugs", MALLEN_DASH_TOKEN="old-tok", MALLEN_DASH_URL=stub.url))
    assert "Owner commented on task #12" in out.stdout and stub.log[0].endswith("Bearer old-tok")


def test_the_new_name_wins_over_the_old_one(stub):
    out = _inbox(_env(CREWBOSS_STREAM="bugs", CREWBOSS_DASH_TOKEN="new", MALLEN_DASH_TOKEN="old", CREWBOSS_DASH_URL=stub.url))
    assert out.returncode == 0 and stub.log[0].endswith("Bearer new")


def test_sessions_env_in_the_instance_folder_supplies_the_dashboard_url(stub, tmp_path):
    (tmp_path / "sessions.env").write_text(f"# comment\nCREWBOSS_DASH_URL={stub.url}\nNOT_A_CREWBOSS_KEY=ignored\n")
    out = _inbox(_env(CREWBOSS_HOME=str(tmp_path), CREWBOSS_STREAM="bugs", CREWBOSS_DASH_TOKEN="tok"))
    assert "Owner commented on task #12" in out.stdout


def test_a_value_in_the_environment_beats_sessions_env(stub, tmp_path):
    (tmp_path / "sessions.env").write_text("CREWBOSS_DASH_URL=http://127.0.0.1:9\n")
    out = _inbox(_env(CREWBOSS_HOME=str(tmp_path), CREWBOSS_STREAM="bugs", CREWBOSS_DASH_TOKEN="tok", CREWBOSS_DASH_URL=stub.url))
    assert "Owner commented on task #12" in out.stdout


def test_sessions_env_is_data_never_executed(stub, tmp_path):
    marker = tmp_path / "pwned"
    (tmp_path / "sessions.env").write_text(f"CREWBOSS_DASH_URL=$(touch {marker})\nCREWBOSS_X=`touch {marker}`\n")
    _inbox(_env(CREWBOSS_HOME=str(tmp_path), CREWBOSS_STREAM="bugs", CREWBOSS_DASH_TOKEN="tok"))
    assert not marker.exists()


def test_the_briefing_takes_its_wording_from_the_instance_folder(stub, tmp_path):
    (tmp_path / "session-briefing.json").write_text(json.dumps({
        "node": "box-7", "default_project": "acme-app", "rule2_extra": " Never nag about the Acme board.",
        "coordinator_agents": ": acme-builder", "annotations_path": "/srv/acme/annotations.yaml"}))
    out = subprocess.run(["bash", str(ROOT / "sessions/hooks/ground.sh")], input="{}", capture_output=True, text=True, timeout=20,
                         env=_env(CREWBOSS_HOME=str(tmp_path), CREWBOSS_STREAM="all", CREWBOSS_DASH_TOKEN="tok",
                                  CREWBOSS_DASH_URL=stub.url))
    ctx = json.loads(out.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "session on box-7 (project: acme-app" in ctx and "Never nag about the Acme board." in ctx
    assert "(the Agent tool: acme-builder)" in ctx and "/srv/acme/annotations.yaml" in ctx
    assert "mallen" not in ctx.lower().replace(str(tmp_path).lower(), "")      # nothing install-specific is built into the script


def test_the_briefing_is_generic_without_an_instance_file(stub, tmp_path):
    out = subprocess.run(["bash", str(ROOT / "sessions/hooks/ground.sh")], input="{}", capture_output=True, text=True, timeout=20,
                         env=_env(CREWBOSS_HOME=str(tmp_path), CREWBOSS_STREAM="bugs", CREWBOSS_DASH_TOKEN="tok",
                                  CREWBOSS_DASH_URL=stub.url))
    ctx = json.loads(out.stdout)["hookSpecificOutput"]["additionalContext"]
    host = socket.gethostname().split(".")[0]                  # the node name defaults to this machine's
    assert f"session on {host} (project: unknown" in ctx
    assert "NO DRIFT" in ctx and "mallen" not in ctx.lower().replace(host.lower(), "").replace(str(tmp_path).lower(), "") and "erp-builder" not in ctx
