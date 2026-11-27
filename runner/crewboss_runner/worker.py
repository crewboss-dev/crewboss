"""Running jobs: claim over the verified socket, one worktree + one agent per job, heartbeats, push, finish.

Fencing from the runner's side: every heartbeat carries the fence. A 409 (stale fence: the job was requeued, finished or
reclaimed), or no successful heartbeat for a whole lease, means the job is no longer ours: the agent is killed, NOTHING is
pushed, nothing is finished. The branch is pushed only after a final heartbeat succeeded, and the job is then finished with
the same fence."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import agentscan, elevation, gemini, guard, keepawake, settings, skills, spawn, store
from .link import Link, LinkDown

log = logging.getLogger("crewboss-runner.worker")

IDLE_POLL_SECONDS = 30.0
MAX_BIG_FRAME = 7168    # the largest `agent_file` frame the dashboard accepts (app/runner_ws.py MAX_BIG_FRAME; its socket closes at 8192)
MIN_CHUNK = 256         # a path so long that fewer raw bytes than this fit in a frame cannot be sent
REPORT_CHUNK = 4096     # raw bytes of an agent file per `agent_file` frame (~5.5 KB as base64): the dashboard's socket takes 8 KiB at most
FINDINGS_BYTES = 3000   # the server drops a frame over 4096 bytes; findings are trimmed to fit
FINISH_GRACE = float(settings.get("FINISH_GRACE", "20"))   # seconds a session gets to exit after /exit, before SIGTERM
RUNNER_STOP_CODES = (-15, 143, -9, 137)   # exit codes of a session the RUNNER ended by signal


def chunk_size(job_id: int, fence: int, fields: dict) -> int:
    """Raw bytes of a file one `agent_file` frame may carry, sized from the frame budget: MAX_BIG_FRAME minus the serialised length of every
    other field (the path escapes: a CJK or emoji path is 6 or 12 bytes per character on the wire, as link.rpc serialises with ensure_ascii),
    measured with the worst-case request id and part numbers. Never more than REPORT_CHUNK; 0 when the path leaves no room."""
    frame = {"type": "agent_file", "id": "r" + "9" * 12, "job_id": job_id, "fence": fence, **fields, "part": 99999, "parts": 99999, "b64": ""}
    room = MAX_BIG_FRAME - len(json.dumps(frame, separators=(",", ":")).encode("utf-8"))
    raw = room // 4 * 3                       # 4 base64 characters per 3 raw bytes
    return min(REPORT_CHUNK, raw) if raw >= MIN_CHUNK else 0


class JobError(Exception):
    pass


def pin_set(job: dict) -> set[tuple[str, str]]:
    """The project Owner's reviewed files from the claim: {(repo path, sha256)}. A claim without them (an older dashboard) pins nothing:
    deny by default."""
    out = set()
    for p in job.get("pins") or []:
        if isinstance(p, dict) and isinstance(p.get("path"), str) and isinstance(p.get("sha256"), str):
            out.add((p["path"], p["sha256"]))
    return out


def release_reason(findings: list[dict], can_report: bool = True) -> str:
    """The sentence a blocked start is released with (the dashboard keeps ~200 characters): the unreviewed files first, then links and other
    blocks, then the frontmatter warnings as extra reasons."""
    blocks = agentscan.blocking(findings)
    unrev = [f for f in blocks if f["rule"] == "config.unreviewed"]
    other = [f for f in blocks if f["rule"] != "config.unreviewed"]
    parts = []
    if unrev:
        names = ", ".join(f["path"].rsplit("/", 1)[-1][:30] for f in unrev[:2]) + (f" +{len(unrev) - 2} more" if len(unrev) > 2 else "")
        parts.append(f"{len(unrev)} agent/command/skill file(s) not reviewed by the project Owner (config.unreviewed: {names})"
                     + ("; the Owner pins them on the Machines page" if can_report else "; this dashboard is too old to list them"))
    parts += [f"{f['rule']} in {f['path']}" for f in other[:2]]
    warn = [f for f in findings if f["severity"] != agentscan.BLOCK and f["rule"].startswith("frontmatter.")]
    if warn:
        parts.append("also: " + ", ".join(sorted({f["rule"].split(".", 1)[1] for f in warn})[:3]))
    return "known-risky agent setup in the project's repository (fix it there): " + "; ".join(parts)


def review_note(changed: list[dict]) -> str:
    names = ", ".join(c["path"].rsplit("/", 1)[-1][:30] for c in changed[:3]) + (f" +{len(changed) - 3} more" if len(changed) > 3 else "")
    return (f"NEEDS OWNER REVIEW: the agent created or changed {len(changed)} agent, command or skill file(s) ({names}); they will not "
            "run on a job until the project Owner reads and pins them. ")


def git(args: list[str], cwd: Path | str | None = None, check: bool = True, timeout: float = 600.0) -> str:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_ALLOW_PROTOCOL=spawn.git_protocols())
    res = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    if check and res.returncode != 0:
        raise JobError(f"git {args[0]} failed: {(res.stderr or res.stdout).strip()[:300]}")
    return res.stdout.strip()


_thread_locks: dict[str, threading.Lock] = {}
_thread_locks_guard = threading.Lock()


@contextlib.contextmanager
def repo_lock(state: Path, project_id: int):
    """One writer at a time on a project's bare repo cache: a file lock (so two runner processes agree) under a thread lock (so two
    threads of this one do). A read-only look and a job both fetch into and add worktrees from the same cache; without this a look
    could fail a job's prepare_worktree on git's own lock files."""
    path = store.ensure_private_dir(state / "repos") / f"p{int(project_id)}.lock"
    with _thread_locks_guard:
        tl = _thread_locks.setdefault(str(path), threading.Lock())
    with tl:
        fh = open(path, "a+")
        try:
            try:
                import fcntl
                fcntl.flock(fh, fcntl.LOCK_EX)
            except ImportError:   # Windows
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            yield
        finally:
            fh.close()           # closing the file releases the lock


def ensure_bare(state: Path, remote: str, project_id: int) -> tuple[Path, str]:
    """ensure_bare_unlocked under the project's repo lock."""
    with repo_lock(state, project_id):
        return ensure_bare_unlocked(state, remote, project_id)


