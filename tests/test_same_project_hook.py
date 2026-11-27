"""sessions/hooks/same-project-only.sh — the PreToolUse guard on SendMessage: sessions within a project may
message each other, sessions in different projects may not (fail closed). Driven with a fake /proc and a fake
tmux; nothing real is inspected."""
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / "sessions/hooks/same-project-only.sh"
UID = os.getuid()


def _proc(root: Path, pid: int, argv: list[str], env: dict, ppid: int = 1, children=()):
    d = root / str(pid)
    (d / "task" / str(pid)).mkdir(parents=True)
    (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
    (d / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in env.items()) + b"\0")
    (d / "stat").write_text(f"{pid} ({os.path.basename(argv[0])}) S {ppid} 0 0")
    (d / "task" / str(pid) / "children").write_text(" ".join(str(c) for c in children))


@pytest.fixture
def box(tmp_path):
    """Sessions: erp-bugs (a shell whose child is claude), dash (claude is the pane process), a pre-project plain
    'features' session (claude without project env), mallen-errors (pre-project launcher: CREWBOSS_STREAM only)."""
    proc = tmp_path / "proc"
    _proc(proc, 100, ["/bin/bash"], {"CREWBOSS_PROJECT": "mallen-erp"}, children=[101])
    _proc(proc, 101, ["claude", "--remote-control", "erp-bugs"], {"CREWBOSS_PROJECT": "mallen-erp", "CREWBOSS_STREAM": "bugs"}, ppid=100)
    _proc(proc, 200, ["claude", "--remote-control", "dash"], {"CREWBOSS_PROJECT": "example-dashboard", "CREWBOSS_STREAM": "all"})
    _proc(proc, 300, ["claude"], {"HOME": "/home/x"})
    _proc(proc, 400, ["claude", "--remote-control", "mallen-errors"], {"CREWBOSS_STREAM": "errors"})
    _proc(proc, 500, ["claude"], {"HOME": "/home/x"})  # not in tmux, no env: unknown
    tmux = tmp_path / "tmux"
    tmux.write_text("#!/bin/bash\nprintf 'erp-bugs\\t100\\ndash\\t200\\nfeatures\\t300\\nmallen-errors\\t400\\n'\n")
    tmux.chmod(0o755)
    # No tree edges by default (the real hook asks the dashboard; tests never do).
    (tmp_path / "no-sessions").mkdir()  # never the real ~/.claude/sessions
    return {"CREWBOSS_HOME": str(ROOT / "tests" / "home"), "CREWBOSS_PROC_ROOT": str(proc), "CREWBOSS_TMUX": str(tmux), "CREWBOSS_PROJECT_TREE": "[]",
            "CREWBOSS_CLAUDE_SESSIONS": str(tmp_path / "no-sessions")}


def _run(box, sender, to):
    env = {"PATH": os.environ["PATH"], **box}
    if sender:
        env["CREWBOSS_PROJECT"] = sender
    out = subprocess.run(["bash", str(HOOK)], input=json.dumps({"tool_name": "SendMessage", "tool_input": {"to": to}}),
                         capture_output=True, text=True, timeout=10, env=env)
    assert out.returncode == 0, out.stderr
    if not out.stdout.strip():
        return "allow", ""
    h = json.loads(out.stdout)["hookSpecificOutput"]
    assert h["hookEventName"] == "PreToolUse"
    return h["permissionDecision"], h["permissionDecisionReason"]


