"""crewboss-sessions.sh refresh (task #578): the nightly fresh restart. Fake tmux (incl. capture-pane), curl, docker and
/proc only; the clock is CREWBOSS_REFRESH_NOW (an epoch), read as Australia/Sydney local time."""
import datetime as dt
import json
import os
import subprocess
from zoneinfo import ZoneInfo

import pytest

from tests.test_sessions_sync import ROOT, SCRIPT, Box, _env_for, _plan_entry

DAY = "2026-10-28"
IDLE = "some output\n> \n  ? for shortcuts\n"
BUSY = "● Reading files\n✻ Working… (42s · ↑ 1.2k tokens · esc to interrupt)\n"


def at(hhmm, day=DAY):
    h, m = map(int, hhmm.split(":"))
    when = dt.datetime.fromisoformat(f"{day}T{h:02d}:{m:02d}:00").replace(tzinfo=ZoneInfo("Australia/Sydney"))
    return str(int(when.timestamp()))


class RBox(Box):
    def __init__(self, tmp):
        super().__init__(tmp)
        (tmp / "pane").mkdir()
        self.pane = tmp / "pane"
        tmux = self.bindir / "tmux"
        tmux.write_text('#!/bin/bash\necho "tmux $*" >> "$LOG"\n'
                        'case "$1" in\n'
                        '  has-session) n="${3#=}"; grep -q "^$n	" "$PANES" 2>/dev/null; exit $?;;\n'
                        '  list-panes) cat "$PANES" 2>/dev/null; exit 0;;\n'
                        '  capture-pane) n="${4#=}"; n="${n%:}"; cat "$PANEDIR/$n" 2>/dev/null; exit $?;;\n'
                        '  kill-session) n="${3#=}"; grep -v "^$n	" "$PANES" > "$PANES.new"; mv "$PANES.new" "$PANES"; exit 0;;\n'
                        '  new-session) exit 0;;\n'
                        'esac; exit 1\n')

    def screen(self, name, text):
        (self.pane / name).write_text(text)

    def refresh(self, hhmm, **extra):
        panes = self.tmp / "panes"  # the fake tmux edits this file on kill, so it stays the truth between runs
        if not panes.exists():
            panes.write_text("".join(f"{n}\t{p}\n" for n, p in self.panes.items()))
        env = {"PATH": f"{self.bindir}:{os.environ['PATH']}", "HOME": str(self.home), "LOG": str(self.log),
               "PANES": str(panes), "CREWBOSS_HOME": str(ROOT / "tests" / "home"), "CREWBOSS_PROC_ROOT": str(self.proc), "CREWBOSS_STATE_DIR": str(self.state),
               "CREWBOSS_DASH_URL": "http://dash.invalid", "CREWBOSS_PLAN_TRIES": "1", "CREWBOSS_PLAN_WAIT": "0",
               "CREWBOSS_DASH_COMPOSE_DIR": str(self.tmp), "STOP_CODE": self.stop_code,
               "CREWBOSS_REFRESH_NOW": at(hhmm), "CREWBOSS_REFRESH_RECHECK": "0", "PANEDIR": str(self.pane), **extra}
        if self.plan is not None:
            env["PLAN"] = json.dumps(self.plan)
        out = subprocess.run(["bash", str(SCRIPT), "refresh"], capture_output=True, text=True, timeout=30, env=env)
        calls = self.log.read_text() if self.log.exists() else ""
        self.log.unlink(missing_ok=True)
        return out, calls


@pytest.fixture
def rb(tmp_path):
    b = RBox(tmp_path)
    b.plan = [_plan_entry("example-erp", "example", "all"), _plan_entry("acme-retail", "acme", "all")]
    b.session("example", _env_for("example-erp", "example", "all", "tok_m"))
    b.session("acme", _env_for("acme-retail", "acme", "all", "tok_a"))
    b.session("old", _env_for("gone-co", "old", "all", "tok_o"))      # left the plan: wind_down's, not ours
    b.panes["owner-shell"] = b._next                                    # a tmux session with no claude/project env
    for n in ("example", "acme", "old", "owner-shell"):
        b.screen(n, IDLE)
    return b


def notices(calls):
    return [ln for ln in calls.splitlines() if "sessions/stopping" in ln]