def ensure_bare_unlocked(state: Path, remote: str, project_id: int) -> tuple[Path, str]:
    """The bare repo cache of one project, freshly fetched from `remote` (the caller holds repo_lock). The fetch is tried twice: a
    transient failure must not fail a job or a look. Returns (bare path, base ref such as origin/main)."""
    remote = (remote or "").strip()
    if not remote or remote.startswith("-"):
        raise JobError("the project has no usable git_remote")
    bare = store.ensure_private_dir(state / "repos") / f"p{int(project_id)}.git"
    if not bare.exists():
        git(["init", "--bare", "-q", str(bare)])
        git(["remote", "add", "origin", remote], cwd=bare)
    else:
        git(["remote", "set-url", "origin", remote], cwd=bare)
    try:
        git(["fetch", "-q", "--prune", "origin", "+refs/heads/*:refs/remotes/origin/*"], cwd=bare)
    except JobError:
        time.sleep(1.0)
        git(["fetch", "-q", "--prune", "origin", "+refs/heads/*:refs/remotes/origin/*"], cwd=bare)
    head = git(["ls-remote", "--symref", "origin", "HEAD"], cwd=bare).splitlines()
    default = ""
    if head and head[0].startswith("ref: refs/heads/"):
        default = head[0].split("\t")[0][len("ref: refs/heads/"):]
    if not default:
        for cand in ("main", "master"):
            if git(["rev-parse", "--verify", "-q", f"refs/remotes/origin/{cand}"], cwd=bare, check=False):
                default = cand
                break
    if not default:
        raise JobError("could not find the remote's default branch")
    return bare, f"origin/{default}"


def prepare_worktree(state: Path, job: dict) -> tuple[Path, str]:
    """A bare repo cache per project and a fresh worktree on `job['branch']` from the remote's default branch.
    Returns (worktree path, base ref)."""
    remote = (job.get("git_remote") or "").strip()
    if not remote or remote.startswith("-"):
        raise JobError("the project has no usable git_remote")
    if not re.fullmatch(r"dash/j\d+-e\d+", job.get("branch") or ""):
        raise JobError("the server gave an unexpected branch name")
    with repo_lock(state, job["project_id"]):
        bare, base = ensure_bare_unlocked(state, remote, job["project_id"])
        wt = store.ensure_private_dir(state / "jobs") / f"j{int(job['job_id'])}-e{int(job['fence'])}" / "work"
        wt.parent.mkdir(parents=True, exist_ok=True)
        git(["worktree", "add", "-q", "-b", job["branch"], str(wt), f"refs/remotes/{base}"], cwd=bare)
    return wt, base


def prepare_look_tree(state: Path, turn: dict, folder: Path) -> Path:
    """A throwaway, DETACHED checkout of the project's default branch for one read-only look (no branch, nothing to push).
    The caller removes it with remove_look_tree. Returns the folder to run the agent in."""
    with repo_lock(state, turn["project_id"]):
        bare, base = ensure_bare_unlocked(state, turn.get("git_remote") or "", turn["project_id"])
        wt = folder / "tree"
        folder.mkdir(parents=True, exist_ok=True)
        git(["worktree", "add", "-q", "--detach", str(wt), f"refs/remotes/{base}"], cwd=bare)
    return wt


def remove_look_tree(state: Path, turn: dict, folder: Path) -> None:
    wt = folder / "tree"
    if wt.exists():
        bare = state / "repos" / f"p{int(turn['project_id'])}.git"
        with repo_lock(state, turn["project_id"]):
            git(["worktree", "remove", "--force", str(wt)], cwd=bare, check=False)
        shutil.rmtree(wt, ignore_errors=True)


def job_dir(state: Path, job: dict) -> Path:
    return state / "jobs" / f"j{int(job['job_id'])}-e{int(job['fence'])}"


