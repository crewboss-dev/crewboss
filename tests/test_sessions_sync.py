"""crewboss-sessions.sh sync (every minute via the sync timer): start what the plan has and tmux lacks;
wind down what left the plan (archived project / sessions turned off) — tell it once, stop it after the grace
period — and never touch anything that isn't a launcher-named project session. Plus the dashboard side
(POST /api/sessions/stopping) and the one lock start + sync share. Fake tmux, curl, docker and /proc only."""
import fcntl
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from app import repo
from app.auth import issue_token

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "sessions/crewboss-sessions.sh"
OWN = {"Origin": "https://crewboss.example.com", "Sec-Fetch-Site": "same-origin"}


# --- the script ------------------------------------------------------------------------------------------------------

def _proc(root: Path, pid: int, argv: list[str], env: dict, children=()):
    d = root / str(pid)
    (d / "task" / str(pid)).mkdir(parents=True)
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    (d / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in env.items()) + b"\0")
    (d / "task" / str(pid) / "children").write_text(" ".join(str(c) for c in children))


def _env_for(slug, short, role, token):
    name = short if role == "all" else f"{short}-{'review' if role == 'reviewer' else role}"
    return {"CREWBOSS_PROJECT": slug, "CREWBOSS_PROJECT_SHORT": short, "CREWBOSS_STREAM": role,
            "CREWBOSS_SESSION_NAME": name, "CREWBOSS_DASH_TOKEN": token}


class Box:
    """A fake dev-box: tmux sessions (name -> pane pid) backed by a fake /proc, and a plan."""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.proc = tmp / "proc"
        self.proc.mkdir()
        self.state = tmp / "state"
        self.log = tmp / "calls.log"
        self.panes: dict = {}
        self.plan: list = []
        self.stop_code = "200"
        bindir = tmp / "bin"
        bindir.mkdir()
        scripts = {
            "tmux": 'echo "tmux $*" >> "$LOG"\n'
                    'case "$1" in\n'
                    '  has-session) n="${3#=}"; grep -q "^$n	" "$PANES" 2>/dev/null; exit $?;;\n'
                    '  list-panes) cat "$PANES" 2>/dev/null; exit 0;;\n'
                    '  new-session|kill-session) exit 0;;\n'
                    'esac; exit 1',
            # a header read from stdin (-H @-) is logged separately, so tests can see it never sat in argv
            "curl": 'case " $* " in *" @- "*) echo "curl $* [stdin: $(cat)]" >> "$LOG";; *) echo "curl $*" >> "$LOG";; esac\n'
                    'case "$*" in *sessions/stopping*) printf "%s" "${STOP_CODE:-200}";; esac; exit 0',
            # the plan is read inside the dashboard container (task #368), never over HTTP
            "docker": 'case "$*" in "compose exec -T app python -m app.sessions_plan") '
                      '[ -n "${PLAN:-}" ] || exit 7; printf "%s" "$PLAN"; exit 0;; esac\n'
                      'echo "docker $*" >> "$LOG"; for a in "$@"; do [ "$prev" = "--stream" ] && s="$a"; prev="$a"; done\n'
                      'echo "mdd_minted_$s"',
        }
        for name, body in scripts.items():
            p = bindir / name
            p.write_text(f"#!/bin/bash\n{body}\n")
            p.chmod(0o755)
        self.bindir = bindir
        self.home = tmp / "home"
        (self.home / ".config/crewboss/tokens").mkdir(parents=True)
        self._next = 1000

    def session(self, name, env, argv=("claude",)):
        """A tmux session whose pane runs bash -> claude (with env)."""
        shell, claude = self._next, self._next + 1
        self._next += 10
        _proc(self.proc, shell, ["/bin/bash"], {}, children=[claude])
        _proc(self.proc, claude, list(argv), env)
        self.panes[name] = shell

    def run(self, *args, extra=None):
        (self.tmp / "panes").write_text("".join(f"{n}\t{p}\n" for n, p in self.panes.items()))
        env = {"PATH": f"{self.bindir}:{os.environ['PATH']}", "HOME": str(self.home), "LOG": str(self.log),
               "PANES": str(self.tmp / "panes"), "CREWBOSS_HOME": str(ROOT / "tests" / "home"), "CREWBOSS_PROC_ROOT": str(self.proc),
               "CREWBOSS_STATE_DIR": str(self.state), "CREWBOSS_DASH_URL": "http://dash.invalid",
               "CREWBOSS_PLAN_TRIES": "1", "CREWBOSS_PLAN_WAIT": "0", "CREWBOSS_DASH_COMPOSE_DIR": str(self.tmp),
               "STOP_CODE": self.stop_code}
        if self.plan is not None:
            env["PLAN"] = json.dumps(self.plan)
        env.update(extra or {})
        out = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, timeout=30, env=env)
        calls = self.log.read_text() if self.log.exists() else ""
        self.log.unlink(missing_ok=True)
        return out, calls


