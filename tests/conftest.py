"""Spins up a throwaway Postgres container for the test session (never the ERP's or staging's
database), runs migrations against it, and points app.db at it BEFORE any app module is imported —
DATABASE_URL is set at conftest module-import time, which happens before pytest imports test modules."""
import os
import subprocess
import time

# Overridable so two checkouts (e.g. parallel agent worktrees) can run the suite at the same time
# without one `docker rm -f`-ing the other's database mid-run.
# Unset = a name unique to this run and a free port, so parallel runs and contributors never collide.
def _free_port() -> str:
    import socket
    with socket.socket() as sk:
        sk.bind(("127.0.0.1", 0))
        return str(sk.getsockname()[1])


TEST_CONTAINER = os.environ.get("CREWBOSS_TEST_CONTAINER") or f"crewboss-test-db-{os.getpid()}"
TEST_PORT = os.environ.get("CREWBOSS_TEST_PORT") or _free_port()
TEST_DATABASE_URL = f"postgresql://postgres:test@127.0.0.1:{TEST_PORT}/dashboard_test"
# Unconditional overwrite: a DATABASE_URL exported in the shell (e.g. the live stack's) never reaches
# the app under test.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
# A developer's own standing Claude session carries session variables (its stream, project, token and dashboard URL: CREWBOSS_*, and
# the pre-rename MALLEN_* of task #589). The session-script tests build their environments from this one, so none may leak in.
_SESSION_VARS = ("STREAM", "PROJECT", "PROJECT_SHORT", "SESSION_NAME", "DASH_URL", "DASH_TOKEN", "DASH_COMPOSE_DIR", "TOKEN_DIR",
                 "STATE_DIR", "SESSIONS_CWD", "SYNC_GRACE", "LOCK_WAIT", "PLAN_TIMEOUT", "PLAN_TRIES", "PLAN_WAIT", "NO_MINT",
                 "FALLBACK_SESSION", "GROUND_PROJECT", "PROC_ROOT", "ROOT_PROJECT", "PROJECT_TREE", "TMUX", "CLAUDE_SESSIONS",
                 "DASH_BUDGET", "DASH_IMPORT_URL", "ANNOTATIONS", "SCANNER_CONFIG",
                 "REFRESH_DISABLE", "REFRESH_NOW", "REFRESH_NOTICE_MIN", "REFRESH_RECHECK", "REFRESH_TZ",
                 "REFRESH_WINDOW")
for _k in list(os.environ):
    if _k.startswith(("MALLEN_", "CREWBOSS_RUNNER_")) or (_k.startswith("CREWBOSS_") and _k[len("CREWBOSS_"):] in _SESSION_VARS):
        del os.environ[_k]
# The instance folder (app/instance.py): the suite NEVER reads a real CREWBOSS_HOME, always the neutral one in tests/home.
# Unconditional overwrite, like DATABASE_URL. test_instance.py's Mallen check passes a home of its own to a subprocess.
# The commit-guard test alone reads the OUTER home's denylist.txt (nothing else of it): remember where that was.
os.environ["CREWBOSS_OUTER_HOME"] = os.environ.get("CREWBOSS_HOME") or os.path.join(os.path.expanduser("~"), ".crewboss")
TEST_HOME = os.path.join(os.path.dirname(os.path.abspath(__file__)), "home")
os.environ["CREWBOSS_HOME"] = TEST_HOME
for _name in ("CREWBOSS_DRIFT_FILE", "CREWBOSS_HEALTH_FEEDS_FILE"):
    os.environ.pop(_name, None)
# Session tokens issued without a project (issue_token(stream, name)) belong to the legacy project — in the
# tests that is the `project` fixture's, so the many token tests act inside it. Isolation tests bind explicitly.
os.environ["CREWBOSS_LEGACY_TOKEN_PROJECT"] = "test-proj"
# No overseer by default (task #129): tests that need one monkeypatch config.OVERSEER_PROJECT, so no other test sees
# plan_stale outbox rows or cross-project reads.
os.environ["CREWBOSS_OVERSEER_PROJECT"] = ""
# The ERP health feed loop stays off in the tests (task #196): tests drive erp_health.sync() with a fake client, and a
# background fetch must never race them.
os.environ["CREWBOSS_ERP_HEALTH_POLL_SECONDS"] = "0"
os.environ["CREWBOSS_ERP_HEALTH_URL"] = ""
# The drift loop (#402) stays off too: tests drive drift.sync() with their own config.
os.environ["CREWBOSS_DRIFT_POLL_SECONDS"] = "0"
# The push sweep (#487) stays off too: tests drive push.sweep() with pywebpush mocked; no key file unless a test makes one.
# The deploy executor loop (task #627, P5) must never run in tests: they drive deploys.Executor by hand with ssh mocked.
os.environ["CREWBOSS_DEPLOY_POLL_SECONDS"] = "0"
# The server probe sweeper (#810) never dials out from the suite; tests drive server_access.probe_all() with the keyscan mocked.
os.environ["CREWBOSS_SERVER_PROBE_SECONDS"] = "0"
# The public-certificate check (#707) never dials out from the suite; tests/test_installer.py drives app/tls_check.py with a fake socket.
os.environ["CREWBOSS_TLS_CHECK"] = "off"
os.environ["CREWBOSS_TLS_CHECK_INTERVAL_SECONDS"] = "0"
os.environ["CREWBOSS_PUSH_SWEEP_SECONDS"] = "0"
# The planning reminder loop (#915) stays off; tests drive planning_reminders.sweep() with pywebpush mocked.
os.environ["CREWBOSS_PLANNING_REMINDER_SECONDS"] = "0"
# CA uploads (#727) are files on disk: the suite writes them to a throwaway folder, never a real one.
import tempfile  # noqa: E402
os.environ["CREWBOSS_CA_UPLOADS_DIR"] = tempfile.mkdtemp(prefix="crewboss-ca-uploads-")
os.environ["CREWBOSS_PUSH_SWEEP_SECONDS"] = "0"
os.environ["CREWBOSS_VAPID_KEY_FILE"] = "/nonexistent/vapid-private.pem"
# Dashboard sign-in (#394) is off for the suite: the page tests act as the owner, as before. tests/test_web_auth.py
# switches it on (config.WEB_AUTH) with its own tenant, client, secret and allowlist.
os.environ["CREWBOSS_WEB_AUTH"] = "off"
# Platform keys (#562): the reminder loop stays off and no writer credential exists unless a test makes one; the tests
# replace the HTTP side (platform_keys._urlopen), so nothing ever reaches a real key store.
os.environ["CREWBOSS_PLATFORM_KEYS_POLL_SECONDS"] = "0"
os.environ["CREWBOSS_PLATFORM_KEYS_FILE"] = "/nonexistent/dash-keys-1"
os.environ["CREWBOSS_PLATFORM_KEYS_URL"] = "https://keys.test.invalid"

