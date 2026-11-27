"""Agent-setup scan (#646), server side: the `attention` frame may carry findings; only well-formed ones of a known rule are kept, a
block shows on the Runners tab as "needs attention" with the finding, and the how-to mentions `crewboss-runner doctor`.
(The runner stops claiming by itself: tests/test_runner_doctor.py.)"""
import json
import re

from app import runner_ws

from test_devices import _keypair, est, signed_in  # noqa: F401  (fixtures)
from test_runner_socket_jobs import _enrol_alice, _rpc  # noqa: F401
from test_jobs import world  # noqa: F401
from test_runner_ws import Runner
from test_web_auth import auth_on  # noqa: F401

GOOD = {"path": ".claude/settings.json", "rule": "perm.allow_all", "severity": "block", "hint": "allows every command without asking"}


def test_clean_findings_keeps_only_known_rules_and_short_strings():
    junk = [GOOD, {**GOOD, "rule": "evil.rule"}, {**GOOD, "severity": "meh"}, {**GOOD, "hint": 5}, "x", None,
            {**GOOD, "path": "p" * 500, "hint": "h" * 500}]
    kept = runner_ws.clean_findings(junk)
    assert [f["rule"] for f in kept] == ["perm.allow_all", "perm.allow_all"]
    assert len(kept[1]["path"]) == 140 and len(kept[1]["hint"]) == 100
    assert set(kept[0]) == {"rule", "severity", "path", "hint"}
    assert runner_ws.clean_findings("nope") == () and len(runner_ws.clean_findings([GOOD] * 50)) == runner_ws.MAX_FINDINGS


def test_attention_frame_with_a_block_finding(world, client):
    dev, key = _enrol_alice(world, "scan-pc")
    r = Runner(client, dev, key)
    r.handshake()
    try:
        extra = {**GOOD, "rule": "secret.aws_key", "path": ".env", "hint": "AWS access key id found (value not shown)", "severity": "block"}
        r.ws.send_text(json.dumps({"type": "attention", "kinds": ["agent_setup_blocked", "bogus"], "findings": [GOOD, extra]}))
        assert _rpc(r, type="claim")["ok"]
        assert runner_ws.attention(dev["device_id"]) == [runner_ws.ATTENTION_TEXT["agent_setup_blocked"]]
        assert [f["rule"] for f in runner_ws.findings(dev["device_id"])] == ["perm.allow_all", "secret.aws_key"]
        r.ws.send_text(json.dumps({"type": "attention", "kinds": [], "findings": []}))   # cleared
        assert _rpc(r, type="claim")["ok"] and runner_ws.findings(dev["device_id"]) == []
    finally:
        r.close()
    assert runner_ws.findings(dev["device_id"]) == []   # offline: nothing


def test_elevated_attention_kind_is_kept_and_has_words(world, client):
    """#972: a runner that finds itself elevated (administrator token) reports the fixed kind `elevated`; the page says jobs are refused."""
    assert "administrator" in runner_ws.ATTENTION_TEXT["elevated"] and "jobs refused" in runner_ws.ATTENTION_TEXT["elevated"]
    dev, key = _enrol_alice(world, "elevated-pc")
    r = Runner(client, dev, key)
    r.handshake()
    try:
        r.ws.send_text(json.dumps({"type": "attention", "kinds": ["elevated", "bogus"], "findings": []}))
        assert _rpc(r, type="claim")["ok"]
        assert runner_ws.attention(dev["device_id"]) == [runner_ws.ATTENTION_TEXT["elevated"]]
    finally:
        r.close()


def test_block_finding_shows_on_the_runners_tab(est, client, signed_in, monkeypatch):
    h = {"cookie": signed_in["cookie"]}
    code = re.search(r"data-code>([^<]+)<", client.post("/acme/runners/codes", data={"mode": "subscription"}, headers=h).text).group(1)
    _key, pub = _keypair()
    dev = client.post("/runner/enrol", json={"code": code, "pubkey": pub, "name": "pc-1", "platform": "linux"}).json()
    page = client.get("/acme/machines?tab=runners", headers=h).text
    assert "data-agent-findings" not in page and "data-attention-chip" not in page
    did = dev["device_id"]
    warn = {**GOOD, "rule": "plugin.enabled", "severity": "warn", "path": "~/.claude/settings.json", "hint": "2 plugin(s) enabled"}
    monkeypatch.setattr(runner_ws, "attention", lambda d: [runner_ws.ATTENTION_TEXT["agent_setup_blocked"]] if d == did else [])
    monkeypatch.setattr(runner_ws, "findings", lambda d: [GOOD, warn] if d == did else [])
    page = client.get("/acme/machines?tab=runners", headers=h).text
    assert "data-attention-chip" in page and "run `crewboss-runner doctor` there" in page and "safe" not in runner_ws.ATTENTION_TEXT["agent_setup_blocked"].replace("known-risky", "")
    assert "fix it in that repository" in runner_ws.ATTENTION_TEXT["repo_setup_blocked"]
    assert 'data-finding="perm.allow_all"' in page and 'data-severity="block"' in page and "blocks jobs" in page
    assert 'data-finding="plugin.enabled"' in page and 'data-severity="warn"' in page and "2 plugin(s) enabled" in page


def test_the_howto_mentions_doctor(est, client, signed_in):
    page = client.get("/acme/machines?tab=runners", headers={"cookie": signed_in["cookie"]}).text
    assert "data-doctor-hint" in page and "crewboss-runner doctor" in page and "crewboss-runner repair" in page
