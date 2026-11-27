#!/bin/bash
# Every standing Claude session in tmux, each with Remote Control on and named for its node + role. Sessions attach
# to ACCOUNTS (task #253): an account's one session is named by its short name and works every project under it; the
# overseer's session reads across accounts. A node with ONE session for every stream is just its short name.
#
#   crewboss-sessions.sh start            every session in the plan (boot: the claude-sessions service)
#   crewboss-sessions.sh start <short>    one project's set (manual fallback)
#   crewboss-sessions.sh sync             keep tmux in step with the plan (the sync timer, every minute):
#                                       start what's missing; wind down what left the plan (archived project /
#                                       sessions turned off): tell it to wrap up, stop it after the grace period
#   crewboss-sessions.sh refresh          nightly fresh restart of every planned session (the refresh timer, idle only;
#                                       sessions/refresh.py; README "Nightly refresh")
#   crewboss-sessions.sh status [<short>]
#
# The plan (no secrets) is read INSIDE the dashboard container, `docker compose exec -T app python -m app.sessions_plan`
# — the same JSON as GET /api/sessions/plan, which is the overseer's only since task #368 (it spans every account, and
# the launcher holds no token before it starts the sessions). Each session's token is its OWN file,
# $TOKEN_DIR/<short>/<stream> (mode 600; an account session's is tokens/<short>/all); a missing one is minted here
# (the dashboard never writes host files and never runs tmux). A session already running under its name — or, for
# the retired legacy session only (sessions.env), its legacy name; TODO #253 cut-over — is left alone. `start` and `sync` hold one lock, so boot and the timer never race.
# Settings: environment CREWBOSS_*, defaults from $CREWBOSS_HOME/sessions.env (sessions/sessions.example.env).
set -u
. "$(dirname "$(readlink -f "$0")")/streams.sh"
# Numbers from the environment or sessions.env reach `for ((...))`, `[ -lt ]`, sleep, flock and python int(): anything that is not
# plain digits falls back to the default, so a value such as 'HOME[$(cmd)]' can never be evaluated by bash arithmetic.
_cb_int() {  # <value> <default>
  case "${1:-}" in ''|*[!0-9]*) echo "$2";; *) echo "$1";; esac
}
CREWBOSS_PLAN_TRIES=$(_cb_int "${CREWBOSS_PLAN_TRIES:-}" 12)
CREWBOSS_PLAN_WAIT=$(_cb_int "${CREWBOSS_PLAN_WAIT:-}" 5)
CREWBOSS_PLAN_TIMEOUT=$(_cb_int "${CREWBOSS_PLAN_TIMEOUT:-}" 20)
CREWBOSS_LOCK_WAIT=$(_cb_int "${CREWBOSS_LOCK_WAIT:-}" 300)
CREWBOSS_SYNC_GRACE=$(_cb_int "${CREWBOSS_SYNC_GRACE:-}" 600)
WORKDIR="${CREWBOSS_SESSIONS_CWD:-${HOME:-/}}"
COMPOSE_DIR="${CREWBOSS_DASH_COMPOSE_DIR:-$(dirname "$(dirname "$(readlink -f "$0")")")}"
STATE_DIR="${CREWBOSS_STATE_DIR:-${HOME:-/tmp}/.local/state/crewboss-sessions}"
GRACE="${CREWBOSS_SYNC_GRACE:-600}"   # seconds between "wrap up" and tmux kill-session
cmd="${1:-status}"
only="${2:-}"

# One lock for start + sync: the boot start waits for a running sync; a sync skips a minute while anything runs.
if [ "$cmd" = "start" ] || [ "$cmd" = "sync" ] || [ "$cmd" = "refresh" ]; then
  mkdir -p "$STATE_DIR" && chmod 700 "$STATE_DIR"
  exec 9>"$STATE_DIR/lock"
  if [ "$cmd" = "sync" ]; then
    flock -n 9 || { echo "sync: another start/sync holds $STATE_DIR/lock; skipping this run"; exit 0; }
  else
    flock -w "${CREWBOSS_LOCK_WAIT:-300}" 9 || { echo "start: $STATE_DIR/lock is busy" >&2; exit 1; }
  fi
fi