import pytest  # noqa: E402


def _docker(*args, check=True):
    return subprocess.run(["docker", *args], check=check, capture_output=True, text=True)


@pytest.fixture(scope="session", autouse=True)
def _pg_container():
    _docker("rm", "-f", "-v", TEST_CONTAINER, check=False)
    _docker(
        "run", "-d", "--rm", "--name", TEST_CONTAINER,
        "-e", "POSTGRES_PASSWORD=test", "-e", "POSTGRES_DB=dashboard_test",
        # Loopback only: the throwaway DB (password "test") is never reachable off-box.
        "-p", f"127.0.0.1:{TEST_PORT}:5432", "postgres:16-alpine",
        # Throwaway data: no fsync. The per-test TRUNCATE below took ~3 s each with it (a ~2 hour suite on a slow disk).
        "-c", "fsync=off", "-c", "synchronous_commit=off", "-c", "full_page_writes=off",
    )
    from app import config, db

    # Belt and braces: every test writes through app.db, which must be pointed at the throwaway
    # container, never the live dashboard database.
    assert config.DATABASE_URL == TEST_DATABASE_URL, config.DATABASE_URL

    deadline = time.monotonic() + 40
    last_err = None
    while time.monotonic() < deadline:
        try:
            db.wait_for_db(timeout=2)
            break
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(1)
    else:
        _docker("logs", TEST_CONTAINER, check=False)
        raise RuntimeError(f"test postgres never came up: {last_err}")

    with db.connect() as conn:
        assert conn.execute("SELECT current_database() AS d").fetchone()["d"] == "dashboard_test"
    db.run_migrations()
    yield
    # -v: postgres:16-alpine declares a VOLUME; without it every run leaves an anonymous volume behind
    # (~500 had piled up by 10-27 and set off the dev-box inode alarm).
    _docker("rm", "-f", "-v", TEST_CONTAINER, check=False)


@pytest.fixture(autouse=True)
def _clean_tables():
    """Truncate everything between tests so they're independent, without a container restart."""
    from app import access
    from app.db import connect

    yield
    access.set_current_user(None)      # a direct MCP tool call in one test must not leave its audit user for the next (#713)
    with connect() as conn:
        conn.execute(
            "TRUNCATE projects, subprojects, tasks, task_comments, subtasks, knowledge, activity, outbox, "
            "session_tokens, catalog_scans, catalog_diffs, ground_checkpoints, sessions, health_problems, health_sources, task_questions, global_plan, global_plan_runs, "
            "push_subscriptions, estate_pushes, platform_key_stages, users, login_attempts, auth_flows, web_sessions, asset_blobs, tour_progress "
            "RESTART IDENTITY CASCADE"
        )
        # global_plan_state is a one-row table (task #129): reset it rather than truncate it.
        conn.execute("UPDATE global_plan_state SET last_morning_date = NULL, pending_since = NULL, pending_why = NULL, "
                     "view_at = NULL")
        conn.commit()


@pytest.fixture
def project():
    from app import repo

    return repo.create_project("test-proj", "Test Project", "internal", "system")


@pytest.fixture(scope="session")
def client():
    """Session-scoped: the MCP session manager's task group can only be entered ONCE per process
    (StreamableHTTPSessionManager.run() raises on a second call), so the FastAPI lifespan — which
    enters it — must start and stop exactly once across the whole test run."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def mcp_caller(monkeypatch):
    """Call MCP tool functions directly as if over HTTP with a given Bearer token (None = no
    Authorization header). The tools read the header per call via mcp_server._request_headers."""
    from app import mcp_server

    def as_caller(token):
        headers = {} if token is None else {"authorization": f"Bearer {token}"}
        monkeypatch.setattr(mcp_server, "_request_headers", lambda: headers)

    return as_caller