def kills(calls):
    return [ln for ln in calls.splitlines() if ln.startswith("tmux kill-session")]


REAL_BACKGROUND = (
    "✻ Waiting for 1 background agent to finish\n"
    "● Agent \"Build #584 …\" failed: …\n"
    "────────\n❯\n────────\n"
    "  ⏵⏵ auto mode on · 2 shells · ⧉ access-matrix · ↓ to manage         ✔ Update installed · Restart to update\n")


def test_capture_pane_target_is_session_colon(rb):
    rb.refresh("07:20")
    out, calls = rb.refresh("07:30")
    caps = [ln for ln in calls.splitlines() if ln.startswith("tmux capture-pane")]
    assert "tmux capture-pane -p -t =acme:" in caps and "tmux capture-pane -p -t =example:" in caps
    assert not any(ln.endswith("=acme") or ln.endswith("=example") for ln in caps)


def test_real_pane_waiting_on_a_background_agent_is_busy(rb):
    rb.screen("acme", REAL_BACKGROUND)
    rb.refresh("07:20")
    out, calls = rb.refresh("07:30")
    assert "tmux kill-session -t =acme" not in calls and "acme: busy" in out.stdout


@pytest.mark.parametrize("screen", [
    "✻ Waiting for 1 background agent to finish\n❯\n",
    "● done\n❯\n  ⏵⏵ auto mode on · 2 shells · ⧉ x\n",
    "● done\n❯\n  ⏵⏵ auto mode on · 1 agent\n",
    "✻ Working… (42s · esc to interrupt)\n❯\n",
    "waiting for a build\n❯\n"])
def test_background_work_markers_are_busy(rb, screen):
    rb.screen("acme", screen)
    rb.refresh("07:20")
    out, calls = rb.refresh("07:30")
    assert "tmux kill-session -t =acme" not in calls
    out, calls = rb.refresh("08:55")                                    # nothing forces it any more
    assert "tmux kill-session -t =acme" not in calls


@pytest.mark.parametrize("summary", [
    "✻ Sautéed for 40s · done 7:20 AM", "✻ Brewed for 3m 12s", "✻ Worked for 1m 5s"])
def test_turn_finished_summary_line_is_idle(rb, summary):
    rb.screen("acme", f"● ok\n{summary}\n──────────\n❯ \n──────────\n  ⏵⏵ auto mode on (shift+tab to cycle)\n")
    rb.refresh("07:20")
    out, calls = rb.refresh("07:30")
    assert "tmux kill-session -t =acme" in calls and "acme: busy" not in out.stdout


def test_notice_goes_to_running_planned_sessions_only(rb):
    out, calls = rb.refresh("07:20")
    n = notices(calls)
    assert len(n) == 2 and all('{"phase": "refresh"}' in ln for ln in n)
    assert any("Bearer tok_m" in ln for ln in n) and any("Bearer tok_a" in ln for ln in n)
    assert not any("tok_o" in ln for ln in n) and not kills(calls)      # nothing killed at 07:20
    assert "example: refresh notice sent" in out.stdout and "old" not in out.stdout
    out, calls = rb.refresh("07:20")                                    # a second run does not repeat the notice
    assert not notices(calls)


def test_idle_sessions_restart_at_0730_and_are_started_again(rb):
    rb.refresh("07:20")
    out, calls = rb.refresh("07:30")
    assert sorted(kills(calls)) == ["tmux kill-session -t =acme", "tmux kill-session -t =example"]
    news = [ln for ln in calls.splitlines() if ln.startswith("tmux new-session")]
    assert len(news) == 2 and any("-s example " in ln for ln in news) and any("-s acme " in ln for ln in news)
    assert "example: restarting fresh (idle)" in out.stdout
    assert "=old" not in calls and "owner-shell" not in calls           # non-planned / non-project: untouched
    out, calls = rb.refresh("07:35")                                    # the next tick must not kill the new ones
    assert not kills(calls) and not notices(calls)


def test_busy_session_is_retried_and_restarted_once_idle(rb):
    rb.screen("acme", BUSY)
    rb.refresh("07:20")
    out, calls = rb.refresh("07:30")
    assert kills(calls) == ["tmux kill-session -t =example"]
    assert "acme: busy; will retry" in out.stdout and "-s acme " not in calls
    for t in ("07:35", "08:10", "08:55"):
        out, calls = rb.refresh(t)
        assert not kills(calls), t
    rb.screen("acme", IDLE)                                             # it finishes: the next tick restarts it
    out, calls = rb.refresh("08:55")
    assert kills(calls) == ["tmux kill-session -t =acme"]