# The plan, one "slug short role session_name" line per session. `plan strict` (sync) fails when the dashboard
# can't be reached — nothing is started or stopped on a guess. Plain `plan` (boot) retries, then falls back to
# $FALLBACK_SESSION (the account's own session, streams.sh) so the owner's main session always comes up.
# Read from the dashboard container (app/sessions_plan.py), not over HTTP: no token needed, nothing on the network.
fetch_plan() {
  (cd "$COMPOSE_DIR" 2>/dev/null && timeout "${CREWBOSS_PLAN_TIMEOUT:-20}" docker compose exec -T app \
     python -m app.sessions_plan 2>/dev/null)
}
plan() {
  local strict="${1:-}" tries="${CREWBOSS_PLAN_TRIES:-12}" i json
  [ -n "$strict" ] && tries=1
  for ((i = 1; i <= tries; i++)); do
    if json=$(fetch_plan); then
      J="$json" python3 - <<'PY' && return 0
import json, os, re
for e in json.loads(os.environ["J"]):
    vals = [str(e.get(k, "")) for k in ("project", "short_name", "role", "session_name")]
    # every field is used in a shell command line: accept only the dashboard's own validated shapes
    if all(re.fullmatch(r"[a-z0-9][a-z0-9-]{0,49}", v) for v in vals):
        print(" ".join(vals))
PY
    fi
    [ "$i" -lt "$tries" ] && sleep "${CREWBOSS_PLAN_WAIT:-5}"
  done
  [ -n "$strict" ] && return 1
  echo "WARNING: the sessions plan (python -m app.sessions_plan in $COMPOSE_DIR) is unreachable; starting the fallback session only ($FALLBACK_SESSION)" >&2
  echo "$FALLBACK_SESSION"
}

running() { tmux has-session -t "=$1" 2>/dev/null; }

# A missing token file is minted by the dashboard container into a 700 dir / 600 file.
mint_token() {  # <slug> <short> <role> -> prints the token (or nothing)
  local dir="$TOKEN_DIR/$2" file="$TOKEN_DIR/$2/$3" tok
  [ -n "${CREWBOSS_NO_MINT:-}" ] && return 0
  (umask 077; mkdir -p "$dir") || return 0
  tok=$(cd "$COMPOSE_DIR" 2>/dev/null && docker compose exec -T app \
        python -m app.tokens create --project "$1" --stream "$3" --name "$(cb_session_name "$2" "$3")" --quiet 2>/dev/null \
        | tr -d '[:space:]')
  # cb_* (and crewboss_*) are the current token prefixes; mdd_* is the pre-rename one (TODO(#589 compat): drop it with app/auth.py LEGACY_SESSION_PREFIXES)
  case "$tok" in crewboss_*|cb_*|mdd_*) (umask 077; printf '%s\n' "$tok" > "$file"); echo "$tok";; esac
  return 0
}

# Start (or, for status, report) each plan line on stdin. $1 = start|status.
start_sessions() {
  local mode="$1" found=0 slug short role name tok
  while read -r slug short role name; do
    [ -n "$name" ] || continue
    [ -z "$only" ] || [ "$only" = "$short" ] || continue
    found=1
    if running "$name"; then [ "$cmd" = "sync" ] || [ "$cmd" = "refresh" ] || echo "$name: running"; continue; fi
    if [ "$mode" != "start" ]; then echo "$name: not running"; continue; fi
    tok=$(cb_token_file "$short" "$role")
    [ -n "$tok" ] || tok=$(mint_token "$slug" "$short" "$role")
    tokpath=$(cb_token_path "$short" "$role")
    [ -n "$tok" ] || echo "$name: WARNING no dashboard token in $TOKEN_DIR/$short/$role — starting ungrounded" >&2
    # Tell the dashboard this session is starting: until the channel server heartbeats (after the owner confirms
    # the development-channel warning Claude Code shows at every start), its Sessions panel reads "waiting for you".
    # The token reaches curl on stdin (-H @-), never on a command line.
    [ -n "$tok" ] && printf 'Authorization: Bearer %s\n' "$tok" | curl -sf --max-time 5 -X POST -H @- \
      -H "Content-Type: application/json" \
      -d "{\"state\":\"starting\",\"project\":\"$slug\"}" "$DASH_URL/api/sessions/heartbeat" >/dev/null 2>&1
    # Env rides into the session: the hooks, the dashboard MCP header (Bearer ${CREWBOSS_DASH_TOKEN}), the
    # same-project-only SendMessage guard and `sync` read these. --dangerously-load-development-channels loads the
    # dashboard channel (sessions/channel/index.mjs); on a personal plan each start needs ONE confirmation.
    # CREWBOSS_DASH_TOKEN is NOT passed with -e (that put the bearer token on tmux's command line, visible in ps and
    # systemctl status): the session's shell reads it from its 0600 token FILE at start-up, so only the path is
    # ever on a command line, and claude gets it through its environment.
    # TODO(#589 compat): the pre-rename MALLEN_* names are exported too, so the MCP header in a user's Claude config
    # (Bearer ${MALLEN_DASH_TOKEN}) and tools that read them keep working; remove with the shim in streams.sh.
    tokload="export CREWBOSS_DASH_TOKEN=; export MALLEN_DASH_TOKEN="
    if [ -n "$tokpath" ]; then
      case "$tokpath" in *"'"*) echo "$name: WARNING token path has a quote; starting ungrounded" >&2;;
        *) tokload="export CREWBOSS_DASH_TOKEN=\"\$(tr -d '[:space:]' < '$tokpath')\"; export MALLEN_DASH_TOKEN=\"\$CREWBOSS_DASH_TOKEN\"";; esac
    fi
    tmux new-session -d -s "$name" -c "$WORKDIR" \
      -e CREWBOSS_STREAM="$role" -e CREWBOSS_PROJECT="$slug" -e CREWBOSS_PROJECT_SHORT="$short" \
      -e CREWBOSS_SESSION_NAME="$name" -e CREWBOSS_DASH_URL="$DASH_URL" \
      -e MALLEN_STREAM="$role" -e MALLEN_PROJECT="$slug" -e MALLEN_PROJECT_SHORT="$short" \
      -e MALLEN_SESSION_NAME="$name" -e MALLEN_DASH_URL="$DASH_URL" \
      "$tokload; exec claude --remote-control $name --dangerously-load-development-channels server:dashboard-channel" 9>&-
    # (9>&-: a tmux server forked here must not inherit the lock fd, or it would hold the lock forever)
    echo "$name: started"
  done
  if [ -n "$only" ] && [ "$found" = 0 ]; then
    echo "no project with short name '$only' runs sessions (check its sessions setting in the dashboard)" >&2
    return 1
  fi
  return 0
}