def _plan_entry(slug, short, role):
    name = short if role == "all" else f"{short}-{'review' if role == 'reviewer' else role}"
    return {"project": slug, "short_name": short, "role": role, "stream": role, "session_name": name}


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


def test_sync_starts_what_the_plan_has_and_tmux_lacks(box):
    box.plan = [_plan_entry("example-erp", "erp", "bugs"), _plan_entry("acme-retail", "acme", "all")]
    box.session("erp-bugs", _env_for("example-erp", "erp", "bugs", "tok"))
    out, calls = box.run("sync")
    assert out.returncode == 0, out.stderr
    new = [l for l in calls.splitlines() if l.startswith("tmux new-session")]
    assert len(new) == 1 and "-s acme" in new[0] and "CREWBOSS_PROJECT=acme-retail" in new[0]
    # minted like `start`; the token itself never sits on tmux's command line, only its 0600 file's path
    assert "mdd_minted_all" not in new[0] and "tokens/acme/all'" in new[0] and "CREWBOSS_DASH_TOKEN=" in new[0]
    assert "--project acme-retail --stream all" in calls
    assert "erp-bugs" not in out.stdout                                     # quiet about what already runs
    assert "sessions/stopping" not in calls


def test_sync_does_nothing_when_the_dashboard_is_unreachable(box):
    box.plan = None
    box.session("acme", _env_for("acme-retail", "acme", "all", "tok"))
    out, calls = box.run("sync")
    assert out.returncode == 0 and "unreachable" in out.stdout
    assert "new-session" not in calls and "kill-session" not in calls and "stopping" not in calls


def test_sync_winds_down_a_session_that_left_the_plan_after_the_grace(box):
    box.plan = [_plan_entry("example-erp", "erp", "bugs")]
    box.session("erp-bugs", _env_for("example-erp", "erp", "bugs", "tok_erp"))
    box.session("acme", _env_for("acme-retail", "acme", "all", "tok_acme"))  # its project was archived
    out, calls = box.run("sync")
    assert "acme: left the plan; told to wrap up, stopping in 10 min" in out.stdout
    notice = [l for l in calls.splitlines() if "sessions/stopping" in l]
    assert len(notice) == 1 and "[stdin: Authorization: Bearer tok_acme]" in notice[0] and '{"phase": "notice"}' in notice[0]
    assert "tok_acme" not in notice[0].split("[stdin:")[0]                  # never in curl's argv
    state = box.state / "stopping" / "acme"
    assert state.exists() and "kill-session" not in calls
    # inside the grace: nothing more, not even a second notice
    out, calls = box.run("sync")
    assert "sessions/stopping" not in calls and "kill-session" not in calls
    # the grace is over
    state.write_text(str(int(time.time()) - 601))
    out, calls = box.run("sync")
    assert "tmux kill-session -t =acme" in calls and '{"phase": "stopped"}' in calls and "acme: stopped" in out.stdout
    assert not state.exists()
    assert "erp-bugs" not in calls.replace("tmux has-session -t =erp-bugs", "")  # the planned one: untouched


def test_unarchived_while_winding_down_carries_on(box):
    box.session("acme", _env_for("acme-retail", "acme", "all", "tok_acme"))
    box.plan = []
    box.run("sync")
    assert (box.state / "stopping" / "acme").exists()
    box.plan = [_plan_entry("acme-retail", "acme", "all")]
    out, calls = box.run("sync")
    assert '{"phase": "cancel"}' in calls and "acme: back in the plan" in out.stdout
    assert not (box.state / "stopping" / "acme").exists() and "kill-session" not in calls


def test_sync_never_touches_foreign_owner_or_legacy_sessions(box):
    box.plan = []
    box.session("manager", {"HOME": "/home/x"})                                    # the owner's / manager: no project
    box.session("bugs", {"HOME": "/home/x"})                                       # legacy erp session, no env
    box.session("example-errors", {"CREWBOSS_STREAM": "errors"})                      # legacy launcher: no project
    box.session("my-notes", _env_for("acme-retail", "acme", "all", "t"))          # project env, but not our name
    box.session("vim", {"CREWBOSS_PROJECT": "acme-retail"}, argv=("vim",))           # not a claude process
    box.state.joinpath("stopping").mkdir(parents=True)
    old = str(int(time.time()) - 9999)
    for n in ("manager", "bugs", "my-notes"):
        (box.state / "stopping" / n).write_text(old)                               # even a stale state file
    out, calls = box.run("sync")
    assert "kill-session" not in calls and "sessions/stopping" not in calls, calls