def test_busy_session_is_never_killed_at_the_end_of_the_window(rb):
    rb.screen("acme", BUSY)
    rb.refresh("07:20")
    rb.refresh("07:30")
    out, calls = rb.refresh("08:55")                                    # the last tick: no forced kill any more
    assert "tmux kill-session -t =acme" not in calls and "-s acme " not in calls
    assert "acme: busy; will retry" in out.stdout
    out, calls = rb.refresh("09:00")                                    # the window is over
    assert "outside" in out.stdout and not kills(calls)


def test_a_dialog_waiting_for_the_owner_counts_as_busy(rb):
    rb.screen("acme", "Do you want to proceed?\n 1. Yes\n 2. No\n Esc to cancel\n")
    rb.refresh("07:20")
    out, calls = rb.refresh("07:30")
    assert "tmux kill-session -t =acme" not in calls


def test_old_busy_text_in_the_scrollback_is_not_busy(rb):
    rb.screen("acme", "(esc to interrupt)\n" + "line\n" * 30 + "> \n")
    rb.refresh("07:20")
    out, calls = rb.refresh("07:30")
    assert "tmux kill-session -t =acme" in calls


def test_the_notice_must_have_had_time_before_a_restart(rb):
    rb.refresh("07:28")                                                 # late first run: notice only
    out, calls = rb.refresh("07:30")                                    # 2 min old, under the 9-minute minimum
    assert not kills(calls) and "not old enough" in out.stdout
    out, calls = rb.refresh("07:40")
    assert len(kills(calls)) == 2


def test_skip_file_skips_the_night(rb):
    rb.state.mkdir(parents=True)
    (rb.state / f"skip-refresh-{DAY}").write_text("")
    out, calls = rb.refresh("07:20")
    assert "skipped tonight" in out.stdout and not notices(calls)
    out, calls = rb.refresh("07:30")
    assert not kills(calls)
    (rb.state / f"skip-refresh-{DAY}").rename(rb.state / "skip-refresh-2026-10-29")  # another night's file: no effect
    out, calls = rb.refresh("07:20")
    assert len(notices(calls)) == 2


def test_disable_env_turns_it_off(rb):
    out, calls = rb.refresh("07:20", CREWBOSS_REFRESH_DISABLE="1")
    assert "disabled" in out.stdout and not notices(calls)
    out, calls = rb.refresh("08:00", CREWBOSS_REFRESH_DISABLE="1")
    assert not kills(calls)


def test_outside_the_window_nothing_happens(rb):
    out, calls = rb.refresh("12:00")
    assert "outside" in out.stdout and not notices(calls) and not kills(calls)


def test_unreachable_dashboard_does_nothing(rb):
    rb.plan = None
    out, calls = rb.refresh("07:30")
    assert out.returncode == 0 and "unreachable" in out.stdout and not kills(calls)


def test_failed_notice_is_retried_not_marked(rb):
    rb.stop_code = "500"
    out, calls = rb.refresh("07:20")
    assert "notice failed" in out.stdout
    rb.stop_code = "200"
    out, calls = rb.refresh("07:25")
    assert len(notices(calls)) == 2


def test_units_use_explicit_sydney_time():
    timer = (ROOT / "sessions/systemd/crewboss-sessions-refresh.timer.example").read_text()
    svc = (ROOT / "sessions/systemd/crewboss-sessions-refresh.service.example").read_text()
    assert "OnCalendar=Mon..Sat *-*-* 07:20:00 UTC" in timer
    assert "OnCalendar=Mon..Sat *-*-* 07:30/5:00 UTC" in timer
    assert "OnCalendar=Mon..Sat *-*-* 08:00/5:00 UTC" in timer
    assert "05:00" not in timer.replace("08:00/5:00", "")             # no forced-restart line
    assert "Sun" not in timer.replace("Mon..Sat", "").split("[Timer]")[1].replace("Sunday", "")
    assert "crewboss-sessions.sh refresh" in svc and "KillMode=process" in svc and "User=SESSIONS_USER" in svc
