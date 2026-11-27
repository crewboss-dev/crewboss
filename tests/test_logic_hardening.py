"""The logic gate cannot be talked past (review of #715): frozen reports, who reported, production-only baseline, validated writes, bad stored
rows that never 500, racing overrides, the CLI inside a repo with its own `app` package, and a scanner that survives hostile input."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app import deploys, logic, logic_map as lm, repo
from app.db import get_cursor

from test_logic import SHA1, SHA2, SHA3, P, _client, _enforce, _promotable, _release, _report, _wf, local, w  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent


def _prod_done(w, sha):
    with get_cursor() as cur:
        cur.execute("INSERT INTO server_jobs (account_id, project_id, target_name, sha, state, env, finished_at) VALUES "
                    "(%s, %s, 'live', %s, 'done', 'prod', now())", (w["acct"]["id"], w["proj"]["id"], sha))


# --- P2-1: the probe, immutability, who reported, the baseline -------------------------------------------------------------------

def test_the_probe_a_refused_commit_cannot_be_rescued_by_posting_an_empty_report(w):
    pid = w["proj"]["id"]
    _enforce(w)
    _promotable(w, SHA1)
    logic.record_report(pid, SHA1, _report(errors=["x.yaml: broken"]), "token:attacker")
    with pytest.raises(repo.BadRequest, match="schema check"):
        _release(w, SHA1, who="member")                                                # refused: the gate judged this commit
    with pytest.raises(repo.Conflict, match="no longer change"):
        logic.record_report(pid, SHA1, _report(), "token:attacker")                    # the swap is refused
    with pytest.raises(repo.BadRequest, match="schema check"):
        _release(w, SHA1, who="member")                                                # still refused
    assert logic.get_report(pid, SHA1)["errors"]


def test_a_commit_with_a_production_request_is_frozen_even_with_the_gate_off(w):
    pid = w["proj"]["id"]
    _promotable(w, SHA1)
    logic.record_report(pid, SHA1, _report(uncovered=["a"]), "job:1")
    assert _release(w, SHA1)["state"] == "requested"
    with pytest.raises(repo.Conflict):
        logic.record_report(pid, SHA1, _report(), "job:2")
    assert logic.record_report(pid, SHA2, _report(), "job:2")["sha"] == SHA2            # the next commit is fine


def test_the_reporter_is_stored_and_shown_next_to_the_logic_chip(w):
    pid = w["proj"]["id"]
    logic.record_report(pid, SHA1, _report(), "job:7")
    logic.record_report(pid, SHA2, _report(), "token:ci-main")
    assert logic.release_diff(pid, SHA1)["reported_by"] == "job #7" and logic.release_diff(pid, SHA2)["reported_by"] == "token:ci-main"
    _promotable(w, SHA1)
    _promotable(w, SHA2)
    page = _client(w["creator"]).get(f"/{w['proj']['slug']}/releases").text
    assert "reported by job #7" in page and "reported by token:ci-main" in page
    assert "reported by token:ci-main" in _client(w["creator"]).get(f"/{w['proj']['slug']}/logic").text
    assert "Reported by token:ci-main" in _client(w["creator"]).get(f"/{w['proj']['slug']}/logic/diff?sha={SHA2}").text


def test_the_baseline_is_a_released_commit_only_never_the_newest_other_report(w):
    pid = w["proj"]["id"]
    logic.record_report(pid, SHA2, _report([_wf("decoy")]), "token:x")                 # posted by someone, never released
    with get_cursor(commit=False) as cur:
        assert logic.baseline_report(cur, pid, SHA1) is None
    assert logic.release_diff(pid, SHA1) is None
    logic.record_report(pid, SHA1, _report([_wf("real")]))
    first = logic.release_diff(pid, SHA1)
    assert first["first"] and first["baseline_sha"] is None                              # the decoy is not a baseline
    _prod_done(w, SHA1)
    logic.record_report(pid, SHA3, _report([_wf("real"), _wf("more")]))
    d = logic.release_diff(pid, SHA3)
    assert d["baseline_sha"] == SHA1 and [x["id"] for x in d["diff"]["added"]] == ["more"]
    _enforce(w)
    _promotable(w, SHA3)
    logic.record_report(pid, SHA2, _report(uncovered=["x", "y"]))                       # a newer decoy cannot become the baseline
    with get_cursor(commit=False) as cur:
        assert logic.baseline_report(cur, pid, SHA3)["sha"] == SHA1


# --- P2-2: validated on write, tolerated on read ---------------------------------------------------------------------------------

BAD_REPORTS = [
    {"workflows": [{"id": "x"}]},                                                       # not a workflow
    {"workflows": [dict(_wf(), steps=[{"id": "s", "kind": "weird", "label": "L"}])]},
    {"workflows": [dict(_wf(), trigger={"type": "pigeon"})]},
    {"uncovered": [{"key": 5}]},
    {"uncovered": [{"key": "k", "name": "n" * 400}]},
    {"uncovered": [{"key": "k", "line": "7"}]},
    {"uncovered": ["text"]},
    {"stale": ["text"]},
    {"stale": [{"workflow": "w", "why": 3}]},
    {"stale": [{"step": "s"}]},
    {"errors": [3]},
    {"errors": [{"a": 1}]},
]


@pytest.mark.parametrize("patch", BAD_REPORTS)
def test_a_malformed_report_is_refused_on_write(w, patch):
    with pytest.raises(repo.BadRequest):
        logic.record_report(w["proj"]["id"], SHA1, {**_report(), **patch})
    assert logic.get_report(w["proj"]["id"], SHA1) is None


def test_the_stored_map_is_the_normalised_one(w):
    wf = _wf()
    wf["steps"][0]["unknown_field"] = "x" * 10
    logic.record_report(w["proj"]["id"], SHA1, _report([wf]))
    assert "unknown_field" not in logic.get_report(w["proj"]["id"], SHA1)["workflows"][0]["steps"][0]


def test_a_bad_stored_row_never_500s_the_pages_or_the_gate(w):
    pid = w["proj"]["id"]
    _enforce(w)
    _promotable(w, SHA1)
    with get_cursor() as cur:                                                           # a row from before validation, or written by hand
        cur.execute("INSERT INTO logic_reports (project_id, sha, workflows, errors, stale, uncovered) VALUES (%s, %s, %s::jsonb, '[]', "
                    "%s::jsonb, %s::jsonb)", (pid, SHA1, json.dumps([{"id": "a", "steps": "nope"}]), json.dumps([{"no": 1}]),
                                              json.dumps([{"nokey": 1}])))
    c = _client(w["creator"])
    slug = w["proj"]["slug"]
    r = c.get(f"/{slug}/logic")
    assert r.status_code == 200 and "data-unreadable" in r.text
    assert c.get(f"/{slug}/logic/diff?sha={SHA1}").status_code == 200
    assert c.get(f"/{slug}/releases").status_code == 200
    with pytest.raises(repo.BadRequest, match="unreadable"):
        _release(w, SHA1)                                                               # refused cleanly, with a reason
    ov = logic.grant_override(P(w["creator"]), w["proj"], SHA1, "the stored row is broken, ship it")
    assert _release(w, SHA1)["state"] == "requested" and ov["id"]                       # the Admin's override still works


# --- P3-4 / P3-5: overrides --------------------------------------------------------------------------------------------------------

def test_one_override_admits_one_production_request_even_across_two_targets(w):
    prod2 = deploys.add_target_assigned(P(w["creator"]), w["proj"], w["srv"], "live2", "/srv/live2", env="production")
    _enforce(w)
    _promotable(w, SHA1)
    logic.grant_override(P(w["creator"]), w["proj"], SHA1, "one release only, please")
    assert _release(w, SHA1)["state"] == "requested"
    with pytest.raises(repo.BadRequest, match="logic map gate refuses"):
        deploys.request_release(P(w["creator"]), w["proj"], prod2["id"], SHA1)        # the same override cannot admit a second target


def test_use_override_raises_when_it_was_already_used(w):
    _enforce(w)
    ov = logic.grant_override(P(w["creator"]), w["proj"], SHA1, "used twice would be a bug")
    with get_cursor() as cur:
        cur.execute("INSERT INTO server_jobs (account_id, project_id, target_name, sha, env) VALUES (%s, %s, 'live', %s, 'prod') RETURNING id",
                    (w["acct"]["id"], w["proj"]["id"], SHA1))
        sj = cur.fetchone()["id"]
        logic.use_override(cur, ov["id"], sj)
    with get_cursor() as cur:
        with pytest.raises(repo.Conflict, match="already used"):
            logic.use_override(cur, ov["id"], sj)


def test_pressing_grant_override_twice_is_a_friendly_refusal_not_a_500(w):
    logic.grant_override(P(w["creator"]), w["proj"], SHA1, "first press goes through")
    with pytest.raises(repo.Conflict, match="already waiting"):
        logic.grant_override(P(w["creator"]), w["proj"], SHA2, "second press at once")
    c = _client(w["creator"])
    slug = w["proj"]["slug"]
    H = {"Origin": "https://crewboss.example.com"}
    r = c.post(f"/{slug}/logic/override", data={"sha": SHA3, "reason": "pressed again from the page"}, headers=H)
    assert r.status_code == 400 and "already waiting" in r.text


# --- P3-7: one baseline for the Releases page ----------------------------------------------------------------------------------------

def test_release_summaries_read_the_baseline_once_for_a_page_of_rows(w):
    pid = w["proj"]["id"]
    logic.record_report(pid, SHA1, _report([_wf("a")]), "job:1")
    _prod_done(w, SHA1)
    logic.record_report(pid, SHA2, _report([_wf("a"), _wf("b")]), "job:2")
    out = logic.release_summaries(pid, [SHA1, SHA2, SHA3, SHA2.upper()])
    assert out[SHA3] is None and out[SHA1]["reported_by"] == "job #1"
    assert out[SHA2]["summary"] == {"added": 1, "removed": 0, "changed": 0, "steps": 0} and out[SHA2]["reported_by"] == "job #2"
    assert out[SHA1]["summary"]["added"] == 1                                            # the released commit has no earlier release: all added


# --- P2-3: the CLI inside a repo that has its own `app` package ------------------------------------------------------------------------

def test_the_launcher_and_script_run_inside_a_repo_with_its_own_app_package(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("raise RuntimeError('the repo app package was imported')\n")
    (tmp_path / "app" / "routes.py").write_text("from fastapi import APIRouter\nr = APIRouter()\n@r.post('/x')\ndef x(): ...\n")
    (tmp_path / "yaml.py").write_text("raise RuntimeError('a shadowing yaml')\n")
    d = tmp_path / ".crewboss" / "logic"
    d.mkdir(parents=True)
    (d / "a.yaml").write_text("workflows:\n  - id: x.run\n    name: X\n    summary: Does x.\n    trigger: {type: route, detail: POST /x, "
                              "code_ref: {file: app/routes.py, function: x}}\n    steps: [{id: s, kind: action, label: Run}]\n")
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    for argv in ([sys.executable, str(ROOT / "crewboss"), "logic", "check"],
                 [sys.executable, "-I", str(ROOT / "scripts" / "crewboss_logic.py"), "check"]):
        out = subprocess.run(argv, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, (argv, out.stdout, out.stderr)
        assert "1 workflows" in out.stdout and "0 uncovered" in out.stdout and "OK" in out.stdout
    rep = subprocess.run([sys.executable, str(ROOT / "crewboss"), "logic", "report", "--sha", "a" * 40], cwd=tmp_path, env=env,
                         capture_output=True, text=True, timeout=60)
    assert rep.returncode == 0 and json.loads(rep.stdout)["workflows"][0]["id"] == "x.run"


def test_only_projects_that_use_the_map_get_the_rule_in_their_brief():
    from app import jobs
    assert "crewboss logic" in jobs.LOGIC_RULE and "python -m app.cli" not in jobs.LOGIC_RULE


# --- P3-6: a scanner that survives hostile input ----------------------------------------------------------------------------------------

def test_symlinks_fifos_and_escaping_refs_are_skipped_not_followed(tmp_path):
    secret = tmp_path / "outside.py"
    secret.write_text("@router.post('/leak')\ndef leak(): ...\n")
    app = tmp_path / "repo"
    (app / "app").mkdir(parents=True)
    os.symlink(secret, app / "app" / "linked.py")
    os.mkfifo(app / "app" / "pipe.py")
    (app / "app" / "real.py").write_text("@router.post('/real')\ndef real(): ...\n")
    names = [e["name"] for e in lm.discover(app)]
    assert names == ["POST /real"]
    d = app / ".crewboss" / "logic"
    d.mkdir(parents=True)
    os.symlink(secret, d / "evil.yaml")
    os.mkfifo(d / "pipe.yaml")
    assert lm.load(app) == {"workflows": [], "ignore": [], "errors": [], "files": []}
    (d / "evil.yaml").unlink()
    (d / "ok.yaml").write_text("workflows:\n  - id: a\n    name: A\n    summary: s\n    trigger: {type: manual}\n"
                               "    steps:\n      - {id: s, kind: action, label: L, code_ref: {file: app/linked.py, function: leak}}\n"
                               "      - {id: t, kind: action, label: T, code_ref: {file: app/pipe.py}}\n"
                               "      - {id: u, kind: action, label: U, code_ref: {file: ../outside.py, function: leak}}\n")
    m = lm.load(app)
    assert any("inside the repo" in e for e in m["errors"])
    d2 = [s for s in lm.stale_refs(app, m["workflows"])]
    assert {s["step"] for s in d2} >= {"s", "t"} and all(s["why"] == "the file no longer exists" for s in d2)


@pytest.mark.parametrize("name, text", [
    ("x.go", 'x.Handle("' * 120000),
    ("x.go", 'a.Handle("POST ' + "/a" * 500000),
    ("x.ts", "app.post('" * 110000),
    ("x.ts", "@Post(" + " " * 900000 + "'/x'"),
    ("x.ts", "function f(){}\napp.post('/a', h);\n" * 30000),
    ("x.go", "func f() {}\nr.Post(\"/a\", h)\n" * 30000),
    ("x.ts", "\n" * 1000000 + "app.post('/late', h)"),
])
def test_discovery_is_fast_on_pathological_files(tmp_path, name, text):
    (tmp_path / name).write_text(text[:1_000_000])
    t = time.monotonic()
    lm.discover(tmp_path)
    assert time.monotonic() - t < 2.0


def test_line_numbers_and_functions_stay_right_after_the_offset_rewrite(tmp_path):
    (tmp_path / "a.ts").write_text("// top\nfunction first() {\n  app.post('/one', h);\n}\n\nfunction second() {\n  app.delete('/two', h);\n}\n")
    got = {(e["name"], e["function"], e["line"]) for e in lm.discover(tmp_path)}
    assert got == {("POST /one", "first", 3), ("DELETE /two", "second", 7)}