def test_a_refused_notice_never_stops_the_session(box):
    box.plan = []
    box.stop_code = "409"  # the dashboard says it's still in the plan (a stale read here)
    box.session("acme", _env_for("acme-retail", "acme", "all", "t"))
    out, calls = box.run("sync")
    assert "not stopping it" in out.stdout and not (box.state / "stopping" / "acme").exists()
    out, calls = box.run("sync")
    assert "kill-session" not in calls


def test_a_pre_project_name_is_no_longer_managed_even_with_project_env(box):
    box.plan = []
    box.session("example-bugs", {**_env_for("example-erp", "erp", "bugs", "t")})   # not named <short>-<suffix>: not ours
    out, calls = box.run("sync")
    assert "left the plan" not in out.stdout and "kill-session" not in calls


def test_start_and_sync_share_one_lock(box):
    box.plan = [_plan_entry("acme-retail", "acme", "all")]
    box.state.mkdir(parents=True)
    fh = open(box.state / "lock", "w")
    fcntl.flock(fh, fcntl.LOCK_EX)
    try:
        out, calls = box.run("sync")
        assert out.returncode == 0 and "skipping this run" in out.stdout and "new-session" not in calls
        out, calls = box.run("start", extra={"CREWBOSS_LOCK_WAIT": "1"})
        assert out.returncode == 1 and "busy" in out.stderr and "new-session" not in calls
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()
    out, calls = box.run("sync")
    assert "tmux new-session -d -s acme" in calls
    # the tmux started under the lock never inherits the lock fd
    assert "9>&-" in SCRIPT.read_text()


def test_units_share_the_lock_and_never_kill_tmux():
    svc = (ROOT / "sessions/systemd/crewboss-sessions-sync.service.example").read_text()
    timer = (ROOT / "sessions/systemd/crewboss-sessions-sync.timer.example").read_text()
    assert "User=SESSIONS_USER" in svc and "crewboss-sessions.sh sync" in svc and "KillMode=process" in svc
    boot = (ROOT / "sessions/systemd/crewboss-sessions.service.example").read_text()
    # 'none' is deprecated; 'process' signals only the (finished) script and leaves the tmux server alone
    assert "KillMode=process" in boot and "KillMode=none" not in boot + svc
    assert "OnUnitActiveSec=1min" in timer and "WantedBy=timers.target" in timer


# --- the dashboard side ----------------------------------------------------------------------------------------------

@pytest.fixture
def acme():
    return repo.create_project("acme-retail", "Acme Retail", "client", "owner", short_name="acme")


def test_archived_projects_leave_the_plan(acme):
    assert "acme" in {p["session_name"] for p in repo.session_plan()}
    repo.set_project_archived(acme["id"], True, "owner")
    assert "acme" not in {p["session_name"] for p in repo.session_plan()}
    repo.set_project_archived(acme["id"], False, "owner")
    assert "acme" in {p["session_name"] for p in repo.session_plan()}


def test_stopping_notice_stopped_and_cancel(client, acme):
    h = {"Authorization": f"Bearer {issue_token('all', 'acme', project_id=acme['id'])}"}
    # still in the plan: refused
    assert client.post("/api/sessions/stopping", json={"phase": "notice"}, headers=h).status_code == 409
    repo.set_project_archived(acme["id"], True, "owner")
    r = client.post("/api/sessions/stopping", json={"phase": "notice"}, headers=h)
    assert r.status_code == 200 and r.json()["reason"] == "archived"
    rows = repo.list_outbox("features", project_id=acme["id"])
    assert [(o["kind"], o["message"]) for o in rows] == [
        ("session_stop", "Project archived: finish what you're doing, commit, and stop")]
    # the session's own poll gets it; a repeat notice doesn't pile up
    client.post("/api/sessions/stopping", json={"phase": "notice"}, headers=h)
    assert len(client.get("/api/outbox?undelivered=1", headers=h).json()) == 1
    # the Sessions panel shows it stopping, and its channel's heartbeat doesn't undo that
    client.post("/api/sessions/heartbeat", headers=h)
    s = {x["session_name"]: x for x in repo.list_sessions()}["acme"]
    assert s["status"] == "stopping" and s["label"] == "acme (stopping, archived)"
    html = client.get("/").text
    assert "acme (stopping, archived)" in html
    r = client.post("/api/sessions/stopping", json={"phase": "stopped"}, headers=h)
    assert r.status_code == 200
    acts = [a["summary"] for a in repo.recent_activity(acme["id"])]
    assert "session acme stopped (project archived)" in acts
    assert "acme" not in {x["session_name"] for x in repo.list_sessions()}


