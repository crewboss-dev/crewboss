"""`crewboss-runner doctor | repair | uninstall` (#646).

doctor     read-only checks, each pass | warn | fail; exit 1 when any fail; `--json` prints the same as JSON.
repair     fixes only what is safe (missing dirs, the autostart unit with the right PATH, stale worktrees of finished jobs),
           asking before each change unless `--yes`; `--dry-run` only lists them.
uninstall  stops and removes the autostart, this PC's key, token, settings and state; the user's own repositories are left alone.

Nothing here prints a secret, a token, a key or file contents: findings are path + rule + redacted hint (agentscan)."""
from __future__ import annotations

import base64
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, admincopy, agentscan, elevation, enrol as enrol_mod, gemini, guard, keepawake, service, settings, spawn, stopctl, store

PASS, WARN, FAIL = "pass", "warn", "fail"
MIN_PYTHON = (3, 12)
DISK_WARN_GB, DISK_FAIL_GB = 5.0, 1.0
TOKEN_WARN_DAYS = 7
# What a label needs on this PC. Labels live on the dashboard, so the runner only knows the ones named with --label (or CREWBOSS_RUNNER_LABELS).
LABEL_TOOLS = {"android": ("adb",), "android-usb": ("adb",), "android-wsl": ("adb",), "macos": ("xcodebuild",), "ios": ("xcodebuild",)}


def row(cid: str, status: str, detail: str, fix: str = "") -> dict:
    return {"id": cid, "status": status, "detail": detail, **({"fix": fix} if fix else {})}


def _free_gb(path: Path) -> float:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free / 1e9


def _service_text(plat: str) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            rc = service.status(plat)
        except OSError as exc:   # systemctl / schtasks / launchctl not found
            return 1, f"cannot query the service manager ({exc.__class__.__name__})"
    return rc, buf.getvalue().strip().splitlines()[0] if buf.getvalue().strip() else ""


def stale_worktrees(state: Path) -> list[Path]:
    """Job directories under state/jobs whose job is not the one running now (a finished or crashed job left them)."""
    jobs = state / "jobs"
    if not jobs.is_dir():
        return []
    active = ""
    try:
        job = json.loads((state / "status.json").read_text()).get("job")
        if job:
            active = f"j{int(job['id'])}-e{int(job['fence'])}"
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return sorted(d for d in jobs.iterdir() if d.is_dir() and d.name != active)