def gemini_out_dir(state: Path, job: dict) -> Path:
    """Where make_image saves for this job: beside the jobs, not inside them, so `repair` (which clears finished job folders) keeps the pictures."""
    return state / "gemini-out" / f"j{int(job['job_id'])}-e{int(job['fence'])}"


def write_job_files(state: Path, job: dict, base_url: str, use_gemini: bool = False) -> str:
    """The agent's MCP config (0600; it holds the job token) in the job dir, which is the worktree's PARENT: outside the
    worktree, so a commit or `git add -A` can never pick it up. Returns its path."""
    d = job_dir(state, job)
    mcp = d / "mcp.json"
    entry = (spawn.gemini_server(base_url, job["job_token"], job["fence"], str(gemini_out_dir(state, job))) if use_gemini else None)
    store.write_private(mcp, spawn.mcp_config(base_url, job["job_token"], spawn.extra_mcp_servers(job), entry))
    return str(mcp)


def keepawake_status() -> dict:
    """What the control panel shows about keep-awake, written by the RUNNER (the only process that knows): held now, effective setting, env override."""
    return {"held": keepawake.GLOBAL.held, "enabled": keepawake.enabled(), "env": settings.from_env(keepawake.SETTING) is not None}


class Status:
    """What the local UI and `status` show. No secrets, ever."""

    def __init__(self, path: Path | None = None):
        self.path = path
        self.data = {"state": "starting", "job": None, "events": [], "last_error": ""}

    def event(self, text: str) -> None:
        self.data["events"] = (self.data["events"] + [{"t": time.strftime("%H:%M:%S"), "text": text[:200]}])[-30:]
        self.flush()

    def set(self, **kw) -> None:
        self.data.update(kw)
        self.flush()

    def flush(self) -> None:
        if self.path:
            try:
                store.write_private(self.path, json.dumps(self.data))
            except OSError:
                pass