def test_cancel_after_unarchive(client, acme):
    h = {"Authorization": f"Bearer {issue_token('all', 'acme', project_id=acme['id'])}"}
    repo.set_project_archived(acme["id"], True, "owner")
    client.post("/api/sessions/stopping", json={"phase": "notice"}, headers=h)
    repo.set_project_archived(acme["id"], False, "owner")
    assert client.post("/api/sessions/stopping", json={"phase": "cancel"}, headers=h).json()["state"] == "online"
    assert {x["session_name"]: x for x in repo.list_sessions()}["acme"]["status"] != "stopping"


def test_stopping_takes_no_free_text_and_is_scoped_to_the_token(client, acme):
    other = repo.create_project("other-proj", "Other", "internal", "owner", short_name="oth")
    repo.set_project_archived(acme["id"], True, "owner")
    h = {"Authorization": f"Bearer {issue_token('all', 'oth', project_id=other['id'])}"}
    assert client.post("/api/sessions/stopping", json={"phase": "notice", "message": "rm -rf"}, headers=h).status_code == 422
    assert client.post("/api/sessions/stopping", json={"phase": "explode"}, headers=h).status_code in (400, 409)
    # another project's token only ever speaks for its own session (in the plan: refused)
    assert client.post("/api/sessions/stopping", json={"phase": "notice"}, headers=h).status_code == 409
    assert repo.list_outbox("features", project_id=acme["id"]) == []
    assert client.post("/api/sessions/stopping", json={"phase": "notice"}).status_code == 401


def test_sessions_turned_off_for_a_stream_role_does_not_tell_the_new_all_session(client):
    erp = repo.create_project("per-stream", "Per", "internal", "owner", short_name="ps",
                              session_roles=list(repo.SESSION_STREAMS))
    h = {"Authorization": f"Bearer {issue_token('bugs', 'ps-bugs', project_id=erp['id'])}"}
    with repo.get_cursor() as cur:  # the owner switched the project to one session
        cur.execute("UPDATE projects SET session_roles = '{all}' WHERE id = %s", (erp["id"],))
    r = client.post("/api/sessions/stopping", json={"phase": "notice"}, headers=h)
    assert r.status_code == 200 and r.json()["reason"] == "sessions_off" and r.json()["told"] is False
    assert repo.list_outbox("bugs", project_id=erp["id"]) == []


def test_archive_notes_and_new_project_message(client, acme):
    # the notes sit in the project's Danger zone (task #148), no longer in the switcher
    html = client.get("/acme-retail/settings").text
    assert "Its Claude sessions stop within ~10 minutes" in html
    assert "data-session-note" not in client.get("/acme-retail/board").text
    repo.set_project_archived(acme["id"], True, "owner")
    assert "Its session starts within a minute" in client.get("/acme-retail/settings").text
    quiet = repo.create_project("quiet", "Quiet", "internal", "owner", short_name="qt", session_roles=[])
    assert "data-session-note" not in client.get("/quiet/settings").text
    # sessions default to none now (only parents run one): ask for its own session explicitly
    r = client.post("/projects", data={"slug": "beta", "name": "Beta", "short_name": "beta", "sessions": "all"},
                    headers=OWN, follow_redirects=False)
    cookie = r.headers["set-cookie"]
    assert "session starts" in cookie and "automatically within a minute" in cookie and "crewboss-sessions.sh" not in cookie
    del quiet


def test_refresh_notice_for_a_planned_session_is_fixed_text_and_scoped(client, acme):
    h = {"Authorization": f"Bearer {issue_token('all', 'acme', project_id=acme['id'])}"}
    r = client.post("/api/sessions/stopping", json={"phase": "refresh"}, headers=h)
    assert r.status_code == 200 and r.json()["state"] == "online"
    rows = client.get("/api/outbox?undelivered=1", headers=h).json()
    assert [(o["kind"], o["message"]) for o in rows] == [("session_refresh", repo.REFRESH_MESSAGE)]
    client.post("/api/sessions/stopping", json={"phase": "refresh"}, headers=h)  # a repeat coalesces
    assert len(client.get("/api/outbox?undelivered=1", headers=h).json()) == 1
    # it does not mark the session stopping (it stays in the plan)
    assert {x["session_name"]: x for x in repo.list_sessions()}.get("acme", {}).get("status") != "stopping"
    # free text is refused by the schema
    assert client.post("/api/sessions/stopping", json={"phase": "refresh", "message": "x"}, headers=h).status_code == 422
    # a session that left the plan is wind_down's: refused
    repo.set_project_archived(acme["id"], True, "owner")
    assert client.post("/api/sessions/stopping", json={"phase": "refresh"}, headers=h).status_code == 409
