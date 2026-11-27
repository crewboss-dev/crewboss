#!/bin/bash
# PreToolUse hook, matcher "SendMessage": sessions WITHIN a project may message each other; sessions in
# DIFFERENT projects never may (owner rule, 2026-09-30). Claude Code's SendMessage can reach any local session —
# they all run as the same Linux user — so this guard decides from the SENDER's and the TARGET's CREWBOSS_PROJECT.
#
# - Sender without CREWBOSS_PROJECT (the owner's own / ad-hoc / manager sessions): allowed, no output.
# - Target "main" or an in-process subagent id (a + 16 hex): allowed.
# - Target "uds:/run/user/<uid>/cc-socks/<pid>.sock": <pid> IS the target claude process (verified
#   2026-09-30: every socket name matches a running `claude` pid) -> its /proc/<pid>/environ.
# - Target a bare name (maybe "name [ref]"): the tmux session of that name -> the claude process under its pane;
#   failing that, the Claude session NAME (what ListAgents prints and SendMessage uses, e.g. "stacks-b1") via
#   Claude's session registry ~/.claude/sessions/<pid>.json -> that pid, if it is a live `claude` process.
# - Target any other "uds:<path>" (the reply address a message carries, e.g. /tmp/cc-socks/<pid>.sock): the session
#   whose registry record names exactly that messagingSocketPath. Unregistered paths stay unknown (denied).
# - A target with no CREWBOSS_PROJECT resolves to no project, so it is DENIED (fail closed; the pre-project legacy sessions are gone).
# - Same project: allowed. Different project, or unknown / unresolvable: DENIED (fail closed).
# - SUBTREES (task #253): sessions attach to ACCOUNTS, so a sender may message any session in its own subtree (the
#   an account session reaches a project session under it), and answer its session parent (dash).
#   Two account sessions are siblings under dash: always denied. dash, the root, reaches every session.
#
# Test seams: CREWBOSS_PROC_ROOT (default /proc), CREWBOSS_TMUX (default tmux), CREWBOSS_CLAUDE_SESSIONS (default
# ~/.claude/sessions). Settings: environment CREWBOSS_*, defaults from $CREWBOSS_HOME/sessions.env.
# TODO(#589 compat): every CREWBOSS_ variable here also reads its pre-rename MALLEN_ twin, because sessions started before the
# rename still carry those; remove once every session was restarted.
exec python3 - "$@" 3<&0 <<'PY'
import json, os, re, subprocess, sys


def _file_cfg():
    """$CREWBOSS_HOME/sessions.env as data: {CREWBOSS_KEY: value}."""
    path = os.path.join(os.environ.get("CREWBOSS_HOME") or os.path.join(os.path.expanduser("~"), ".crewboss"), "sessions.env")
    out = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                k, sep, v = line.rstrip("\n").partition("=")
                if sep and re.fullmatch(r"CREWBOSS_[A-Z0-9_]+", k):
                    out[k] = v.replace("${HOME}", os.path.expanduser("~"))
    except OSError:
        pass
    return out


FILE_CFG = _file_cfg()


def opt(name, default=""):
    """CREWBOSS_<name>: the environment, else the pre-rename MALLEN_<name> (TODO(#589 compat)), else sessions.env."""
    return (os.environ.get("CREWBOSS_" + name) or os.environ.get("MALLEN_" + name)
            or FILE_CFG.get("CREWBOSS_" + name) or default)


def cb(env, name):
    """A SESSION's variable (from its /proc environ) by suffix, same fallback."""
    return env.get("CREWBOSS_" + name) or env.get("MALLEN_" + name) or ""


PROC = opt("PROC_ROOT", "/proc")
TMUX = opt("TMUX", "tmux")
CLAUDE_SESSIONS = opt("CLAUDE_SESSIONS") or os.path.expanduser("~/.claude/sessions")


def out(decision=None, reason=""):
    if decision == "deny":
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                                 "permissionDecisionReason": reason}}))
    sys.exit(0)


def environ(pid):
    try:
        with open(f"{PROC}/{int(pid)}/environ", "rb") as fh:
            pairs = [p.split(b"=", 1) for p in fh.read().split(b"\0") if b"=" in p]
        return {k.decode(errors="replace"): v.decode(errors="replace") for k, v in pairs}
    except (OSError, ValueError):
        return None


def cmdline(pid):
    try:
        with open(f"{PROC}/{int(pid)}/cmdline", "rb") as fh:
            return [a.decode(errors="replace") for a in fh.read().split(b"\0") if a]
    except (OSError, ValueError):
        return []


def children(pid):
    kids = []
    try:
        for tid in os.listdir(f"{PROC}/{int(pid)}/task"):
            try:
                with open(f"{PROC}/{int(pid)}/task/{tid}/children") as fh:
                    kids += [int(x) for x in fh.read().split()]
            except OSError:
                pass
    except (OSError, ValueError):
        pass
    return kids


def is_claude(pid):
    argv = cmdline(pid)
    return bool(argv) and os.path.basename(argv[0]) == "claude"