# sync, part 2: wind down sessions that left the plan. Only a tmux session whose claude process carries
# CREWBOSS_PROJECT (or the pre-rename MALLEN_PROJECT), and whose name is the one the launcher would give it (or, for the
# legacy project, a legacy name), is ever touched — never the owner's / manager sessions (no project) or anything else.
wind_down() {
  PLAN_LINES="$1" STATE_DIR="$STATE_DIR" GRACE="$GRACE" DASH_URL="$DASH_URL" TOKEN_DIR="$TOKEN_DIR" python3 - <<'PY'
import json, os, re, subprocess, time

PROC = os.environ.get("CREWBOSS_PROC_ROOT") or os.environ.get("MALLEN_PROC_ROOT", "/proc")   # TODO(#589 compat): MALLEN_ fallback
STATE = os.path.join(os.environ["STATE_DIR"], "stopping")
GRACE = int(os.environ.get("GRACE", "600"))
URL = os.environ["DASH_URL"]
SUFFIX = {"features": "features", "reviewer": "review", "bugs": "bugs", "tweaks": "tweaks", "errors": "errors"}
plan = set()
for line in os.environ.get("PLAN_LINES", "").splitlines():
    parts = line.split()
    if len(parts) == 4:
        plan.add((parts[0], parts[2]))
os.makedirs(STATE, mode=0o700, exist_ok=True)


def read(path, mode="rb"):
    try:
        with open(path, mode) as fh:
            return fh.read()
    except OSError:
        return None


def cb(env, name):
    """A session variable by its suffix: CREWBOSS_<name>, else the pre-rename MALLEN_<name> (TODO(#589 compat))."""
    return env.get("CREWBOSS_" + name) or env.get("MALLEN_" + name) or ""


def environ(pid):
    raw = read(f"{PROC}/{pid}/environ")
    if raw is None:
        return {}
    return {k.decode(errors="replace"): v.decode(errors="replace")
            for k, _, v in (p.partition(b"=") for p in raw.split(b"\0") if b"=" in p)}


def children(pid):
    kids = []
    for tid in os.listdir(f"{PROC}/{pid}/task") if os.path.isdir(f"{PROC}/{pid}/task") else []:
        raw = read(f"{PROC}/{pid}/task/{tid}/children", "r")
        kids += [int(x) for x in (raw or "").split()]
    return kids


def is_claude(pid):
    raw = read(f"{PROC}/{pid}/cmdline") or b""
    argv = [a for a in raw.split(b"\0") if a]
    return bool(argv) and os.path.basename(argv[0].decode(errors="replace")) == "claude"


def session_env(pane_pids):
    """The env of the claude process under a tmux session's panes (the pane process itself or a descendant)."""
    for pane in pane_pids:
        todo, seen = [pane], []
        while todo:
            p = todo.pop()
            seen.append(p)
            todo += children(p)
        for p in [q for q in seen if is_claude(q)]:
            env = environ(p)
            if cb(env, "PROJECT"):
                return env
    return None


def post(token, phase):
    """POST /api/sessions/stopping with the session's own token -> HTTP status (0 = unreachable)."""
    # The token reaches curl on stdin (-H @-), never on its command line (visible in ps / systemctl status).
    res = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "5", "-X", "POST",
                          "-H", "@-", "-H", "Content-Type: application/json",
                          "-d", json.dumps({"phase": phase}), f"{URL}/api/sessions/stopping"],
                         input=f"Authorization: Bearer {token}\n", capture_output=True, text=True)
    return int(res.stdout.strip() or 0) if res.stdout.strip().isdigit() else 0


