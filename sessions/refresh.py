#!/usr/bin/env python3
"""Nightly refresh of the standing Claude sessions (task #578; `crewboss-sessions.sh refresh` runs this).

Long-lived sessions re-read an ever larger context on every turn, so each planned session is restarted fresh once a
morning. State lives in the Dashboard and memory, so little is lost. Window, local time in CREWBOSS_REFRESH_TZ (default UTC;
the times below are the defaults, the timer unit decides which days run):
  07:20  every running PLANNED session gets the fixed refresh notice (POST /api/sessions/stopping {"phase":"refresh"}).
  07:30  a session is killed (tmux kill-session) only when idle, and only once its notice is at least NOTICE_MIN old.
         The timer re-runs this every 5 minutes; a busy session waits for the next run.
  09:00  the window ends. NEVER forced: a session still busy is skipped that day and tried again the next morning.
         (Each restart stops at Claude Code's dev-channel confirmation until the owner attaches to tmux, so a
         pre-dawn forced restart would leave sessions dead until morning; interim until #615 / change #49.)
  (The caller starts every missing planned session after a kill.)
Never touched: a session that is not in the plan (wind_down owns it), or that is not a launcher-named project session.
Everything is logged to stdout (the journal). Prints "KILLED <name>" lines the shell wrapper acts on.

Env: PLAN_LINES ("slug short role name" per line), STATE_DIR, DASH_URL, TOKEN_DIR, CREWBOSS_REFRESH_DISABLE,
CREWBOSS_REFRESH_NOW (epoch, tests), CREWBOSS_REFRESH_NOTICE_MIN (seconds, default 540), CREWBOSS_REFRESH_RECHECK
(seconds between the two idle looks, default 3), CREWBOSS_PROC_ROOT (tests), CREWBOSS_REFRESH_TZ (IANA zone the window is read
in, default UTC), CREWBOSS_REFRESH_WINDOW ("HH:MM,HH:MM,HH:MM" = notice, restart, end; default 07:20,07:30,09:00).
TODO(#589 compat): every CREWBOSS_ variable here also reads its pre-rename MALLEN_ twin."""
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import time
from zoneinfo import ZoneInfo



def opt(name, default=""):
    """CREWBOSS_<name>, else the pre-rename MALLEN_<name> (TODO(#589 compat)), else the default."""
    return os.environ.get("CREWBOSS_" + name) or os.environ.get("MALLEN_" + name) or default


def cb(env, name):
    """A SESSION's variable (from its /proc environ) by suffix, same fallback."""
    return env.get("CREWBOSS_" + name) or env.get("MALLEN_" + name) or ""


PROC = opt("PROC_ROOT", "/proc")
STATE_ROOT = os.path.join(os.environ["STATE_DIR"], "refresh")
URL = os.environ["DASH_URL"]
TZ = ZoneInfo(opt("REFRESH_TZ", "UTC"))
NOTICE_MIN = int(opt("REFRESH_NOTICE_MIN", "540"))
RECHECK = float(opt("REFRESH_RECHECK", "3"))
SUFFIX = {"features": "features", "reviewer": "review", "bugs": "bugs", "tweaks": "tweaks", "errors": "errors"}
def _hm(text):
    h, m = text.strip().split(":")
    return int(h), int(m)


NOTICE_AT, RESTART_AT, END_AT = (_hm(x) for x in opt("REFRESH_WINDOW", "07:20,07:30,09:00").split(","))
FORCE_AT = None  # None = never force

# Claude Code prints "esc to interrupt" in its status line for as long as a turn runs (spinner line:
# "✻ Working… (42s · ↑ 1.2k tokens · esc to interrupt)"). A permission/choice dialog waits for the owner instead.
BUSY_MARKERS = ("esc to interrupt", "ctrl+c to interrupt")
DIALOG_MARKERS = ("esc to cancel", "do you want to proceed", "enter to confirm")
# Background work keeps running after the prompt returns: "✻ Waiting for 1 background agent to finish", and the status
# bar's "· 2 shells" / "· 1 agent" segment (builds, tests). Conservative: any of these is busy (never forced).
BACKGROUND_MARKERS = ("background agent", "waiting for")
BACKGROUND_BAR = re.compile(r"·\s*\d+\s+(shells?|agents?|bashes|background)", re.I)
# Completed-turn summary Claude Code leaves behind, e.g. "✻ Sautéed for 40s · done 7:20 AM": no ellipsis, not busy.
DONE_LINE = re.compile(r"^\s*\S\s+[\w-]+\s+for\s+(\d+[hms]\s*)+(·\s*done\b[^…]*)?$")
SPINNER = re.compile(r"^\s*(✻\s|\S\s+[A-Za-z-]+…\s*\(.*\d+s)")  # other "✻ " status lines count as busy; completed-turn summary lines (DONE_LINE) are not
TAIL_LINES = 15  # only the live bottom of the pane: quoted text further up must not read as "busy"


def log(msg):
    print(msg, flush=True)


def read(path, mode="rb"):
    try:
        with open(path, mode) as fh:
            return fh.read()
    except OSError:
        return None


def environ(pid):
    raw = read(f"{PROC}/{pid}/environ")
    if raw is None:
        return {}
    return {k.decode(errors="replace"): v.decode(errors="replace")
            for k, _, v in (p.partition(b"=") for p in raw.split(b"\0") if b"=" in p)}


def children(pid):
    kids = []
    d = f"{PROC}/{pid}/task"
    for tid in os.listdir(d) if os.path.isdir(d) else []:
        kids += [int(x) for x in (read(f"{d}/{tid}/children", "r") or "").split()]
    return kids