def tmux_panes():
    """[(session_name, pane_pid)] for every pane."""
    try:
        res = subprocess.run([TMUX, "list-panes", "-a", "-F", "#{session_name}\t#{pane_pid}"],
                             capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    panes = []
    for line in res.stdout.splitlines():
        name, _, pid = line.partition("\t")
        if pid.strip().isdigit():
            panes.append((name, int(pid)))
    return panes


def project_of_pid(pid):
    env = environ(pid)
    if env is None:
        return None
    return cb(env, "PROJECT") or None


def project_of_tmux_session(name):
    panes = [p for n, p in tmux_panes() if n == name]
    for pane in panes:
        todo, found = [pane], []
        while todo:
            p = todo.pop()
            found.append(p)
            todo += children(p)
        claude = [p for p in found if is_claude(p)] or found
        for p in claude:
            proj = project_of_pid(p)
            if proj:
                return proj
    return None


def project_of_claude_name(name):
    """A Claude session name (ListAgents / SendMessage) -> its pid via the registry -> its project. Only a file
    whose own pid matches its filename and is a live `claude` process counts; anything odd resolves to None."""
    try:
        files = os.listdir(CLAUDE_SESSIONS)
    except OSError:
        return None
    pids = []
    for f in files:
        if not re.fullmatch(r"\d+\.json", f):
            continue
        try:
            with open(os.path.join(CLAUDE_SESSIONS, f)) as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(rec, dict) and rec.get("name") == name and str(rec.get("pid")) == f[:-5]:
            pids.append(int(f[:-5]))
    live = [p for p in pids if is_claude(p)]
    if len(live) != 1:  # none, or an ambiguous name: unresolvable
        return None
    return project_of_pid(live[0])


def project_of_socket(path):
    """A uds: reply address (the form a message's sender carries, e.g. /tmp/cc-socks/<pid>.sock) -> the session whose
    registry record names EXACTLY that messagingSocketPath -> its project. Unregistered paths resolve to None."""
    try:
        files = os.listdir(CLAUDE_SESSIONS)
    except OSError:
        return None
    for f in files:
        if not re.fullmatch(r"\d+\.json", f):
            continue
        try:
            with open(os.path.join(CLAUDE_SESSIONS, f)) as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            continue
        if (isinstance(rec, dict) and rec.get("messagingSocketPath") == path and str(rec.get("pid")) == f[:-5]
                and is_claude(int(f[:-5]))):
            return project_of_pid(int(f[:-5]))
    return None


try:
    data = json.loads(os.fdopen(3).read() or "{}")
except ValueError:
    data = {}
sender = os.environ.get("CREWBOSS_PROJECT") or os.environ.get("MALLEN_PROJECT") or ""   # TODO(#589 compat)
if not sender:
    out()  # the owner's / ad-hoc / manager sessions are not project-bound
to = str(((data.get("tool_input") or {}).get("to")) or "").strip()
to = re.sub(r"\s*\[[^\]]*\]\s*$", "", to)  # "name [ref]" -> "name"

if to == "main" or re.fullmatch(r"a[0-9a-f]{16}", to):
    out()  # in-process: the sender's own subagents / main thread

target = None
m = re.fullmatch(r"uds:(/run/user/\d+/cc-socks/(\d+)\.sock)", to)
if m:
    target = project_of_pid(int(m.group(2)))
elif to.startswith("uds:"):
    target = project_of_socket(to[4:])
elif re.fullmatch(r"[A-Za-z0-9._-]{1,100}", to):
    target = project_of_tmux_session(to) or project_of_claude_name(to)

if target and target == sender:
    out()


def project_tree():
    """[{slug, parent, session_parent, has_session}] from the dashboard (GET /api/projects/tree: shape only, no
    task data), asked with THIS session's own token (CREWBOSS_DASH_TOKEN, set by the launcher; task #368: the tree
    needs a token and answers only the sender's own account, so a target in another account is simply absent).
    The token rides in a request header only — never printed, logged or put on a command line. Test seam:
    CREWBOSS_PROJECT_TREE (the same JSON). Unreachable, refused (no / bad token) = [] = fail closed."""
    raw = os.environ.get("CREWBOSS_PROJECT_TREE") or os.environ.get("MALLEN_PROJECT_TREE")   # TODO(#589 compat)
    if raw is None:
        import urllib.request
        base = (opt("DASH_URL") or "http://127.0.0.1:8931").rstrip("/")
        req = urllib.request.Request(base + "/api/projects/tree")
        token = (os.environ.get("CREWBOSS_DASH_TOKEN") or os.environ.get("MALLEN_DASH_TOKEN") or "").strip()   # TODO(#589 compat)
        if token:
            req.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(req, timeout=3) as resp:
                raw = resp.read().decode()
        except Exception:  # noqa: BLE001 — no answer, no permission (never echo the error: it may carry the request)
            return []
    try:
        rows = json.loads(raw)
        return rows if isinstance(rows, list) else []
    except ValueError:
        return []


# PARENT ORDER, ALWAYS GOING DOWN (owner, 2026-10-01 + 2026-10-02; dashboard k30): a session may message ANY session
# below it in the project tree (dash, the root, therefore reaches every session), and may answer only the session
# directly above it (its session parent: the nearest ancestor that runs one). Siblings and unrelated projects stay
# apart; they talk through their shared parent.
ROOT = opt("ROOT_PROJECT")   # the overseer project's slug (sessions.env); none = nobody is the root
if ROOT and sender == ROOT and target != sender:
    out()  # the top session controls every session, even if the dashboard (the tree) is unreachable
if target:
    rows = [r for r in project_tree() if isinstance(r, dict)]
    parent = {r.get("slug"): r.get("parent") for r in rows}
    session_parent = {r.get("slug"): r.get("session_parent") for r in rows}
    seen, cur = set(), parent.get(target)
    while cur and cur not in seen:  # walk up from the target: is the sender one of its ancestors?
        if cur == sender:
            out()
        seen.add(cur)
        cur = parent.get(cur)
    if session_parent.get(sender) == target:
        out()  # one step up: answer your own session parent
out("deny", f"cross-project messaging is not allowed; {sender} → {target or 'unknown'}")
PY