@pytest.mark.parametrize("sender,to,decision", [
    # same project: allowed
    ("mallen-erp", "erp-bugs", "allow"),
    ("mallen-erp", "erp-bugs [ref]", "allow"),
    ("mallen-erp", f"uds:/run/user/{UID}/cc-socks/101.sock", "allow"),
    ("example-dashboard", "dash", "allow"),
    ("example-dashboard", f"uds:/run/user/{UID}/cc-socks/200.sock", "allow"),
    # pre-project sessions (plain name / CREWBOSS_STREAM-only env) belong to no project: denied (fail closed)
    ("mallen-erp", "features", "deny"),
    ("mallen-erp", f"uds:/run/user/{UID}/cc-socks/300.sock", "deny"),
    ("mallen-erp", f"uds:/run/user/{UID}/cc-socks/400.sock", "deny"),
    ("mallen-erp", "mallen-errors", "deny"),
    # different project: denied
    ("example-dashboard", "erp-bugs", "allow"),  # dash is the top session
    ("mallen-erp", "dash", "deny"),  # no tree here: up needs the session-parent edge
    ("example-dashboard", f"uds:/run/user/{UID}/cc-socks/101.sock", "allow"),
    ("example-dashboard", "features", "allow"),
    ("example-dashboard", f"uds:/run/user/{UID}/cc-socks/400.sock", "allow"),
    # unknown / unresolvable: denied (fail closed)
    ("mallen-erp", "nosuch", "deny"),
    ("mallen-erp", f"uds:/run/user/{UID}/cc-socks/500.sock", "deny"),
    ("mallen-erp", f"uds:/run/user/{UID}/cc-socks/999.sock", "deny"),
    ("mallen-erp", "uds:/tmp/evil/101.sock", "deny"),
    ("mallen-erp", "", "deny"),
    ("mallen-erp", "../../etc", "deny"),
    # in-process: allowed
    ("mallen-erp", "main", "allow"),
    ("mallen-erp", "a0123456789abcdef", "allow"),
    # the owner's / manager sessions (no CREWBOSS_PROJECT) may message anyone
    ("", "erp-bugs", "allow"),
    ("", "nosuch", "allow"),
])
def test_same_project_only(box, sender, to, decision):
    got, reason = _run(box, sender, to)
    assert got == decision, (sender, to, reason)
    if decision == "deny":
        assert reason.startswith("cross-project messaging is not allowed; ") and sender in reason


def test_deny_reason_names_both_projects(box):
    assert _run(box, "mallen-erp", "dash")[1] == \
        "cross-project messaging is not allowed; mallen-erp → example-dashboard"
    assert _run(box, "mallen-erp", "nosuch")[1] == "cross-project messaging is not allowed; mallen-erp → unknown"