def run_checks(labels: list[str] | None = None) -> list[dict]:
    out: list[dict] = []
    secrets, config = store.Secrets(), store.Config()
    cfg = config.load()
    state = store.state_dir()
    plat = service.detect()

    v = sys.version_info
    out.append(row("python", PASS if (v.major, v.minor) >= MIN_PYTHON else FAIL, f"Python {v.major}.{v.minor}.{v.micro}"
                   + ("" if (v.major, v.minor) >= MIN_PYTHON else f" (need {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer)")))
    out.append(row("runner", PASS, f"crewboss-runner {__version__} on {plat}"))
    out.append(row("git", PASS, "git found") if shutil.which("git") else row("git", FAIL, "git is not installed or not on PATH"))

    claude = spawn.claude_binary()
    have = bool(shutil.which(claude) or (os.path.isfile(claude) and os.access(claude, os.X_OK)))
    if not have:
        out.append(row("claude", FAIL, "Claude Code (`claude`) is not installed or not on PATH"))
    else:
        out.append(row("claude", PASS, "Claude Code found"))
        if (cfg or {}).get("mode") != "api":
            ok = guard.signed_in(claude)
            out.append(row("claude_auth", PASS, "Claude Code is signed in") if ok else
                       row("claude_auth", FAIL, guard.SIGNED_OUT_MESSAGE) if ok is False else
                       row("claude_auth", WARN, "could not tell whether Claude Code is signed in"))

    out.append(_gemini_row())

    if secrets.backend == "os-keyring":
        out.append(row("keyring", PASS, "OS keyring in use"))
    else:
        mode_ok = secrets.file_mode_ok()
        out.append(row("keyring", WARN if mode_ok else FAIL, "no OS keyring on this machine; secrets are in a private file"
                       + ("" if mode_ok else " that is NOT 0600"), "" if mode_ok else f"chmod 600 {secrets.file}"))

    if not cfg:
        out.append(row("enrolment", FAIL, "this PC is not enrolled", "crewboss-runner enrol '<code>'"))
    else:
        have_secrets = bool(secrets.get("device_key") and secrets.get("device_token"))
        out.append(row("enrolment", PASS if have_secrets else FAIL,
                       f"enrolled as device #{cfg.get('device_id')} with {cfg.get('server')}" if have_secrets
                       else "the device key or token is missing from the keyring", "" if have_secrets else "crewboss-runner forget; then enrol again"))
        try:
            pinned_ok = enrol_mod.fp8_of(base64.b64decode(cfg.get("server_public_key", ""))) == cfg.get("fp8")
        except Exception:  # noqa: BLE001 - garbled base64
            pinned_ok = False
        out.append(row("pinned_key", PASS, f"server key pinned ({cfg.get('fp8')})") if pinned_ok else
                   row("pinned_key", FAIL, "the pinned server key is missing or does not match its fingerprint",
                       "crewboss-runner forget; then enrol again"))
        out.append(_expiry(cfg.get("expires_at")))

    for label in labels or []:
        tools = LABEL_TOOLS.get(label)
        if tools is None:
            out.append(row(f"label:{label}", PASS, f"label '{label}' needs nothing this check knows about"))
            continue
        missing = [t for t in tools if not shutil.which(t)]
        out.append(row(f"label:{label}", FAIL if missing else PASS,
                       f"label '{label}': {', '.join(missing)} not found on PATH" if missing else f"label '{label}': {', '.join(tools)} found"))

    rc, text = _service_text(plat)
    out.append(row("service", PASS if rc == 0 else WARN, text or ("autostart installed" if rc == 0 else "autostart not installed"),
                   "" if rc == 0 else "crewboss-runner service install"))

    ka_ok, ka_how = keepawake.availability(plat)
    ka_on = keepawake.enabled()
    out.append(row("keep_awake", PASS if (ka_ok and ka_on) else WARN,
                   f"no sleep while a job runs: {'on' if ka_on else 'OFF (keep_awake_during_jobs=0)'}; {ka_how}" if ka_ok or not ka_on else f"not available ({ka_how})",
                   "" if ka_ok and ka_on else "set CREWBOSS_RUNNER_KEEP_AWAKE_DURING_JOBS=1" if ka_ok else ""))

    out.append(_elevation_row(state))
    if plat == "windows" and admincopy.program_dir().exists():
        out.append(_admincopy_row())

    free = _free_gb(state)
    out.append(row("disk", FAIL if free < DISK_FAIL_GB else WARN if free < DISK_WARN_GB else PASS, f"{free:.1f} GB free for job worktrees"))
    for name, d in (("config_dir", store.config_dir()), ("state_dir", state)):
        out.append(row(name, PASS, "exists") if d.is_dir() else row(name, WARN, f"{d} does not exist", "crewboss-runner repair"))
    stale = stale_worktrees(state)
    out.append(row("stale_worktrees", WARN, f"{len(stale)} leftover job folder(s) of finished jobs", "crewboss-runner repair") if stale
               else row("stale_worktrees", PASS, "no leftover job folders"))

    findings = agentscan.scan_user(server_host=(cfg or {}).get("server"))
    jobs = state / "jobs"
    for d in sorted(jobs.iterdir()) if jobs.is_dir() else []:
        findings += [{**f, "path": f"{d.name}/{f['path']}"} for f in agentscan.scan_tree(d / "work", pins=None)]   # no pin list here: unreviewed agent files only warn
    for f in findings:
        out.append(row(f"agent:{f['rule']}", FAIL if f["severity"] == agentscan.BLOCK else WARN, f"{f['path']}: {f['hint']}"))
    if not findings:
        out.append(row("agent_setup", PASS, "no known-risky setup found (a scan for known patterns, not a guarantee)"))
    return out