def is_claude(pid):
    argv = [a for a in (read(f"{PROC}/{pid}/cmdline") or b"").split(b"\0") if a]
    return bool(argv) and os.path.basename(argv[0].decode(errors="replace")) == "claude"


def session_env(pane_pids):
    """Env of the claude process under a tmux session's panes (same test as wind_down in crewboss-sessions.sh)."""
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


def token_for(env, short, role):
    if cb(env, "DASH_TOKEN"):
        return cb(env, "DASH_TOKEN")
    raw = read(f"{os.environ['TOKEN_DIR']}/{short}/{role}", "r")
    return raw.strip() if raw and raw.strip() else ""


def post(token, phase):
    # token via stdin (-H @-), never on curl's command line
    res = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "--max-time", "5", "-X", "POST",
                          "-H", "@-", "-H", "Content-Type: application/json", "-d", json.dumps({"phase": phase}),
                          f"{URL}/api/sessions/stopping"],
                         input=f"Authorization: Bearer {token}\n", capture_output=True, text=True)
    out = res.stdout.strip()
    return int(out) if out.isdigit() else 0


def pane_busy(name):
    """True when the live bottom of the pane shows Claude working or waiting on a dialog."""
    res = subprocess.run(["tmux", "capture-pane", "-p", "-t", f"={name}:"], capture_output=True, text=True)
    if res.returncode != 0:
        return True  # can't look: not provably idle
    tail = [ln for ln in res.stdout.splitlines() if ln.strip()][-TAIL_LINES:]
    for line in tail:
        if DONE_LINE.match(line):
            continue  # "✻ Sautéed for 40s · done 7:20 AM": the turn finished
        low = line.lower()
        if any(m in low for m in BUSY_MARKERS + DIALOG_MARKERS + BACKGROUND_MARKERS) \
                or SPINNER.match(line) or BACKGROUND_BAR.search(line):
            return True
    return False


def idle(name):
    """Idle = two looks RECHECK seconds apart both show no busy marker (a turn flickering between tool calls)."""
    if pane_busy(name):
        return False
    time.sleep(RECHECK)
    return not pane_busy(name)


def main():
    if opt("REFRESH_DISABLE") == "1":
        log("refresh: disabled (CREWBOSS_REFRESH_DISABLE=1); nothing done")
        return
    now = int(opt("REFRESH_NOW") or time.time())
    local = dt.datetime.fromtimestamp(now, TZ)
    day, hm = local.strftime("%Y-%m-%d"), (local.hour, local.minute)
    skip = os.path.join(os.environ["STATE_DIR"], f"skip-refresh-{day}")
    if os.path.exists(skip):
        log(f"refresh: skipped tonight ({skip} exists)")
        return
    if not (NOTICE_AT <= hm < END_AT):
        log(f"refresh: {local:%H:%M} {TZ.key} is outside the {NOTICE_AT[0]:02d}:{NOTICE_AT[1]:02d}-{END_AT[0]:02d}:{END_AT[1]:02d} window; nothing done")
        return
    plan = set()
    for line in os.environ.get("PLAN_LINES", "").splitlines():
        parts = line.split()
        if len(parts) == 4:
            plan.add((parts[0], parts[2]))
    state = os.path.join(STATE_ROOT, day)
    os.makedirs(state, mode=0o700, exist_ok=True)
    for old in os.listdir(STATE_ROOT):  # older nights' markers
        if old != day:
            shutil.rmtree(os.path.join(STATE_ROOT, old), ignore_errors=True)
    res = subprocess.run(["tmux", "list-panes", "-a", "-F", "#{session_name}\t#{pane_pid}"],
                         capture_output=True, text=True)
    sessions: dict = {}
    for line in res.stdout.splitlines():
        name, _, pid = line.partition("\t")
        if pid.strip().isdigit():
            sessions.setdefault(name, []).append(int(pid))
    force = FORCE_AT is not None and hm >= FORCE_AT
    for name, panes in sorted(sessions.items()):
        env = session_env(panes)
        if not env:
            continue  # the owner's / a manager session: never touched
        project, role, short = cb(env, "PROJECT"), cb(env, "STREAM"), cb(env, "PROJECT_SHORT")
        expected = short if role == "all" else f"{short}-{SUFFIX.get(role, '?')}"
        if not short or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,49}", name) or name != expected:
            continue
        if (project, role) not in plan:
            continue  # left the plan: wind_down owns it
        done, noticed = os.path.join(state, f"{name}.restarted"), os.path.join(state, f"{name}.noticed")
        if os.path.exists(done):
            continue
        if not os.path.exists(noticed):
            code = post(token_for(env, short, role), "refresh")
            if code == 200:
                with open(noticed, "w") as fh:
                    fh.write(str(now))
                log(f"{name}: refresh notice sent (restart from 07:30 when idle)")
            else:
                log(f"{name}: refresh notice failed (dashboard answered {code or 'nothing'}); will retry")
            if not force:
                continue  # restart only on a later run, after the session had time to wrap up
        if hm < RESTART_AT:
            continue
        since = read(noticed, "r")
        waited = now - int(since.strip()) if since and since.strip().isdigit() else None
        if not force and (waited is None or waited < NOTICE_MIN):
            log(f"{name}: notice not old enough yet; waiting")
            continue
        if not force and not idle(name):
            log(f"{name}: busy; will retry in 5 min, skipped today if still busy at 09:00 (never forced)")
            continue
        subprocess.run(["tmux", "kill-session", "-t", f"={name}"], capture_output=True)
        with open(done, "w") as fh:
            fh.write(str(now))
        log(f"{name}: restarting fresh (idle)")
        print(f"KILLED {name}", flush=True)


main()