class Worker:
    def __init__(self, link: Link, cfg: dict, state: Path, status: Status, base_url: str, claude: str | None = None,
                 heartbeat_every: float | None = None, idle_poll: float = IDLE_POLL_SECONDS,
                 failure_backoff: float = 5.0, failure_backoff_max: float = 300.0, watch_window: float | None = None,
                 watch_poll: float | None = None, auth_cache: float = 60.0, permission_grace: float | None = None):
        self.link, self.cfg, self.state, self.status = link, cfg, state, status
        self.base_url, self.claude = base_url, claude
        self.heartbeat_every, self.idle_poll = heartbeat_every, idle_poll
        self.failure_backoff, self.failure_backoff_max = failure_backoff, failure_backoff_max
        self._fail_delay = 0.0
        self.watch_window, self.watch_poll, self.auth_cache = watch_window, watch_poll, auth_cache
        self.permission_grace = permission_grace
        self.problem_kinds: list[str] = []     # the fixed kinds of the current preflight problems (sent to the dashboard)
        self._signed_in_at = float("-inf")     # when `claude auth status` last said signed in (cached briefly)
        self._reported: tuple | None = None    # (kinds, link session) last sent as `attention`
        self.scan_cache = 120.0                # seconds a user-level agent-setup scan is reused
        self._scanned_at = float("-inf")
        self.scan_findings: list[dict] = []    # user-level findings (agentscan); redacted, the only thing ever sent
        self.job_findings: list[dict] = []     # findings from the last job worktree scanned; cleared when a worktree scans clean
        self.gemini_cap: dict = {}             # what detect() last said (kept for the capability frame); {} = none
        self._gemini_at = float("-inf")
        self.link.on_gemini_key = self.gemini_key_changed
        self.stopping = False
        self.results: list[tuple[int, str]] = []   # (job_id, outcome), for tests and the UI
        self.finish_grace = FINISH_GRACE
        self.finish_outcome: str | None = None     # the agent's job_finish outcome ('done' | 'blocked'), from the heartbeat reply
        self.keep_open = False
        self.runner_stopped = False                # the RUNNER sent the signal that ended the session

    # --- is this PC able to run a job at all? -----------------------------------------------------------------------
    def preflight(self) -> list[str]:
        """Problems that would make every job fail to start. Checked BEFORE claiming, so a broken PC claims nothing. (The
        project's remote isn't known until a job is claimed; an unreachable remote is handled by releasing the job.)"""
        problems, kinds = [], []
        claude = self.claude or spawn.claude_binary()
        have_claude = bool(shutil.which(claude) or (os.path.isfile(claude) and os.access(claude, os.X_OK)))
        if not have_claude:
            problems.append(f"`{claude}` (Claude Code) is not installed or not on PATH")
            kinds.append("claude_missing")
        elevated = elevation.refusal()   # Windows: an administrator token would be inherited by every agent (no protocol kind: shown locally)
        if elevated:
            problems.append(elevated)
            kinds.append("elevated")
        if not shutil.which("git"):
            problems.append("`git` is not installed or not on PATH")
            kinds.append("git_missing")
        if (have_claude and self.cfg.get("mode") != "api"
                and time.monotonic() - self._signed_in_at >= self.auth_cache):
            # a signed-out Claude Code would sit at a login prompt nobody can see: one boolean from an exit code, nothing else
            ok = guard.signed_in(claude)   # None = undecided (timeout etc.): never blocks
            if ok is False:
                problems.append(guard.SIGNED_OUT_MESSAGE)
                kinds.append("not_signed_in")
            elif ok:
                self._signed_in_at = time.monotonic()
        if time.monotonic() - self._scanned_at >= self.scan_cache:
            try:
                self.scan_findings = agentscan.scan_user(server_host=self.cfg.get("server"))
            except Exception:  # noqa: BLE001 - a scan bug must not stop the runner; it just reports nothing
                self.scan_findings = []
            self._scanned_at = time.monotonic()
        blocks = agentscan.blocking(self.scan_findings)
        if blocks:   # this PC's own setup: claim nothing. Paths and rule ids are on the dashboard and in `doctor`; here a fixed sentence
            problems.append(f"known-risky agent setup on this PC ({len(blocks)} blocking finding(s)): run `crewboss-runner doctor`")
            kinds.append("agent_setup_blocked")
        if agentscan.blocking(self.job_findings):   # a repository's block only gives that job back (a fixed repo then works)
            kinds.append("repo_setup_blocked")
        self.problem_kinds = kinds
        self.refresh_gemini()
        return problems

    def refresh_gemini(self) -> dict:
        """The Gemini capability (#861), checked at most every scan_cache seconds: a usable key file (0600, owned by this user). No call is made."""
        if time.monotonic() - self._gemini_at >= self.scan_cache:
            try:
                self.gemini_cap = gemini.detect()
            except Exception:  # noqa: BLE001 - a detection bug must not stop the runner; it just reports no capability
                self.gemini_cap = {}
            self._gemini_at = time.monotonic()
        return self.gemini_cap

    def gemini_key_changed(self) -> None:
        """The dashboard wrote or removed the key file: look at it again at the next pass instead of waiting out the cache."""
        self._gemini_at = float("-inf")

    def job_may_use_gemini(self, job: dict) -> bool:
        """Both halves of the gate: this runner can (detected), and the job's project allows it (the claim says so; absent = no)."""
        return job.get("gemini_allowed") is True and bool(self.refresh_gemini().get("ok"))

    def all_findings(self) -> list[dict]:
        return self.scan_findings + self.job_findings

    async def _report(self, kinds: list[str]) -> None:
        """Tell the dashboard which fixed kinds of problem this runner has (so the Runners page can say "needs attention"):
        once per socket session and whenever the set changes. An all-clear is sent only after a problem was."""
        found = agentscan.wire(self.all_findings())
        while found and len(json.dumps(found, separators=(",", ":"))) > FINDINGS_BYTES:   # the server caps a frame at 4096 bytes
            found.pop()
        caps = gemini.wire(self.gemini_cap)
        key = (tuple(kinds), self.link.sessions, json.dumps(found, sort_keys=True), json.dumps(caps, sort_keys=True))
        if key == self._reported:
            return
        if not kinds and not found and not caps and (self._reported is None or self._reported[1] != self.link.sessions
                                                     or not (self._reported[0] or json.loads(self._reported[2]) or json.loads(self._reported[3]))):
            self._reported = key   # nothing to clear on a fresh session
            return
        if await self.link.notify("attention", kinds=kinds, findings=found, capabilities=caps):
            self._reported = key

    async def loop(self) -> None:
        while not self.stopping:
            problems = await asyncio.to_thread(self.preflight)
            self.status.set(preflight=problems)
            await self._report(self.problem_kinds)
            if problems:
                await asyncio.sleep(self.idle_poll)   # shown in `status`; claim nothing until it passes
                continue
            if not self.link.ready.is_set():
                await asyncio.sleep(0.2)
                continue
            try:
                reply = await self.link.rpc("claim")
            except LinkDown:
                continue
            job = reply.get("job") if reply.get("ok") else None
            if job:
                self.link.nudges.clear()
                with keepawake.GLOBAL:
                    self.status.set(keep_awake=keepawake_status())   # no sleep while a job prepares, runs, pushes and finishes (and on cancel / crash)
                    outcome = await self.run_job(job)
                self.results.append((job["job_id"], outcome))
                self.status.set(recent_jobs=(getattr(self.status, "data", {}).get("recent_jobs", []) + [
                    {"id": job["job_id"], "task_id": job.get("task_id"), "outcome": outcome, "ended": time.time()}])[-5:],
                    keep_awake=keepawake_status())   # the control panel's "last few jobs" and keep-awake state (#974)
                if outcome == "released":   # could not start: don't take the next job straight away
                    self._fail_delay = min(max(self._fail_delay * 2, self.failure_backoff), self.failure_backoff_max)
                    self.status.event(f"start failed; not claiming for {self._fail_delay:.0f}s")
                    await asyncio.sleep(self._fail_delay)
                else:
                    self._fail_delay = 0.0
                continue
            self.link.nudges.clear()
            try:
                await asyncio.wait_for(self.link.nudges.wait(), self.idle_poll)
            except asyncio.TimeoutError:
                pass

    # --- one job -------------------------------------------------------------------------------------------------
    async def run_job(self, job: dict) -> str:
        """-> 'done' | 'failed' | 'released' (could not start; given back) | 'lost' (stale fence / lease lost: nothing
        pushed or finished). Cancelling this task (shutdown, revoke) kills the agent before the worktree goes."""
        jid, fence = job["job_id"], job["fence"]
        self.status.set(job={"id": jid, "task_id": job["task_id"], "branch": job["branch"], "fence": fence,
                             "state": "preparing", "started": time.time()})   # `started`: for the control panel's elapsed time (#974); ids only, no task text
        self.status.event(f"claimed job {jid} (fence {fence})")
        lease = float(job.get("lease_seconds") or 90)
        max_seconds = float(job["max_seconds"]) if job.get("max_seconds") else None
        mode = "api" if self.cfg.get("mode") == "api" else "subscription"
        wt = agent = hb = None
        baseline: dict = {}
        pins: set = set()
        lost = asyncio.Event()
        finish_now = asyncio.Event()      # job_finish seen and the session is not to be kept open: end it
        self.finish_outcome, self.keep_open, self.runner_stopped = None, bool(job.get("keep_open")), False
        try:
            try:
                wt, base = await asyncio.to_thread(prepare_worktree, self.state, job)
                ceiling = spawn.tool_ceiling(job)   # the profile's allowed tools: only used for WARNINGS about agent / skill frontmatter
                pins = pin_set(job)                  # the project Owner's reviewed (repo path, sha256) pairs; none = nothing is pinned
                # what the agent would load from this repository: deny by default (agentscan), every agent / command / skill file must be pinned
                self.job_findings, unreviewed, _inv = await asyncio.to_thread(agentscan.scan_worktree, wt, ceiling, pins)
                blocks = agentscan.blocking(self.job_findings)
                if blocks:
                    failed = ""
                    if unreviewed and "pins" in job:   # an older dashboard has no place to put them: the reason still says so
                        failed = await self._report_files(job, wt, unreviewed, "preflight")
                    # the worktree is removed afterwards, so `doctor` cannot see it: name the files, and say to fix them in the repository
                    return await self._release(job, (f"could not send {failed} to the dashboard for review; " if failed else "")
                                               + release_reason(self.job_findings, "pins" in job))
                use_gemini = self.job_may_use_gemini(job)
                mcp = await asyncio.to_thread(write_job_files, self.state, job, self.base_url, use_gemini)
                skills_report = await asyncio.to_thread(self._install_skills, wt, job)
                # look again at the tree the agent will actually see: skills were copied into .claude/skills/ (the repo's .crewboss/skills
                # and the server's) after the first scan. Trusted: the Owner's pins, what the SERVER delivered (Crewboss's own), and a repo skill's
                # copy only when the pin of its SOURCE path matches the bytes copied (skills.trusted_files). Anything else is not.
                trusted = set(pins) | skills.trusted_files(skills_report, pins)
                again, _unrev2, baseline = await asyncio.to_thread(agentscan.scan_agent_files, wt, trusted, ceiling)
                blocks = agentscan.blocking(again)
                if blocks:
                    self.job_findings = self.job_findings + again
                    return await self._release(job, release_reason(again, "pins" in job))
                agent = spawn.start(mode, job, mcp, str(wt), str(job_dir(self.state, job) / "agent.log"), self.claude,
                                    extra_env=skills.git_env((skills_report or {}).get("exclude_file")), gemini=use_gemini)
            except (JobError, OSError, ValueError, subprocess.SubprocessError) as exc:
                # setup error: give the job back (requeued, attempts counted) rather than failing it for good
                return await self._release(job, f"runner could not start the job: {exc}"[:300])
            self.status.set(job={**self.status.data["job"], "state": "running", "mode": mode})
            await self._report_skills(job, skills_report)
            # heartbeats run from here until AFTER finish (through the push too)
            hb = asyncio.create_task(self._heartbeats(job, lease, lost, finish_now))
            waiter = asyncio.create_task(asyncio.to_thread(agent.wait))
            lost_wait = asyncio.create_task(lost.wait())
            finish_wait = asyncio.create_task(finish_now.wait())
            dog = (asyncio.create_task(guard.watch(agent, f"dash-j{jid}", self.watch_window, self.watch_poll, worktree=str(wt)))
                   if mode != "api" else None)
            perm = (asyncio.create_task(guard.watch_permission(agent, self.watch_poll, self.permission_grace))
                    if mode != "api" else None)   # a tool outside the profile's allowed list: its prompt would hang the job
            started = time.monotonic()
            watched = {waiter, lost_wait, finish_wait} | ({dog} if dog else set()) | ({perm} if perm else set())
            while True:   # the watchdog settles first (all well, or a reason to give the job back); then wait as before
                left = None if max_seconds is None else max(max_seconds - (time.monotonic() - started), 0)
                await asyncio.wait(watched, timeout=left, return_when=asyncio.FIRST_COMPLETED)
                if perm is not None and perm.done():
                    blocked_on = perm.result()
                    watched.discard(perm)
                    perm = None
                    # A keep_open job that already called job_finish is DONE (finish_outcome set, finish_now not): a prompt on the
                    # kept-open session is not a reason to give the job back (that would requeue it and wipe the finished work).
                    # It falls through to the normal end: session stopped, pushed, finished, like the max-duration path.
                    if blocked_on and not lost.is_set() and not finish_now.is_set() and not self.finish_outcome:
                        lost_wait.cancel()
                        finish_wait.cancel()
                        if dog is not None:
                            dog.cancel()
                        await asyncio.to_thread(agent.kill)
                        await waiter
                        return await self._release(job, self._permission_reason(blocked_on))
                if dog is not None and waiter.done() and not dog.done():
                    await asyncio.wait({dog})   # the agent is gone: let the watchdog take its last look at the log
                if dog is not None and dog.done() and not lost.is_set():
                    verdict = dog.result()
                    watched.discard(dog)
                    dog = None
                    if verdict:
                        lost_wait.cancel()
                        finish_wait.cancel()
                        await asyncio.to_thread(agent.kill)
                        await waiter
                        return await self._release(job, self._start_failure_reason(verdict))
                    continue
                break
            if dog is not None:
                dog.cancel()
            if perm is not None:
                perm.cancel()
            lost_wait.cancel()
            finish_wait.cancel()
            if not waiter.done():
                if finish_now.is_set() and not lost.is_set():
                    # the agent called job_finish: close the session gracefully (/exit), then SIGTERM, then the normal path
                    self.status.event(f"job {jid}: the agent finished (job_finish); closing its session")
                    await self._stop_session(agent, waiter)
                else:
                    self.runner_stopped = True
                    await asyncio.to_thread(agent.kill)
                    await waiter
                    if lost.is_set():
                        self.status.event(f"job {jid}: lease lost or stale fence; agent stopped, nothing pushed")
                        return "lost"
                    if not self.finish_outcome:   # a session kept open after job_finish that hits the cap is not a failure
                        self.status.event(f"job {jid}: exceeded the maximum duration ({max_seconds:.0f}s); agent stopped")
                        # Commits the agent made before the cap are kept (the job still fails), but only by a runner that still holds
                        # the job at this fence, and with the same agent-file review flagging as the normal end.
                        if await self._final_heartbeat(job, lease) is None:
                            self.status.event(f"job {jid}: stale fence at the time limit; nothing pushed")
                            return "lost"
                        _ok, pushed = await self._flag_and_push(job, wt, base, baseline, pins, 1, None)
                        return await self._end(job, ok=False, summary=f"exceeded the maximum duration of {max_seconds:.0f}s; "
                                               f"the agent was stopped; {pushed}")
            rc = waiter.result()
            if lost.is_set():
                self.status.event(f"job {jid}: stale fence; nothing pushed")
                return "lost"
            self.status.set(job={**self.status.data["job"], "state": "finishing"})
            # the final heartbeat: only a runner that still holds the job at this fence may push. Its reply also carries
            # the finish mark, so a finish the heartbeat loop had not seen yet still counts.
            final = await self._final_heartbeat(job, lease)
            if final is None:
                self.status.event(f"job {jid}: stale fence at the end; nothing pushed")
                return "lost"
            if final.get("finish"):
                self.finish_outcome = final["finish"]
            outcome = self.finish_outcome
            # an agent that said job_finish and was then stopped BY THE RUNNER (SIGTERM/SIGKILL) is not a failure; a non-zero
            # exit the agent produced on its own (a crash) keeps its code, with or without keep_open
            clean = bool(outcome) and (rc == 0 or (self.runner_stopped and rc in RUNNER_STOP_CODES))
            ok, summary = await self._flag_and_push(job, wt, base, baseline, pins, 0 if clean else rc, outcome if clean else None)
            return await self._end(job, ok=ok, summary=summary, outcome=(outcome or "") if clean else "")
        except asyncio.CancelledError:
            if agent is not None:   # shutting down: never leave the agent running without its worktree
                await asyncio.to_thread(agent.kill)
            raise
        finally:
            if hb is not None:
                hb.cancel()
            await asyncio.to_thread(self._cleanup, job, wt)
            self.status.set(job=None)

    async def _flag_and_push(self, job: dict, wt: Path, base: str, baseline: dict, pins: set, rc: int, finish: str | None) -> tuple[bool, str]:
        """The end of every job that may push: flag agent / command / skill files the agent changed for the Owner's review, then push."""
        changed = [c for c in await asyncio.to_thread(self._changed_agent_files, wt, baseline) if (c["path"], c["sha256"]) not in pins]   # a file the Owner already pinned needs no review
        unsent = ""
        if changed:   # flag the job and send the files for the Owner's review
            try:      # NON-FATAL: a report that fails (socket, server, anything) must never cost the push or the finish
                unsent = await self._report_files(job, wt, changed, "job")
            except Exception as exc:  # noqa: BLE001
                log.exception("job-end agent-file report failed")
                unsent = f"{len(changed)} file(s) ({type(exc).__name__})"
        ok, summary = await asyncio.to_thread(self._push, wt, job, base, rc, finish)
        if changed:
            summary = review_note(changed) + (f"(could not send {unsent} for review: look at them in the job's branch) " if unsent else "") + summary
        return ok, summary

    async def _stop_session(self, agent, waiter) -> None:
        """Close a finished session by SIGNAL only: SIGTERM to its process group, the grace period, then SIGKILL. Nothing is ever
        typed into the session: an Enter could approve a permission dialog that is on screen, and that dialog is the safeguard
        against untrusted task text. (guard.watch's one-time folder-trust answer is the only other write to the pty.)"""
        self.runner_stopped = True
        await asyncio.to_thread(agent.kill, self.finish_grace)
        await waiter

    async def _heartbeats(self, job: dict, lease: float, lost: asyncio.Event, finish_now: asyncio.Event | None = None) -> None:
        every = self.heartbeat_every or max(lease / 3, 0.05)
        last_ok = time.monotonic()
        while True:
            await asyncio.sleep(every)
            try:
                reply = await self.link.rpc("heartbeat", job_id=job["job_id"], fence=job["fence"], timeout=max(every, 5))
            except LinkDown:
                reply = {}
            if reply.get("ok"):
                last_ok = time.monotonic()
                self.keep_open = bool(reply.get("keep_open", self.keep_open))
                if reply.get("finish") and finish_now is not None:
                    self.finish_outcome = reply["finish"]
                    if not self.keep_open:   # a person steering over Remote Control may keep the session open (job flag)
                        finish_now.set()
            elif reply.get("status") == 409:
                lost.set()
                return
            if time.monotonic() - last_ok > lease:   # nothing renewed it for a whole lease: the server has given it away
                lost.set()
                return

    async def _final_heartbeat(self, job: dict, lease: float) -> dict | None:
        deadline = time.monotonic() + lease
        while time.monotonic() < deadline:
            try:
                reply = await self.link.rpc("heartbeat", job_id=job["job_id"], fence=job["fence"])
            except LinkDown:
                await asyncio.sleep(min(0.5, lease / 10))
                continue
            if reply.get("ok"):
                return reply
            if reply.get("status") == 409:
                return None
            await asyncio.sleep(min(0.5, lease / 10))
        return None

    def _permission_reason(self, tool: str) -> str:
        name = str(self.cfg.get("name") or "this PC")[:60]
        return (f"blocked on the permission prompt: Claude Code on {name} asked to use {tool[:80]}, which this job's profile does not "
                "allow (nothing was approved; add the tool to the profile's allowed tools)")

    def _start_failure_reason(self, verdict: str) -> str:
        name = str(self.cfg.get("name") or "this PC")[:60]
        if verdict == "login":
            return f"Claude Code needs you to sign in on {name}"
        if verdict == "trust":
            return f"Claude Code is waiting for you to trust the job folder on {name}"
        return "the Claude Code session did not start"

    def _push(self, wt: Path, job: dict, base: str, rc: int, finish: str | None = None) -> tuple[bool, str]:
        try:
            dirty = bool(git(["status", "--porcelain"], cwd=wt))
            ahead = int(git(["rev-list", "--count", f"refs/remotes/{base}..HEAD"], cwd=wt) or 0)
            how = f"agent finished ({finish}), session closed" if finish else f"agent exit {rc}"
            note = " (uncommitted changes were not pushed and are discarded with the worktree)" if dirty else ""
            if ahead:   # to the URL the server gave, explicitly, not to whatever `origin` has become
                git(["push", "-q", "--", job["git_remote"], f"HEAD:refs/heads/{job['branch']}"], cwd=wt)
                return rc == 0, f"pushed {ahead} commit(s) to {job['branch']}; {how}{note}"
            return rc == 0, f"no commits to push; {how}{note}"
        except (JobError, subprocess.SubprocessError, ValueError) as exc:
            return False, f"push failed: {exc}"[:500]

    def _changed_agent_files(self, wt: Path, baseline: dict) -> list[dict]:
        """Files under .claude/{agents,commands,skills} (any depth) that the agent created or changed since the start of the job, and any link
        it made there: [{path, sha256, size, reviewable}] (a link has no sha256). They are NOT blocked from the push (the work is kept) but the
        job is flagged and the files go to the project Owner for review: with no pin they will not run on the next job."""
        try:
            files, found, _cd = agentscan.inventory(wt)
        except Exception:  # noqa: BLE001 - never fail a finished job on a scan bug; the next job's pin check still blocks
            log.exception("job-end agent-file check failed")
            return []
        out = [{"path": p, **meta} for p, meta in sorted(files.items()) if (baseline.get(p) or {}).get("sha256") != meta["sha256"]]
        out += [{"path": f["path"], "sha256": "", "size": 0, "reviewable": False} for f in found if f["rule"] == "config.symlink"]
        return out

    async def _report_files(self, job: dict, wt: Path, files: list[dict], source: str) -> str:
        """Send files the Owner has not reviewed (path, sha256, bytes) to the dashboard so the Owner can read and pin them. A file's bytes go in
        CHUNKS of REPORT_CHUNK bytes (the dashboard's socket takes frames of 8 KiB at most), each answered before the next is sent; the server
        checks the sha256 when the last arrives. Only text up to MAX_REVIEW_BYTES carries bytes; the rest are named only (they cannot be pinned).
        -> "" when everything was sent, else a short reason ("2 of 5 file(s): the dashboard said ...") for the caller to put on the job:
        the sending stops at the first failure, and nothing here raises for a dead socket."""
        todo = files[:agentscan.MAX_AGENT_FILES]
        for i, f in enumerate(todo):
            fields = {"path": f["path"], "sha256": f.get("sha256") or "", "source": source}
            data = None
            if f.get("reviewable") and f.get("sha256"):
                try:
                    data = (wt / f["path"]).read_bytes()
                except OSError:
                    continue
                if hashlib.sha256(data).hexdigest() != f["sha256"]:
                    continue   # changed under us: the next look sees the new bytes
            size = chunk_size(job["job_id"], job["fence"], fields)
            if size == 0:
                continue   # a path too long to fit any bytes in a frame: not sent (the next look finds it unreported again; the job is not blocked)
            chunks = [data[n:n + size] for n in range(0, len(data), size)] if data else [None]
            for part, chunk in enumerate(chunks):
                extra = {} if chunk is None else {"part": part, "parts": len(chunks), "b64": base64.b64encode(chunk).decode("ascii")}
                try:
                    reply = await self.link.rpc("agent_file", job_id=job["job_id"], fence=job["fence"], **fields, **extra)
                except LinkDown:
                    return f"{len(todo) - i} of {len(todo)} file(s): the connection dropped"
                if not reply.get("ok"):
                    return f"{len(todo) - i} of {len(todo)} file(s): the dashboard said {str(reply.get('detail') or reply.get('status') or 'no')[:80]}"
        return ""

    def _install_skills(self, wt: Path, job: dict) -> dict | None:
        """The skills and rules the server sent with the claim, written into the worktree before the agent starts (#718). A failure
        here never stops the job: it runs without them and the server is told which were not written."""
        if not job.get("skills"):
            return None
        try:
            return skills.install(wt, job["skills"], job_dir(self.state, job))
        except Exception as exc:  # noqa: BLE001
            log.exception("skills: install failed")
            return {"written": [], "skipped": [s.get("key") for s in job["skills"].get("skills") or [] if isinstance(s, dict)],
                    "notes": [f"install failed: {exc}"[:200]], "exclude_file": None}

    async def _report_skills(self, job: dict, report: dict | None) -> None:
        """Tell the server which skills were written (best effort: an old server ignores it, a stale fence changes nothing)."""
        if not report:
            return
        for note in report["notes"][:5]:
            self.status.event(f"job {job['job_id']}: {note}")
        try:
            await self.link.rpc("skills", job_id=job["job_id"], fence=job["fence"], written=report["written"], skipped=report["skipped"])
        except LinkDown:
            pass

    async def _release(self, job: dict, reason: str) -> str:
        """Give the job back over the socket: the server requeues it (new fence, attempts counted) like a lapsed lease."""
        self.status.set(last_error=reason)
        self.status.event(f"job {job['job_id']}: {reason}")
        for _ in range(5):
            try:
                reply = await self.link.rpc("release", job_id=job["job_id"], fence=job["fence"], reason=reason)
            except LinkDown:
                await asyncio.sleep(0.5)
                continue
            return "lost" if reply.get("status") == 409 else "released"
        return "released"   # the lease will lapse by itself

    async def _end(self, job: dict, ok: bool, summary: str, outcome: str = "") -> str:
        """finish over the socket; a 409 here means the job moved on (still nothing to undo: the push was fence-checked)."""
        for _ in range(5):
            try:
                reply = await self.link.rpc("finish", job_id=job["job_id"], fence=job["fence"], ok=ok, summary=summary,
                                          **({"outcome": outcome} if outcome else {}))
            except LinkDown:
                await asyncio.sleep(0.5)
                continue
            if reply.get("status") == 409:
                return "lost"
            self.status.event(f"job {job['job_id']} {'done' if ok else 'failed'}: {summary}")
            return ("blocked" if outcome == "blocked" else "done") if ok else "failed"
        return "failed"

    def _cleanup(self, job: dict, wt: Path | None) -> None:
        d = job_dir(self.state, job)
        try:   # the token never outlives the job
            (d / "mcp.json").unlink()
        except OSError:
            pass
        if wt is not None and wt.exists():
            bare = self.state / "repos" / f"p{int(job['project_id'])}.git"
            git(["worktree", "remove", "--force", str(wt)], cwd=bare, check=False)