def _admincopy_row() -> dict:
    """Windows service: the supervisor's admin-only copy must exist, still be admin-only, and match this runner (a `pipx upgrade` does not
    update it)."""
    root = admincopy.program_dir()
    try:
        writers = admincopy.writable_by_non_admins(root)
    except Exception as exc:  # noqa: BLE001
        return row("service_copy", WARN, f"could not read the permissions of {root} ({exc})")
    if writers:
        return row("service_copy", FAIL, f"{root} can be modified by {', '.join(writers)}: the admin service would run code they can change",
                   "crewboss-runner service install --windows-service (administrator PowerShell)")
    why = admincopy.stale_reason(root)
    if why:
        return row("service_copy", WARN, why + ". Re-run `crewboss-runner service install --windows-service` as administrator NOW: an old service copy "
                   "does not know profiles or the graceful switch", "crewboss-runner service install --windows-service (administrator PowerShell)")
    return row("service_copy", PASS, f"the service's admin-only copy in {root} is {admincopy.installed_stamp(root).split()[0] or '?'}, matching this runner, and not writable by users")


def _elevation_row(state: Path) -> dict:
    """Windows: the RUNNING runner reports whether its own token is elevated (its agents inherit it). Not elevated is the goal, also
    under the Windows service, whose supervisor starts the runner with a filtered token."""
    try:
        elevated = json.loads((state / "status.json").read_text()).get("elevated")
    except (OSError, ValueError):
        elevated = None
    if elevated is None or not store.run_alive():
        return row("elevation", PASS, "runner not running, or not Windows: nothing to check")
    if not elevated:
        return row("elevation", PASS, "the runner (and its agents) run with a standard-user token, not elevated")
    if elevation.allowed():
        return row("elevation", WARN, "the runner is ELEVATED (administrator token); allowed by CREWBOSS_RUNNER_ALLOW_ELEVATED")
    return row("elevation", FAIL, "the runner is ELEVATED (administrator token) and claims no jobs: its agents would inherit it",
               "start it from a normal PowerShell, or reinstall: crewboss-runner service install")


def _gemini_row() -> dict:
    """Gemini (#861, optional): usable = a private key file (mode 0600, owned by this user). No call is made, no CLI is started."""
    cap = gemini.detect()
    if cap["ok"]:
        return row("gemini", PASS, "Gemini ready: key file found; second opinions and pictures for projects that allow it")
    if cap["reason"] == "no key file":
        return row("gemini", PASS, f"Gemini: no key file (optional: put a Gemini API key in {gemini.key_file()}, mode 0600, to enable it)")
    return row("gemini", WARN, f"Gemini: {cap['reason']}", f"fix {gemini.key_file()}; see docs/gemini.md")


def _expiry(raw) -> dict:
    if not raw:
        return row("token_expiry", WARN, "no expiry date recorded")
    try:
        when = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
    except ValueError:
        return row("token_expiry", WARN, "the recorded expiry date is unreadable")
    days = (when - datetime.now(timezone.utc)).total_seconds() / 86400
    if days < 0:
        return row("token_expiry", FAIL, "the enrolment token has expired", "crewboss-runner forget; then enrol again")
    if days < TOKEN_WARN_DAYS:
        return row("token_expiry", WARN, f"the enrolment token expires in {days:.0f} day(s)")
    return row("token_expiry", PASS, f"the enrolment token is valid for {days:.0f} more day(s)")


def labels_from(args) -> list[str]:
    raw = list(getattr(args, "label", None) or [])
    raw += [x for x in (settings.get("LABELS") or "").split(",")]
    return [x.strip() for x in raw if x and x.strip()]


