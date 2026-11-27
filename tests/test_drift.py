"""No drift (#402): the dashboard checks each linked admin site's catalog against what its app runs (from the project's
own health feed + a read-only copy of its repo), and flags workflow changes left undeployed. A throwaway git repo plays
the ERP's; the ERP feed snapshot is stored the way erp_health.sync stores it. Nothing calls an admin site."""
import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from psycopg.types.json import Json

from app import config, drift, erp_health, health_reports, plan, repo
from app.db import get_cursor

ROOT = Path(__file__).resolve().parent.parent


def _git(repo_dir, *args):
    return subprocess.run(["git", "-C", str(repo_dir), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


class Repo:
    """A tiny ERP-shaped repo; commit() writes files and returns the new sha."""

    def __init__(self, path: Path):
        self.path = path
        path.mkdir()
        _git(path, "init", "-q")

    def commit(self, files: dict, when: str = "2026-10-15T00:00:00+00:00") -> str:
        for rel, text in files.items():
            p = self.path / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        _git(self.path, "add", "-A")
        subprocess.run(["git", "-C", str(self.path), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                        "commit", "-qm", "c"], check=True, capture_output=True,
                       env={"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when, "PATH": "/usr/bin:/bin"})
        return _git(self.path, "rev-parse", "HEAD")


@pytest.fixture(autouse=True)
def _no_live_zabbix(monkeypatch, tmp_path):
    """No test reaches the real Zabbix or estate repo: tests that want them patch in fakes."""
    monkeypatch.setattr(config, "ZABBIX_TOKEN_FILE", str(tmp_path / "no-zabbix-token"))
    monkeypatch.setattr(config, "ESTATE_DIR", str(tmp_path / "no-estate"))


@pytest.fixture
def erp_repo(tmp_path):
    r = Repo(tmp_path / "erp")
    r.c1 = r.commit({"app/workers/w.py": "def main():\n    return 1\n", "README.md": "x"})
    r.assets = r.commit({"app/generated/catalog.json": "{}", "app/static/canvas/x.js": "1", "README.md": "y"})
    r.code = r.commit({"app/workers/w.py": "def main():\n    return 2\n"})
    return r


@pytest.fixture
def cfg(project, erp_repo, monkeypatch):
    """The test project plays the ERP: its health still fetched (pull), its admin site linked."""
    monkeypatch.setattr(config, "HEALTH_PROJECT", project["slug"])
    health_reports.use_config({project["slug"]: {"mode": "pull", "expect_every_seconds": 120, "sources": [],
                                                 "erp": True, "erp_source": None}})
    site = {"repo": str(erp_repo.path), "catalog_paths": ["app", "connector", "aios", "scanner"],
            "exclude_paths": ["app/generated/", "app/static/canvas/"], "undeployed_hours": 24.0}
    yield {"admin_sites": {project["slug"]: site}, "os_timers": []}
    health_reports.use_config(None)


def _feed(app_sha=None, catalog=None, **extra):
    body = {"v": 1, "generated_at": datetime.now(timezone.utc).isoformat(), "events": [], **extra}
    if app_sha:
        body["app_sha"] = app_sha
    if catalog is not None:
        body["catalog"] = catalog
    with get_cursor() as cur:
        cur.execute("INSERT INTO health_sources (source, snapshot, generated_at) VALUES (%s, %s, now()) "
                    "ON CONFLICT (source) DO UPDATE SET snapshot = EXCLUDED.snapshot", (erp_health.SOURCE, Json(body)))


def _outbox():
    with get_cursor() as cur:
        cur.execute("SELECT message, dedupe_key FROM outbox WHERE stream = 'errors' ORDER BY id")
        return cur.fetchall()


# --- the rule ------------------------------------------------------------------------------------------------------

def test_no_feed_yet_is_off_and_quiet(cfg, project):
    assert drift.sync(cfg=cfg)["ok"]
    s = drift.summary()
    assert s["admin"][0]["state"] == "off" and "hasn't been fetched" in s["admin"][0]["word"]
    assert s["open"] == [] and _outbox() == []


def test_fields_absent_until_the_erp_ships_them_are_not_reported(cfg, project):
    _feed()
    drift.sync(cfg=cfg)
    a = drift.summary()["admin"][0]
    assert a["state"] == "not_reported" and "app_sha or catalog.commit" in a["word"]
    assert drift.summary()["open"] == []


def test_catalog_one_commit_behind_the_app_is_normal_when_only_assets_changed(cfg, project, erp_repo):
    """The release builds the catalog for the code commit and ships it in a 'release assets' commit right after."""
    _feed(app_sha=erp_repo.assets, catalog={"commit": erp_repo.c1, "built_at": "2026-10-15T00:00:00Z"})
    drift.sync(cfg=cfg)
    a = drift.summary()["admin"][0]
    assert a["state"] == "ok" and a["files"] == [] and drift.summary()["open"] == []


def test_workflow_code_changed_since_the_catalog_is_an_error_sent_once_then_recovers(cfg, project, erp_repo):
    _feed(app_sha=erp_repo.code, catalog={"commit": erp_repo.c1, "built_at": "x"})
    drift.sync(cfg=cfg)
    s = drift.summary()
    assert s["admin"][0]["state"] == "behind" and s["admin"][0]["files"] == ["app/workers/w.py"]
    assert len(s["errors"]) == 1 and drift.BEHIND_TITLE in s["errors"][0]["name"]
    assert s["errors"][0]["host"] == project["slug"]
    sent = _outbox()
    assert len(sent) == 1 and sent[0]["dedupe_key"].startswith("drift:") and drift.BEHIND_TITLE in sent[0]["message"]
    drift.sync(cfg=cfg)                                    # still behind: no second push
    assert len(_outbox()) == 1
    _feed(app_sha=erp_repo.code, catalog={"commit": erp_repo.code, "built_at": "y"})
    drift.sync(cfg=cfg)
    assert drift.summary()["open"] == []
    msgs = [r["message"] for r in _outbox()]
    assert len(msgs) == 1 and "RESOLVED" in msgs[0]          # same dedupe key: the undelivered alert is replaced


def test_a_commit_the_copy_lacks_is_a_warning_never_an_error(cfg, project, erp_repo):
    _feed(app_sha="f" * 40, catalog={"commit": erp_repo.c1})
    drift.sync(cfg=cfg)
    s = drift.summary()
    assert s["admin"][0]["state"] == "unverified" and "isn't in the dashboard's copy" in s["admin"][0]["word"]
    assert s["errors"] == [] and len(s["warnings"]) == 1 and _outbox() == []


def test_an_unmounted_repo_is_a_warning(cfg, project, erp_repo, tmp_path):
    cfg["admin_sites"][project["slug"]]["repo"] = str(tmp_path / "nowhere")
    _feed(app_sha=erp_repo.code, catalog={"commit": erp_repo.c1})
    drift.sync(cfg=cfg)
    s = drift.summary()
    assert s["admin"][0]["state"] == "unverified" and "isn't mounted" in s["admin"][0]["word"]
    assert s["errors"] == []


def _scan(project, commit, hours_ago):
    repo.import_catalog(project["slug"], {"project": "acme-erp", "commit": commit,
                                          "generated_at": "2026-10-15T00:00:00+00:00", "workflows": []})
    with get_cursor() as cur:
        cur.execute("UPDATE catalog_scans SET created_at = now() - make_interval(hours => %s) WHERE id = "
                    "(SELECT max(id) FROM catalog_scans)", (hours_ago,))


def test_workflow_changes_on_main_undeployed_for_a_day_are_a_warning(cfg, project, erp_repo):
    _feed(app_sha=erp_repo.c1, catalog={"commit": erp_repo.c1})
    _scan(project, erp_repo.c1, 50)
    _scan(project, erp_repo.assets, 40)                    # assets only: not a workflow change
    _scan(project, erp_repo.code, 30)                      # first seen on main 30h ago
    drift.sync(cfg=cfg)
    s = drift.summary()
    assert s["admin"][0]["state"] == "ok"
    assert s["admin"][0]["pending"]["first"] == erp_repo.code and 29 < s["admin"][0]["pending"]["hours"] < 31
    assert [w["name"] for w in s["warnings"]] and "aren't deployed" in s["warnings"][0]["name"]
    assert s["errors"] == [] and _outbox() == []


def test_recent_undeployed_changes_are_fine(cfg, project, erp_repo):
    _feed(app_sha=erp_repo.c1, catalog={"commit": erp_repo.c1})
    _scan(project, erp_repo.code, 2)
    drift.sync(cfg=cfg)
    s = drift.summary()
    assert s["admin"][0]["pending"]["hours"] < 3 and s["open"] == []


def test_another_project_links_by_config_alone_and_its_pushed_report_counts(cfg, project, erp_repo):
    """A project that pushes its health (#404): a health_feeds entry + a drift.yaml entry, no code."""
    import uuid
    from app.auth import issue_token, resolve_token
    other = repo.create_project("portal", "Portal", "internal", "system")
    feeds = dict(health_reports.feeds())
    feeds["portal"] = {"mode": "push", "expect_every_seconds": 120, "sources": [], "erp": False, "erp_source": None}
    health_reports.use_config(feeds)
    cfg["admin_sites"]["portal"] = dict(cfg["admin_sites"][project["slug"]])
    token = resolve_token(issue_token("reporter", "portal-web-1", project_id=other["id"]))
    now = datetime.now(timezone.utc)
    batch = {"reports": [{"report_id": str(uuid.uuid4()), "source": "web-1", "observed_at": now.isoformat(),
                          "health": {"v": 1, "app_sha": erp_repo.code, "catalog": {"commit": erp_repo.c1}}}]}
    assert health_reports.receive(token, json.dumps(batch).encode())["accepted"] == 1
    drift.sync(cfg=cfg)
    by = {a["project"]: a for a in drift.summary()["admin"]}
    assert by["portal"]["state"] == "behind"
    assert by[project["slug"]]["state"] == "off"


def test_drift_warnings_dont_outrank_planned_work_but_errors_do(cfg, project, erp_repo):
    _feed(app_sha="f" * 40, catalog={"commit": erp_repo.c1})
    drift.sync(cfg=cfg)
    with get_cursor() as cur:
        assert plan._open_health(cur) == []
    _feed(app_sha=erp_repo.code, catalog={"commit": erp_repo.c1})
    drift.sync(cfg=cfg)
    with get_cursor() as cur:
        assert [r["source"] for r in plan._open_health(cur)] == ["drift"]


# --- config + page -------------------------------------------------------------------------------------------------

def test_the_reviewed_config_links_the_erp_with_its_deploy_check_paths():
    c = drift.load_config(str(ROOT / "tests/home/drift.yaml"))
    erp = c["admin_sites"]["acme-erp"]
    assert erp["catalog_paths"] == ["app", "connector", "aios", "scanner"]
    feeds = health_reports.load_config(str(ROOT / "tests/home/health_feeds.yaml"))
    assert feeds["acme-erp"]["erp"] and feeds["acme-erp"]["mode"] in ("pull", "push")
    assert erp["exclude_paths"] == ["app/generated/", "app/static/canvas/"]
    from scanner import scan
    assert erp["repo"] == scan.PROJECT_REPOS["acme-erp"]   # the CLI and the health check read the same repo


def test_health_page_shows_the_admin_site_in_plain_words(client, cfg, project, erp_repo):
    _feed(app_sha=erp_repo.code, catalog={"commit": erp_repo.c1})
    drift.sync(cfg=cfg)
    html = client.get(f"/{project['slug']}/health").text
    assert "data-drift-section" in html, html[-3000:]
    assert drift.BEHIND_TITLE.replace("'", "&#39;") in html
    assert f'data-drift-admin-site="{project["slug"]}" data-state="behind"' in html
    _feed()
    drift.sync(cfg=cfg)
    html = client.get(f"/{project['slug']}/health").text
    assert "not reported" in html and "Nothing has drifted." in html


# --- server timers -------------------------------------------------------------------------------------------------

SERVERS_YML = """
defaults: {account: example}
servers:
  - {name: core-1, zabbix_host: core-1-syd.example.internal}
  - {name: ha-1, zabbix_host: ha-1.example.internal}
  - {name: pbx-1, zabbix_host: pbx-1.example.internal}
  - {name: mon-2, zabbix_host: mon-2.syd.example.com}
"""


def _discovery(*units, inactive=()):
    rows = [{"{#UNIT.NAME}": u, "{#UNIT.ACTIVESTATE}": "active", "{#UNIT.UNITFILESTATE}": "enabled"} for u in units]
    rows += [{"{#UNIT.NAME}": u, "{#UNIT.ACTIVESTATE}": "inactive", "{#UNIT.UNITFILESTATE}": "disabled"}
             for u in inactive]
    return json.dumps(rows)


class FakeZabbix:
    """Read-only, like the real client: host.get + item.get only."""

    def __init__(self, items, visible=("core-1-syd.example.internal", "ha-1.example.internal", "pbx-1.example.internal")):
        self.hosts = [{"hostid": str(i + 1), "host": h} for i, h in enumerate(visible)]
        self.items = items        # zabbix host -> item fields
        self.calls = []

    def call(self, method, params):
        self.calls.append(method)
        assert method.endswith(".get")
        if method == "host.get":
            return [h for h in self.hosts if h["host"] in params["filter"]["host"]]
        ids = {h["host"]: h["hostid"] for h in self.hosts}
        assert params["filter"] == {"key_": drift.TIMER_KEY}
        return [dict(it, hostid=ids[h]) for h, it in self.items.items() if h in ids]


@pytest.fixture
def estate(tmp_path, monkeypatch):
    (tmp_path / "servers.yml").write_text(SERVERS_YML)
    monkeypatch.setattr(config, "ESTATE_DIR", str(tmp_path))


def _zabbix(monkeypatch, fake):
    from app import health
    monkeypatch.setattr(health, "client_from_config", lambda: fake)


def _annotated(project, box_jobs):
    repo.import_catalog(project["slug"], {
        "project": "acme-erp", "commit": "c", "generated_at": "2026-10-15T00:00:00+00:00",
        "workflows": [{"id": "infrastructure.x.run", "name": "Infrastructure › X › Run", "steps": [],
                       "box_jobs": box_jobs}]})


def test_an_unannotated_timer_on_a_server_is_a_warning(cfg, project, estate, monkeypatch):
    cfg["os_timers"] = ["apt-daily.timer", "logrotate.timer"]
    _annotated(project, [{"unit": "netbird-watchdog.timer", "hosts": ["core-1", "ha-1"]},
                         {"unit": "everywhere.timer"}])
    _zabbix(monkeypatch, FakeZabbix({
        "core-1-syd.example.internal": {"lastvalue": _discovery("apt-daily.timer", "netbird-watchdog.timer",
                                                          "example-backup.timer", "everywhere.timer",
                                                          inactive=["old.timer"]),
                                  "lastclock": "1760000000", "state": "0", "error": ""},
        "ha-1.example.internal": {"lastvalue": "", "lastclock": "0", "state": "0", "error": ""},
        "pbx-1.example.internal": {"lastvalue": "", "lastclock": "0", "state": "1",
                             "error": "Unsupported item key."},
    }))
    drift.sync(cfg=cfg)
    s = drift.summary()
    names = sorted(w["name"] for w in s["warnings"])
    assert names == [
        "Automation on core-1 with no diagram: example-backup.timer",
        "Can't check server automation on mon-2: the dashboard's Zabbix login can't see it",
        "Can't list the timers on pbx-1: Zabbix says Unsupported item key.",
    ]
    assert s["errors"] == [] and _outbox() == []           # warnings only show
    hosts = {h["box"]: h for h in s["timers"]["hosts"]}
    assert hosts["core-1"]["uncovered"] == ["example-backup.timer"] and "old.timer" not in hosts["core-1"]["timers"]
    assert hosts["ha-1"]["state"] == "waiting" and hosts["mon-2"]["state"] == "invisible"


def test_one_warning_per_unit_names_every_server_it_runs_on(cfg, project, estate, monkeypatch):
    item = {"lastvalue": _discovery("example-backup.timer"), "lastclock": "1", "state": "0", "error": ""}
    _zabbix(monkeypatch, FakeZabbix({"core-1-syd.example.internal": item, "ha-1.example.internal": item,
                                     "pbx-1.example.internal": item}))
    drift.sync(cfg=cfg)
    backup = [w["name"] for w in drift.summary()["warnings"] if "example-backup" in w["name"]]
    assert backup == ["Automation on core-1, ha-1, pbx-1 with no diagram: example-backup.timer"]


def test_a_diagram_for_the_timer_clears_the_warning(cfg, project, estate, monkeypatch):
    item = {"lastvalue": _discovery("example-backup.timer"), "lastclock": "1", "state": "0", "error": ""}
    _zabbix(monkeypatch, FakeZabbix({"core-1-syd.example.internal": item}, visible=("core-1-syd.example.internal",
                                                                             "ha-1.example.internal",
                                                                             "pbx-1.example.internal",
                                                                             "mon-2.syd.example.com")))
    drift.sync(cfg=cfg)
    assert [w["name"] for w in drift.summary()["warnings"] if "example-backup" in w["name"]]
    _annotated(project, [{"unit": "example-backup.timer", "hosts": ["core-1"]}])
    drift.sync(cfg=cfg)
    assert not [w for w in drift.summary()["warnings"] if "example-backup" in w["name"]]


def test_zabbix_down_keeps_the_last_timer_findings(cfg, project, estate, monkeypatch):
    item = {"lastvalue": _discovery("example-backup.timer"), "lastclock": "1", "state": "0", "error": ""}
    _zabbix(monkeypatch, FakeZabbix({"core-1-syd.example.internal": item}))
    drift.sync(cfg=cfg)
    before = sorted(w["name"] for w in drift.summary()["warnings"])

    class Down:
        def call(self, method, params):
            raise OSError("connection refused")

    _zabbix(monkeypatch, Down())
    drift.sync(cfg=cfg)
    s = drift.summary()
    assert sorted(w["name"] for w in s["warnings"]) == before
    assert s["timers"]["state"] == "error" and "didn't answer" in s["timers"]["word"]


def test_the_reviewed_os_list_and_box_jobs_cover_the_known_estate_timers():
    from scanner.lib import schema
    c = drift.load_config(str(ROOT / "tests/home/drift.yaml"))
    assert {"apt-daily.timer", "logrotate.timer", "fstrim.timer", "man-db.timer", "e2scrub_all.timer",
            "motd-news.timer", "systemd-tmpfiles-clean.timer", "dpkg-db-backup.timer"} <= set(c["os_timers"])
    assert not [u for u in c["os_timers"] if u.startswith("example-")]   # our own jobs always need a diagram
    # (the check that THIS install's annotations cover its estate timers lives in its instance folder, tests/)
    problems = []
    schema.validate_workflow({"id": "a.b.c", "name": "A › B › C", "group": "Infrastructure",
                              "subproject": "Infrastructure", "summary": "s", "confidence": "annotated",
                              "trigger": {"type": "timer"}, "steps": [{"id": "t", "kind": "trigger", "label": "T",
                                                                     "system": "internal",
                                                                     "code_ref": {"file": "f", "function": "g"}}],
                              "box_jobs": [{"unit": "not a unit"}, {"unit": "x.timer", "hosts": []}]}, problems)
    assert len([p for p in problems if "box_jobs" in p]) == 2


def test_health_page_lists_each_server_s_timers(client, cfg, project, estate, monkeypatch):
    item = {"lastvalue": _discovery("example-backup.timer", "apt-daily.timer"), "lastclock": "1", "state": "0",
            "error": ""}
    cfg["os_timers"] = ["apt-daily.timer"]
    _zabbix(monkeypatch, FakeZabbix({"core-1-syd.example.internal": item}))
    drift.sync(cfg=cfg)
    html = client.get(f"/{project['slug']}/health").text
    assert 'data-drift-box="core-1" data-state="ok"' in html and "no diagram: example-backup.timer" in html
    assert "Automation on core-1 with no diagram: example-backup.timer" in html
    assert 'data-drift-box="mon-2" data-state="invisible"' in html


# --- #828 piece 4: the slim version endpoint -----------------------------------------------------------------------

class _Resp:
    def __init__(self, body):
        self._raw = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self, n=-1):
        return self._raw


@pytest.fixture
def slim(cfg, project, monkeypatch, tmp_path):
    """The site names a version_url; erp_health._open (the HTTP) is replaced, recording the request."""
    tok = tmp_path / "read-token"
    tok.write_text("tok-abc\n")
    monkeypatch.setattr(config, "ERP_HEALTH_READ_TOKEN_FILE", str(tok))
    monkeypatch.setattr(config, "ERP_HEALTH_URL", "https://erp.example.test")
    site = next(iter(cfg["admin_sites"].values()))
    site["version_url"] = "https://erp.example.test/api/health/release"
    seen = []
    answer = {"body": {}}

    def fake_open(req, timeout):
        seen.append((req.full_url, req.get_header("Authorization")))
        if isinstance(answer["body"], Exception):
            raise answer["body"]
        return _Resp(answer["body"])

    monkeypatch.setattr(erp_health, "_open", fake_open)
    return type("S", (), {"seen": seen, "answer": answer})


def test_a_version_url_is_read_with_the_erp_bearer_and_its_app_sha_is_used(cfg, project, erp_repo, slim):
    _feed(app_sha=erp_repo.c1, catalog={"commit": erp_repo.c1, "built_at": "x"})     # the report says in step...
    slim.answer["body"] = {"app_sha": erp_repo.code, "catalog_sha": erp_repo.c1}      # ...the endpoint says behind
    drift.sync(cfg=cfg)
    a = drift.summary()["admin"][0]
    assert a["state"] == "behind" and a["app_sha"] == erp_repo.code and a["files"] == ["app/workers/w.py"]
    assert slim.seen == [("https://erp.example.test/api/health/release", "Bearer tok-abc")]


def test_an_unreachable_version_url_falls_back_to_the_health_report(cfg, project, erp_repo, slim):
    _feed(app_sha=erp_repo.code, catalog={"commit": erp_repo.code, "built_at": "x"})
    slim.answer["body"] = erp_health.FeedError("HTTP 500")
    drift.sync(cfg=cfg)
    assert drift.summary()["admin"][0]["state"] == "ok" and len(slim.seen) == 1


def test_without_a_version_url_the_report_path_is_unchanged_and_nothing_is_fetched(cfg, project, erp_repo, slim):
    next(iter(cfg["admin_sites"].values()))["version_url"] = None
    _feed(app_sha=erp_repo.code, catalog={"commit": erp_repo.c1, "built_at": "x"})
    drift.sync(cfg=cfg)
    assert drift.summary()["admin"][0]["state"] == "behind" and slim.seen == []


def test_version_erp_derives_the_url_from_the_erp_health_url(monkeypatch):
    monkeypatch.setattr(config, "ERP_HEALTH_URL", "https://erp.example.test")
    assert drift.version_url({"version": "erp"}) == "https://erp.example.test/api/health/release"
    assert drift.version_url({"version": "erp", "version_url": "https://x.test/v"}) == "https://x.test/v"
    assert drift.version_url({}) is None


def test_version_settings_load_from_drift_yaml(tmp_path):
    p = tmp_path / "drift.yaml"
    p.write_text("admin_sites:\n  a: {repo: /r, version_url: 'https://e.test/api/health/release'}\n  b: {repo: /r, version: erp}\n")
    sites = drift.load_config(str(p))["admin_sites"]
    assert sites["a"]["version_url"] == "https://e.test/api/health/release" and sites["b"]["version"] == "erp"


def test_a_version_url_on_another_host_never_gets_the_token(cfg, project, erp_repo, slim, caplog):
    _feed(app_sha=erp_repo.code, catalog={"commit": erp_repo.code, "built_at": "x"})
    next(iter(cfg["admin_sites"].values()))["version_url"] = "https://evil.example.org/api/health/release"
    slim.answer["body"] = {"app_sha": erp_repo.c1, "catalog_sha": erp_repo.c1}
    with caplog.at_level("WARNING"):
        drift.sync(cfg=cfg)
    assert slim.seen == []                                                         # no request at all, so no bearer
    assert drift.summary()["admin"][0]["state"] == "ok"                            # the report path decided
    assert "evil.example.org" in caplog.text and "tok-abc" not in caplog.text


@pytest.mark.parametrize("url", ["https://erp.example.test:8443/api/health/release", "http://erp.example.test/api/health/release",
                                 "https://erp.example.test.evil.org/x", "https://user@erp.example.test@evil.org/x"])
def test_only_the_erps_own_scheme_host_and_port_count_as_same_origin(monkeypatch, url):
    monkeypatch.setattr(config, "ERP_HEALTH_URL", "https://erp.example.test")
    assert erp_health.same_origin(url) is False
    assert erp_health.same_origin("https://ERP.example.test:443/api/health/release") is True
    with pytest.raises(erp_health.FeedError):
        erp_health.fetch_json(url)


def test_an_unset_erp_url_trusts_no_version_url(monkeypatch):
    monkeypatch.setattr(config, "ERP_HEALTH_URL", "")
    assert erp_health.same_origin("https://erp.example.test/x") is False


def test_a_half_version_answer_falls_back_to_the_report(cfg, project, erp_repo, slim):
    _feed(app_sha=erp_repo.code, catalog={"commit": erp_repo.code, "built_at": "x"})     # the report: in step
    slim.answer["body"] = {"app_sha": erp_repo.c1}                                       # app_sha only: not used
    drift.sync(cfg=cfg)
    a = drift.summary()["admin"][0]
    assert len(slim.seen) == 1 and a["state"] == "ok" and a["app_sha"] == erp_repo.code