def token_for(env, short, role):
    if cb(env, "DASH_TOKEN"):
        return cb(env, "DASH_TOKEN")
    raw = read(f"{os.environ['TOKEN_DIR']}/{short}/{role}", "r")
    return raw.strip() if raw and raw.strip() else ""


res = subprocess.run(["tmux", "list-panes", "-a", "-F", "#{session_name}\t#{pane_pid}"], capture_output=True, text=True)
sessions: dict = {}
for line in res.stdout.splitlines():
    name, _, pid = line.partition("\t")
    if pid.strip().isdigit():
        sessions.setdefault(name, []).append(int(pid))

now = int(time.time())
for name, panes in sorted(sessions.items()):
    env = session_env(panes)
    if not env:
        continue  # not a project session (the owner's / a manager session): never touched
    project, role, short = cb(env, "PROJECT"), cb(env, "STREAM"), cb(env, "PROJECT_SHORT")
    expected = short if role == "all" else f"{short}-{SUFFIX.get(role, '?')}"
    if not short or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,49}", name) or name != expected:
        continue  # not named the way the launcher names sessions: not ours to manage
    state = os.path.join(STATE, name)
    since = read(state, "r")
    if (project, role) in plan:
        if since is not None:  # back in the plan (e.g. unarchived) while winding down: carry on as normal
            post(token_for(env, short, role), "cancel")
            os.remove(state)
            print(f"{name}: back in the plan")
        continue
    token = token_for(env, short, role)
    if since is None:
        code = post(token, "notice")
        if code == 200:
            with open(state, "w") as fh:
                fh.write(str(now))
            print(f"{name}: left the plan; told to wrap up, stopping in {GRACE // 60} min")
        else:
            print(f"{name}: left the plan, but the dashboard answered {code or 'nothing'}; not stopping it")
        continue
    if now - int(since.strip() or now) < GRACE:
        continue
    subprocess.run(["tmux", "kill-session", "-t", f"={name}"], capture_output=True)
    post(token, "stopped")
    os.remove(state)
    print(f"{name}: stopped")

for leftover in os.listdir(STATE):  # a session that ended by itself while winding down
    if leftover not in sessions:
        os.remove(os.path.join(STATE, leftover))
PY
}

case "$cmd" in
  start) plan | start_sessions start ;;
  sync)
    lines=$(plan strict) || { echo "sync: the sessions plan (python -m app.sessions_plan) is unreachable; nothing started or stopped"; exit 0; }
    only=""
    printf '%s\n' "$lines" | start_sessions start
    wind_down "$lines"
    ;;
  refresh)
    # Nightly fresh restart (#578). Lock held (blocking, like start): a sync mid-run finishes first. refresh.py kills
    # the idle sessions; then every missing planned session is started right here with the same start function
    # (its existing token file), instead of waiting up to a minute for the next sync.
    lines=$(plan strict) || { echo "refresh: the sessions plan (python -m app.sessions_plan) is unreachable; nothing done"; exit 0; }
    only=""
    out=$(PLAN_LINES="$lines" STATE_DIR="$STATE_DIR" DASH_URL="$DASH_URL" TOKEN_DIR="$TOKEN_DIR" \
          python3 "$(dirname "$(readlink -f "$0")")/refresh.py" 9>&-); rc=$?
    printf '%s\n' "$out" | grep -v '^KILLED '
    [ "$rc" = 0 ] || { echo "refresh: refresh.py failed ($rc); nothing restarted"; exit 0; }
    if printf '%s\n' "$out" | grep -q '^KILLED '; then
      printf '%s\n' "$lines" | start_sessions start
    fi
    ;;
  *) plan | start_sessions status ;;
esac
