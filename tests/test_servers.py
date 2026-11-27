"""Servers tab (task #300): read-only, from servers.yml + policy docs (mounted repo) and Zabbix (read-only)."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from app import config, servers as srv, tree, repo

YML = """
verified: 2026-10-13
verified_by: test
defaults:
  account: acme
  backup: {retention: {keep_daily: 7}}
  updates: {unattended_upgrades: on}
servers:
  - name: box1
    role: apps
    projects: [app]
    zabbix_host: box1.z
    backup: {schedule: {on_calendar: "*-*-* 03:30:00", sydney: daily 03:30}, conf_drift_vs_repo: none}
  - name: box2
    role: site
    zabbix_host: box2.z
    backup: {schedule: {on_calendar: "Sun *-*-* 03:00:00", sydney: weekly}, conf_drift_vs_repo: "script differs"}
  - name: other
    account: elsewhere
"""
UPD = """# Update policy - PROPOSAL (awaiting owner approval)
| Box | Window (Sydney) | Backup-first run at | Why | Today |
|---|---|---|---|---|
| box1 (apps) | Thu 22:00 | 21:45 | quiet | no schedule |
"""
NOW = datetime(2026, 10, 13, 8, 0, tzinfo=timezone.utc)


@pytest.fixture
def estate(tmp_path, monkeypatch):
    (tmp_path / "infra" / "backups").mkdir(parents=True)
    (tmp_path / "servers.yml").write_text(YML)
    (tmp_path / "infra" / "updates.md").write_text(UPD)
    (tmp_path / "infra" / "backups" / "strategy.md").write_text("# Backup strategy\n")
    monkeypatch.setattr(config, "ESTATE_DIR", str(tmp_path))
    srv._cache.update(at=0.0, key=None, data=None)
    return tmp_path


def _live(monkeypatch, hosts):
    monkeypatch.setattr(srv, "_fetch_live", lambda h: {"ok": True, "error": "", "hosts": hosts})


def _items(backup_age_h, reboot=0, sec=0, uptime_h=100):
    ts = (NOW - timedelta(hours=backup_age_h)).timestamp()
    return {"items": {srv.KEYS["backup"]: {"lastvalue": str(ts), "lastclock": "1"},
                      srv.KEYS["reboot"]: {"lastvalue": str(reboot), "lastclock": "1"},
                      srv.KEYS["sec_updates"]: {"lastvalue": str(sec), "lastclock": "1"},
                      srv.KEYS["uptime"]: {"lastvalue": str(uptime_h * 3600), "lastclock": "1"}},
            "problems": []}


def test_account_filter_windows_and_all_well_parts(estate, monkeypatch):
    _live(monkeypatch, {"box1.z": _items(5), "box2.z": _items(100)})
    s = srv.summary("acme", now=NOW)
    assert [c["name"] for c in s["servers"]] == ["box1", "box2"]
    b1 = s["servers"][0]
    assert b1["window"] == {"day": "Thu", "time": "22:00", "backup_first": "21:45", "status": "proposed"}
    assert b1["backup_ok"] and s["servers"][1]["backup_ok"]  # weekly box allows 8 days
    assert s["wrong"] == []
    assert any("drift" in t["what"] for t in s["todo"]) and any("restore drill" in t["what"] for t in s["todo"])


def test_stale_backup_reboot_and_problems(estate, monkeypatch):
    h = _items(30, reboot=1, sec=3)
    h["problems"] = [{"eventid": "1", "name": "Backup stale", "severity": "4", "clock": str(int(NOW.timestamp()))}]
    _live(monkeypatch, {"box1.z": h})
    s = srv.summary("acme", now=NOW)
    whats = [w["what"] for w in s["wrong"]]
    assert any("Backup is 30 h old" in w for w in whats) and "Backup stale" in whats
    assert any(t["server"] == "box2" and "can't see" in t["what"] for t in s["todo"])  # box2 missing from Zabbix
    assert any(t["server"] == "box1" and "Thu 22:00" in t["what"] for t in s["todo"])
    assert s["servers"][0]["security_updates"] == 3
    assert s["happened"][0]["what"] == "Backup finished"


def test_zabbix_down_is_shown_not_fatal(estate, monkeypatch):
    monkeypatch.setattr(srv, "_fetch_live", lambda h: {"ok": False, "error": "boom", "hosts": {}})
    s = srv.summary("acme", now=NOW)
    assert any("Live status unavailable" in w["what"] for w in s["wrong"])


def test_page_account_only(client, estate, monkeypatch):
    _live(monkeypatch, {"box1.z": _items(5), "box2.z": _items(5)})
    acct = repo.create_project("acme", "Acme", "client", "owner", short_name="acme", session_roles=["all"])
    tree.set_place(acct["id"], "owner", None, "account")
    app_ = repo.create_project("acme-app", "Acme App", "client", "owner", short_name="acmeapp", session_roles=[])
    tree.set_place(app_["id"], "owner", acct["id"], "project")
    html = client.get("/acme/servers").text
    assert 'data-servers-page' in html and 'data-server="box1"' not in html      # the estate stub is gone (#810)
    assert client.get("/acme-app/servers").status_code == 404


def test_missing_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ESTATE_DIR", str(tmp_path / "nope"))
    assert srv.summary("acme")["configured"] is False


def test_account_without_servers_gets_empty_state_not_other_accounts_data(estate, monkeypatch):
    # #366: a new account must never see the estate owner's servers or policy documents.
    monkeypatch.setattr(srv, "_fetch_live", lambda h: pytest.fail("no Zabbix call for an account with no servers"))
    s = srv.summary("newco", now=NOW)
    assert s["configured"] and s.get("empty") and s["servers"] == []
    assert s["strategy_md"] == "" and s["updates_md"] == "" and not s["wrong"] and not s["todo"]


def test_policy_docs_only_for_the_estate_owner(estate, monkeypatch):
    _live(monkeypatch, {})
    owner, other = srv.summary("acme", now=NOW), srv.summary("elsewhere", now=NOW)
    assert "Backup strategy" in owner["strategy_md"] and "PROPOSAL" in owner["updates_md"]
    assert [c["name"] for c in other["servers"]] == ["other"]
    assert other["strategy_md"] == "" and other["updates_md"] == ""


def test_owner_policy_residue_never_reaches_another_account(estate, monkeypatch):
    # task #368: the owner's infra/updates.md (reboot windows, proposal status) and the owner's own wording (its
    # Zabbix host group, its strategy doc) only ever apply to the owner's account (servers.yml defaults.account).
    (estate / "servers.yml").write_text(YML.replace("  - name: other\n    account: elsewhere\n",
                                                    "  - name: other\n    account: elsewhere\n    zabbix_host: other.z\n"))
    (estate / "infra" / "updates.md").write_text(UPD + "| other (x) | Mon 01:00 | 00:45 | quiet | none |\n")
    _live(monkeypatch, {"box1.z": _items(5, reboot=1)})  # Zabbix answers, but can't see other.z
    owner, other = srv.summary("acme", now=NOW), srv.summary("elsewhere", now=NOW)
    assert owner["updates_status"] == "proposed" and owner["owns_docs"] is True
    assert next(c for c in owner["servers"] if c["name"] == "box1")["window"]["day"] == "Thu"
    card = other["servers"][0]
    assert card["window"] is None and other["updates_status"] == "" and other["owns_docs"] is False
    text = json.dumps(other["todo"])
    assert "Mallen Servers" not in text and "strategy.md" not in text and "#299" not in text
    assert "No live status" in text and "No restore drill" in text      # the same to-dos, in neutral words


def test_page_empty_account_shows_no_estate_content(client, estate, monkeypatch):
    _live(monkeypatch, {"box1.z": _items(5), "box2.z": _items(5)})
    acct = repo.create_project("newco", "NewCo", "client", "owner", short_name="newco", session_roles=["all"])
    tree.set_place(acct["id"], "owner", None, "account")
    html = client.get("/newco/servers").text
    assert "data-servers-empty" in html
    for leak in ('data-server="box1"', "box2", "Backup strategy", "Update policy", 'data-doc='):
        assert leak not in html