def cmd_doctor(args) -> int:
    checks = run_checks(labels_from(args))
    fails = sum(c["status"] == FAIL for c in checks)
    warns = sum(c["status"] == WARN for c in checks)
    if getattr(args, "json", False):
        print(json.dumps({"ok": fails == 0, "fail": fails, "warn": warns, "checks": checks}, indent=2))
    else:
        mark = {PASS: "PASS", WARN: "WARN", FAIL: "FAIL"}
        for c in checks:
            print(f"{mark[c['status']]}  {c['id']:<22} {c['detail']}" + (f"   -> {c['fix']}" if c.get("fix") else ""))
        print(f"\n{len(checks) - fails - warns} passed, {warns} warning(s), {fails} failed")
    return 1 if fails else 0


# --- repair -------------------------------------------------------------------------------------------------------------
def _service_needs_reinstall(plat: str) -> str:
    """'' when fine, else why the autostart should be written again."""
    rc, _ = _service_text(plat)
    if rc != 0:
        return "the autostart is not installed"
    claude = shutil.which("claude")
    if plat in ("linux", "wsl") and claude:
        try:
            text = service.unit_path().read_text()
        except OSError:
            return ""   # a Windows-task WSL install has no unit: leave it
        if str(Path(claude).parent) not in text:
            return "the autostart's PATH does not include Claude Code's folder"
    return ""


def planned_repairs() -> list[tuple[str, callable]]:
    plan: list[tuple[str, callable]] = []
    for d in (store.config_dir(), store.state_dir()):
        if not d.is_dir():
            plan.append((f"create the folder {d}", lambda d=d: store.mark_dir(d)))
    plat = service.detect()
    why = _service_needs_reinstall(plat)
    if why:
        plan.append((f"write the autostart again ({why})", lambda: service.install(plat)))
    state = store.state_dir()
    for d in stale_worktrees(state):
        plan.append((f"delete the leftover job folder {d.name}", lambda d=d: _remove_job_dir(state, d)))
    return plan


def _remove_job_dir(state: Path, d: Path) -> None:
    """Delete one finished job's folder, and tell each cached bare repo to forget the worktree."""
    bares = list((state / "repos").glob("*.git")) if (state / "repos").is_dir() else []
    for bare in bares:
        subprocess.run(["git", "worktree", "remove", "--force", str(d / "work")], cwd=bare, capture_output=True)
    shutil.rmtree(d, ignore_errors=True)
    for bare in bares:
        subprocess.run(["git", "worktree", "prune"], cwd=bare, capture_output=True)


def _ask(question: str) -> bool:
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def cmd_repair(args) -> int:
    plan = planned_repairs()
    if not plan:
        print("Nothing to repair.")
        return 0
    done = 0
    for desc, fn in plan:
        if getattr(args, "dry_run", False):
            print(f"would: {desc}")
            continue
        if not getattr(args, "yes", False) and not _ask(f"Repair: {desc}?"):
            print(f"skipped: {desc}")
            continue
        fn()
        done += 1
        print(f"done: {desc}")
    if getattr(args, "dry_run", False):
        print("Dry run: nothing was changed.")
    return 0


# --- uninstall ----------------------------------------------------------------------------------------------------------
def _safe_to_delete(d: Path) -> bool:
    """Only an absolute folder that is the runner's own: named like one, or carrying the marker the runner writes on first use.
    Never a relative path (CREWBOSS_RUNNER_STATE_DIR=. would mean the current folder), the current folder, home, a parent of home, or a root."""
    if not os.path.isabs(str(d)):
        return False
    r = d.resolve()
    home = Path.home().resolve()
    if r in (home, Path(r.anchor), Path.cwd().resolve()) or r in home.parents:
        return False
    return r.name in store.OWN_NAMES or r.parent.name in store.OWN_NAMES or (r / store.MARKER).is_file()


def _active_job() -> bool:
    try:
        job = json.loads((store.state_dir() / "status.json").read_text()).get("job")
    except (OSError, ValueError):
        return False
    return bool(job) and store.run_alive()


