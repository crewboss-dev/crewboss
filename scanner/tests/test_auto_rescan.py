"""scanner/auto_rescan.py against throwaway git repos (a bare "origin" + a checkout of it, seeded from
the fixture tree) and a local HTTP server standing in for POST /api/catalog/import. No network, no
dashboard, no real repos."""
import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from scanner import auto_rescan

FIXTURES = Path(__file__).parent / "fixtures"
TOKEN = "cb_" + "test-scanner-token-do-not-print"
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "init.defaultBranch=main"]


def _git(cwd, *args):
    return subprocess.run([*GIT, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """(origin bare repo, checkout) — the checkout has origin/main, like a deployed repo."""
    seed = tmp_path / "seed"
    shutil.copytree(FIXTURES, seed)
    _git(seed, "init", "-q")
    _git(seed, "add", ".")
    _git(seed, "commit", "-qm", "seed")
    origin = tmp_path / "origin.git"
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(origin))
    checkout = tmp_path / "checkout"
    _git(tmp_path, "clone", "-q", str(origin), str(checkout))
    return {"seed": seed, "origin": origin, "checkout": checkout}


def _push_change(repo, rel="app/workers/fixture_worker.py"):
    """Commit a change to `rel` in the seed and push it to origin (NOT pulled into the checkout)."""
    p = repo["seed"] / rel
    p.write_text(p.read_text() + "\n\ndef added_later():\n    return 1\n")
    _git(repo["seed"], "commit", "-qam", "change")
    _git(repo["seed"], "push", "-q", str(repo["origin"]), "HEAD:main")
    return _git(repo["seed"], "rev-parse", "HEAD")


class _Server:
    def __init__(self, status=201, body=None):
        self.requests = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length", 0))
                outer.requests.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                       "body": json.loads(self.rfile.read(n))})
                payload = json.dumps(outer.body if outer.body is not None else {
                    "scan_id": len(outer.requests), "workflow_count": 1, "diff_count": 0,
                    "stale_count": 0, "broken_count": 0}).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass

        self.status, self.body = status, body
        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/api/catalog/import"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()   # stop LISTENING too, or a connect would hang in the backlog


@pytest.fixture
def server():
    s = _Server()
    yield s
    s.close()


@pytest.fixture
def env(tmp_path, repo, server):
    token_file = tmp_path / "tokens" / "scanner"
    token_file.parent.mkdir()
    token_file.write_text(TOKEN + "\n")
    token_file.chmod(0o600)
    annotations = tmp_path / "annotations.yaml"
    annotations.write_text("workflows: []\n")
    state = tmp_path / "state"
    args = ["--primary-repo", str(repo["checkout"]), "--secondary-repo", "", "--knowledge-repo", "", "--project", "acme-app",
            "--url", server.url,
            "--token-file", str(token_file), "--state-dir", str(state), "--annotations", str(annotations)]
    return {"args": args, "state": state, "annotations": annotations, "token_file": token_file}


def _status(env):
    return json.loads((env["state"] / "rescan-status.json").read_text())


def test_first_run_imports_with_bearer_token_and_never_prints_it(env, server, repo, capsys):
    assert auto_rescan.main(env["args"]) == 0
    assert len(server.requests) == 1
    req = server.requests[0]
    assert req["auth"] == f"Bearer {TOKEN}"
    assert req["path"].endswith("?project=acme-app")
    assert req["body"]["commit"] == _git(repo["checkout"], "rev-parse", "origin/main")
    assert all(r.get("hash") for w in req["body"]["workflows"] for r in w["source_refs"])
    out = capsys.readouterr()
    assert TOKEN not in out.out + out.err
    st = _status(env)
    assert st["ok"] is True and st["status"] == "imported" and st["scan_id"] == 1
    assert st["consecutive_failures"] == 0 and st["last_import_epoch"]
    assert TOKEN not in (env["state"] / "rescan-status.json").read_text()
    assert TOKEN not in (env["state"] / "rescan.json").read_text()


def test_unchanged_inputs_do_not_reimport(env, server):
    assert auto_rescan.main(env["args"]) == 0
    assert auto_rescan.main(env["args"]) == 0
    assert len(server.requests) == 1
    assert _status(env)["status"] == "unchanged" and _status(env)["ok"] is True


def test_force_reimports(env, server):
    auto_rescan.main(env["args"])
    auto_rescan.main(env["args"] + ["--force"])
    assert len(server.requests) == 2


