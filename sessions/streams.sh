# Shared by the launcher, hooks and notifier. Each ACCOUNT runs one standing Claude session named by its short name
# (task #253), which works every project under it; the overseer project's session ("dash") reads across accounts; any other
# node may run its own. A session is <short> (one session for every stream) or <short>-<role> (acme-bugs, acme-review...).
# The plan comes from the dashboard container (`docker compose exec -T app python -m app.sessions_plan`, the same JSON as
# GET /api/sessions/plan, which is the overseer's only since task #368).
# Safe under `set -u` (the launcher sets it): every environment read has a default.
#
# Configuration is environment (CREWBOSS_*), with defaults from $CREWBOSS_HOME/sessions.env (KEY=value lines;
# sessions/sessions.example.env lists every key). A value already in the environment wins over the file.
STREAMS="features reviewer bugs tweaks errors"

# sessions.env: only CREWBOSS_* keys, read as data (never sourced); ${HOME} is expanded.
_cb_load_env() {
  local f="${CREWBOSS_HOME:-${HOME:-}/.crewboss}/sessions.env" k v
  [ -r "$f" ] || return 0
  while IFS='=' read -r k v || [ -n "$k" ]; do
    case "$k" in CREWBOSS_[A-Z0-9_]*) ;; *) continue;; esac
    case "$k" in *[!A-Z0-9_]*) continue;; esac
    [ -z "${!k:-}" ] || continue
    v="${v//\$\{HOME\}/${HOME:-}}"
    export "$k=$v"
  done < "$f"
}
# TODO(#589 compat, remove once every standing session was restarted after the rename): sessions started before it carry the
# old variable prefix. The new name wins; the old one fills it in.
_cb_compat() {
  local v new old
  for v in "$@"; do
    new="CREWBOSS_$v"; old="MALLEN_$v"
    if [ -z "${!new:-}" ] && [ -n "${!old:-}" ]; then export "$new=${!old}"; fi
  done
}
_cb_load_env
_cb_compat STREAM PROJECT PROJECT_SHORT SESSION_NAME DASH_URL DASH_TOKEN DASH_COMPOSE_DIR TOKEN_DIR STATE_DIR SESSIONS_CWD \
  SYNC_GRACE LOCK_WAIT PLAN_TIMEOUT PLAN_TRIES PLAN_WAIT NO_MINT FALLBACK_SESSION GROUND_PROJECT PROC_ROOT ROOT_PROJECT \
  REFRESH_DISABLE REFRESH_NOW REFRESH_NOTICE_MIN REFRESH_RECHECK

DASH_URL="${CREWBOSS_DASH_URL:-http://127.0.0.1:8931}"
# Tokens: one 600 file per (node, stream): $TOKEN_DIR/<short>/<stream>  ('all' for a one-session node).
TOKEN_DIR="${CREWBOSS_TOKEN_DIR:-${HOME:-}/.config/crewboss/tokens}"
# The boot fallback when the plan can't be fetched: "slug short role session_name" (the account's own session). Unset = none.
FALLBACK_SESSION="${CREWBOSS_FALLBACK_SESSION:-}"

# role -> the suffix in a session name (reviewer reads better as "review")
cb_role_suffix() { case "${1:-}" in reviewer) echo review;; *) echo "${1:-}";; esac; }
cb_suffix_role() { case "${1:-}" in review) echo reviewer;; *) echo "${1:-}";; esac; }
# <short> <role> -> the session (tmux + --remote-control) name: <short> for 'all', else <short>-<suffix>
cb_session_name() { [ "${2:-}" = "all" ] && echo "${1:-}" || echo "${1:-}-$(cb_role_suffix "${2:-}")"; }

_cb_tmux_name() {
  [ -n "${TMUX:-}" ] || return 0
  tmux display-message -p '#S' 2>/dev/null || true
}

# Which stream is THIS process? $CREWBOSS_STREAM (set by the launcher; 'all' for a one-session project), else
# from the tmux session name: <short>-<suffix> (acme-review -> reviewer).
# Empty = not a stream session.
cb_stream() {
  if [ -n "${CREWBOSS_STREAM:-}" ]; then echo "$CREWBOSS_STREAM"; return; fi
  local n; n=$(_cb_tmux_name); [ -n "$n" ] || return 0
  local suffix=${n##*-} s
  [ "$suffix" != "$n" ] || return 0
  s=$(cb_suffix_role "$suffix")
  case " $STREAMS " in *" $s "*) echo "$s";; esac
}

# Which project (short name) is THIS process? $CREWBOSS_PROJECT_SHORT, else the tmux name's prefix.
cb_project_short() {
  if [ -n "${CREWBOSS_PROJECT_SHORT:-}" ]; then echo "$CREWBOSS_PROJECT_SHORT"; return; fi
  local n; n=$(_cb_tmux_name); [ -n "$n" ] || return 0
  [ "${n%-*}" != "$n" ] && echo "${n%-*}"
  return 0
}

# A token FILE only (the launcher must never inherit a caller's env token):
#   cb_token_file <stream>          -> $TOKEN_DIR/<stream> (a role token such as scanner)
#   cb_token_file <short> <stream>  -> $TOKEN_DIR/<short>/<stream> (acme all -> tokens/acme/all)
cb_token_file() {
  local f
  f=$(cb_token_path "$@")
  [ -n "$f" ] && tr -d '[:space:]' < "$f"
  return 0
}

# The token FILE's path (same lookup as cb_token_file), or nothing. The launcher hands sessions this PATH,
# never the token itself, so a bearer token is never on a command line.
cb_token_path() {
  local f=""
  if [ $# -ge 2 ]; then
    [ -n "${1:-}" ] && [ -n "${2:-}" ] && f="$TOKEN_DIR/$1/$2"
  else
    [ -n "${1:-}" ] && f="$TOKEN_DIR/$1"
  fi
  [ -n "$f" ] && [ -f "$f" ] && [ -r "$f" ] && echo "$f"
  return 0
}

# Token for THIS session: env first (set by the launcher), else its project's token file.
cb_token() {
  if [ -n "${CREWBOSS_DASH_TOKEN:-}" ]; then echo "$CREWBOSS_DASH_TOKEN"; return; fi
  cb_token_file "$(cb_project_short)" "${1:-}"
}