def cmd_uninstall(args) -> int:
    """Remove this PC's runner: the ACTIVE profile (or --profile X) when others remain, otherwise everything. `--all` removes every
    profile and the autostart. With several profiles the one autostart serves the others, so it stays."""
    from . import profiles
    me = store.current_profile()
    others = [n for n in profiles.names() if n != me]
    everything = bool(getattr(args, "all", False)) or not others
    if not everything and me == profiles.active_profile():
        print(f"{me!r} is the active profile and other profiles remain ({', '.join(others)}). Switch first (crewboss-runner use "
              f"{others[0]}) and then uninstall {me!r} with --profile {me}, or remove everything with: crewboss-runner uninstall --all", file=sys.stderr)
        return 1
    if everything:
        todo = profiles.names() if me is not None else [None]
        if me is not None and me not in todo:
            todo.append(me)
        targets = [d for d in (store.root_config_dir(), store.root_state_dir()) if d.exists()]
    else:
        todo = [me]
        targets = [d for d in (store.config_dir(), store.state_dir()) if d.exists()]
    secrets = store.Secrets()
    head = ("This will stop and remove the autostart, delete " + ("every profile's" if len(todo) > 1 else "this PC's")) if everything \
        else f"This will delete the profile {me!r}'s"
    print(f"{head} device key and token from {secrets.describe()}, and delete {', '.join(str(t) for t in targets) or 'no local folders'}.\n"
          "Your own repositories are not touched. The device stays on the dashboard until someone revokes it there.")
    if any(_job_running_in(n) for n in todo):
        print("A job is running on this PC right now. Let it finish (or stop it) and run uninstall again.", file=sys.stderr)
        return 1
    if not getattr(args, "yes", False) and not _ask("Uninstall the runner from this PC?" if everything else f"Remove profile {me!r}?"):
        print("Cancelled.")
        return 1
    if everything:
        if service.uninstall(service.detect()) != 0:
            print("The autostart could not be removed (see above), so nothing else was removed: the key, token, settings and state are all "
                  "still here. Fix that (a Windows service needs an administrator PowerShell) and run uninstall again.", file=sys.stderr)
            return 1
        if any(_alive_in(n) for n in todo):
            print("The runner is still running (not started by the autostart?). Stop it (Ctrl+C in its terminal) and run uninstall again; "
                  "nothing else was removed.", file=sys.stderr)
            return 1
    elif _alive_in(me):
        print("That profile's runner is still running; stop it and run uninstall again.", file=sys.stderr)
        return 1
    if not stopctl.wait_exit(30 if service.detect() == "windows" else 3, alive=lambda: any(_alive_in(n) for n in todo)):   # a stopped service's runner may still be shutting down: wait before judging
        print("The runner is still running (not started by the autostart?). Stop it (Ctrl+C in its terminal) and run uninstall again; "
              "nothing else was removed.", file=sys.stderr)
        return 1
    for n in todo:
        for name in ("device_key", "device_token"):
            store.Secrets(profile=n).delete(name)
        store.Config(profile=n).delete()
    refused = 0
    for d in targets:
        if _safe_to_delete(d):
            shutil.rmtree(d, ignore_errors=True)
        else:
            refused += 1
            print(f"NOT deleted: {d} does not look like the runner's own folder (not named crewboss-runner, no runner marker, or a relative path).",
                  file=sys.stderr)
    if not everything:
        print(f"Removed profile {me!r}. Revoke its device on that server's Runners page.")
        return 1 if refused else 0
    print("Removed. To remove the program itself: pipx uninstall crewboss-runner (or pip uninstall crewboss-runner).")
    return 1 if refused else 0


def _job_running_in(name) -> bool:
    from . import profiles
    if name is None:
        return _active_job()
    with profiles.as_profile(name):
        return _active_job()


def _alive_in(name) -> bool:
    from . import profiles
    if name is None:
        return store.run_alive()
    return profiles._alive(name)