def test_new_origin_commit_is_fetched_and_scanned_without_touching_the_working_tree(env, server, repo):
    auto_rescan.main(env["args"])
    head_before = _git(repo["checkout"], "rev-parse", "HEAD")
    worker = repo["checkout"] / "app/workers/fixture_worker.py"
    text_before = worker.read_text()

    new_sha = _push_change(repo)
    assert auto_rescan.main(env["args"]) == 0

    assert len(server.requests) == 2
    assert server.requests[1]["body"]["commit"] == new_sha
    assert _git(repo["checkout"], "rev-parse", "origin/main") == new_sha      # fetched
    assert _git(repo["checkout"], "rev-parse", "HEAD") == head_before         # not checked out
    assert worker.read_text() == text_before                                  # tree untouched
    assert _git(repo["checkout"], "status", "--porcelain") == ""


def test_annotation_change_triggers_a_rescan(env, server):
    auto_rescan.main(env["args"])
    env["annotations"].write_text("# fixed an annotation\nworkflows: []\n")
    auto_rescan.main(env["args"])
    assert len(server.requests) == 2


def test_import_failure_exits_nonzero_records_status_and_retries_next_tick(env, server):
    server.status, server.body = 500, {"detail": "boom"}
    assert auto_rescan.main(env["args"]) == 1
    st = _status(env)
    assert st["ok"] is False and st["status"] == "failed" and "HTTP 500" in st["last_error"]
    assert st["consecutive_failures"] == 1
    assert not (env["state"] / "rescan.json").exists()     # fingerprint NOT saved

    assert auto_rescan.main(env["args"]) == 1
    assert _status(env)["consecutive_failures"] == 2

    server.status, server.body = 201, None
    assert auto_rescan.main(env["args"]) == 0                # retried although nothing changed
    st = _status(env)
    assert st["ok"] is True and st["consecutive_failures"] == 0 and st["last_error"] is None
    assert len(server.requests) == 3


def test_rejected_token_error_never_echoes_the_token(env, server, capsys):
    server.status, server.body = 401, {"detail": f"invalid token {TOKEN}"}   # a hostile/buggy server
    assert auto_rescan.main(env["args"]) == 1
    out = capsys.readouterr()
    assert TOKEN not in out.out + out.err
    assert TOKEN not in (env["state"] / "rescan-status.json").read_text()


def test_missing_token_file_fails(env, server):
    env["token_file"].unlink()
    assert auto_rescan.main(env["args"]) == 1
    assert "no scanner token" in _status(env)["last_error"]
    assert server.requests == []


def test_unreachable_dashboard_fails(env, server):
    server.close()
    assert auto_rescan.main(env["args"]) == 1
    assert "can't reach" in _status(env)["last_error"]


def test_schema_problem_refuses_to_import(env, server):
    env["annotations"].write_text(
        "workflows:\n  - {id: bad.one.here, name: 'Bad › One › Here', group: NotAGroup, subproject: ERP,\n"
        "     summary: x, trigger: {type: timer, detail: x}, source_refs: [],\n"
        "     steps: [{id: t, kind: trigger, label: T, code_ref: {file: app/workers/fixture_worker.py, function: main}}]}\n")
    assert auto_rescan.main(env["args"]) == 1
    assert "refusing to import" in _status(env)["last_error"]
    assert server.requests == []


def test_not_a_checkout_fails(env, tmp_path):
    args = list(env["args"])
    args[args.index("--primary-repo") + 1] = str(tmp_path / "nope")
    assert auto_rescan.main(args) == 1


def test_dry_run_scans_but_neither_posts_nor_saves(env, server):
    assert auto_rescan.main(env["args"] + ["--dry-run"]) == 0
    assert server.requests == []
    assert not (env["state"] / "rescan.json").exists()
    assert not (env["state"] / "rescan-status.json").exists()


def test_a_missing_annotations_file_fails_the_tick_and_imports_nothing(env, server, tmp_path, monkeypatch):
    """scanner.yaml names repos, so an absent annotations file must not become an empty catalog over the real one."""
    home = tmp_path / "home"
    (home / "scanner").mkdir(parents=True)
    (home / "scanner" / "scanner.yaml").write_text("repos:\n  primary: /srv/acme-app\n")
    monkeypatch.setenv("CREWBOSS_HOME", str(home))
    env["annotations"].unlink()
    assert auto_rescan.main(env["args"]) == 1
    assert server.requests == []
    assert "annotations file" in (env["state"] / "rescan-status.json").read_text()


def test_missing_annotations_are_fine_when_scanner_yaml_says_optional(env, server, tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "scanner").mkdir(parents=True)
    (home / "scanner" / "scanner.yaml").write_text("annotations: optional\nrepos:\n  primary: /srv/acme-app\n")
    monkeypatch.setenv("CREWBOSS_HOME", str(home))
    env["annotations"].unlink()
    assert auto_rescan.main(env["args"]) == 0
    assert len(server.requests) == 1
