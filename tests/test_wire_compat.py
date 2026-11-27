"""The pre-rename wire names (task #589 part 3) are still ACCEPTED while new ones are issued.

DELETE THIS FILE together with the `TODO(#589 compat)` code it covers, once every live session, runner and script holds a
Crewboss-prefixed token and the web server config sets only `X-Crewboss-Front-Door`:
  - app/auth.py LEGACY_SESSION_PREFIXES / LEGACY_DEVICE_PREFIXES / LEGACY_JOB_PREFIXES (and `mdd` in app/deploys.py redaction),
  - app/main.py: the `x-mdd-front-door` reading,
  - sessions/crewboss-sessions.sh: `mdd_*` in mint_token's case.
`grep -rn "TODO(#589 compat)"` lists every place."""
import pytest

from app import auth
from app.db import get_cursor


def test_new_session_tokens_carry_the_crewboss_prefix(project):
    token = auth.issue_token("bugs", "wire-new", project_id=project["id"])
    assert token.startswith("cb_") and not token.startswith("mdd_")
    assert auth.resolve_token(token)["name"] == "wire-new"


def test_a_session_token_issued_before_the_rename_still_resolves(project):
    legacy = "mdd_" + "L" * 43
    with get_cursor() as cur:
        cur.execute("INSERT INTO session_tokens (stream, name, token_hash, project_id, agent) VALUES (%s, %s, %s, %s, %s)",
                    ("bugs", "wire-legacy", auth.hash_token(legacy), project["id"], "claude"))
    assert auth.resolve_token(legacy)["name"] == "wire-legacy"


def test_runner_tokens_of_both_prefixes_are_routed_to_the_runner_resolvers(monkeypatch):
    seen = []
    monkeypatch.setattr(auth, "_resolve_device", lambda t: seen.append(("device", t)))
    monkeypatch.setattr(auth, "_resolve_job", lambda t: seen.append(("job", t)))
    for t in ("cbr_a", "mdr_a", "cbj_a", "mdj_a"):
        auth.resolve_token(t)
    assert seen == [("device", "cbr_a"), ("device", "mdr_a"), ("job", "cbj_a"), ("job", "mdj_a")]
    assert auth.DEVICE_PREFIX == "cbr_" and auth.JOB_PREFIX == "cbj_"


@pytest.mark.parametrize("header", ["X-Crewboss-Front-Door", "X-MDD-Front-Door"])
def test_the_satellite_front_door_is_recognised_under_either_header(client, header):
    assert client.get("/api/tasks", headers={header: "satellite"}).status_code == 404
    assert client.get("/api/tasks", headers={header: "something-else"}).status_code != 404


def test_both_token_shapes_are_scrubbed_from_deploy_output():
    from app import deploys

    for tok in ("cb_" + "A" * 32, "cbr_" + "B" * 32, "cbj_" + "C" * 32, "mdd_" + "D" * 32, "mdj_" + "E" * 32):
        assert tok not in deploys.redact(f"oops {tok} leaked")