def test_garbage_input_fails_closed(box):
    env = {"PATH": os.environ["PATH"], **box, "CREWBOSS_PROJECT": "mallen-erp"}
    out = subprocess.run(["bash", str(HOOK)], input="not json", capture_output=True, text=True, timeout=10, env=env)
    assert json.loads(out.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


# --- the project tree's edges are its connections (task #132) ---------------------------------------------------

TREE = [
    # dash (root, session) → Mallen (account, no session) → mallen-erp (session) → customer-portal (no session)
    {"slug": "example-dashboard", "parent": None, "session_parent": None, "has_session": True},
    {"slug": "mallen", "parent": "example-dashboard", "session_parent": "example-dashboard", "has_session": False},
    {"slug": "mallen-erp", "parent": "mallen", "session_parent": "example-dashboard", "has_session": True},
    {"slug": "customer-portal", "parent": "mallen-erp", "session_parent": "mallen-erp", "has_session": False},
    {"slug": "personal-pa", "parent": "example-dashboard", "session_parent": "example-dashboard", "has_session": True},
]


@pytest.mark.parametrize("sender,to,decision", [
    ("example-dashboard", "erp-bugs", "allow"),   # dash ↔ erp: erp's session parent is dash (Mallen runs no session)
    ("mallen-erp", "dash", "allow"),
    ("personal-pa", "erp-bugs", "deny"),         # siblings under dash: through their parent only
    ("unrelated", "dash", "deny"),
])
def test_parent_and_child_sessions_may_talk(box, sender, to, decision):
    got, reason = _run({**box, "CREWBOSS_PROJECT_TREE": json.dumps(TREE)}, sender, to)
    assert got == decision, (sender, to, reason)


def test_unreachable_tree_fails_closed(box):
    got, _ = _run({**box, "CREWBOSS_PROJECT_TREE": "not json"}, "mallen-erp", "dash")
    assert got == "deny"
    # ...except the top session, which reaches every session regardless (owner, 2026-10-02)
    assert _run({**box, "CREWBOSS_PROJECT_TREE": "not json"}, "example-dashboard", "erp-bugs")[0] == "allow"


def _registry(tmp_path, rows):
    d = tmp_path / "cc-sessions"
    d.mkdir(exist_ok=True)
    for fname, rec in rows.items():
        (d / fname).write_text(json.dumps(rec))
    return str(d)


@pytest.mark.parametrize("sender,to,decision", [
    ("mallen-erp", "stacks-b1", "allow"),        # Claude name -> pid 101 (mallen-erp)
    ("example-dashboard", "stacks-7b", "allow"),  # -> pid 200 (example-dashboard)
    ("example-dashboard", "stacks-b1", "allow"),  # dash reaches every session
    ("mallen-erp", "stacks-dead", "deny"),       # registry pid not running
    ("mallen-erp", "stacks-liar", "deny"),       # record's pid doesn't match its filename
    ("mallen-erp", "stacks-dup", "deny"),        # ambiguous name
    ("mallen-erp", "stacks-shell", "deny"),      # pid is not a claude process
])
def test_claude_session_names_resolve(box, tmp_path, sender, to, decision):
    box = {**box, "CREWBOSS_CLAUDE_SESSIONS": _registry(tmp_path, {
        "101.json": {"pid": 101, "name": "stacks-b1"},
        "200.json": {"pid": 200, "name": "stacks-7b"},
        "999.json": {"pid": 999, "name": "stacks-dead"},
        "102.json": {"pid": 101, "name": "stacks-liar"},
        "300.json": {"pid": 300, "name": "stacks-dup"},
        "400.json": {"pid": 400, "name": "stacks-dup"},
        "100.json": {"pid": 100, "name": "stacks-shell"},
    })}
    assert _run(box, sender, to)[0] == decision


def test_claude_name_parent_child_edge(box, tmp_path):
    tree = [{"slug": "mallen-erp", "session_parent": "example-dashboard"},
            {"slug": "example-dashboard", "session_parent": None}]
    box = {**box, "CREWBOSS_PROJECT_TREE": json.dumps(tree),
           "CREWBOSS_CLAUDE_SESSIONS": _registry(tmp_path, {"101.json": {"pid": 101, "name": "stacks-b1"}})}
    assert _run(box, "example-dashboard", "stacks-b1")[0] == "allow"


def test_parent_order_always_going_down(box, tmp_path):
    """Owner 2026-10-02: a session reaches ANY session below it (dash reaches all); upward only one step, to its own
    session parent; siblings and unrelated projects stay apart."""
    proc = Path(box["CREWBOSS_PROC_ROOT"])
    _proc(proc, 600, ["claude", "--remote-control", "cp"], {"CREWBOSS_PROJECT": "customer-portal"})
    _proc(proc, 650, ["claude", "--remote-control", "cpw"], {"CREWBOSS_PROJECT": "portal-widgets"})
    _proc(proc, 700, ["claude", "--remote-control", "pa"], {"CREWBOSS_PROJECT": "personal-pa"})
    reg = _registry(tmp_path, {"600.json": {"pid": 600, "name": "s-cp"}, "650.json": {"pid": 650, "name": "s-cpw"},
                               "700.json": {"pid": 700, "name": "s-pa"}, "101.json": {"pid": 101, "name": "s-erp"},
                               "200.json": {"pid": 200, "name": "s-dash"}})
    tree = [
        {"slug": "example-dashboard", "parent": None, "session_parent": None},
        {"slug": "mallen", "parent": "example-dashboard", "session_parent": "example-dashboard"},
        {"slug": "mallen-erp", "parent": "mallen", "session_parent": "example-dashboard"},
        {"slug": "customer-portal", "parent": "mallen-erp", "session_parent": "mallen-erp"},
        {"slug": "portal-widgets", "parent": "customer-portal", "session_parent": "customer-portal"},
        {"slug": "personal-pa", "parent": "example-dashboard", "session_parent": "example-dashboard"},
    ]
    b = {**box, "CREWBOSS_CLAUDE_SESSIONS": reg, "CREWBOSS_PROJECT_TREE": json.dumps(tree)}
    cases = [
        ("example-dashboard", "s-cpw", "allow"),  # dash → great-grandchild
        ("mallen-erp", "s-cpw", "allow"),        # erp → grandchild (down, skipping a level)
        ("mallen-erp", "s-cp", "allow"),         # erp → child
        ("customer-portal", "s-erp", "allow"),   # one step up: its session parent
        ("customer-portal", "s-dash", "deny"),   # two steps up: goes through erp
        ("portal-widgets", "s-erp", "deny"),     # two steps up
        ("customer-portal", "s-pa", "deny"),     # unrelated
        ("personal-pa", "s-cp", "deny"),         # not its descendant
        ("mallen-erp", "s-pa", "deny"),          # sibling under dash
    ]
    for sender, to, want in cases:
        assert _run(b, sender, to)[0] == want, (sender, to)


def test_reply_socket_addresses_resolve_via_registry(box, tmp_path):
    """A reply goes to the sender's uds address, e.g. uds:/tmp/cc-socks/<pid>.sock (seen live 2026-10-02: erp's
    acknowledgement to dash was denied as 'unknown'). Resolved only via an exact registry match."""
    reg = _registry(tmp_path, {"200.json": {"pid": 200, "name": "s-dash", "messagingSocketPath": "/tmp/cc-socks/200.sock"},
                               "102.json": {"pid": 101, "name": "liar", "messagingSocketPath": "/tmp/cc-socks/102.sock"}})
    tree = [{"slug": "example-dashboard", "parent": None, "session_parent": None},
            {"slug": "mallen-erp", "parent": "example-dashboard", "session_parent": "example-dashboard"},
            {"slug": "personal-pa", "parent": "example-dashboard", "session_parent": "example-dashboard"}]
    b = {**box, "CREWBOSS_CLAUDE_SESSIONS": reg, "CREWBOSS_PROJECT_TREE": json.dumps(tree)}
    assert _run(b, "mallen-erp", "uds:/tmp/cc-socks/200.sock")[0] == "allow"   # child acknowledges its parent
    assert _run(b, "personal-pa", "uds:/tmp/cc-socks/200.sock")[0] == "allow"  # also a child of dash
    assert _run(b, "mallen-erp", "uds:/tmp/cc-socks/102.sock")[0] == "deny"    # record pid != filename
    assert _run(b, "mallen-erp", "uds:/tmp/cc-socks/999.sock")[0] == "deny"    # unregistered path
    assert _run(b, "mallen-erp", "uds:/tmp/evil/200.sock")[0] == "deny"        # not the registered path


def test_account_sessions_task_253(box, tmp_path):
    """Sessions attach to ACCOUNTS: `mallen` reaches every session in its subtree (a still-running erp during the
    cut-over), answers dash, and never reaches another account's session or personal-pa."""
    proc = Path(box["CREWBOSS_PROC_ROOT"])
    _proc(proc, 800, ["claude", "--remote-control", "mallen"], {"CREWBOSS_PROJECT": "mallen", "CREWBOSS_STREAM": "all"})
    _proc(proc, 850, ["claude", "--remote-control", "acme"], {"CREWBOSS_PROJECT": "acme", "CREWBOSS_STREAM": "all"})
    _proc(proc, 700, ["claude", "--remote-control", "my-pa"], {"CREWBOSS_PROJECT": "personal-pa"})
    _proc(proc, 600, ["claude", "--remote-control", "cp"], {"CREWBOSS_PROJECT": "customer-portal"})
    reg = _registry(tmp_path, {"800.json": {"pid": 800, "name": "s-mallen"}, "850.json": {"pid": 850, "name": "s-acme"},
                               "700.json": {"pid": 700, "name": "s-pa"}, "101.json": {"pid": 101, "name": "s-erp"},
                               "200.json": {"pid": 200, "name": "s-dash"}, "600.json": {"pid": 600, "name": "s-cp"}})
    tree = [  # GET /api/projects/tree after migration 0023
        {"slug": "example-dashboard", "parent": None, "session_parent": None, "has_session": True},
        {"slug": "mallen", "parent": "example-dashboard", "session_parent": "example-dashboard", "has_session": True},
        {"slug": "mallen-erp", "parent": "mallen", "session_parent": "mallen", "has_session": False},
        {"slug": "customer-portal", "parent": "mallen-erp", "session_parent": "mallen", "has_session": False},
        {"slug": "acme", "parent": "example-dashboard", "session_parent": "example-dashboard", "has_session": True},
        {"slug": "personal-pa", "parent": "example-dashboard", "session_parent": "example-dashboard", "has_session": True},
    ]
    b = {**box, "CREWBOSS_CLAUDE_SESSIONS": reg, "CREWBOSS_PROJECT_TREE": json.dumps(tree)}
    cases = [
        ("mallen", "s-erp", "allow"),             # down into its own subtree (the retiring erp)
        ("mallen", "s-cp", "allow"),
        ("mallen-erp", "s-mallen", "allow"),      # erp answers its session parent, the account session
        ("mallen", "s-dash", "allow"),            # one step up: dash
        ("example-dashboard", "s-mallen", "allow"),
        ("example-dashboard", "s-acme", "allow"),  # dash reaches every account session
        ("mallen", "s-acme", "deny"),             # two account sessions: siblings, isolated
        ("acme", "s-mallen", "deny"),
        ("acme", "s-erp", "deny"),                # nor another account's projects
        ("acme", "s-cp", "deny"),
        ("mallen", "s-pa", "deny"),
        ("personal-pa", "s-mallen", "deny"),
    ]
    for sender, to, want in cases:
        assert _run(b, sender, to)[0] == want, (sender, to)


def test_the_tree_is_asked_with_the_senders_own_token_task_368(box, tmp_path):
    """GET /api/projects/tree needs a token and answers only the sender's own account (task #368). The hook sends
    CREWBOSS_DASH_TOKEN as a Bearer header (never printing it); without it the dashboard refuses, the tree is empty and
    the hook fails closed. A target in another account is simply absent from the sender's tree: denied."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    mallen_tree = [  # what the dashboard answers the `mallen` account session: its own account only
        {"slug": "mallen", "parent": "example-dashboard", "session_parent": "example-dashboard", "has_session": True},
        {"slug": "mallen-erp", "parent": "mallen", "session_parent": "mallen", "has_session": False},
    ]
    seen = []

    class Tree(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen.append((self.path, self.headers.get("Authorization")))
            if self.path == "/api/projects/tree" and self.headers.get("Authorization") == "Bearer mdd_secret_mallen":
                body, code = json.dumps(mallen_tree).encode(), 200
            else:
                body, code = b'{"detail": "missing Authorization: Bearer <token>"}', 401
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Tree)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        proc = Path(box["CREWBOSS_PROC_ROOT"])
        _proc(proc, 800, ["claude", "--remote-control", "mallen"], {"CREWBOSS_PROJECT": "mallen", "CREWBOSS_STREAM": "all"})
        _proc(proc, 850, ["claude", "--remote-control", "acme"], {"CREWBOSS_PROJECT": "acme", "CREWBOSS_STREAM": "all"})
        reg = _registry(tmp_path, {"101.json": {"pid": 101, "name": "s-erp"}, "850.json": {"pid": 850, "name": "s-acme"},
                                   "200.json": {"pid": 200, "name": "s-dash"}})
        b = {k: v for k, v in box.items() if k != "CREWBOSS_PROJECT_TREE"}
        b.update(CREWBOSS_CLAUDE_SESSIONS=reg, CREWBOSS_DASH_URL=f"http://127.0.0.1:{srv.server_port}")
        with_tok = {**b, "CREWBOSS_DASH_TOKEN": "mdd_secret_mallen"}
        assert _run(with_tok, "mallen", "s-erp")[0] == "allow"      # down into its own account
        assert _run(with_tok, "mallen", "s-dash")[0] == "allow"     # answer its session parent (dash)
        decision, reason = _run(with_tok, "mallen", "s-acme")       # another account: absent from its tree
        assert decision == "deny" and "mdd_secret_mallen" not in reason
        assert ("/api/projects/tree", "Bearer mdd_secret_mallen") in seen
        assert _run(b, "mallen", "s-erp")[0] == "deny"              # no token: the tree is refused, fail closed
        assert ("/api/projects/tree", None) in seen
        # the token never reaches the hook's output
        env = {"PATH": os.environ["PATH"], **with_tok, "CREWBOSS_PROJECT": "mallen"}
        for to in ("s-erp", "s-acme"):
            out = subprocess.run(["bash", str(HOOK)], input=json.dumps({"tool_input": {"to": to}}),
                                 capture_output=True, text=True, timeout=10, env=env)
            assert "mdd_secret_mallen" not in out.stdout + out.stderr
    finally:
        srv.shutdown()
